#!/usr/bin/env python3
"""Small dependency-free local bridge for NotiPlay.

The bridge deliberately speaks plain HTTP so the Playdate can use the
official SDK HTTP client. Chrome and Playdate authenticate with the same
bearer token. Events are kept in memory and are replayable by cursor.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from macos_media import NativeMediaError, NowPlayingMonitor, backend_available, execute_native_command
from pairing_client import PairingError, claim_pairing


MAX_EVENTS = 500
PAIRING_TTL_SECONDS = 10 * 60
CLIENT_TIMEOUT_SECONDS = 35.0


class BridgeState:
    def __init__(self, data_path: Path) -> None:
        self.data_path = data_path
        self.lock = threading.Condition()
        self.events: dict[str, list[dict[str, Any]]] = {
            "playdate": [],
            "chrome": [],
        }
        self.sequence = 0
        self.pairing_code = ""
        self.pairing_expires_at = 0.0
        self.last_seen: dict[str, float] = {}
        self.token = self._load_token()

    def _load_token(self) -> str:
        try:
            value = json.loads(self.data_path.read_text(encoding="utf-8"))
            token = str(value.get("token", "")).strip()
            if token:
                return token
        except (OSError, ValueError, TypeError):
            pass
        token = secrets.token_urlsafe(24)
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_path.write_text(json.dumps({"token": token}, indent=2), encoding="utf-8")
        return token

    def new_pairing(self) -> tuple[str, str, float]:
        alphabet = string.digits
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        with self.lock:
            self.pairing_code = code
            self.pairing_expires_at = time.time() + PAIRING_TTL_SECONDS
        return code, self.token, self.pairing_expires_at

    def claim_pairing(self, code: str) -> str | None:
        with self.lock:
            if code != self.pairing_code or time.time() > self.pairing_expires_at:
                return None
            self.pairing_code = ""
            self.pairing_expires_at = 0
            return self.token

    def append(self, role: str, event: dict[str, Any]) -> dict[str, Any]:
        if role not in self.events:
            raise ValueError("unknown role")
        with self.lock:
            self.sequence += 1
            item = dict(event)
            item.setdefault("event", "message")
            # Include wall-clock nanoseconds so IDs remain unique when the
            # bridge process restarts and the in-memory counter resets.
            item["id"] = f"b{time.time_ns():020d}{self.sequence:04d}"
            item.setdefault("time", int(time.time()))
            self.events[role].append(item)
            del self.events[role][:-MAX_EVENTS]
            self.lock.notify_all()
            return item

    @staticmethod
    def _cursor_number(cursor: str) -> int | None:
        if not cursor or cursor in {"latest", "30s"}:
            return None
        if cursor.startswith("b"):
            try:
                return int(cursor[1:])
            except ValueError:
                return None
        return None

    def read(self, role: str, cursor: str, wait_seconds: float) -> list[dict[str, Any]]:
        deadline = time.time() + max(0.0, min(wait_seconds, 25.0))
        with self.lock:
            # A successful long-poll is also the client's heartbeat. This
            # lets /v1/command choose between the Chrome queue and the native
            # macOS fallback without ever delivering a command twice.
            self.last_seen[role] = time.monotonic()
            # `latest` means: ignore old history, but return events published
            # after this long-poll request starts.
            if cursor in {"", "latest"}:
                existing = self.events[role]
                baseline_number = self._cursor_number(str(existing[-1].get("id", ""))) if existing else 0
            else:
                baseline_number = self._cursor_number(cursor)
                # A persisted ntfy cursor is not comparable with Bridge IDs.
                # Treat it as the beginning of Bridge history so switching
                # transports cannot leave the client polling an empty stream
                # forever.
                if baseline_number is None:
                    baseline_number = 0
            while True:
                events = self.events[role]
                if cursor == "30s":
                    cutoff = int(time.time()) - 30
                    result = [item for item in events if int(item.get("time", 0)) >= cutoff]
                elif baseline_number is None:
                    result = []
                else:
                    result = [
                        item for item in events
                        if self._cursor_number(str(item.get("id", ""))) is not None
                        and self._cursor_number(str(item.get("id", ""))) > baseline_number
                    ]
                if result or time.time() >= deadline:
                    return result
                self.lock.wait(timeout=max(0.05, deadline - time.time()))

    def client_connected(self, role: str) -> bool:
        with self.lock:
            return time.monotonic() - self.last_seen.get(role, 0) <= CLIENT_TIMEOUT_SECONDS

    def client_status(self) -> dict[str, bool]:
        return {role: self.client_connected(role) for role in self.events}


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "PlayMac/0.1"

    @property
    def state(self) -> BridgeState:
        return self.server.bridge_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            # A long-poll client may time out or reload while the response is
            # being written. The request is already over from its perspective.
            pass

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 256 * 1024:
            raise ValueError("invalid request body")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return secrets.compare_digest(header, f"Bearer {self.state.token}")

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {
                "ok": True,
                "service": "notiplay-bridge",
                "version": 1,
                "clients": self.state.client_status(),
                "mediaRemote": backend_available(),
            })
            return
        if parsed.path == "/v1/events":
            if not self._require_auth():
                return
            query = parse_qs(parsed.query)
            role = query.get("role", [""])[0]
            if role not in {"playdate", "chrome"}:
                self._send_json(400, {"error": "role must be playdate or chrome"})
                return
            cursor = query.get("since", [""])[0]
            try:
                wait_seconds = float(query.get("wait", ["20"])[0])
            except ValueError:
                wait_seconds = 20
            events = self.state.read(role, cursor, wait_seconds)
            self._send_json(200, {"events": events})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/pair/start":
            code, token, expires_at = self.state.new_pairing()
            self._send_json(200, {"code": code, "token": token, "expiresAt": int(expires_at)})
            return
        if parsed.path == "/v1/pair/claim":
            try:
                body = self._read_json()
                token = self.state.claim_pairing(str(body.get("code", "")).strip())
            except (ValueError, json.JSONDecodeError):
                token = None
            if not token:
                self._send_json(400, {"error": "invalid or expired pairing code"})
                return
            self._send_json(200, {"token": token})
            return
        if parsed.path not in {"/v1/message", "/v1/command", "/v1/state"}:
            self._send_json(404, {"error": "not found"})
            return
        if not self._require_auth():
            return
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": str(error)})
            return

        if parsed.path == "/v1/message":
            event = {
                "event": "message",
                "title": str(body.get("title", "NotiPlay")),
                "message": str(body.get("message", "")),
                "click": str(body.get("click", "")),
                "tags": body.get("tags", []),
                "topic": "notiplay",
            }
            role = "playdate"
        elif parsed.path == "/v1/state":
            event = {
                "event": "message",
                "title": str(body.get("title", "Now Playing")),
                "message": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                "tags": ["notiplay", "now-playing"],
                "topic": "notiplay",
            }
            role = "playdate"
        else:
            event = {
                "event": "message",
                "title": "NotiPlay command",
                "message": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                "tags": ["notiplay", "command"],
                "topic": "notiplay-commands",
            }
            role = "chrome"
        if parsed.path == "/v1/command" and str(body.get("action", "")) == "scratch":
            if not self.state.client_connected("chrome"):
                self._send_json(503, {
                    "error": "DJ Scratch 需要 NotiPlay Chrome 扩展在线；MediaRemote 不支持反向音频",
                    "delivery": "unhandled",
                })
                return
            item = self.state.append("chrome", event)
            self._send_json(201, {**item, "delivery": "chrome-dj"})
            return
        if parsed.path == "/v1/command" and backend_available():
            try:
                native = execute_native_command(body)
            except NativeMediaError as error:
                self._send_json(503, {
                    "error": str(error),
                    "delivery": "unhandled",
                })
                return
            self._send_json(201, {
                "ok": True,
                "delivery": native["backend"],
                "state": native.get("state"),
            })
            return
        item = self.state.append(role, event)
        self._send_json(201, {**item, "delivery": "chrome-queue"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local NotiPlay bridge")
    parser.add_argument("--host", default="127.0.0.1", help="bind address; use 0.0.0.0 for a physical Playdate")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=Path(__file__).with_name("data.json"))
    parser.add_argument("--pairing-url", default="", help="HTTPS rendezvous service URL")
    parser.add_argument("--pair-code", default="", help="six-digit code shown on Playdate")
    parser.add_argument("--public-host", default="", help="LAN address advertised to Playdate")
    parser.add_argument("--companion-name", default="", help="Mac name shown after pairing")
    args = parser.parse_args()

    state = BridgeState(args.data)
    if args.pair_code:
        if not args.pairing_url:
            parser.error("--pairing-url is required with --pair-code")
        try:
            paired = claim_pairing(
                args.pairing_url, args.pair_code, state.token, args.port,
                args.public_host, args.companion_name,
            )
        except PairingError as error:
            parser.error(str(error))
        print(f"Paired code {paired['code']} as {paired['bridgeUrl']}")
    monitor = NowPlayingMonitor(lambda event: state.append("playdate", event))
    monitoring = monitor.start()
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    server.bridge_state = state  # type: ignore[attr-defined]
    print(f"PlayMac listening on http://{args.host}:{args.port}")
    print(f"macOS MediaRemote: {'online' if monitoring else 'unavailable'}")
    print("Use the Chrome Options page to generate a pairing token.")
    print(f"Bridge token is stored in {args.data}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPlayMac stopped")
    finally:
        monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
