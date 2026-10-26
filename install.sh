#!/usr/bin/env bash
set -euo pipefail

# Safe to execute from an existing checkout or as: curl -fsSL URL | bash
if [ "$(id -u)" -eq 0 ]; then
    echo "Run this installer as your regular Klipper user, not root." >&2
    exit 1
fi
for command in git python3; do
    command -v "$command" >/dev/null 2>&1 || { echo "Missing dependency: $command" >&2; exit 1; }
done

SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || pwd)"
if [ -d "$SCRIPT_DIR/.git" ] && [ -f "$SCRIPT_DIR/scripts/setup.py" ]; then
    REPO_DIR="$SCRIPT_DIR"
else
    REPO_DIR="$HOME/klipper-revive"
    if [ ! -d "$REPO_DIR/.git" ]; then
        if [ -e "$REPO_DIR" ]; then
            echo "Refusing to overwrite existing path: $REPO_DIR" >&2
            exit 1
        fi
        git clone --branch main https://github.com/sezgynus/klipper-revive.git "$REPO_DIR"
    fi
fi

ORIGIN="$(git -C "$REPO_DIR" remote get-url origin)"
case "$ORIGIN" in
    https://github.com/sezgynus/klipper-revive|https://github.com/sezgynus/klipper-revive.git|git@github.com:sezgynus/klipper-revive.git) ;;
    *) echo "Unexpected Git origin: $ORIGIN" >&2; exit 1 ;;
esac

exec python3 "$REPO_DIR/scripts/setup.py" install "$@"
