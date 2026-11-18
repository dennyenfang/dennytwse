#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
runtime="$HOME/.local/libexec/denny888"
store="$HOME/.local/share/denny888"
config="$HOME/.config/denny888"
units="$HOME/.config/systemd/user"
for program in git ssh bwrap flock curl python3 systemctl; do command -v "$program" >/dev/null; done
test -s "$config/deploy_key"
test -s "$config/known_hosts"
mkdir -p "$units"
install -d -m 0700 "$runtime" "$store" "$store/releases" "$store/venvs" "$store/backups" "$store/data"
install -m 0700 deploy.sh sandbox.sh "$runtime/"
install -m 0600 denny888.service denny888-deploy.service denny888-deploy.timer "$units/"
systemctl --user daemon-reload
systemctl --user start denny888-deploy.service
systemctl --user enable denny888.service
systemctl --user enable --now denny888-deploy.timer
