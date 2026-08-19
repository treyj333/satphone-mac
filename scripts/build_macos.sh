#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_BUNDLE_NAME="Tacthrift Notecard Satphone.app"
ARCHIVE_PREFIX="Tacthrift-Notecard-Satphone-macOS"

cd "$PROJECT_DIR"
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean packaging/SATPHONE.spec

mkdir -p "$PROJECT_DIR/release"
ARCH_NAME="$(uname -m)"
ARCHIVE="$PROJECT_DIR/release/${ARCHIVE_PREFIX}-${ARCH_NAME}.zip"

# Desktop may be managed by macOS File Provider, which adds Finder metadata
# inside bundles and prevents even ad-hoc signing. Package in a clean temp
# directory, then copy only the finished zip back into the project.
PACKAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$PACKAGE_DIR"' EXIT
ditto --norsrc "$PROJECT_DIR/dist/$APP_BUNDLE_NAME" "$PACKAGE_DIR/$APP_BUNDLE_NAME"
xattr -cr "$PACKAGE_DIR/$APP_BUNDLE_NAME"
codesign --force --deep --sign - "$PACKAGE_DIR/$APP_BUNDLE_NAME"
codesign --verify --deep --strict "$PACKAGE_DIR/$APP_BUNDLE_NAME"
ditto -c -k --sequesterRsrc --keepParent "$PACKAGE_DIR/$APP_BUNDLE_NAME" "$PACKAGE_DIR/Tacthrift.zip"
cp "$PACKAGE_DIR/Tacthrift.zip" "$ARCHIVE"

echo "Built $PROJECT_DIR/dist/$APP_BUNDLE_NAME"
echo "Created $ARCHIVE"
