#!/usr/bin/env bash
# Shim for a legacy host-wait.sh: wait (forever) until the host has room, nothing on stdout. The legacy
# overrides LOAD_MAX, PSI_MAX, MEM_MIN_GB, SWAP_MAX_MB and MEMPSI_MAX become explicit `loadctl wait` flags;
# thresholds you do not set come from the daemon config for the project in LOADGOV_HOSTWAIT_PROJECT, else LOADGOV_WAIT_PROJECT (default:
# default) and the kind in LOADGOV_WAIT_KIND (default: impl).
# Without loadgovd the legacy script runs: LOADGOV_LEGACY_SCRIPT, else
# $LOADGOV_LEGACY_DIR/<project>/host-wait.sh. Without one either, the shim exits 3 with a message.
set -u
LEGACY=${LOADGOV_LEGACY_DIR:-/opt/loadgov/legacy}
LOADCTL=${LOADCTL:-loadctl}
SOCK=${LOADGOV_SOCKET:-/run/loadgovd/loadgovd.sock}
PROJECT=${LOADGOV_HOSTWAIT_PROJECT:-${LOADGOV_WAIT_PROJECT:-default}}
LEGACY_SCRIPT=${LOADGOV_LEGACY_SCRIPT:-$LEGACY/$PROJECT/host-wait.sh}
up() {
  [ -S "$SOCK" ] || return 1
  "$LOADCTL" status --json >/dev/null 2>&1
  local rc=$?
  [ "$rc" -ne 3 ] && [ "$rc" -ne 127 ]
}
fallback() {
  [ -x "$LEGACY_SCRIPT" ] && exec "$LEGACY_SCRIPT" "$@"
  echo "host-wait: loadgovd is not running and there is no legacy script ($LEGACY_SCRIPT)" >&2
  exit 3
}
up || fallback "$@"
args=(--kind "${LOADGOV_WAIT_KIND:-impl}" --project "$PROJECT" --label host-wait --timeout 0)
[ -n "${LOAD_MAX:-}" ] && args+=(--load1-max "$LOAD_MAX")
[ -n "${PSI_MAX:-}" ] && args+=(--psi-max "$PSI_MAX")
[ -n "${MEM_MIN_GB:-}" ] && args+=(--mem-min-gb "$MEM_MIN_GB")
[ -n "${SWAP_MAX_MB:-}" ] && args+=(--swap-max-mb "$SWAP_MAX_MB")
[ -n "${MEMPSI_MAX:-}" ] && args+=(--mem-psi-max "$MEMPSI_MAX")
"$LOADCTL" wait "${args[@]}" 1>&2
rc=$?
[ "$rc" -eq 3 ] && fallback "$@"
exit "$rc"
