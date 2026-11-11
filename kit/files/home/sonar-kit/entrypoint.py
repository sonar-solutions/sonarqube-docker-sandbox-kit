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

"""Gate Claude on authenticated, complete Sonar integration in this workspace."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone


class SetupError(RuntimeError):
    pass


def redact(text):
    # The intended runtime receives a proxy sentinel, but also redact direct
    # credentials if an operator supplies them while diagnosing the setup.
    for name, value in os.environ.items():
        if value and any(word in name.upper() for word in ("TOKEN", "SECRET", "API_KEY")):
            text = text.replace(value, "[REDACTED]")
    return re.sub(r"\b(?:squ|sqp|sqa)_[A-Za-z0-9]+", "[REDACTED]", text)


def run(command, workspace, timeout=240):
    print("Checking: " + " ".join(command[:3]), flush=True)
    try:
        result = subprocess.run(command, cwd=workspace, capture_output=True,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"{command[0]} timed out. Check sbx policy log and service reachability.") from exc
    if result.returncode:
        detail = redact((result.stdout + "\n" + result.stderr)[-12000:])
        raise SetupError(f"Command failed ({result.returncode}): {' '.join(command[:3])}\n{detail}")
    return result.stdout


def project_features(state, workspace):
    """Return the Claude Code features recorded for exactly this workspace."""
    features = {}
    for integration in state.get("integrations", {}).get("installed", []):
        if integration.get("integrationId") != "claude-code":
            continue
        for feature in integration.get("features", []):
            root = feature.get("targetRoot")
            if feature.get("scope") == "project" and root and Path(root).resolve() == workspace.resolve():
                features[feature["featureId"]] = feature
    return features


def subfeature_ids(feature):
    return {part.get("featureId") for part in feature.get("subfeatures", [])}


def resource_paths(features):
    paths = []
    for feature in features.values():
        for part in [feature] + feature.get("subfeatures", []):
            paths.extend(Path(resource["path"]).expanduser()
                         for resource in part.get("resources", []) if resource.get("path"))
    return paths


def verify_hooks(workspace):
    settings_path = workspace / ".claude" / "settings.json"
    try:
        hooks = json.loads(settings_path.read_text()).get("hooks", {})
    except (OSError, ValueError) as exc:
        raise SetupError("Claude's project hook configuration is missing or unreadable.") from exc
    for event, marker in [("SessionStart", "sonar-vortex"), ("PostToolUse", "sonar-sqaa")]:
        if marker not in json.dumps(hooks.get(event, [])):
            raise SetupError(f"Claude {event} does not reference the expected Sonar hook.")


def verify_features(state, workspace):
    """Validate the pinned CLI's recorded feature graph and materialized files."""
    features = project_features(state, workspace)
    if not {"vortex", "sonar-sqaa-hook"}.issubset(features):
        raise SetupError("Sonar did not install both Vortex guidance and verification. "
                         "Confirm the project, Vortex subscription and permissions; Claude was not started.")
    guidance_complete = {"context-augmentation", "sqaa-instructions"}.issubset(subfeature_ids(features["vortex"]))
    verification_complete = "sqaa-posttooluse" in subfeature_ids(features["sonar-sqaa-hook"])
    if not (guidance_complete and verification_complete):
        raise SetupError("Vortex integration is incomplete: guidance, DEEP instructions or edit verification missing.")
    paths = resource_paths(features)
    if not paths or any(not path.exists() for path in paths):
        raise SetupError("Recorded Sonar integration files are missing. Re-run the kit to repair them.")
    verify_hooks(workspace)
    return sorted(features)


def bootstrap(workspace, project):
    workspace = Path(workspace).resolve()
    if not workspace.is_dir() or not project or not re.fullmatch(r"[A-Za-z0-9_.:-]+", project):
        raise SetupError("A valid workspace and Sonar project key are required.")
    if not os.environ.get("SONARQUBE_CLI_TOKEN") or not os.environ.get("SONARQUBE_CLI_ORG"):
        raise SetupError("Sonar credential was withheld or organization is missing. "
                         "Configure sbx secret set sonar-cloud and approve the kit's regional credential binding.")
    if os.environ.get("SONARQUBE_CLI_SERVER") not in ("https://sonarcloud.io", "https://sonarqube.us"):
        raise SetupError("This reference kit supports SonarQube Cloud EU or US only.")
    expected = json.loads(Path(__file__).with_name("versions.json").read_text())["cli_version"]
    version = run(["sonar", "--version"], workspace).strip()
    if version != expected:
        raise SetupError(f"Expected Sonar CLI {expected}, found {redact(version)}. Recreate the kit.")
    run(["sonar", "auth", "status"], workspace)
    # The official integrator validates the project and merges its own config.
    # Re-run it on each entry to repair removed hooks and recheck entitlement.
    run(["sonar", "integrate", "claude", "--project", project, "--non-interactive"], workspace)
    state_file = Path(os.environ.get("SONAR_USER_HOME", str(Path.home() / ".sonar"))) / "sonarqube-cli/state.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError) as exc:
        raise SetupError("Sonar integration did not produce readable state.") from exc
    features = verify_features(state, workspace)
    return {"status": "configured", "checked_at": datetime.now(timezone.utc).isoformat(),
            "workspace": str(workspace), "project": project, "sonar_cli": version,
            "features": features, "live_guidance_verified": False, "live_analysis_verified": False}


def main():
    runtime = Path(__file__).parent
    report = runtime / "readiness.json"
    # Invalidate a previous success before attempting a new session.
    report.unlink(missing_ok=True)
    try:
        # The v3 kit exports the region as SONAR_KIT_CLOUD_DOMAIN (a per-run env
        # arg) rather than a prebuilt SONARQUBE_CLI_SERVER, because v3 cannot
        # bake a composed value. Derive the server URL the Sonar CLI reads from
        # the environment; the arg's enum restricts the domain to the two Cloud
        # regions, and bootstrap revalidates it.
        os.environ["SONARQUBE_CLI_SERVER"] = "https://" + os.environ.get(
            "SONAR_KIT_CLOUD_DOMAIN", "sonarcloud.io")
        workspace = (runtime / "workspace.txt").read_text().strip()
        if not workspace or "${WORKDIR}" in workspace:
            raise SetupError("Docker did not resolve the primary workspace.")
        status = bootstrap(workspace, os.environ.get("SONAR_KIT_PROJECT", ""))
        # Create with owner-only permissions rather than tightening afterwards.
        descriptor = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(status, indent=2) + "\n")
        print("Sonar guidance and verification configured. Live validation is still required.", flush=True)
        os.chdir(workspace)
        # Start Claude with no forwarded arguments: the kit is the only launcher.
        os.execvp("claude", ["claude"])
    except (SetupError, OSError, ValueError) as exc:
        print("Sonar kit setup failed: " + redact(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
