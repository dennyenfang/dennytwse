#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /
store="$HOME/.local/share/denny888"
config="$HOME/.config/denny888"
box="$HOME/.local/libexec/denny888/sandbox.sh"
repo="$store/repo.git"
port=${DENNY888_PORT:-5001}
[[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1 && 10#$port <= 65535)) || exit 2
exec 9>"$store/deploy.lock"
flock -n 9 || exit 0

if [[ ! -d "$repo" ]]; then git init --quiet --bare "$repo"; fi
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes -o UserKnownHostsFile='$config/known_hosts' -i '$config/deploy_key'"
git --git-dir="$repo" fetch --quiet --no-tags git@github.com:dennyroland/denny888.git +refs/heads/main:refs/remotes/origin/main
sha=$(git --git-dir="$repo" rev-parse 'refs/remotes/origin/main^{commit}')
[[ "$sha" =~ ^[a-f0-9]{40}$ ]]
release="$store/releases/$sha"
active=$(readlink -f "$store/current" || true)

healthy() {
    curl --fail --silent --noproxy '*' --max-time 4 "http://127.0.0.1:$port/healthz" |
        python3 -I -c 'import json,sys; d=json.load(sys.stdin); sys.exit(d.get("status") != "ok" or d.get("commit") != sys.argv[1])' "$1" 2>/dev/null
}
await_health() {
    for ((attempt=0; attempt<30; attempt++)); do
        if healthy "$1"; then return 0; fi
        sleep 1
    done
    return 1
}
link_to() {
    local target=$1 name=$2
    ln -sfnT "$target" "$store/.$name.next"
    mv -Tf "$store/.$name.next" "$store/$name"
}
if [[ "$active" == "$release" ]] && healthy "$sha"; then exit 0; fi

stage=""
snapshot=""
cleanup() {
    if [[ -n "$stage" ]]; then rm -rf -- "$stage"; fi
    if [[ -n "$snapshot" ]]; then rm -f -- "$snapshot"; fi
}
trap cleanup EXIT
if [[ ! -d "$release" ]]; then
    stage=$(mktemp -d "$store/releases/.candidate-XXXXXXXX")
    git --git-dir="$repo" archive "$sha" | tar --extract --no-same-owner --no-same-permissions -C "$stage"
    for reserved in .deploy-revision .deploy-venv .env; do
        if [[ -e "$stage/$reserved" || -L "$stage/$reserved" ]]; then
            echo "Reserved deployment filename in source: $reserved" >&2
            exit 1
        fi
    done
    [[ -f "$stage/requirements.txt" && ! -L "$stage/requirements.txt" ]]
    venv_id=$({ python3 --version; uname -m; cat "$stage/requirements.txt"; } | sha256sum | cut -d' ' -f1)
    printf '%s\n' "$sha" >"$stage/.deploy-revision"
    printf '%s\n' "$venv_id" >"$stage/.deploy-venv"
    if [[ ! -f "$store/venvs/$venv_id.ready" ]]; then
        # Recreate an interrupted dependency install before retrying.
        rm -rf -- "$store/venvs/$venv_id"
        mkdir "$store/venvs/$venv_id"
        "$box" build "$stage"
        touch "$store/venvs/$venv_id.ready"
    fi
    "$box" check "$stage" "$sha"
    mv -T "$stage" "$release"
    stage=""
fi
if [[ -f "$store/data/portfolio.db" && "$active" != "$release" ]]; then
    snapshot=$(mktemp "$store/backups/.candidate-XXXXXXXX")
    "$box" backup "$release" >"$snapshot"
    mv -T "$snapshot" "$store/backups/$(date -u +%Y%m%dT%H%M%SZ)-$sha.sqlite3"
    snapshot=""
fi

if [[ -d "$active" && "$active" != "$release" ]]; then link_to "$active" previous; fi
link_to "$release" current
if systemctl --user restart denny888.service && await_health "$sha"; then
    echo "Serving dennyroland/denny888@$sha"
else
    previous=$(readlink -f "$store/previous" || true)
    if [[ -d "$previous" && "$previous" != "$release" ]]; then
        link_to "$previous" current
        systemctl --user restart denny888.service
        await_health "$(basename "$previous")"
        echo "Restored previous release $(basename "$previous")" >&2
    fi
    echo "Release $sha failed its live health check" >&2
    exit 1
fi

# Keep the active and fallback releases plus five pre-deploy database snapshots.
previous=$(readlink -f "$store/previous" || true)
for old in "$store"/releases/*; do
    if [[ -d "$old" && "$old" != "$release" && "$old" != "$previous" ]]; then rm -rf -- "$old"; fi
done
for ready in "$store"/venvs/*.ready; do
    [[ -f "$ready" ]] || continue
    key=$(basename "$ready" .ready)
    [[ "$key" =~ ^[a-f0-9]{64}$ ]] || continue
    if ! grep -Fqx "$key" "$store"/releases/*/.deploy-venv; then
        rm -rf -- "$store/venvs/$key"
        rm -f -- "$ready"
    fi
done
mapfile -t backups < <(find "$store/backups" -maxdepth 1 -type f -name '*.sqlite3' -printf '%f\n' | sort -r)
for old in "${backups[@]:5}"; do rm -f -- "$store/backups/$old"; done
