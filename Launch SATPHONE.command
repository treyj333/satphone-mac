#!/bin/zsh

# Always launch relative to this file, even when it is opened from Finder.
APP_DIR="${0:A:h}"
PYTHON="$APP_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "SATPHONE's Python environment is missing."
    echo "Reinstall it from this folder before trying again."
    echo
    read "?Press Return to close..."
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "SATPHONE's saved environment uses an unsupported Python version."
    echo "Recreate .venv with /opt/homebrew/bin/python3.12 as shown in README.md."
    echo
    read "?Press Return to close..."
    exit 1
fi

cd "$APP_DIR" || exit 1
"$PYTHON" "$APP_DIR/satphone.py"
EXIT_CODE=$?

# Keep the window open when startup fails so the error remains readable.
if (( EXIT_CODE != 0 )); then
    echo
    read "?Press Return to close..."
fi

exit "$EXIT_CODE"
