"""Mac-side client for claiming a NotiPlay Playdate pairing code."""

from __future__ import annotations

import json
import platform
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

try:
    import certifi
except ImportError:  # The development CLI can use the system trust store.
    certifi = None


class PairingError(RuntimeError):
    pass


def detect_lan_ip() -> str:
    """Return the source address macOS uses for the local network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect selects a route without sending application data.
        sock.connect(("192.0.2.1", 9))
        address = str(sock.getsockname()[0])
    except OSError as error:
        raise PairingError("无法确定 Mac 的局域网地址") from error
    finally:
        sock.close()
    if address.startswith("127.") or address == "0.0.0.0":
        raise PairingError("Mac 当前没有可用的局域网地址")
    return address


def claim_pairing(
    service_url: str,
    code: str,
    bridge_token: str,
    bridge_port: int,
    public_host: str = "",
    companion_name: str = "",
) -> dict[str, Any]:
    code = "".join(character for character in str(code) if character.isdigit())
    if len(code) != 6:
        raise PairingError("配对码必须是 6 位数字")
    host = public_host.strip() or detect_lan_ip()
    bridge_url = f"http://{host}:{bridge_port}"
    payload = json.dumps({
        "bridgeUrl": bridge_url,
        "bridgeToken": bridge_token,
        "companionName": companion_name.strip() or platform.node() or "Mac",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{service_url.rstrip('/')}/v1/sessions/{code}/claim",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "PlayMac/1.0",
        },
    )
    try:
        context = ssl.create_default_context(cafile=certifi.where() if certifi else None)
        with urllib.request.urlopen(request, timeout=8, context=context) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise PairingError(f"配对服务返回 {error.code}: {detail}") from error
    except (OSError, ValueError) as error:
        raise PairingError(f"无法连接配对服务: {error}") from error
    if result.get("status") != "paired":
        raise PairingError(f"配对失败: {result.get('status', 'unknown')}")
    return {"status": "paired", "bridgeUrl": bridge_url, "code": code}
