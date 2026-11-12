---
name: loadgov
description: Use the host load governor before and during heavy work: wait for headroom, run builds in a governed slot, lower or raise CPU limits with a reason, and explain refusals. Use when the user says "fan is too loud", "throttle down", "can I start a build", "go up to 3 implementers until 07:00", "why is the host slow", or before gates, compiles and parallel implementers.
---

# loadgov

Tools: MCP `load_get_status`, `load_set_limits`, `load_wait_headroom`, `load_explain` (plus `load_reap_idle_daemons`, `load_freeze`, `load_unfreeze` when listed), or the `loadctl` CLI. Commands: `references/commands.md`.

## Before heavy work

1. Call `load_wait_headroom` (kind `impl`, `review`, `gate` or `compile`) or `loadctl wait --kind <kind> --project <p>`. Do not start the job until it passes.
2. Run builds, gates and compiles through `loadctl run --kind <kind> --project <p> --label <l> -- <cmd>`. It takes the heavy or compile slot, starts the job in its own scope under the project slice, and sets the job env from the active profile (`CARGO_BUILD_JOBS`, `VITEST_MAX_WORKERS`, `GATE_SHARD_BUDGET`). A bare command bypasses all three.

## Changing limits

- Lower freely: `load_set_limits` with a `reason`. It expires after 2 h by default, 12 h at most.
- Raise only while no human restriction is active, always with a TTL (`ttl` or `until`) and a `reason`. Pass `agent_id` and `project`.
- The user's words set the TTL: "until 07:00" means `until: "07:00 Europe/Berlin"`.

## When the reply says `refused: true`

A human restricted the host. Stop. Tell the user who and why (`restrictedBy`, the reason, and `load_explain` for the ceiling, overrides and expiry). Do not retry, do not split the request into smaller raises, do not work around it, and do not use `--owner`.

## Owner claims

`--owner` is for the human. Use it only when the user explicitly instructs it in this conversation and you run interactively (never inside build-work or a dispatcher loop). It is audited and sent to the user's phone. MCP has no owner tool.

## Never

Freeze, kill or pause other agents' work. Edit the daemon's config or state. Retry a refusal.

## Exit codes

A short timeout does not make `loadctl wait` quick on a calm host: a pass needs two samples `confirm_s` apart and a started confirmation always completes, so even `--timeout 1` blocks for about `confirm_s` (60 s; 30 s for a gate). `loadctl wait`: `0` ok, `1` `HEADROOM_TIMEOUT` (callers exit 75 and try later), `3` daemon not reachable (fall back to the legacy scripts), `5` daemon error. `loadctl set`: `4` refused or owner denied. A `load_wait_headroom` timeout is `result: timeout`: call again, or use `loadctl wait` for longer waits (one call waits at most 1800 s).
