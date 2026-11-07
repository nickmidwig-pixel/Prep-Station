#!/bin/bash
# Builds dist/<App Name>.app and a zip. The name, version and bundle id come from app/midwigsync/brand.py.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
eval "$(cd "$ROOT/app" && python3 -c 'from midwigsync import brand as b; print(f"NAME=\"{b.APP_NAME}\"; VERSION={b.APP_VERSION}; BID={b.BUNDLE_ID}")')"
APP="$DIST/$NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"
# a build number that goes up with every version (1.5.2 -> 10502), so macOS never mixes up two builds
BUILD="$(echo "$VERSION" | awk -F. '{ printf "%d%02d%02d", $1, $2, ($3 == "" ? 0 : $3) }')"
sed -e "s/@APP_NAME@/$NAME/g" -e "s/@BUNDLE_ID@/$BID/g" -e "s/@VERSION@/$VERSION/g" -e "s/@BUILD@/$BUILD/g" "$ROOT/packaging/Info.plist" > "$APP/Contents/Info.plist"
sed -e "s/@APP_NAME@/$NAME/g" "$ROOT/packaging/launcher.sh" > "$APP/Contents/MacOS/$NAME"
chmod 755 "$APP/Contents/MacOS/$NAME"
cp "$ROOT/packaging/AppIcon.icns" "$APP/Contents/Resources/"
cp "$ROOT/app/requirements.txt" "$ROOT/app/requirements-extra.txt" "$APP/Contents/Resources/"
cp -R "$ROOT/app/midwigsync" "$APP/Contents/Resources/app/"
printf 'APPL????' > "$APP/Contents/PkgInfo"
mkdir -p "$APP/Contents/Resources/dev"
cp -R "$ROOT/tests" "$ROOT/packaging" "$APP/Contents/Resources/dev/"
cp "$ROOT/README.md" "$APP/Contents/Resources/dev/" 2>/dev/null || true
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
if grep -q "@APP_NAME@\|@BUNDLE_ID@\|@VERSION@\|@BUILD@" "$APP/Contents/Info.plist" "$APP/Contents/MacOS/$NAME"; then
  echo "placeholder left in bundle" >&2; exit 1
fi
cd "$DIST" && rm -f "$NAME $VERSION.zip"
zip -qry "$NAME $VERSION.zip" "$NAME.app"
echo "built $APP ($VERSION)"
