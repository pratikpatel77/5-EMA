"""Loopback-only HTTP server for the 5 EMA desk.

Binds to 127.0.0.1 and refuses any other Host or Origin, so nothing on the
network can reach it. Writes additionally carry a per-process token handed
out with the first page load, so a stray page in another tab cannot post to
it. No credentials are ever sent to the browser.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import Desk, ScanError

STATIC = Path(__file__).resolve().parent / "static"
APP = "5ema-desk"
VERSION = "1.0.0"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def create_server(desk: Desk, port: int) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"{APP}/{VERSION}"

        def log_message(self, *args):
            pass

        # -- plumbing ---------------------------------------------------- #
        def send(self, status: int, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def allowed(self, write: bool = False) -> bool:
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in hosts:
                self.send(403, {"error": "This dashboard only answers on localhost."})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in hosts}:
                self.send(403, {"error": "Origin not allowed."})
                return False
            if write and not secrets.compare_digest(self.headers.get("X-Desk-Token", ""), token):
                self.send(403, {"error": "Reload the dashboard, then try again."})
                return False
            return True

        def body(self) -> dict:
            size = int(self.headers.get("Content-Length", "0") or 0)
            if not 0 < size <= 65536:
                raise ValueError("A small JSON request is required.")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("Send JSON.")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("Send a JSON object.")
            return payload

        # -- routes ------------------------------------------------------ #
        def do_GET(self):
            if not self.allowed():
                return
            route = urlparse(self.path)
            query = parse_qs(route.query)
            try:
                if route.path == "/api/health":
                    self.send(200, {"app": APP, "version": VERSION})
                    return
                if route.path == "/api/state":
                    self.send(200, {**desk.state(), "token": token})
                    return
                if route.path == "/api/prices":
                    self.send(200, {**desk.refresh_prices(), "token": token})
                    return
                if route.path == "/api/detail":
                    symbol = query.get("symbol", [""])[0]
                    self.send(200, desk.detail(symbol, refresh_debt=query.get("debt") == ["1"]))
                    return
                self.serve_static(route.path)
            except ScanError as exc:
                self.send(409, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                self.send(400, {"error": str(exc).strip("'\"")})
            except Exception:
                self.send(500, {"error": "The dashboard could not read its own data."})

        def serve_static(self, path: str):
            relative = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (STATIC / relative).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                self.send(404, {"error": "Not found"})
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self.send(200, target.read_bytes(), ctype)

        def do_POST(self):
            if not self.allowed(write=True):
                return
            try:
                request = self.body()
                path = urlparse(self.path).path
                if path == "/api/scan":
                    threading.Thread(
                        target=_safe_scan,
                        args=(desk, bool(request.get("refresh")), bool(request.get("offline"))),
                        daemon=True).start()
                    self.send(202, {"started": True})
                elif path == "/api/take":
                    self.send(200, {"position": desk.take(request), **desk.state()})
                elif path == "/api/ignore":
                    desk.ignore(request)
                    self.send(200, desk.state())
                elif path == "/api/entry":
                    self.send(200, {"position": desk.update_entry(request), **desk.state()})
                elif path == "/api/close":
                    self.send(200, {"position": desk.close_entry(request), **desk.state()})
                elif path == "/api/forget":
                    desk.forget(str(request.get("id") or ""))
                    self.send(200, desk.state())
                elif path == "/api/restore":
                    desk.restore(str(request.get("id") or ""))
                    self.send(200, desk.state())
                elif path == "/api/debt":
                    self.send(200, {"debt": desk.lookup_debt(str(request.get("symbol") or ""))})
                elif path == "/api/settings":
                    self.send(200, {"settings": desk.save_settings(request), **desk.state()})
                else:
                    self.send(404, {"error": "Not found"})
            except ScanError as exc:
                self.send(409, {"error": str(exc)})
            except (KeyError, ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc).strip("'\"")})
            except Exception:
                self.send(500, {"error": "That change could not be saved."})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _safe_scan(desk: Desk, refresh: bool, offline: bool):
    try:
        desk.scan(refresh=refresh, offline=offline)
    except Exception:
        pass  # the failure is already published in desk.scan_state


def _startup(desk: Desk, resumed: bool, scan: bool, offline: bool):
    if resumed:
        try:
            desk.refresh_prices()
        except Exception:
            pass  # the saved prices stay, labelled as daily closes
    if scan:
        _safe_scan(desk, False, offline)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="5 EMA monthly desk -- local dashboard")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--no-scan", action="store_true",
                        help="open on the saved scan and do not run a fresh one")
    parser.add_argument("--offline", action="store_true", help="use only cached bars")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port from 1024 to 65535.")

    url = f"http://127.0.0.1:{args.port}"
    from urllib.request import urlopen
    try:  # a second launch just reopens the running one
        with urlopen(url + "/api/health", timeout=1) as response:
            if json.load(response).get("app") == APP:
                if not args.no_open:
                    webbrowser.open(url)
                print(f"The 5 EMA desk is already running: {url}", flush=True)
                return 0
    except (OSError, ValueError):
        pass

    desk = Desk()
    try:
        server = create_server(desk, args.port)
    except OSError:
        print(f"Port {args.port} is busy. Try:  python -m ema5 --port {args.port + 1}", flush=True)
        return 1

    desk.start()
    # Open on the last scan, put live prices on it, and only then run the full
    # scan -- so candidates and open trades are usable straight away.
    resumed = desk.load_snapshot()
    threading.Thread(target=_startup, args=(desk, resumed, not args.no_scan, args.offline),
                     daemon=True).start()
    if resumed:
        print("Resumed from the saved scan.", flush=True)
    print(f"5 EMA monthly desk: {url}\nKeep this window open. Press Ctrl+C to stop.", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        desk.close()
        server.server_close()
    return 0
