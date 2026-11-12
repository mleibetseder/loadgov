#!/usr/bin/env bash
# Shim for the legacy `heavy-slot.sh`. Usage (source it, then call):
#   . heavy-slot.sh; heavy_slot_acquire "<project> <label>"
# With loadgovd up: the shell opens fd 9 on the lock file and `loadctl slot acquire heavy --fd 9 --wait` queues
# for the slot, takes the flock on that descriptor (it lives until this shell exits, like the legacy lock) and
# prints the same HEAVY_SLOT_OK / HEAVY_SLOT_WAIT lines. Without it the legacy function runs.
# Sourced: this file sets no shell options.
HEAVY_SLOT_LOCK=${HEAVY_SLOT_LOCK:-/run/lock/host-heavy-slot.lock}
HEAVY_SLOT_HOLDER=${HEAVY_SLOT_HOLDER:-/run/lock/host-heavy-slot.holder}

__loadgov_up() {
  [ -S "${LOADGOV_SOCKET:-/run/loadgovd/loadgovd.sock}" ] || return 1
  "${LOADCTL:-loadctl}" status --json >/dev/null 2>&1
  local rc=$?
  [ "$rc" -ne 3 ] && [ "$rc" -ne 127 ]
}

__loadgov_legacy_heavy_slot() {
  local src="${LOADGOV_LEGACY_DIR:-/opt/loadgov/legacy}/heavy-slot/heavy-slot.sh" ours rc
  [ -r "$src" ] || { echo "heavy-slot: loadgovd is not running and $src is missing" >&2; return 2; }
  ours=$(declare -f heavy_slot_acquire)
  . "$src"
  heavy_slot_acquire "$1"
  rc=$?
  eval "$ours" # sourcing the legacy file replaced this function: put it back
  return "$rc"
}

heavy_slot_acquire() {
  local who=$1 project label rc
  project=${who%% *}
  label=${who#* }
  [ "$label" = "$who" ] && label=job
  if __loadgov_up; then
    exec 9>>"$HEAVY_SLOT_LOCK"
    "${LOADCTL:-loadctl}" slot acquire heavy --project "$project" --label "$label" --fd 9 --wait --pid $$
    rc=$?
    [ "$rc" -ne 3 ] && return "$rc"
    exec 9>&- # the daemon went away between the check and the call
  fi
  __loadgov_legacy_heavy_slot "$who"
}
