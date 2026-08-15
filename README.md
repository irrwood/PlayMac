# PlayMac

PlayMac is an open-source macOS menu bar companion that lets a Playdate control
the Mac's current media session over the local network.

It provides:

- six-digit pairing with a Playdate client;
- play, pause, previous, next, seek, and system-volume controls;
- Now Playing state forwarded to Playdate;
- a local HTTP bridge authenticated with a random bearer token;
- English and Chinese menu-bar interfaces.

Messages and media controls travel directly between the Playdate and Mac on the
same Wi-Fi. The public pairing endpoint is only a short-lived rendezvous service
that exchanges the Mac's LAN address and random token.

## Requirements

- Apple Silicon Mac running macOS 13 or newer;
- Xcode command-line tools for building from source;
- [`media-control`](https://github.com/ungive/media-control) for macOS Now
  Playing monitoring and control.

Install the media backend:

```bash
brew tap ungive/media-control
brew install media-control
```

## Build

```bash
tools/package_macos_app.sh
```

The script creates a self-contained `PlayMac.app` ZIP in `artifacts/`. End users
do not need Python or Xcode because the bridge runtime is embedded in the app.

Unsigned local builds are intended for development. Public distribution requires
a Developer ID Application signature, hardened runtime, and Apple notarization.

## Run the bridge without the app

```bash
python3 bridge/server.py --host 0.0.0.0
```

Pair a six-digit code shown by the Playdate client:

```bash
python3 bridge/server.py --host 0.0.0.0 \
  --pairing-url https://pair.tofukanban.uk \
  --pair-code 123456
```

## Local API

- `GET /health`
- `GET /v1/events?role=playdate|chrome&since=<cursor>&wait=20`
- `POST /v1/message`
- `POST /v1/state`
- `POST /v1/command`

Except for `/health` and local pairing bootstrap routes, endpoints require the
bearer token stored in `~/Library/Application Support/PlayMac/data.json`.

## Scope

This repository contains only the macOS companion and local bridge. The Playdate
client, Chrome extension, design assets, and hosted pairing service are maintained
separately.

## License

Apache License 2.0. See [LICENSE](LICENSE).
