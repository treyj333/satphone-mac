#!/bin/zsh

APP_DIR="${0:A:h}"
APP_BUNDLE="$APP_DIR/dist/Tacthrift Notecard Satphone.app"

if [[ ! -d "$APP_BUNDLE" ]]; then
    osascript -e 'display alert "Tacthrift Notecard Satphone is not built yet" message "Run scripts/build_macos.sh once, or download the app from GitHub Releases." as critical'
    exit 1
fi

open "$APP_BUNDLE"
