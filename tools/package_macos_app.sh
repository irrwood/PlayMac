#!/bin/zsh
set -euo pipefail

project_root="${0:A:h:h}"
version="$(tr -d '[:space:]' < "$project_root/VERSION")"
build_number="$(echo "$version" | awk -F. '{ print ($1 * 10000) + ($2 * 100) + $3 }')"
build_root="$(mktemp -d /tmp/notiplay-macos.XXXXXX)"
app="$build_root/PlayMac.app"

if [[ ! -x "$project_root/macos-app/.venv/bin/pyinstaller" ]]; then
  python3 -m venv "$project_root/macos-app/.venv"
  "$project_root/macos-app/.venv/bin/python" -m pip install -r "$project_root/requirements-build.txt"
fi

mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
swiftc -O -framework AppKit \
  "$project_root/macos-app/Sources/main.swift" \
  -o "$app/Contents/MacOS/PlayMac"

sed -e "s/__VERSION__/$version/g" -e "s/__BUILD__/$build_number/g" \
  "$project_root/macos-app/Info.plist" > "$app/Contents/Info.plist"
"$project_root/macos-app/.venv/bin/pyinstaller" \
  --noconfirm --clean --onefile \
  --name PlayMacServer \
  --paths "$project_root/bridge" \
  --distpath "$app/Contents/Resources" \
  --workpath "$build_root/pyinstaller-work" \
  --specpath "$build_root" \
  "$project_root/bridge/server.py" >/dev/null

codesign --force --deep --sign - "$app"

archive="$project_root/artifacts/PlayMac-$version.app.zip"
ditto -c -k --keepParent "$app" "$archive"
echo "$archive"
