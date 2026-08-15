"""System-wide Now Playing monitoring and control for the macOS bridge."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any


class NativeMediaError(RuntimeError):
    """Raised when the system media backend cannot perform a command."""


SUPPORTED_ACTIONS = {
    "get-state", "play", "pause", "toggle", "next", "previous", "seek",
    "seek-relative", "volume", "volume-relative",
}
_CLI_COMMANDS = {
    "play": "play", "pause": "pause", "toggle": "togglePlayPause",
    "next": "next", "previous": "previous",
}
_cached_volume: float | None = None
_cached_volume_at = 0.0


def _cli_path() -> str | None:
    configured = os.environ.get("NOTIPLAY_NOWPLAYING_CLI", "").strip()
    return configured or shutil.which("nowplaying-cli")


def _media_control_path() -> str | None:
    return shutil.which("media-control")


def backend_available() -> bool:
    return platform.system() == "Darwin" and (_media_control_path() is not None or _cli_path() is not None)


def _run_cli(*args: str, timeout: float = 4.0) -> str:
    binary = _cli_path()
    if not binary:
        raise NativeMediaError("缺少 MediaRemote 后端；请运行 brew install nowplaying-cli")
    if os.environ.get("NOTIPLAY_NATIVE_DRY_RUN") == "1":
        return "{}"
    try:
        completed = subprocess.run(
            [binary, *args], check=False, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeMediaError(f"无法调用 macOS 媒体接口: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise NativeMediaError(detail or "macOS MediaRemote 命令失败")
    return completed.stdout.strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _system_volume() -> float:
    global _cached_volume, _cached_volume_at
    if os.environ.get("NOTIPLAY_NATIVE_DRY_RUN") == "1":
        return 1.0
    if _cached_volume is not None and time.monotonic() - _cached_volume_at < 1.5:
        return _cached_volume
    completed = subprocess.run(
        ["/usr/bin/osascript", "-e", "output volume of (get volume settings)"],
        check=False, capture_output=True, text=True, timeout=2,
    )
    if completed.returncode != 0:
        return 1.0
    _cached_volume = max(0.0, min(1.0, _number(completed.stdout, 100) / 100))
    _cached_volume_at = time.monotonic()
    return _cached_volume


def get_now_playing_state() -> dict[str, Any] | None:
    """Return the system's primary Now Playing session."""
    media_control = _media_control_path()
    if media_control:
        completed = subprocess.run(
            [media_control, "get", "--now", "--no-artwork"], check=False,
            capture_output=True, text=True, timeout=4,
        )
        if completed.returncode != 0:
            raise NativeMediaError((completed.stderr or "读取媒体状态失败").strip())
        raw = completed.stdout.strip()
        raw_keys = False
    else:
        # Compatibility fallback. media-control is preferred because it
        # preserves MediaRemote's timestamp and calculates elapsedTimeNow.
        raw = _run_cli("get-raw")
        raw_keys = True
    if not raw or raw in {"null", "{}"}:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as error:
        raise NativeMediaError("macOS 返回了无法识别的播放状态") from error
    if not isinstance(info, dict):
        return None
    def field(name: str) -> Any:
        return info.get(f"kMRMediaRemoteNowPlayingInfo{name}") if raw_keys else info.get(name[0].lower() + name[1:])

    title = str(field("Title") or "").strip()
    if not title:
        return None
    rate = _number(field("PlaybackRate"))
    position = info.get("elapsedTimeNow") if not raw_keys else field("ElapsedTime")
    return {
        "event": "now-playing",
        "title": title,
        "artist": str(field("Artist") or ""),
        "album": str(field("Album") or ""),
        "position": max(0.0, _number(position)),
        "duration": max(0.0, _number(field("Duration"))),
        "volume": _system_volume(),
        "playing": rate > 0,
        "source": "macos-now-playing",
    }


def _set_system_volume(value: Any) -> None:
    global _cached_volume, _cached_volume_at
    level = max(0, min(100, round(_number(value) * 100)))
    if os.environ.get("NOTIPLAY_NATIVE_DRY_RUN") == "1":
        return
    completed = subprocess.run(
        ["/usr/bin/osascript", "-e", f"set volume output volume {level}"],
        check=False, capture_output=True, text=True, timeout=2,
    )
    if completed.returncode != 0:
        raise NativeMediaError((completed.stderr or "设置系统音量失败").strip())
    _cached_volume = level / 100
    _cached_volume_at = time.monotonic()


def _change_system_volume(value: Any) -> None:
    """Adjust and return volume in one AppleScript process instead of get + set."""
    global _cached_volume, _cached_volume_at
    delta = round(_number(value) * 100)
    if os.environ.get("NOTIPLAY_NATIVE_DRY_RUN") == "1":
        return
    script = (
        "set currentLevel to output volume of (get volume settings)\n"
        f"set targetLevel to currentLevel + ({delta})\n"
        "if targetLevel > 100 then set targetLevel to 100\n"
        "if targetLevel < 0 then set targetLevel to 0\n"
        "set volume output volume targetLevel\n"
        "return targetLevel"
    )
    completed = subprocess.run(
        ["/usr/bin/osascript", "-e", script], check=False,
        capture_output=True, text=True, timeout=2,
    )
    if completed.returncode != 0:
        raise NativeMediaError((completed.stderr or "调节系统音量失败").strip())
    _cached_volume = max(0.0, min(1.0, _number(completed.stdout) / 100))
    _cached_volume_at = time.monotonic()


def execute_native_command(command: dict[str, Any]) -> dict[str, Any]:
    """Control the system's current Now Playing application."""
    if platform.system() != "Darwin":
        raise NativeMediaError("原生媒体控制只能在 macOS 上运行")
    action = str(command.get("action", ""))
    if action not in SUPPORTED_ACTIONS:
        raise NativeMediaError(f"不支持的控制动作: {action or '空'}")
    value = command.get("value")

    if action == "get-state":
        state = get_now_playing_state()
        if state is None:
            raise NativeMediaError("macOS 当前没有可控制的媒体会话")
        return {"backend": "macos-mediaremote", "state": state}
    media_control = _media_control_path()
    if action in _CLI_COMMANDS:
        command_name = {
            "play": "play", "pause": "pause", "toggle": "toggle-play-pause",
            "next": "next-track", "previous": "previous-track",
        }[action]
        if media_control:
            subprocess.run([media_control, command_name], check=True, timeout=4)
        else:
            _run_cli(_CLI_COMMANDS[action])
    elif action == "seek":
        subprocess.run([media_control, "seek", str(max(0.0, _number(value)))], check=True, timeout=4) if media_control else _run_cli("seek", str(max(0.0, _number(value))))
    elif action == "seek-relative":
        state = get_now_playing_state()
        if state is None:
            raise NativeMediaError("macOS 当前没有可控制的媒体会话")
        target = str(max(0.0, state["position"] + _number(value)))
        subprocess.run([media_control, "seek", target], check=True, timeout=4) if media_control else _run_cli("seek", target)
    elif action == "volume":
        _set_system_volume(value)
    elif action == "volume-relative":
        _change_system_volume(value)

    # Controls return immediately. The monitor publishes authoritative state
    # asynchronously, so rapid crank/button input never waits on another full
    # MediaRemote + AppleScript readback cycle.
    return {"backend": "macos-mediaremote", "state": None}


class NowPlayingMonitor:
    """Poll MediaRemote and publish compact, authoritative state updates."""

    def __init__(self, publish: Callable[[dict[str, Any]], None]) -> None:
        self.publish = publish
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="now-playing")

    def start(self) -> bool:
        if not backend_available() or os.environ.get("NOTIPLAY_NATIVE_DRY_RUN") == "1":
            return False
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _run(self) -> None:
        last_signature: tuple[Any, ...] | None = None
        last_publish = 0.0
        while not self.stop_event.is_set():
            try:
                state = get_now_playing_state()
                if state:
                    signature = (state["title"], state["playing"], state["duration"], state["volume"])
                    now = time.monotonic()
                    if signature != last_signature or now - last_publish >= 3.0:
                        self.publish(state)
                        last_signature, last_publish = signature, now
            except NativeMediaError:
                pass
            self.stop_event.wait(0.75)
