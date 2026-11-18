#!/usr/bin/env bash
set -euo pipefail

mode=${1:?mode required}
release=$(realpath -e -- "${2:?release required}")
store="$HOME/.local/share/denny888"
venv_id=$(<"$release/.deploy-venv")
[[ "$venv_id" =~ ^[a-f0-9]{64}$ ]]
port=${DENNY888_PORT:-5001}
[[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1 && 10#$port <= 65535)) || exit 2

# The private SSH key, sibling processes, user bus, agent sockets and host home
# are outside these mounts. Use a fresh network-capable namespace for each run.
box=(bwrap --unshare-all --share-net --die-with-parent --new-session --cap-drop ALL
     --clearenv --setenv PATH /venv/bin:/usr/bin:/bin --setenv HOME /tmp
     --setenv LANG C.UTF-8 --setenv PYTHONDONTWRITEBYTECODE 1
     --ro-bind /usr /usr --proc /proc --dev /dev --tmpfs /tmp --tmpfs /run
     --ro-bind "$release" /app --chdir /app)
for directory in bin sbin lib lib64; do
    if [[ -L "/$directory" ]]; then
        box+=(--symlink "$(readlink "/$directory")" "/$directory")
    elif [[ -d "/$directory" ]]; then
        box+=(--ro-bind "/$directory" "/$directory")
    fi
done
for item in /etc/ssl/certs /etc/hosts /etc/resolv.conf /etc/nsswitch.conf /etc/localtime /etc/ld.so.cache; do
    if [[ -e "$item" ]]; then box+=(--ro-bind "$(realpath "$item")" "$item"); fi
done

if [[ "$mode" == build ]]; then
    box+=(--bind "$store/venvs/$venv_id" /venv)
    exec "${box[@]}" /bin/sh -ec '
        /usr/bin/python3 -m venv /venv
        /venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir -r /app/requirements.txt
        /venv/bin/python -m pip check
    '
fi
box+=(--ro-bind "$store/venvs/$venv_id" /venv)
case "$mode" in
    check)
        exec "${box[@]}" --setenv DENNY888_DB_PATH /tmp/test.db /venv/bin/python -c '
import sys
from app import app, init_db
init_db()
with app.test_client() as client:
    if client.get("/healthz").get_json() != {"status": "ok", "commit": sys.argv[1]}:
        raise RuntimeError("candidate health check failed")
    if client.get("/").status_code != 200 or client.get("/api/holdings").get_json() != []:
        raise RuntimeError("candidate portfolio smoke test failed")
' "${3:?expected commit required}"
        ;;
    backup)
        # Stream a consistent snapshot. Only this outer deployment process has access to the backup directory.
        exec "${box[@]}" --bind "$store/data" /data /usr/bin/python3 -I -c '
import shutil, sqlite3, sys
with sqlite3.connect("file:/data/portfolio.db?mode=ro", uri=True) as source, sqlite3.connect("/tmp/snapshot.db") as destination:
    source.backup(destination)
with open("/tmp/snapshot.db", "rb") as snapshot:
    shutil.copyfileobj(snapshot, sys.stdout.buffer)
'
        ;;
    serve)
        box+=(--bind "$store/data" /data --setenv DENNY888_DB_PATH /data/portfolio.db
              --setenv XDG_CACHE_HOME /data/cache --setenv SHEET_ID "${SHEET_ID:-}" --setenv SHEET_GID "${SHEET_GID:-0}")
        exec "${box[@]}" /bin/sh -ec '
            /venv/bin/python -c "from app import init_db; init_db()"
            exec /venv/bin/python -m gunicorn --bind "127.0.0.1:$1" --workers 1 --threads 4 --timeout 300 --access-logfile - app:app
        ' sh "$port"
        ;;
    *) exit 2 ;;
esac
