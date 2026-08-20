#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_BUNDLE_NAME="Tacthrift Notecard Satphone.app"
ARCHIVE_PREFIX="Tacthrift-Notecard-Satphone-macOS"

cd "$PROJECT_DIR"
# Desktop may be managed by macOS File Provider, which adds Finder metadata
# inside bundles and can race PyInstaller while it creates or signs files.
# Build and sign away from Desktop, then copy only finished artifacts back.
PACKAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$PACKAGE_DIR"' EXIT
"$PYTHON_BIN" -m PyInstaller \
    --noconfirm \
    --clean \
    --workpath "$PACKAGE_DIR/build" \
    --distpath "$PACKAGE_DIR/dist" \
    packaging/SATPHONE.spec

PACKAGE_APP="$PACKAGE_DIR/dist/$APP_BUNDLE_NAME"
xattr -cr "$PACKAGE_APP"
codesign --force --deep --sign - "$PACKAGE_APP"
codesign --verify --deep --strict "$PACKAGE_APP"

mkdir -p "$PROJECT_DIR/dist" "$PROJECT_DIR/release"
if [[ -e "$PROJECT_DIR/dist/$APP_BUNDLE_NAME" ]]; then
    mv "$PROJECT_DIR/dist/$APP_BUNDLE_NAME" "$PACKAGE_DIR/previous.app"
fi
ditto --norsrc "$PACKAGE_APP" "$PROJECT_DIR/dist/$APP_BUNDLE_NAME"

ARCH_NAME="$(uname -m)"
ARCHIVE="$PROJECT_DIR/release/${ARCHIVE_PREFIX}-${ARCH_NAME}.zip"
ditto -c -k --sequesterRsrc --keepParent "$PACKAGE_APP" "$PACKAGE_DIR/Tacthrift.zip"
cp "$PACKAGE_DIR/Tacthrift.zip" "$ARCHIVE"

echo "Built $PROJECT_DIR/dist/$APP_BUNDLE_NAME"
echo "Created $ARCHIVE"
