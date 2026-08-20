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

cd "$APP_DIR" || exit 1
"$PYTHON" "$APP_DIR/discord_bridge.py" --config "$APP_DIR/discord-bridge.json"
EXIT_CODE=$?

if (( EXIT_CODE != 0 )); then
    echo
    read "?Press Return to close..."
fi

exit "$EXIT_CODE"
