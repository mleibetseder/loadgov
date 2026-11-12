#!/usr/bin/env bash
# RUSTC_WRAPPER shim for a legacy rustc-slot.sh: a heavy crate compiles through `loadctl run --kind compile`
# (compile slot only, like the legacy script, own scope); any other rustc runs at once.
#   LOADGOV_HEAVY_CRATES   the heavy crate names, separated by spaces or commas (unset or empty: none is heavy)
#   LOADGOV_COMPILE_PROJECT  the loadgov project the jobs run under (default: default)
#   LOADGOV_LEGACY_SCRIPT  the legacy wrapper to run when loadgovd is down (default:
#                          $LOADGOV_LEGACY_DIR/<project>/rustc-slot.sh); without one, rustc runs unwrapped
# Once `loadctl run` has started the job its exit code is the job's, so a rustc that exits 3 is never
# mistaken for "daemon down" and run twice.
set -u
LEGACY=${LOADGOV_LEGACY_DIR:-/opt/loadgov/legacy}
LOADCTL=${LOADCTL:-loadctl}
SOCK=${LOADGOV_SOCKET:-/run/loadgovd/loadgovd.sock}
PROJECT=${LOADGOV_COMPILE_PROJECT:-default}
LEGACY_SCRIPT=${LOADGOV_LEGACY_SCRIPT:-$LEGACY/$PROJECT/rustc-slot.sh}
up() {
  [ -S "$SOCK" ] || return 1
  "$LOADCTL" status --json >/dev/null 2>&1
  local rc=$?
  [ "$rc" -ne 3 ] && [ "$rc" -ne 127 ]
}
rustc=$1; shift
heavy=0
crates=${LOADGOV_HEAVY_CRATES:-}
for name in ${crates//,/ }; do
  case " $* " in *" --crate-name $name "*) heavy=1 ;; esac
done
[ "$heavy" -eq 1 ] || exec "$rustc" "$@"
# RUSTC_SLOT_ROOM_GB re-enables the bypass: enough memory available at the start means no slot.
if [ -n "${RUSTC_SLOT_ROOM_GB:-}" ]; then
  avail=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
  [ "$avail" -ge "$RUSTC_SLOT_ROOM_GB" ] && exec "$rustc" "$@"
fi
if ! up; then
  [ -x "$LEGACY_SCRIPT" ] && exec "$LEGACY_SCRIPT" "$rustc" "$@"
  exec "$rustc" "$@"
fi
crate=compile
prev=
for arg in "$@"; do
  [ "$prev" = --crate-name ] && { crate=$arg; break; }
  prev=$arg
done
exec "$LOADCTL" run --kind compile --project "$PROJECT" --label "$crate" --headroom none -- "$rustc" "$@"
