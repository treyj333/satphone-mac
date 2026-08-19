#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$PROJECT_DIR"
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean packaging/SATPHONE.spec

mkdir -p "$PROJECT_DIR/release"
ARCH_NAME="$(uname -m)"
ARCHIVE="$PROJECT_DIR/release/SATPHONE-macOS-${ARCH_NAME}.zip"

# Desktop may be managed by macOS File Provider, which adds Finder metadata
# inside bundles and prevents even ad-hoc signing. Package in a clean temp
# directory, then copy only the finished zip back into the project.
PACKAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$PACKAGE_DIR"' EXIT
ditto --norsrc "$PROJECT_DIR/dist/SATPHONE.app" "$PACKAGE_DIR/SATPHONE.app"
xattr -cr "$PACKAGE_DIR/SATPHONE.app"
codesign --force --deep --sign - "$PACKAGE_DIR/SATPHONE.app"
codesign --verify --deep --strict "$PACKAGE_DIR/SATPHONE.app"
ditto -c -k --sequesterRsrc --keepParent "$PACKAGE_DIR/SATPHONE.app" "$PACKAGE_DIR/SATPHONE.zip"
cp "$PACKAGE_DIR/SATPHONE.zip" "$ARCHIVE"

echo "Built $PROJECT_DIR/dist/SATPHONE.app"
echo "Created $ARCHIVE"
