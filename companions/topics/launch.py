#!/usr/bin/env python3
"""Start the topic dashboard on demand, reusing an existing matching instance."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import webbrowser

from topics import Workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--state")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-open", action="store_true", help="Start the server without opening a browser")
    args = parser.parse_args()
    workspace = Workspace(args.history, args.state)
    url = "http://127.0.0.1:" + str(args.port) + "/"
    # Probe a non-sensitive identity endpoint; never reuse another app's port.
    def running():
        try:
            with urlopen(url + "health", timeout=1) as response:
                result = json.load(response)
                if result.get("app") != "hstry-topics" or result.get("workspace_id") != workspace.identity():
                    raise ValueError("That port belongs to another workspace. Choose another --port.")
                return True
        except HTTPError:
            raise ValueError("That port is occupied by another service. Choose another --port.")
        except URLError:
            return False

    if not running():
        # Explicit launch refreshes the imported subset once; MCP startups do not.
        result = workspace.sync()
        print("History checked: " + str(result["entries"]) + " entries; " + str(result["changed"]) + " changed.")
        log = workspace.state.parent / "dashboard.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as output:
            command = [sys.executable, str(Path(__file__).with_name("topics.py")), "--history", str(workspace.history), "--state", str(workspace.state), "serve", "--port", str(args.port), "--watch"]
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        for _ in range(50):
            if running():
                break
            if process.poll() is not None:
                raise ValueError("The dashboard did not start. Check its local log.")
            time.sleep(0.1)
        else:
            raise ValueError("Dashboard startup timed out. Check its local log.")
    print(url)
    if not args.no_open:
        webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
