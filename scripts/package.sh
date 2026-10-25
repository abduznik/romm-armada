#!/usr/bin/env bash
# Builds RomM-Armada-<version>.zip: the file Decky's "Install Plugin from Zip" takes.
#
# Run ./native/build.sh and `pnpm run build` first. This script only gathers what they made and checks it:
# the build output exists, both helpers run (the x86-64 one here; the aarch64 one under qemu when it is
# installed), the version is the same in package.json and in the Python code, and nothing from the tests or
# from a Python cache gets into the zip. Builds from the same inputs give the same zip bytes.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
OUT=${OUT:-$ROOT}
NAME=romm-armada

VERSION=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' package.json | head -1)
PYVER=$(sed -n 's/^PLUGIN_VERSION = "\(.*\)"/\1/p' py_modules/rommarmada/connect.py)
[ "$VERSION" = "$PYVER" ] || { echo "package.json says $VERSION but connect.py says $PYVER" >&2; exit 1; }

for f in dist/index.js plugin.json main.py LICENSE THIRD_PARTY.md README.md bin/savebridge-x86_64 bin/savebridge-aarch64; do
    [ -f "$f" ] || { echo "missing $f (run native/build.sh and pnpm run build)" >&2; exit 1; }
done

echo "== checking the helpers"
chmod 755 bin/savebridge-*
./bin/savebridge-x86_64 version | grep -q '"ok":true' || { echo "the x86-64 helper does not run" >&2; exit 1; }
file bin/savebridge-aarch64 | grep -q 'ARM aarch64' || { echo "bin/savebridge-aarch64 is not an aarch64 executable" >&2; exit 1; }
if command -v qemu-aarch64-static > /dev/null 2>&1; then
    qemu-aarch64-static ./bin/savebridge-aarch64 version | grep -q '"arch":"aarch64"' \
        || { echo "the aarch64 helper does not run under qemu" >&2; exit 1; }
else
    echo "   (qemu-aarch64-static not installed: the aarch64 helper was not run)"
fi

echo "== staging"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/$NAME"
mkdir -p "$DEST/bin" "$DEST/dist" "$DEST/licenses" "$DEST/py_modules"
cp plugin.json package.json main.py LICENSE THIRD_PARTY.md README.md "$DEST/"
cp dist/index.js "$DEST/dist/"
[ -f dist/index.js.map ] && cp dist/index.js.map "$DEST/dist/" || true
cp bin/savebridge-x86_64 bin/savebridge-aarch64 "$DEST/bin/"
cp -r py_modules/rommarmada "$DEST/py_modules/"
[ -d licenses ] && cp licenses/* "$DEST/licenses/" || true
# how the two executables were made: the helper's own source and the pinned argosy-sigil revision it links
mkdir -p "$DEST/source"
cp native/savebridge/savebridge.c native/savebridge/CMakeLists.txt native/SIGIL_PIN native/build.sh native/toolchain-aarch64.cmake "$DEST/source/"
find "$DEST" -name '__pycache__' -prune -exec rm -rf {} + 2> /dev/null || true
find "$DEST" -name '*.pyc' -delete
find "$DEST" -type d -exec chmod 755 {} +
find "$DEST" -type f -exec chmod 644 {} +
chmod 755 "$DEST"/bin/savebridge-*
find "$DEST" -exec touch -d '2026-01-01 00:00:00 UTC' {} +

echo "== zipping"
ZIP="$OUT/RomM-Armada-$VERSION.zip"
rm -f "$ZIP"
(cd "$STAGE" && find "$NAME" | LC_ALL=C sort | TZ=UTC zip -X -q "$ZIP" -@)

echo "== contents"
unzip -l "$ZIP" | tail -n +4 | awk '{print $1, $4}' | grep -v '/$' | sed 's/^/   /'
if unzip -l "$ZIP" | grep -E '__pycache__|\.pyc|/test/|\.part$'; then
    echo "unwanted files in the zip" >&2
    exit 1
fi
unzip -tq "$ZIP" > /dev/null || { echo "the zip does not pass its own integrity test" >&2; exit 1; }
(cd "$OUT" && sha256sum "$(basename "$ZIP")" > "$(basename "$ZIP").sha256")
echo "== done"
cat "$ZIP.sha256"
ls -l "$ZIP"
