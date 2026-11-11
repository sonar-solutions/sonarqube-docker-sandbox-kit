# Sonar integration for Docker Sandboxes
# Copyright (C) SonarSource Sàrl
# mailto:info AT sonarsource DOT com
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

# Keep __pycache__ out of kit/, which is packaged as-is.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


entry = module("entrypoint", ROOT / "kit/files/home/sonar-kit/entrypoint.py")
installer = module("installer", ROOT / "kit/files/home/sonar-kit/install.py")


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        config = self.workspace / ".claude/settings.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"command": "sonar-vortex/session-start"}]}],
            "PostToolUse": [{"hooks": [{"command": "sonar-sqaa/posttool"}]}]}}))
        self.config = config
        self.features = [
            {"featureId": "vortex", "scope": "project", "targetRoot": str(self.workspace),
             "resources": [], "subfeatures": [
                 {"featureId": "context-augmentation", "resources": [{"path": str(config)}]},
                 {"featureId": "sqaa-instructions", "resources": [{"path": str(config)}]}]},
            {"featureId": "sonar-sqaa-hook", "scope": "project", "targetRoot": str(self.workspace),
             "resources": [{"path": str(config)}], "subfeatures": [{"featureId": "sqaa-posttooluse"}]}]
        self.state = {"integrations": {"installed": [{"integrationId": "claude-code", "features": self.features}]}}

    def test_complete_configuration(self):
        self.assertEqual(entry.verify_features(self.state, self.workspace), ["sonar-sqaa-hook", "vortex"])

    def test_skipped_vortex_is_not_ready(self):
        self.features.pop(0)
        with self.assertRaisesRegex(entry.SetupError, "did not install"):
            entry.verify_features(self.state, self.workspace)

    def test_other_project_state_cannot_satisfy_readiness(self):
        for feature in self.features:
            feature["targetRoot"] = str(self.workspace / "different")
        with self.assertRaises(entry.SetupError):
            entry.verify_features(self.state, self.workspace)

    def test_missing_context_rejected(self):
        self.features[0]["subfeatures"].pop(0)
        with self.assertRaisesRegex(entry.SetupError, "incomplete"):
            entry.verify_features(self.state, self.workspace)

    def test_deleted_resource_rejected(self):
        self.features[0]["resources"] = [{"path": str(self.workspace / "missing.sh")}]
        with self.assertRaisesRegex(entry.SetupError, "missing"):
            entry.verify_features(self.state, self.workspace)

    def test_unhooked_configuration_rejected(self):
        self.config.write_text('{"hooks": {}}')
        with self.assertRaisesRegex(entry.SetupError, "SessionStart"):
            entry.verify_features(self.state, self.workspace)

    def test_no_credentials_fails_before_any_subprocess(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(entry, "run") as run:
            with self.assertRaisesRegex(entry.SetupError, "withheld"):
                entry.bootstrap(self.workspace, "demo")
            run.assert_not_called()

    def test_failure_output_is_redacted(self):
        result = subprocess.CompletedProcess([], 1, "token=very-private-value", "squ_abc123")
        with patch.dict(os.environ, {"SONARQUBE_CLI_TOKEN": "very-private-value"}), \
                patch.object(entry.subprocess, "run", return_value=result):
            with self.assertRaises(entry.SetupError) as caught:
                entry.run(["sonar", "auth", "status"], self.workspace)
            self.assertNotIn("very-private-value", str(caught.exception))
            self.assertNotIn("squ_abc123", str(caught.exception))

    def test_timeout_is_actionable(self):
        with patch.object(entry.subprocess, "run", side_effect=subprocess.TimeoutExpired("sonar", 1)):
            with self.assertRaisesRegex(entry.SetupError, "timed out"):
                entry.run(["sonar", "auth", "status"], self.workspace)

    def test_failed_restart_removes_stale_success_and_never_starts_claude(self):
        (self.workspace / "workspace.txt").write_text(str(self.workspace))
        report = self.workspace / "readiness.json"
        report.write_text('{"status": "configured"}')
        with patch.object(entry, "__file__", str(self.workspace / "entrypoint.py")), \
                patch.object(entry, "bootstrap", side_effect=entry.SetupError("auth expired")), \
                patch.object(entry.os, "execvp") as launch, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entry.main(), 1)
        launch.assert_not_called()
        self.assertFalse(report.exists())

    def test_version_drift_is_rejected_before_authentication(self):
        env = {"SONARQUBE_CLI_TOKEN": "proxy-managed", "SONARQUBE_CLI_ORG": "demo",
               "SONARQUBE_CLI_SERVER": "https://sonarcloud.io"}
        with patch.dict(os.environ, env), patch.object(entry, "run", return_value="9.0.0") as run:
            with self.assertRaisesRegex(entry.SetupError, "Expected Sonar CLI"):
                entry.bootstrap(self.workspace, "demo")
        self.assertEqual(run.call_count, 1)


class InstallTest(unittest.TestCase):
    def test_checksum_failure_preserves_previous_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sonar"
            destination.write_bytes(b"previous working binary")
            def download(command, **kwargs):
                Path(command[-1]).write_bytes(b"corrupted response")
            with patch.object(installer.platform, "system", return_value="Linux"), \
                    patch.object(installer.platform, "machine", return_value="aarch64"), \
                    patch.object(installer.subprocess, "run", side_effect=download):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    installer.install(destination)
            self.assertEqual(destination.read_bytes(), b"previous working binary")
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_identical_binary_skips_download(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sonar"
            destination.touch()
            checksum = json.loads((ROOT / "kit/files/home/sonar-kit/versions.json").read_text())["sha256"]["linux-arm64"]
            with patch.object(installer.platform, "system", return_value="Linux"), \
                    patch.object(installer.platform, "machine", return_value="aarch64"), \
                    patch.object(installer, "digest", return_value=checksum), \
                    patch.object(installer.subprocess, "run") as download:
                installer.install(destination)
                download.assert_not_called()

    def test_unsupported_platform_is_rejected(self):
        with patch.object(installer.platform, "system", return_value="Darwin"):
            with self.assertRaisesRegex(RuntimeError, "supported Linux"):
                installer.install(Path("unused"))

    def test_malformed_release_is_rejected_before_download(self):
        lock = json.loads((ROOT / "kit/files/home/sonar-kit/versions.json").read_text())
        lock["release"] = "1.0/../../evil"
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(installer.platform, "system", return_value="Linux"), \
                patch.object(installer.platform, "machine", return_value="aarch64"), \
                patch.object(installer.json, "loads", return_value=lock), \
                patch.object(installer.subprocess, "run") as download:
            with self.assertRaisesRegex(RuntimeError, "numeric release"):
                installer.install(Path(directory) / "sonar")
            download.assert_not_called()

    def test_successful_install_replaces_binary_and_selects_architecture(self):
        lock = json.loads((ROOT / "kit/files/home/sonar-kit/versions.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "sonar"
            urls = []

            def download(command, **kwargs):
                urls.append(command[-3])
                Path(command[-1]).write_bytes(b"new binary")

            with patch.object(installer.platform, "system", return_value="Linux"), \
                    patch.object(installer.platform, "machine", return_value="x86_64"), \
                    patch.object(installer, "digest", side_effect=lambda path: lock["sha256"]["linux-x86-64"]), \
                    patch.object(installer.subprocess, "run", side_effect=download), \
                    contextlib.redirect_stdout(io.StringIO()):
                installer.install(destination)
            self.assertEqual(destination.read_bytes(), b"new binary")
            self.assertTrue(urls[0].endswith("-linux-x86-64.bin"))
            self.assertTrue(os.access(destination, os.X_OK))
            self.assertEqual(list(Path(directory).iterdir()), [destination])


class MetadataTest(unittest.TestCase):
    def test_pinned_versions_are_consistent(self):
        lock = json.loads((ROOT / "kit/files/home/sonar-kit/versions.json").read_text())
        self.assertTrue(lock["release"].startswith(lock["cli_version"] + "."))
        self.assertEqual(set(lock["sha256"]), {"linux-arm64", "linux-x86-64"})
        for checksum in lock["sha256"].values():
            self.assertRegex(checksum, r"^[0-9a-f]{64}$")

    def test_kit_and_project_versions_match(self):
        descriptor = (ROOT / "kit/sonar-claude.yaml").read_text()
        project = (ROOT / "pyproject.toml").read_text()
        kit_version = re.search(r'^version: "([^"]+)"', descriptor, re.M).group(1)
        project_version = re.search(r'^version = "([^"]+)"', project, re.M).group(1)
        self.assertEqual(kit_version, project_version)


class BootstrapTest(unittest.TestCase):
    def test_successful_bootstrap_reports_unverified_live_state(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
            workspace = Path(work)
            config = workspace / ".claude/settings.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"hooks": {
                "SessionStart": [{"command": "sonar-vortex/start"}],
                "PostToolUse": [{"command": "sonar-sqaa/post"}]}}))
            state = {"integrations": {"installed": [{"integrationId": "claude-code", "features": [
                {"featureId": "vortex", "scope": "project", "targetRoot": str(workspace),
                 "subfeatures": [{"featureId": "context-augmentation", "resources": [{"path": str(config)}]},
                                 {"featureId": "sqaa-instructions"}]},
                {"featureId": "sonar-sqaa-hook", "scope": "project", "targetRoot": str(workspace),
                 "subfeatures": [{"featureId": "sqaa-posttooluse"}]}]}]}}
            state_file = Path(home) / "sonarqube-cli/state.json"
            state_file.parent.mkdir()
            state_file.write_text(json.dumps(state))
            expected = json.loads((ROOT / "kit/files/home/sonar-kit/versions.json").read_text())["cli_version"]
            env = {"SONARQUBE_CLI_TOKEN": "proxy-managed", "SONARQUBE_CLI_ORG": "demo",
                   "SONARQUBE_CLI_SERVER": "https://sonarcloud.io", "SONAR_USER_HOME": home}
            with patch.dict(os.environ, env), patch.object(entry, "run", return_value=expected) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = entry.bootstrap(workspace, "demo")
            self.assertEqual(result["status"], "configured")
            self.assertEqual(result["features"], ["sonar-sqaa-hook", "vortex"])
            self.assertFalse(result["live_analysis_verified"])
            self.assertEqual(run.call_args_list[-1].args[0][:3], ["sonar", "integrate", "claude"])

    def test_unsupported_server_is_rejected(self):
        env = {"SONARQUBE_CLI_TOKEN": "proxy-managed", "SONARQUBE_CLI_ORG": "demo",
               "SONARQUBE_CLI_SERVER": "https://example.com"}
        with patch.dict(os.environ, env), patch.object(entry, "run") as run:
            with self.assertRaisesRegex(entry.SetupError, "EU or US"):
                entry.bootstrap(Path("."), "demo")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
