#!/usr/bin/env bash
# Install or update loadgovd from this checkout. Idempotent. Never turns on any enforce flag.
#   sudo packaging/install.sh                  install into /opt/loadgov and (re)start loadgovd.service
#   sudo packaging/install.sh --no-start       install only
#   sudo packaging/install.sh --project NAME   also create build-work-NAME.slice (repeatable)
# One build-work-<project>.slice is generated for every project named in /etc/loadgov/loadgov.toml
# ([projects.*] and [memguard.projects.*]) and for every --project. systemd cannot template slice units
# (a build-work-@.slice fails to load with "Invalid argument"), so the units are written, not shipped.
set -euo pipefail
src=$(cd "$(dirname "$0")/.." && pwd)
prefix=/opt/loadgov
conf=/etc/loadgov/loadgov.toml
start=1
projects=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) start=0 ;;
    --project) shift; projects+=("${1:?install.sh: --project needs a name}") ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 1 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || { echo "install.sh: run as root" >&2; exit 1; }
command -v uv >/dev/null || { echo "install.sh: uv not found" >&2; exit 1; }

install -d -m 0755 "$prefix"
# Pinned interpreter and a non-editable install, independent of the checkout and of apt's python3.
UV_PROJECT_ENVIRONMENT="$prefix/.venv" nice -n 10 uv sync --project "$src" --locked --no-dev --no-editable --managed-python --python 3.14
ln -sf "$prefix/.venv/bin/loadctl" /usr/local/bin/loadctl

# Optional fallback copies for the compat shims (they exec these when the daemon is down). Only when this
# checkout carries legacy scripts; modes preserved, the originals are never touched.
if [ -d "$src/legacy" ]; then
  install -d -m 0755 "$prefix/legacy"
  cp -a "$src/legacy/." "$prefix/legacy/"
fi

# The config: a site-specific file (site/loadgov.toml, private) is installed once, never over an existing one.
install -d -m 0755 /etc/loadgov
if [ -f "$src/site/loadgov.toml" ] && [ ! -e "$conf" ]; then
  install -m 0644 "$src/site/loadgov.toml" "$conf"
  echo "install.sh: installed $src/site/loadgov.toml as $conf"
fi

install -m 0644 "$src/packaging/systemd/loadgovd.service" /etc/systemd/system/loadgovd.service
install -m 0644 "$src/packaging/systemd/build-work.slice" /etc/systemd/system/build-work.slice

# One child slice per configured project; the names come from the same function `loadctl run` uses.
if [ -f "$conf" ]; then
  while IFS= read -r name; do projects+=("$name"); done < <(
    "$prefix/.venv/bin/python" -I - "$conf" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
names = set(cfg.get("projects", {})) | set(cfg.get("memguard", {}).get("projects", {}))
print("\n".join(sorted(names)))
PY
  )
fi
for name in "${projects[@]+"${projects[@]}"}"; do
  unit=$("$prefix/.venv/bin/python" -I -c 'import sys; from loadgov.cli.run import slice_name; print(slice_name(sys.argv[1]))' "$name")
  printf '[Unit]\nDescription=loadgov: build work of %s (child of build-work.slice by name)\n\n[Slice]\nCPUAccounting=yes\nMemoryAccounting=yes\n' \
    "$name" > "/etc/systemd/system/$unit"
  chmod 0644 "/etc/systemd/system/$unit"
done

systemctl daemon-reload
if [ "$start" -eq 1 ]; then
  systemctl enable loadgovd.service
  systemctl restart loadgovd.service
  systemctl --no-pager --lines=5 status loadgovd.service
fi
