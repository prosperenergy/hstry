#!/usr/bin/env python3
"""One unattended collection, topic-refresh and optional cloud-upload pass."""
import argparse
import fcntl
import json
from pathlib import Path

from adapter_import import run as import_adapters
from cloud_sync import sync as cloud_sync, scrub
from local_import import configured_refresh, private_json
from topics import Workspace, now


def run(config_path, apply=False):
    config_path = Path(config_path).expanduser().resolve()
    config = json.loads(config_path.read_text())
    workspace = Workspace(config["history"], config.get("state"))
    status_path = workspace.state.parent / "background-status.json"
    lock_path = workspace.state.parent / "background.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running", "changed": False}
        status = {"checked_at": now(), "status": "dry_run" if not apply else "running",
                  "all_ai_complete": False, "stages": {}}
        if not apply:
            status["stages"]["cloud"] = bool(config.get("cloud"))
            return status
        try:
            status["active_stage"] = "local"
            private_json(status_path, status)
            status["stages"]["local"] = configured_refresh(workspace, background=True)
            if config.get("adapter_config"):
                status["active_stage"] = "adapters"
                private_json(status_path, status)
                status["stages"]["adapters"] = import_adapters(
                    workspace.history, config["adapter_config"],
                    workspace.state.parent / "background-adapters", config.get("bun", "bun"), False)
            status["active_stage"] = "topics"
            private_json(status_path, status)
            status["stages"]["topics"] = workspace.sync()
            if config.get("cloud"):
                status["stages"]["cloud"] = cloud_sync(
                    workspace, config["cloud"], workspace.state.parent / "cloud-sync", True)
            status["status"] = "success"
            status["active_stage"] = "complete"
            status["completed_at"] = now()
            private_json(status_path, status)
            return status
        except Exception as exc:
            status["status"] = "failed"
            status["error_type"] = type(exc).__name__
            status["error"] = scrub(str(exc))[:400]
            status["completed_at"] = now()
            private_json(status_path, status)
            raise RuntimeError("Background refresh failed; previous cloud data retained. See background-status.json.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = run(args.config, args.apply)
    print(json.dumps({key: result[key] for key in ("status", "all_ai_complete") if key in result}))


if __name__ == "__main__":
    main()
