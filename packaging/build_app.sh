#!/bin/bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP="$DIST/Midwig Sync.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
cp "$ROOT/packaging/Info.plist" "$APP/Contents/"
cp "$ROOT/packaging/launcher.sh" "$APP/Contents/MacOS/Midwig Sync"
chmod 755 "$APP/Contents/MacOS/Midwig Sync"
cp "$ROOT/packaging/AppIcon.icns" "$APP/Contents/Resources/"
cp "$ROOT/app/requirements.txt" "$APP/Contents/Resources/"
cp -R "$ROOT/app/midwigsync" "$APP/Contents/Resources/app/" && find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
printf 'APPL????' > "$APP/Contents/PkgInfo"
mkdir -p "$APP/Contents/Resources/dev"
cp -R "$ROOT/tests" "$ROOT/packaging" "$APP/Contents/Resources/dev/"
cp "$ROOT/README.md" "$APP/Contents/Resources/dev/" 2>/dev/null || true
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
cd "$DIST" && rm -f "Midwig Sync.zip" "MidwigSync.tar.gz"
zip -qry "Midwig Sync.zip" "Midwig Sync.app"
tar czf MidwigSync.tar.gz "Midwig Sync.app"
echo "built $APP"
