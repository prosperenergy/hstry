#!/usr/bin/env python3
"""Install a five-minute device-local collector; dry-run unless --apply."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from local_import import private_json

LABEL = "io.prospershield.chat-library"


def definition(config, state, python=sys.executable):
    return {
        "Label": LABEL,
        "ProgramArguments": [str(Path(python).resolve()), str(Path(__file__).with_name("background.py").resolve()),
                             "--config", str(Path(config).expanduser().resolve()), "--apply"],
        "StartInterval": 300, "RunAtLoad": True, "ProcessType": "Background",
        "WorkingDirectory": str(Path(__file__).resolve().parent),
        "StandardOutPath": str(Path(state) / "background.stdout.log"),
        "StandardErrorPath": str(Path(state) / "background.stderr.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("This installer is for macOS launchd; use background.py from your scheduler elsewhere.")
    config, state = Path(args.config).expanduser().resolve(), Path(args.state).expanduser().resolve()
    if not config.is_file():
        raise SystemExit("Save and review the background configuration first.")
    plist = plistlib.dumps(definition(config, state))
    target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    if not args.apply:
        print(plist.decode())
        return
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    local_config = state / "local-import-config.json"
    if local_config.exists():
        local = json.loads(local_config.read_text())
        settings = json.loads(config.read_text())
        if local.get("enabled") is True and local.get("history") == settings.get("history"):
            saved = state / "local-import-before-background.json"
            if not saved.exists():
                private_json(saved, local)
            local["background_only"] = True
            private_json(local_config, local)
    for name in ("background.stdout.log", "background.stderr.log"):
        log = state / name
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plist)
    target.chmod(0o600)
    domain = "gui/" + str(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    subprocess.run(["launchctl", "print", domain + "/" + LABEL], check=True)
    print("Installed. Verify background-status.json and the private Brain collector receipt.")


if __name__ == "__main__":
    main()
