# Contributing

Issues and pull requests are welcome. Keep changes focused on the macOS companion
or local bridge, and do not commit tokens, device pairing data, signing credentials,
or generated app bundles.

Before opening a pull request, run:

```bash
python3 -m py_compile bridge/*.py
python3 tools/test_bridge.py
swiftc -O -framework AppKit macos-app/Sources/main.swift -o /tmp/PlayMac
```
