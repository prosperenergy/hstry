import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlparse


def make_server(workspace, port=8767, watch=False, interval=30):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log searches, private text, or access tokens.

        def respond(self, code, body, content_type="application/json", set_session=False):
            if content_type == "application/json":
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if set_session:
                self.send_header("Set-Cookie", "hstry_topics=" + token + "; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)

        def authorized(self):
            expected_origin = "http://127.0.0.1:" + str(self.server.server_port)
            origin = self.headers.get("Origin")
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
                cookie = cookies.get("hstry_topics")
                cookie_value = cookie.value if cookie else ""
            except Exception:
                cookie_value = ""
            credential = self.headers.get("X-Workspace-Token", "") or cookie_value
            return (self.valid_host() and (origin is None or origin == expected_origin)
                    and self.headers.get("Sec-Fetch-Site", "none") in ("none", "same-origin")
                    and secrets.compare_digest(credential, token))

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {"error": "Invalid host"})
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                return self.respond(200, {"app": "hstry-topics", "workspace_id": workspace.identity()})
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if parsed.path in static:
                filename, content_type = static[parsed.path]
                # A top-level navigation bootstraps a same-site HttpOnly session.
                # Embeds and fetches cannot do this; SOP and SameSite protect it
                # even when the navigation originated in a browser extension.
                bootstrap = (parsed.path == "/" and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and self.headers.get("Sec-Fetch-Dest") == "document")
                return self.respond(200, (Path(__file__).parent / filename).read_bytes(), content_type, bootstrap)
            if not self.authorized():
                return self.respond(403, {"error": "Open the private launcher URL."})
            query = parse_qs(parsed.query)
            get = lambda name, default="": query.get(name, [default])[0]
            try:
                if parsed.path == "/api/status":
                    result = workspace.overview()
                    result["automatic_refresh"] = self.server.watch_history
                elif parsed.path == "/api/chats":
                    result = workspace.chats(get("topic") or None, get("q"), offset=int(get("offset", "0")))
                elif parsed.path == "/api/context":
                    result = workspace.context(get("topic"))
                elif parsed.path == "/api/search":
                    result = workspace.search(get("q"), get("topic") or None)
                elif parsed.path == "/api/expand":
                    result = workspace.expand(get("chat"), int(get("start", "0")))
                elif parsed.path == "/api/native-plan":
                    result = workspace.native_plan(get("topic"))
                else:
                    return self.respond(404, {"error": "Not found"})
                self.respond(200, result)
            except (ValueError, sqlite3.Error) as exc:
                self.respond(400, {"error": str(exc)})

        def do_POST(self):
            if not self.authorized():
                return self.respond(403, {"error": "Access denied"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 50000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
                if self.path == "/api/sync":
                    result = workspace.sync()
                elif self.path == "/api/topic":
                    result = workspace.add_topic(body["name"], body["terms"])
                elif self.path == "/api/assign":
                    result = workspace.assign(body["topic_id"], body["chat_id"], body["kind"])
                elif self.path == "/api/canonical":
                    result = workspace.canonical(body["topic_id"], body["chat_id"])
                elif self.path == "/api/native-canonical":
                    result = workspace.choose_native_canonical(body["topic_id"], body["url"])
                elif self.path == "/api/brief":
                    result = workspace.save_brief(body["topic_id"], body["brief"], body["evidence"])
                else:
                    return self.respond(404, {"error": "Not found"})
                self.respond(200, result)
            except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                self.respond(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.access_token = token
    server.watch_history = watch
    server.watch_stop = threading.Event()
    if watch:
        def signature():
            result = []
            for path in (workspace.history, Path(str(workspace.history) + "-wal")):
                try:
                    stat = path.stat()
                    result.append((stat.st_mtime_ns, stat.st_size))
                except FileNotFoundError:
                    result.append(None)
            return result

        def refresh_changes():
            previous = signature()
            while not server.watch_stop.wait(interval):
                current = signature()
                if current != previous:
                    try:
                        workspace.sync()
                        previous = current
                    except (ValueError, OSError, sqlite3.Error):
                        # Keep the last complete index; retry on the next tick.
                        continue

        worker = threading.Thread(target=refresh_changes, daemon=True)
        worker.start()
        close = server.server_close

        def close_watcher():
            server.watch_stop.set()
            worker.join(timeout=2)
            close()

        server.server_close = close_watcher
    return server


def serve(workspace, port, watch=False):
    server = make_server(workspace, port, watch)
    url = "http://127.0.0.1:" + str(server.server_port) + "/"
    launch = workspace.state.parent / "dashboard-url.txt"
    launch.touch(mode=0o600, exist_ok=True)
    launch.chmod(0o600)
    launch.write_text(url + "\n")
    print("Private dashboard URL saved to " + str(launch), flush=True)
    print("HSTRY remains read-only. Press Ctrl-C to close.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

