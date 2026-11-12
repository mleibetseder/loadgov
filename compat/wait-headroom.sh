#!/usr/bin/env bash
# Usage: wait-headroom.sh <label>
# Shim for a legacy wait-headroom.sh: same HEADROOM_OK / HEADROOM_WAIT / HEADROOM_TIMEOUT lines and exit
# codes (0 room, 1 timeout). The legacy HEADROOM_* overrides become explicit `loadctl wait` flags (LOAD1, LOAD5,
# PSI, MEM_GB, SWAP_MB, MEM_PSI, CONFIRM, POLL, MAX_WAIT); the rest comes from the daemon config for the
# project in LOADGOV_HEADROOM_PROJECT, else LOADGOV_WAIT_PROJECT (default: default) and the kind in LOADGOV_WAIT_KIND (default: impl).
# Without loadgovd the legacy script runs: LOADGOV_LEGACY_SCRIPT, else
# $LOADGOV_LEGACY_DIR/<project>/wait-headroom.sh. Without one either, the shim exits 3 with a message.
set -u
LEGACY=${LOADGOV_LEGACY_DIR:-/opt/loadgov/legacy}
LOADCTL=${LOADCTL:-loadctl}
SOCK=${LOADGOV_SOCKET:-/run/loadgovd/loadgovd.sock}
PROJECT=${LOADGOV_HEADROOM_PROJECT:-${LOADGOV_WAIT_PROJECT:-default}}
LEGACY_SCRIPT=${LOADGOV_LEGACY_SCRIPT:-$LEGACY/$PROJECT/wait-headroom.sh}
up() {
  [ -S "$SOCK" ] || return 1
  "$LOADCTL" status --json >/dev/null 2>&1
  local rc=$?
  [ "$rc" -ne 3 ] && [ "$rc" -ne 127 ]
}
fallback() {
  [ -x "$LEGACY_SCRIPT" ] && exec "$LEGACY_SCRIPT" "$@"
  echo "wait-headroom: loadgovd is not running and there is no legacy script ($LEGACY_SCRIPT)" >&2
  exit 3
}
up || fallback "$@"
args=(--kind "${LOADGOV_WAIT_KIND:-impl}" --project "$PROJECT" --label "${1:-job}")
[ -n "${HEADROOM_LOAD1:-}" ] && args+=(--load1-max "$HEADROOM_LOAD1")
[ -n "${HEADROOM_LOAD5:-}" ] && args+=(--load5-max "$HEADROOM_LOAD5")
[ -n "${HEADROOM_PSI:-}" ] && args+=(--psi-max "$HEADROOM_PSI")
[ -n "${HEADROOM_MEM_GB:-}" ] && args+=(--mem-min-gb "$HEADROOM_MEM_GB")
[ -n "${HEADROOM_SWAP_MB:-}" ] && args+=(--swap-max-mb "$HEADROOM_SWAP_MB")
[ -n "${HEADROOM_MEM_PSI:-}" ] && args+=(--mem-psi-max "$HEADROOM_MEM_PSI")
[ -n "${HEADROOM_CONFIRM:-}" ] && args+=(--confirm "$HEADROOM_CONFIRM")
[ -n "${HEADROOM_POLL:-}" ] && args+=(--poll "$HEADROOM_POLL")
[ -n "${HEADROOM_MAX_WAIT:-}" ] && args+=(--timeout "$HEADROOM_MAX_WAIT")
"$LOADCTL" wait "${args[@]}"
rc=$?
[ "$rc" -eq 3 ] && fallback "$@"
exit "$rc"
