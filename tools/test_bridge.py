#!/usr/bin/env python3
"""Smoke-test the dependency-free local bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def request(base: str, path: str, method: str = "GET", body: dict | None = None, token: str = "") -> dict:
    raw = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if raw is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=raw, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.loads(response.read().decode())


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        data_path = Path(directory) / "data.json"
        test_environment = os.environ.copy()
        test_environment["NOTIPLAY_NATIVE_DRY_RUN"] = "1"
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge/server.py"), "--port", "18765", "--data", str(data_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=test_environment,
        )
        try:
            base = "http://127.0.0.1:18765"
            for _ in range(30):
                try:
                    request(base, "/health")
                    break
                except urllib.error.URLError:
                    time.sleep(0.05)
            pairing = request(base, "/v1/pair/start", "POST", {})
            token = pairing["token"]
            assert request(base, "/v1/pair/claim", "POST", {"code": pairing["code"]})["token"] == token
            message = request(
                base,
                "/v1/message",
                "POST",
                {"title": "测试", "message": "Hello bridge"},
                token,
            )
            events = request(base, "/v1/events?role=playdate&since=30s&wait=0", token=token)["events"]
            assert events and events[-1]["id"] == message["id"]

            latest_result: dict[str, list[dict]] = {}

            def wait_for_latest() -> None:
                latest_result["events"] = request(
                    base,
                    "/v1/events?role=playdate&since=latest&wait=2",
                    token=token,
                )["events"]

            waiter = threading.Thread(target=wait_for_latest)
            waiter.start()
            time.sleep(0.1)
            live_message = request(
                base,
                "/v1/message",
                "POST",
                {"title": "实时", "message": "Long poll"},
                token,
            )
            waiter.join(timeout=3)
            assert latest_result.get("events")
            assert latest_result["events"][-1]["id"] == live_message["id"]

            command = {"event": "command", "action": "play", "target": "chrome"}
            native_result = request(base, "/v1/command", "POST", command, token)
            assert native_result["delivery"] == "macos-mediaremote"

            # A connected extension no longer intercepts basic transport
            # commands: MediaRemote remains authoritative system-wide.
            request(base, "/v1/events?role=chrome&since=latest&wait=0", token=token)
            second_native = request(base, "/v1/command", "POST", command, token)
            assert second_native["delivery"] == "macos-mediaremote"
            scratch_result = request(
                base, "/v1/command", "POST",
                {"event": "command", "action": "scratch", "value": -0.15, "target": "chrome"},
                token,
            )
            assert scratch_result["delivery"] == "chrome-dj"
            chrome_events = request(base, "/v1/events?role=chrome&since=30s&wait=0", token=token)["events"]
            assert json.loads(chrome_events[-1]["message"])["action"] == "scratch"
            print("bridge-ok")
        finally:
            process.terminate()
            process.wait(timeout=3)


if __name__ == "__main__":
    main()
