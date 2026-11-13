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

# Keep __pycache__ out of kit/ and kit-mixin/, which are build contexts.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "kit/sonar-kit"
KITS = {"workload": ROOT / "kit/sonar-claude.yaml",
        "mixin": ROOT / "kit-mixin/sonar-claude-mixin.yaml"}
RECIPES = {"workload": ROOT / "kit/sonar-claude.dockerfile",
           "mixin": ROOT / "kit-mixin/sonar-claude-mixin.dockerfile"}
# Digest-only pin of the upstream `claude-code-docker` tag's multi-platform index.
BASE_IMAGE = ("dhi.io/sbx-templates@sha256:"
              "a7a75a11d5b202525fcc70cee2028fbfaf3948c6f448e9f127de84fdf7ae12eb")
REPOSITORY_URL = "https://github.com/sonar-solutions/sonarqube-docker-sandbox-kit"

SONAR_ENV = {"SONARQUBE_CLI_TOKEN": "proxy-managed", "SONARQUBE_CLI_ORG": "demo",
             "SONAR_KIT_CLOUD_DOMAIN": "sonarcloud.io", "SONARQUBE_CLI_SERVER": "https://sonarcloud.io"}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


entry = module("entrypoint", RUNTIME / "entrypoint.py")
installer = module("installer", RUNTIME / "install.py")
settings_helper = module("claude_settings", RUNTIME / "claude_settings.py")


def descriptor_version(descriptor):
    return re.search(r'^version: "([^"]+)"', descriptor.read_text(), re.M).group(1)


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

    def test_other_integrations_cannot_satisfy_readiness(self):
        self.state["integrations"]["installed"][0]["integrationId"] = "other-agent"
        with self.assertRaisesRegex(entry.SetupError, "did not install"):
            entry.verify_features(self.state, self.workspace)

    def test_missing_context_rejected(self):
        self.features[0]["subfeatures"].pop(0)
        with self.assertRaisesRegex(entry.SetupError, "incomplete"):
            entry.verify_features(self.state, self.workspace)

    def test_missing_edit_verification_rejected(self):
        self.features[1]["subfeatures"] = []
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

    def test_malformed_hook_configuration_fails_closed(self):
        for content in ("[]", '{"hooks": []}', "not json"):
            with self.subTest(content=content):
                self.config.write_text(content)
                with self.assertRaisesRegex(entry.SetupError, "missing or unreadable"):
                    entry.verify_features(self.state, self.workspace)

    def test_no_credentials_fails_before_any_subprocess(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(entry, "run") as run:
            with self.assertRaisesRegex(entry.SetupError, "withheld"):
                entry.bootstrap(self.workspace, "demo")
            run.assert_not_called()

    def test_invalid_workspace_or_project_fails_before_any_subprocess(self):
        with patch.dict(os.environ, SONAR_ENV), patch.object(entry, "run") as run:
            for workspace, project in ((self.workspace / "absent", "demo"), (self.workspace, ""),
                                       (self.workspace, "demo; echo bad"), (self.workspace, "../demo")):
                with self.subTest(workspace=workspace.name, project=project):
                    with self.assertRaisesRegex(entry.SetupError, "valid workspace"):
                        entry.bootstrap(workspace, project)
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

    def test_version_drift_is_rejected_before_authentication(self):
        with patch.dict(os.environ, SONAR_ENV), patch.object(entry, "run", return_value="9.0.0") as run:
            with self.assertRaisesRegex(entry.SetupError, "Expected Sonar CLI"):
                entry.bootstrap(self.workspace, "demo")
        self.assertEqual(run.call_count, 1)

    def test_region_mismatch_rejected_before_any_subprocess(self):
        cases = [{"SONAR_KIT_CLOUD_DOMAIN": "sonarqube.us"},
                 {"SONARQUBE_CLI_SERVER": "https://sonarqube.us"},
                 {"SONAR_KIT_CLOUD_DOMAIN": "example.com", "SONARQUBE_CLI_SERVER": "https://example.com"}]
        for change in cases:
            with self.subTest(change=change), patch.dict(os.environ, {**SONAR_ENV, **change}), \
                    patch.object(entry, "run") as run:
                with self.assertRaisesRegex(entry.SetupError, "same region"):
                    entry.bootstrap(self.workspace, "demo")
                run.assert_not_called()
        with patch.dict(os.environ, {k: v for k, v in SONAR_ENV.items() if k != "SONAR_KIT_CLOUD_DOMAIN"}), \
                patch.object(entry, "run") as run:
            with self.assertRaisesRegex(entry.SetupError, "same region"):
                entry.bootstrap(self.workspace, "demo")
            run.assert_not_called()

    def test_consistent_us_region_reaches_cli_checks(self):
        us = {**SONAR_ENV, "SONAR_KIT_CLOUD_DOMAIN": "sonarqube.us", "SONARQUBE_CLI_SERVER": "https://sonarqube.us"}
        with patch.dict(os.environ, us), \
                patch.object(entry, "run", side_effect=entry.SetupError("cli reached")) as run:
            with self.assertRaisesRegex(entry.SetupError, "cli reached"):
                entry.bootstrap(self.workspace, "demo")
        self.assertEqual(run.call_args.args[0], ["sonar", "--version"])

    def test_bypass_settings_rejected(self):
        home = self.workspace / "home"
        entry.check_permission_mode(self.workspace, home)
        local = self.workspace / ".claude/settings.local.json"
        local.write_text('{"permissions": {"defaultMode": "bypassPermissions"}}')
        with self.assertRaisesRegex(entry.SetupError, "bypassPermissions"):
            entry.check_permission_mode(self.workspace, home)
        local.unlink()
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text('{"permissions": {"defaultMode": "bypassPermissions"}}')
        with self.assertRaisesRegex(entry.SetupError, "bypassPermissions"):
            entry.check_permission_mode(self.workspace, home)

    def test_unreadable_permission_settings_fail_closed(self):
        local = self.workspace / ".claude/settings.local.json"
        local.write_text("not json")
        with self.assertRaisesRegex(entry.SetupError, "unreadable"):
            entry.check_permission_mode(self.workspace, self.workspace / "home")


class GateTest(unittest.TestCase):
    """The synchronous entrypoint: Claude starts only after readiness passes."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "workspace with spaces"
        self.workspace.mkdir()
        self.report = self.root / "home/sonar-kit/readiness.json"
        patcher = patch.object(entry, "readiness_path", return_value=self.report)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, argv, env, bootstrap=None):
        with patch.dict(os.environ, env, clear=True), \
                patch.object(entry, "bootstrap", **(bootstrap or {"return_value": {"status": "configured"}})) as setup, \
                patch.object(entry.os, "chdir") as chdir, \
                patch.object(entry.os, "execvp") as launch, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = entry.main(argv)
        return code, setup, chdir, launch

    def test_ready_gate_launches_bare_claude_with_owner_only_report(self):
        # A permissive umask must not widen the report, even briefly.
        previous = os.umask(0)
        try:
            code, setup, chdir, launch = self.start([], {"WORKSPACE_DIR": str(self.workspace),
                                                         "SONAR_KIT_PROJECT": "demo"})
        finally:
            os.umask(previous)
        self.assertIsNone(code)
        setup.assert_called_once_with(self.workspace, "demo")
        chdir.assert_called_once_with(self.workspace)
        launch.assert_called_once_with("claude", ["claude"])
        self.assertEqual(json.loads(self.report.read_text())["status"], "configured")
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o600)

    def test_missing_or_unresolved_workspace_never_starts_claude(self):
        for value in (None, "", "relative/path", "${{ kit.env.WORKSPACE_DIR }}", str(self.root / "absent")):
            with self.subTest(value=value):
                env = {} if value is None else {"WORKSPACE_DIR": value}
                code, setup, _, launch = self.start([], env)
                self.assertEqual(code, 1)
                setup.assert_not_called()
                launch.assert_not_called()

    def test_any_forwarded_argument_is_refused_before_setup(self):
        settings_override = '{"permissions":{"defaultMode":"bypassPermissions"}}'
        for argv in (["--settings", settings_override], ["--dangerously-skip-permissions"],
                     ["--continue"], ["-p", "hello"], [""]):
            with self.subTest(argv=argv):
                code, setup, _, launch = self.start(argv, {"WORKSPACE_DIR": str(self.workspace),
                                                           "SONAR_KIT_PROJECT": "demo"})
                self.assertEqual(code, 1)
                setup.assert_not_called()
                launch.assert_not_called()
                self.assertFalse(self.report.exists())

    def test_failed_restart_removes_stale_success_and_never_starts_claude(self):
        self.report.parent.mkdir(parents=True)
        self.report.write_text('{"status": "configured"}')
        code, _, _, launch = self.start([], {"WORKSPACE_DIR": str(self.workspace)},
                                        {"side_effect": entry.SetupError("auth expired")})
        self.assertEqual(code, 1)
        launch.assert_not_called()
        self.assertFalse(self.report.exists())

    def test_refused_arguments_also_remove_a_stale_report(self):
        self.report.parent.mkdir(parents=True)
        self.report.write_text('{"status": "configured"}')
        code, setup, _, launch = self.start(["--continue"], {"WORKSPACE_DIR": str(self.workspace)})
        self.assertEqual(code, 1)
        setup.assert_not_called()
        launch.assert_not_called()
        self.assertFalse(self.report.exists())


class ClaudeSettingsTest(unittest.TestCase):
    def test_bound_credential_adds_helper_and_preserves_hooks(self):
        current = {"hooks": {"SessionStart": ["sonar"]}, "permissions": {"allow": ["Bash(ls)"]}}
        for mode in ("apiKey", "oauth"):
            merged = settings_helper.merge(current, mode)
            self.assertEqual(merged["apiKeyHelper"], settings_helper.HELPER)
            self.assertEqual(merged["hooks"], current["hooks"])
            self.assertEqual(merged["permissions"], current["permissions"])

    def test_unbound_credential_removes_only_the_kit_helper(self):
        self.assertNotIn("apiKeyHelper", settings_helper.merge({"apiKeyHelper": settings_helper.HELPER}, "none"))
        self.assertEqual(settings_helper.merge({"apiKeyHelper": "my-helper"}, "none")["apiKeyHelper"], "my-helper")

    def test_bypass_settings_are_stripped(self):
        merged = settings_helper.merge({"permissions": {"defaultMode": "bypassPermissions", "deny": ["x"]},
                                        "bypassPermissionsModeAccepted": True,
                                        "skipDangerousModePermissionPrompt": True}, "none")
        self.assertEqual(merged, {"permissions": {"deny": ["x"]}})

    def test_file_is_merged_in_place_and_non_objects_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".claude/settings.json"
            with patch.dict(os.environ, {"SBX_CRED_ANTHROPIC_MODE": "apiKey"}):
                settings_helper.main(path)
                self.assertEqual(json.loads(path.read_text()), {"apiKeyHelper": settings_helper.HELPER})
                path.write_text("[1]")
                with self.assertRaises(SystemExit):
                    settings_helper.main(path)
            self.assertEqual(path.read_text(), "[1]")
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["settings.json"])


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
            checksum = json.loads((RUNTIME / "versions.json").read_text())["sha256"]["linux-arm64"]
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
        lock = {**installer.load_lock(), "release": "1.0/../../evil"}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(installer.platform, "system", return_value="Linux"), \
                patch.object(installer.platform, "machine", return_value="aarch64"), \
                patch.object(installer, "load_lock", return_value=lock), \
                patch.object(installer.subprocess, "run") as download:
            with self.assertRaisesRegex(RuntimeError, "not a valid Sonar CLI pin"):
                installer.install(Path(directory) / "sonar")
            download.assert_not_called()

    def test_successful_install_replaces_binary_and_selects_architecture(self):
        lock = json.loads((RUNTIME / "versions.json").read_text())
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

    def test_mixin_installs_the_same_pinned_artifact(self):
        # The mixin's build context cannot reach kit/, so it carries copies.
        for name in ("install.py", "versions.json"):
            self.assertEqual((ROOT / "kit-mixin/sonar-kit" / name).read_bytes(), (RUNTIME / name).read_bytes(), name)

    def test_copy_detection_exclusion_is_only_the_mirror(self):
        # The mirror is out of duplication measurement; the canonical copy is not.
        settings = [line for line in (ROOT / ".sonarcloud.properties").read_text().splitlines()
                    if line and not line.startswith("#")]
        self.assertEqual(settings, ["sonar.cpd.exclusions=kit-mixin/sonar-kit/install.py"])

    def test_cli_installs_only_to_named_destinations(self):
        with patch.object(installer, "install") as install:
            for argv in ([], ["/etc/passwd"], ["../sonar"], ["/usr/local/bin/sonar"], ["workload", "extra"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    installer.main(argv)
            install.assert_not_called()
            for name, path in (("workload", "/usr/local/bin/sonar"), ("overlay", "/out/usr/local/bin/sonar")):
                installer.main([name])
                self.assertEqual(install.call_args.args[0], Path(path))

    def test_untrusted_lock_is_refused_before_any_download_or_write(self):
        lock = installer.load_lock()
        url, expected = installer.pinned_artifact(lock, "linux-arm64")
        self.assertEqual(url, f"{installer.BASE_URL}/{lock['release']}/linux/"
                              f"sonarqube-cli-{lock['release']}-linux-arm64.bin")
        self.assertEqual(expected, lock["sha256"]["linux-arm64"])
        bad = [{"base_url": "https://attacker.example/sonarqube-cli"}, {"base_url": "--output=/etc/x"},
               {"release": "1.8.0.5274/../../x"}, {"release": "--next"}, {"cli_version": "9.9.9"},
               {"sha256": {"linux-arm64": "00" * 31}}, {"sha256": {}}]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested/sonar"
            for change in bad:
                with self.subTest(change=change), \
                        patch.object(installer, "load_lock", return_value={**lock, **change}), \
                        patch.object(installer.platform, "system", return_value="Linux"), \
                        patch.object(installer.platform, "machine", return_value="aarch64"), \
                        patch.object(installer.subprocess, "run") as download:
                    with self.assertRaisesRegex(RuntimeError, "not a valid Sonar CLI pin"):
                        installer.install(destination)
                    download.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])


class MetadataTest(unittest.TestCase):
    def test_pinned_versions_are_consistent(self):
        lock = json.loads((RUNTIME / "versions.json").read_text())
        self.assertTrue(lock["release"].startswith(lock["cli_version"] + "."))
        self.assertEqual(set(lock["sha256"]), {"linux-arm64", "linux-x86-64"})
        for checksum in lock["sha256"].values():
            self.assertRegex(checksum, r"^[0-9a-f]{64}$")

    def test_project_and_both_kit_versions_match(self):
        project = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        lock = re.search(r'^version = "([^"]+)"', (ROOT / "uv.lock").read_text().split("[[package]]")[1], re.M)
        self.assertEqual({descriptor_version(KITS["workload"]), descriptor_version(KITS["mixin"]),
                          lock.group(1)}, {project})

    def test_descriptors_declare_license_and_public_source(self):
        for variant, descriptor in KITS.items():
            with self.subTest(variant=variant):
                text = descriptor.read_text()
                self.assertRegex(text, r"(?m)^schemaVersion: \"3\"$")
                self.assertRegex(text, rf"(?m)^sourceUrl: {re.escape(REPOSITORY_URL)}$")
                self.assertRegex(text, r"(?m)^licenses: \[LGPL-3\.0-or-later\]$")

    def test_both_recipes_pin_the_same_verified_base_image(self):
        for variant, recipe in RECIPES.items():
            with self.subTest(variant=variant):
                bases = re.findall(r"^FROM (\S+)", recipe.read_text(), re.M)
                self.assertEqual([b for b in bases if b != "scratch"], [BASE_IMAGE])

    def test_build_contexts_exclude_local_state(self):
        for directory in ("kit", "kit-mixin"):
            ignored = (ROOT / directory / ".dockerignore").read_text().split()
            self.assertLessEqual({"**/__pycache__", "**/.env", "**/.env.*", "**/readiness.json"}, set(ignored),
                                 directory)


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
            expected = json.loads((RUNTIME / "versions.json").read_text())["cli_version"]
            env = {**SONAR_ENV, "SONAR_USER_HOME": home}
            with patch.dict(os.environ, env), patch.object(entry.Path, "home", return_value=Path(home)), \
                    patch.object(entry, "run", return_value=expected) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = entry.bootstrap(workspace, "demo")
            self.assertEqual(result["status"], "configured")
            self.assertEqual(result["features"], ["sonar-sqaa-hook", "vortex"])
            self.assertFalse(result["live_analysis_verified"])
            self.assertEqual(run.call_args_list[-1].args[0][:3], ["sonar", "integrate", "claude"])

    def test_unsupported_server_is_rejected(self):
        env = {**SONAR_ENV, "SONARQUBE_CLI_SERVER": "https://example.com"}
        with patch.dict(os.environ, env), patch.object(entry, "run") as run:
            with self.assertRaisesRegex(entry.SetupError, "EU or US"):
                entry.bootstrap(Path("."), "demo")
            run.assert_not_called()


class KitContractTest(unittest.TestCase):
    """Cross-file contracts the build cannot check from the host."""

    @staticmethod
    def declared_args(descriptor):
        block = re.search(r"^args:\n(.*?)^\S", descriptor.read_text(), re.S | re.M).group(1)
        names = re.findall(r"^  ([A-Za-z_][A-Za-z0-9_]*):\n", block, re.M)
        exports = dict(re.findall(r"^  ([A-Za-z_]\w*):\n(?:    .*\n)*?    env: (\w+)", block, re.M))
        return names, exports, block

    @staticmethod
    def network_phases(descriptor):
        """Allow lists per phase of the network-policy entry (indentation-based)."""
        text = descriptor.read_text()
        block = re.search(r"^  - type: com\.docker\.sandbox/network-policy@1\n(.*?)(?=^  - |\Z)", text, re.S | re.M)
        phases, current = {}, None
        for line in block.group(1).splitlines():
            if match := re.fullmatch(r"      (install|runtime):", line):
                current = phases.setdefault(match.group(1), [])
            elif current is not None and (match := re.fullmatch(r'          - (?:"([^"]+)"|(\S+))', line)):
                current.append(match.group(1) or match.group(2))
        return phases

    @staticmethod
    def inject_domains(descriptor):
        pairs = re.findall(r'domain: (?:"([^"]+)"|([^",}\s]+))', descriptor.read_text())
        return [quoted or bare for quoted, bare in pairs]

    def test_sonar_components_are_reachable_where_integration_runs(self):
        binaries = "binaries.sonarsource.com:443"
        # The workload gate integrates at every start (runtime); its install hooks are local.
        workload = self.network_phases(KITS["workload"])
        self.assertIn(binaries, workload["runtime"])
        self.assertNotIn("install", workload)
        # The mixin integrates only in its install hook.
        mixin = self.network_phases(KITS["mixin"])
        self.assertIn(binaries, mixin["install"])
        self.assertNotIn(binaries, mixin["runtime"])

    def test_sonar_token_targets_only_the_selected_region(self):
        regional = {"${{ kit.args.cloudDomain }}", "api.${{ kit.args.cloudDomain }}"}
        for variant, descriptor in KITS.items():
            with self.subTest(variant=variant):
                phases = self.network_phases(descriptor)
                domains = self.inject_domains(descriptor)
                self.assertLessEqual(regional, set(domains))
                self.assertFalse([d for d in domains if "sonarsource" in d or "sonardata" in d])
                self.assertEqual(set(domains) - regional, {"api.anthropic.com"} if variant == "workload" else set())
                for phase, allowed in phases.items():
                    if phase == "runtime" or variant == "mixin":  # the credential's phases
                        self.assertLessEqual(regional, set(allowed), phase)
                    # Exact hosts only: no wildcards, telemetry or other Sonar hosts.
                    self.assertFalse([a for a in allowed if "*" in a or "sonardata" in a], phase)
                    self.assertEqual([a for a in allowed if "sonarsource" in a],
                                     [a for a in allowed if a == "binaries.sonarsource.com:443"], phase)

    def test_region_args_are_exactly_consistent(self):
        for variant, descriptor in KITS.items():
            with self.subTest(variant=variant):
                block = self.declared_args(descriptor)[2]
                domains = re.search(r"cloudDomain:\n(?:    .*\n)*?    enum: \[([^\]]+)\]", block).group(1).split(", ")
                servers = re.search(r"cloudServer:\n(?:    .*\n)*?    enum: \[([^\]]+)\]", block).group(1).split(", ")
                self.assertEqual(domains, list(entry.REGIONS))
                self.assertEqual(servers, list(entry.REGIONS.values()))
                self.assertEqual(servers, ["https://" + domain for domain in domains])
                defaults = re.findall(r"^    default: (\S+)$", block, re.M)
                self.assertEqual(defaults, [domains[0], servers[0]])

    def test_descriptors_declare_the_same_arguments(self):
        workload = self.declared_args(KITS["workload"])[:2]
        self.assertEqual(workload, self.declared_args(KITS["mixin"])[:2])
        names, exports = workload
        self.assertEqual(set(names), {"organization", "project", "cloudDomain", "cloudServer"})
        # The gate and mixin hook read these exported names.
        self.assertEqual(set(exports.values()), {"SONARQUBE_CLI_ORG", "SONAR_KIT_PROJECT",
                                                 "SONAR_KIT_CLOUD_DOMAIN", "SONARQUBE_CLI_SERVER"})
        for variant, descriptor in KITS.items():
            for reference in re.findall(r"\$\{\{\s*kit\.args\.([^\s}]+)\s*\}\}", descriptor.read_text()):
                self.assertIn(reference, names, variant)

    def test_workload_launch_is_the_gate_without_bypass(self):
        recipe = RECIPES["workload"].read_text()
        entrypoints = re.findall(r"^ENTRYPOINT (.+)$", recipe, re.M)
        self.assertEqual(len(entrypoints), 1)
        self.assertEqual(json.loads(entrypoints[0]), ["python3", "/opt/sonar-kit/entrypoint.py"])
        self.assertFalse(re.search(r"^CMD ", recipe, re.M))
        # The gate and its helpers are root-owned; the agent (uid 1000) runs the image.
        self.assertRegex(recipe, r"(?m)^COPY --chown=0:0 sonar-kit/ /opt/sonar-kit/$")
        self.assertEqual(re.findall(r"^USER (\S+)", recipe, re.M), ["root", "agent"])
        mixin = RECIPES["mixin"].read_text()
        # The mixin's final stage is file content only: no launch fields, and an
        # explicit unprivileged numeric USER that v3 ignores at assembly.
        final = mixin.split("FROM scratch")[1]
        self.assertFalse(re.search(r"^(ENTRYPOINT|CMD|WORKDIR) ", final, re.M))
        self.assertEqual(re.findall(r"^USER (\S+)$", final, re.M), ["1000"])
        for text in (recipe, KITS["workload"].read_text()):
            for path in re.findall(r"/opt/sonar-kit/([\w.]+)", text):
                self.assertTrue((RUNTIME / path).is_file(), path)

    def test_no_bypass_flags_in_launch_paths(self):
        sources = [RECIPES["workload"], RECIPES["mixin"], KITS["workload"], KITS["mixin"], RUNTIME / "entrypoint.py"]
        for path in sources:
            with self.subTest(path=path.name):
                # Comments may name the flags they forbid; code and config must not.
                text = "\n".join(line for line in path.read_text().splitlines()
                                 if not line.lstrip().startswith("#"))
                self.assertNotIn("--dangerously-skip-permissions", text)
                self.assertNotIn("--permission-mode", text)
        # The launched command is exactly bare `claude`.
        self.assertIn('os.execvp("claude", ["claude"])', (RUNTIME / "entrypoint.py").read_text())

    def test_descriptors_never_select_bypass_permissions(self):
        for variant, descriptor in KITS.items():
            with self.subTest(variant=variant):
                for line in descriptor.read_text().splitlines():
                    if not line.lstrip().startswith("#"):
                        self.assertNotIn("bypassPermissions", line)
                        self.assertNotIn("defaultMode", line)


if __name__ == "__main__":
    unittest.main()
