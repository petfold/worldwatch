#!/usr/bin/env bash
# Worldwatch VPS deploy. Idempotent; re-run to update. Run as root (or sudo).
#
#   sudo ops/deploy.sh
#
# Assumes: Debian/Ubuntu-like host with python3.12+, systemd, git. Adjust the
# variables below to taste. See ops/README.md for the full walkthrough.
set -euo pipefail

APP_USER=worldwatch
APP_DIR=/opt/worldwatch                 # repo checkout lives here
VENV_DIR="$APP_DIR/.venv"
DATA_DIR=/var/lib/worldwatch            # WW_DB_PATH parent
ETC_DIR=/etc/worldwatch                 # env file lives here
REPO_URL="${REPO_URL:-https://github.com/petfold/worldwatch}"

echo ">> system user"
id -u "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo ">> directories"
mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR"

echo ">> code"
if [ -d "$APP_DIR/.git" ]; then
  before=$(git -C "$APP_DIR" rev-parse HEAD)
  git -C "$APP_DIR" pull --ff-only
  # bash keeps running the script it started with; hand over to the pulled one
  # so an update that changes deploy.sh takes effect in this same run.
  if [ -z "${WW_DEPLOY_REEXEC:-}" ] && [ "$(git -C "$APP_DIR" rev-parse HEAD)" != "$before" ]; then
    echo "   code updated — re-running the new deploy.sh"
    WW_DEPLOY_REEXEC=1 exec "$APP_DIR/ops/deploy.sh" "$@"
  fi
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo ">> venv + install"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "$APP_DIR[export]"   # [export]: pyarrow, for the daily Parquet export

echo ">> environment file"
if [ ! -f "$ETC_DIR/worldwatch.env" ]; then
  cp "$APP_DIR/ops/worldwatch.env.example" "$ETC_DIR/worldwatch.env"
  echo "   created $ETC_DIR/worldwatch.env — EDIT IT (push channel, keys)"
fi

echo ">> ownership"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR" "$ETC_DIR"

echo ">> initialize database + register sources"
sudo -u "$APP_USER" env $(grep -v '^#' "$ETC_DIR/worldwatch.env" | xargs) \
  "$VENV_DIR/bin/python" -m worldwatch init

echo ">> systemd units"
cp "$APP_DIR"/ops/systemd/*.slice "$APP_DIR"/ops/systemd/*.service "$APP_DIR"/ops/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now worldwatch-poll.service worldwatch-api.service
systemctl enable --now worldwatch-consolidate.timer worldwatch-detect.timer worldwatch-presence.timer worldwatch-nursery.timer worldwatch-digest.timer worldwatch-export.timer
# enable --now leaves running services alone; restart them onto new code/units.
systemctl try-restart worldwatch-poll.service worldwatch-api.service

echo ">> done. Check: systemctl status 'worldwatch-*'"
