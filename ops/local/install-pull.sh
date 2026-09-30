#!/usr/bin/env bash
# Install the daily pull on this (local) machine as a systemd user timer.
# No sudo: everything lives under your home directory.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
install -D -m 755 "$here/pull-archive.sh" "$HOME/.local/bin/worldwatch-pull"
install -D -m 644 -t "$HOME/.config/systemd/user/" "$here/worldwatch-pull.service" "$here/worldwatch-pull.timer"
systemctl --user daemon-reload
systemctl --user enable --now worldwatch-pull.timer
echo "installed. First pull now: systemctl --user start worldwatch-pull.service"
