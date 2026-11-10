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

"""Merge Claude's auth helper into user settings without weakening permissions."""
import json
import os
from pathlib import Path
import tempfile

# The proxy replaces this placeholder with the bound Anthropic credential.
HELPER = "echo proxy-managed"
BYPASS_KEYS = ("bypassPermissionsModeAccepted", "skipDangerousModePermissionPrompt")


def merge(settings, mode):
    """Return settings with the auth helper matching the surfaced mode."""
    result = dict(settings)
    if mode and mode != "none":
        result.setdefault("apiKeyHelper", HELPER)
    elif result.get("apiKeyHelper") == HELPER:
        # Only remove the helper this kit added; keep an operator's own.
        del result["apiKeyHelper"]
    for key in BYPASS_KEYS:
        result.pop(key, None)
    permissions = result.get("permissions")
    if isinstance(permissions, dict) and permissions.get("defaultMode") == "bypassPermissions":
        result["permissions"] = {k: v for k, v in permissions.items() if k != "defaultMode"}
    return result


def main(path=Path.home() / ".claude" / "settings.json"):
    try:
        current = json.loads(path.read_text())
    except FileNotFoundError:
        current = {}
    if not isinstance(current, dict):
        raise SystemExit(f"{path} is not a JSON object; refusing to replace it.")
    updated = merge(current, os.environ.get("SBX_CRED_ANTHROPIC_MODE", "none"))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(updated, stream, indent=2)
        stream.write("\n")
    Path(staged).replace(path)


if __name__ == "__main__":
    main()
