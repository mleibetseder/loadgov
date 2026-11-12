# loadgovd protocol (v1)

## Transport

- Unix stream socket `/run/loadgovd/loadgovd.sock`, mode `0600` (root only).
- One request per connection: a single JSON object on one line, answered by a single JSON object on one line.

```json
{"v": 1, "op": "status", "args": {}}
{"ok": true, "status": { … }}
```

Errors have `ok: false` with `error` and `message`:

| `error` | Meaning |
|---|---|
| `bad_request` | The line is not a JSON object, or an argument has the wrong JSON type (`args` not an object, non-string `profile`/`reason`/`ttl`/`until`/`project`, non-number limit, non-boolean `owner`) |
| `bad_version` | `v` is not `1` |
| `unknown_op` | The operation does not exist |
| `invalid_args` | A required argument is missing or a value is out of range, malformed or contradictory (`message` says which) |
| `owner_denied` | `owner: true` was claimed but the caller does not qualify (see Principals), or an owner-only op was called without it |
| `not_ready` | The daemon has not taken its first sample yet |

`loadctl` exit codes: `0` ok, `2` usage, `3` daemon not reachable, `4` owner denied or request refused, `5` daemon returned another error.

## Operations

Nothing is enforced unless the matching `enforce` flag is on (`[slots]`, `[governor]`, `[memguard]`, `[reaper]`, `[backstop] enabled`, all default off): no cgroup writes, signals, container pauses or `/run/lock` locks without it (AGENTS.md rule 2).

| `op` | Args | Reply |
|---|---|---|
| `status` | none | `{"ok": true, "status": {…}}` |
| `set` | `profile`, `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers`, `ram_floor_gb` (at least one), `ttl` or `until` (not both), `reason` (required), `owner` (bool), `project` | accepted: `{"ok": true, "accepted": true, "principal", "profile", "expires_at", "ttl_s", "ttl_capped", "changed": {param: {before, after}}, "effective"}`; refused: see below |
| `ceiling_set` | `profile`, `reason` (required), `ttl` or `until`, `owner: true` | `{"ok": true, "accepted": true, "ceiling", "expires_at", "effective"}` |
| `ceiling_clear` | `reason`, `owner: true` | `{"ok": true, "cleared": bool, "effective"}` |
| `reset` | `params` (list of limit names) or `all: true`, `reason` (required), `owner: true` | `{"ok": true, "accepted": true, "removed": [override ids], "effective"}`. Removes the owner overrides for `params`; with `all` removes every override, owner and agent. The profile and the ceiling are not touched (use `set --profile`, `ceiling_clear`). Owner only: an owner override with no TTL otherwise stays until reset or replaced. `loadctl reset <param...>\|--all --owner --reason …` |
| `limits` | none | `{"ok": true, "limits": {profile, profile_expires_at, effective, overrides[], ceiling, hold, freeze, restriction}}` |
| `slot_acquire` | `slot` (`heavy`/`compile`), `project`, `label`, `pid` (holder process, required), `wait` (bool), `timeout_s`, `client_lock` (bool; required when enforce is on) | `{"ok": true, "status": "granted"\|"queued"\|"busy"\|"timeout", "slot", "who", "holder", "position"?, "queue"?}`; under enforce a `granted` reply also has `lock_path`, `holder_path`, `holder_line`; see Slots |
| `slot_release` | `slot`, `pid` (required) | `{"ok": true, "slot", "released": bool}` |
| `slot_status` | none | `{"ok": true, "slots": {…as in status}, "queues": {slot: [{project, label, pid}]}}` |
| `hold` | `state` (`on`/`off`), `slot` (`heavy`/`compile`/`all`, default `all`), `reason` (required for `on`), `owner: true` | `{"ok": true, "hold": "on"\|"off", "slots": {…}}`; owner only (`owner_denied` otherwise) |
| `headroom` | `kind` (`impl`, `review`, `gate`, `compile`; required), `project` (default `default`), `overrides` (optional object with any of `load1_max`, `load5_max`, `psi_max`, `mem_min_gb`, `swap_max_mb`, `mem_psi_max`: numbers >= 0 that replace the configured value for this call; the profile scaling still applies to them) | `{"ok": true, "kind", "project", "decision": {pass, reasons[], would_block_io, values, paused[], thresholds, factor}, "thresholds", "factor", "timing": {confirm_s, poll_s, max_wait_s}, "sample_at", "sample_at_ms"}`. One decision on the latest sample (see Headroom below). Wrong `kind` or `project` type: `bad_request`; no sample yet or one older than 3 x `tick_s`: `not_ready` ("stale sample"). |
| `job_start` | `kind`, `project`, `label`, `unit` (`loadgov-*.scope`), `cgroup` (absolute cgroup v2 path ending in `/<unit>`), `pid` (the job process), `owner_pid` (the `loadctl run` process; default: the socket peer), `slot` (`heavy`/`compile`, only when `owner_pid` holds it) | `{"ok": true, "unit", "slot", "bound": bool}`; registers the job and binds the slot to the scope |
| `job_end` | `unit` | `{"ok": true, "unit", "ended": bool, "released": bool}`; removes the job and releases its slot |
| `cancel_holder` | `slot` (`heavy`/`compile`), `confirm: true` (required), `reason`, `owner: true` | `{"ok": true, "slot", "signalled": bool, "signal": "SIGTERM", "pid", "start", "source", "holder", "comm"}`; owner only (`owner_denied` otherwise); see Cancel holder |
| `freeze` | `reason` (required), `project`, `ttl` (`90s`, `15m`, `2h`; default none: until `unfreeze`), `owner: true` | `{"ok": true, "frozen": [entry ids], "failed": [what], "would": bool, "expires_at", "project"}`; owner only (`owner_denied` otherwise); see Memory guard |
| `unfreeze` | `project`, `reason`, `owner: true` | `{"ok": true, "thawed": [ids], "flag_cleared": bool, "still_paused": [ids], "restrictedBy"?: "system"}`, or `{"ok": false, "refused": true, "restrictedBy": "system", "reason", "still_paused"}` when only memory-guard pauses remain and memory is below their resume threshold; owner only |
| `reap` | `reason`, `owner` (bool), `pre_gate` (bool) | `{"ok": true, "reaped": [{pid, start, in_scope, result}], "would": bool}`; any caller (it only frees memory), audited with the caller's principal; `pre_gate: true` is `loadctl run --kind gate`'s call and reaps only with enforce on and `MemAvailable` below `[memguard] pre_gate_reap_gb`, else `{"reaped": [], "skipped": "<why>"}` |
| `history` | `n` (default 40, capped at 5760 = one day, so the daemon never builds a huge reply) | `{"ok": true, "columns": [...], "rows": [[...]]}`: the newest `n` rows of the sample history, oldest first, every cell a string exactly as in `history.csv` (see Sample history) |
| `log` | `n` (default 20, max 1000) | `{"ok": true, "lines": [audit entries, oldest first]}` |

Ranges: `max_cores` 1.0-6.0 step 0.5, `target_cpu_pct` 35-90 step 5, `load_max` 1.0-6.0 step 0.5, `max_implementers` 0-4, `ram_floor_gb` 2.0-6.0 step 0.5. `ttl` is `90s`, `15m` or `2h`; `until` is `HH:MM Zone` (`07:00 Europe/Berlin`), the next such time (tomorrow if today's has passed), resolved with the zone's DST rules; without a zone the host's zone is used.

### Headroom

`headroom` answers "is there room for one more heavy job?" on the daemon's latest sample (at most one tick old); `loadctl wait` asks twice, `confirm_s` apart. All comparisons are strict, as in `wait-headroom.sh`. A decision passes when all of these hold: `load1 < load1_max`; `load5 < load5_max` (only when configured); cpu PSI `some avg60 < psi_max`; `MemAvailable/1024 >= mem_min_gb`; swap used `<= swap_max_mb` unless `MemAvailable >= 2 x mem_min_gb`; memory PSI `some avg60 < mem_psi_max`; not paused; and, only when `[headroom] io_psi_gate = true`, io PSI `some avg60 < io_psi_max` (25). With the gate off `would_block_io` still reports whether io PSI is over its limit.

*Not paused*: no non-empty pause marker file (`[memguard] pause_marker`, `<lock_root>/<file>`; host-wide, any project; none is configured by default); no owner `freeze` (all kinds); no owner `hold` (kinds `gate` and `compile`). `paused[]` names which.

*Thresholds* are `[headroom]` defaults, then `[headroom.kinds.<kind>]`, then `[projects.<name>.kinds.<kind>]` (see `packaging/config/loadgov.toml.example`). *Profile influence*: load thresholds are multiplied by `effective.load_max / default_profile.load_max` and `mem_min_gb` is raised by `max(0, effective.ram_floor_gb - default_profile.ram_floor_gb)`; `factor` reports `{load, mem_add_gb}` and `thresholds` the applied values. PSI and swap limits do not change with the profile.

`loadctl wait --kind K [--project P] [--label L] [--timeout S] [--confirm S] [--poll S]` prints `HEADROOM_WAIT <label> <state> waited=Ns` to stderr (at most every 300 s), then `HEADROOM_OK <label> <state> waited=Ns` to stdout and exits 0, or `HEADROOM_TIMEOUT <label> <state> after Ns: not started` to stderr and exits 1. Daemon unreachable: exit 3; negative `--timeout`/`--confirm`/`--poll`: exit 2. The confirming query must come from a newer daemon sample (`sample_at`) than the first pass, so one sample can never satisfy both (compared at millisecond resolution through `sample_at_ms` when the daemon sends it). A `not_ready` answer (daemon starting or restarting, stale sample) is retried inside the wait budget (120 s when no `--timeout` and no reply has supplied one yet) instead of failing with exit 5. After a confirmation sample that fails, the wait looks again at once, like `host-wait.sh`, and only then sleeps a poll. `max_wait_s = 0` (or `--timeout 0`) waits forever; a project may set `poll_s = 120`, `max_wait_s = 0` to wait like a legacy `host-wait.sh` (the defaults are `poll_s = 30`, `max_wait_s = 7200`). A `load1_max`/`load5_max` expression that resolves to <= 0 on the host (e.g. `nproc-2` on 2 cores) makes every decision fail with a `misconfigured:` reason; expressions are validated by shape at config load.

### Jobs and `loadctl run`

`loadctl run --kind K --project P --label L [--headroom full|level|none] [--floor-mb N --on-floor kill] [--no-wait] -- cmd...` runs `cmd` in its own transient scope and stays its parent (exit code passed through; SIGINT and SIGTERM are forwarded to the scope with `systemctl kill`). Steps, in order:

1. `gate` takes the `heavy` slot and `compile` the `compile` slot (`impl` and `review` none), waiting in the FIFO queue (slot first, then the headroom wait).
2. `--headroom full` (default): headroom wait for kind `K` and project `P` while holding the slot: two passing samples, `confirm_s` apart (60 s; 30 s for a `gate` that holds the slot), exactly like `loadctl wait`. `HEAVY_SLOT_*` and `HEADROOM_*` lines go to stderr so the job's stdout stays clean. Exit 1 when the wait times out. `--headroom level` waits (forever, polling every 15 s) until the hwmon level is `OK` on 2 samples (`BUSY` keeps waiting), as legacy `guarded` did (`grep -q 'level OK'`); `loadctl level-wait` and `hwmon wait` stay below `LIMIT`, like legacy `hwmon wait`; `--headroom none` takes the slot only, as legacy `rustc-slot.sh` did.
3. Launch (the job inherits the caller's descriptors like the legacy `exec` did, so cargo's jobserver pipes reach rustc; loadctl's own descriptors are close-on-exec; SIGINT and SIGTERM handlers are installed before the launcher starts and a signal that arrives before the scope exists is sent again once it does): `systemd-run --scope --quiet --collect --slice=build-work-<P>.slice --unit=loadgov-<K>-<label>-<UTC time>-<pid> -- nice -n 10 ionice -c 2 -n 7 cmd...`. A `gate` or `compile` job uses `ionice -c 3` instead while a `rustc` runs (as the legacy gate wrapper did). The slice name replaces characters other than letters, digits, `_` and `.` in `P` with `-`.
4. Environment of the job: the caller's, plus `CARGO_BUILD_JOBS` (effective `cargo_build_jobs`; a `gate` gets 3 only when `GATE_JOBS=3`, at least 8 GB are available and the effective value is not 1), `VITEST_MAX_WORKERS`, `GATE_SHARD_BUDGET` (both effective), `GATE_SHARD_THREADS` (the caller's, else 2) and `LOADGOV_JOB=<unit>.scope`.
5. `job_start` registers the job (listed in the status `jobs` section); `job_end` after it exits. If `job_end` never arrives, the job is dropped and its slot released when the scope's `cgroup.procs` is empty. A scope whose `cgroup.procs` cannot be read (not merely missing) counts as alive: the slot stays held and the file is read again every second. The job runs in its own session (`start_new_session`, like legacy `guarded`'s `setsid`), so a terminal Ctrl-C reaches only `loadctl run`, which forwards one signal through the scope.

For `gate` and `compile`, in either `enforce` mode, `loadctl run` opens the lock file itself without `O_CLOEXEC`, takes `flock` after the grant, writes the holder line and the job inherits the descriptor, so the lock lives as long as `loadctl run` or the job (or any process it leaves behind) lives, never only in the daemon. `job_end` frees the slot only when the scope is empty; otherwise the daemon frees it when `cgroup.procs` empties.

`--floor-mb N --on-floor kill` (used by the `guarded` shim): every 2 s `loadctl` reads `MemAvailable` (daemon status; `/proc/meminfo` when the daemon is down); below `N` MB it runs `systemctl kill --signal=SIGKILL <scope>` and `pkill -f` on `GradleDaemon`/`KotlinCompileDaemon` JVMs (nothing else), and exits 137.

Exit codes: the job's own (128+n when a signal n ended it), 137 floor kill, 1 headroom wait timed out, 2 usage, 3 daemon not reachable (nothing started), 5 daemon error, 75 `--no-wait` and the slot is busy or the host has no room, 127 the launcher could not be started. A hidden `--slice` flag (tests) replaces the slice name.

systemd nests a slice under every dash-separated prefix of its name, so `build-work-web-app.slice` lives at `/build.slice/build-work.slice/build-work-web-app.slice` in the cgroup tree (`systemd-cgls`), and `build-work-web-app-ci.slice` below the implicit `build-work-web.slice` and `build-work-web-app.slice`. The daemon takes the real path from `/proc/<job pid>/cgroup`. A project without its own slice unit gets an implicit slice without accounting properties; `packaging/install.sh` writes `build-work-<project>.slice` for every project named in the config (`[projects.*]`, `[memguard.projects.*]`) and for each `--project`. systemd cannot template slice units (`build-work-@.slice` fails to load with `Invalid argument`), so the units are generated, not shipped.

`loadctl level-wait [max_s] [--poll S]` is `hwmon wait`: blocks until the hwmon level is below `LIMIT` on 2 samples (default max 1800 s, polling every 15 s), prints the five hwmon lines, exit 0; at the limit it prints `hwmon wait: still at LIMIT after N s` and exits 1.

`loadctl wait` takes `--load1-max --load5-max --psi-max --mem-min-gb --swap-max-mb --mem-psi-max N`: each replaces the configured threshold for that call only; the profile scaling (load thresholds times the profile factor, `mem_min_gb` plus the RAM floor difference) is applied to the replacement as it is to a configured value.

### Compat shims (`compat/`)

Bash scripts that keep the legacy call sites working. Each first checks that the socket exists and that `loadctl status --json` does not exit 3; otherwise it `exec`s the legacy script from `${LOADGOV_LEGACY_DIR:-/opt/loadgov/legacy}` with the same arguments and environment. A job started by `loadctl run` is never re-run by a fallback: its exit code (3 included) is the job's.

| Shim | Behaviour |
|---|---|
| `rustc-slot.sh` | `RUSTC_WRAPPER`: crates not named in `LOADGOV_HEAVY_CRATES` (space or comma separated; unset means none) run at once; the named ones go through `loadctl run --kind compile --project $LOADGOV_COMPILE_PROJECT --label <crate> --headroom none` (slot only, like a legacy wrapper; the project defaults to `default`). Without loadgovd the legacy wrapper `$LOADGOV_LEGACY_SCRIPT` (default `$LOADGOV_LEGACY_DIR/<project>/rustc-slot.sh`) runs if it exists, else rustc runs unwrapped. `RUSTC_SLOT_ROOM_GB` bypasses the slot when that much memory is available at the start (the legacy script re-checks while waiting) |
| `guarded` | `loadctl run --kind compile --project <basename of $PWD> --label guarded --headroom level --floor-mb 2560 --on-floor kill`; exit 137 kept. `--headroom level` is the legacy wait for the hwmon level |
| `hwmon` | `status` = `loadctl status --hwmon`; `gate` = same, exit 1 at `LIMIT`; `wait [max_s]` = `loadctl level-wait`; `history [n]` = `loadctl history n --csv` (the daemon's own columns, see "Sample history"); `watch` and the rest: legacy |
| `host-wait.sh` | `loadctl wait --kind impl --project $LOADGOV_HOSTWAIT_PROJECT --timeout 0` (project: that variable, else `LOADGOV_WAIT_PROJECT`, else `default`) on stderr, nothing on stdout. `LOAD_MAX`, `PSI_MAX`, `MEM_MIN_GB`, `SWAP_MAX_MB`, `MEMPSI_MAX` become `--load1-max`, `--psi-max`, `--mem-min-gb`, `--swap-max-mb`, `--mem-psi-max`; unset ones come from the config, not from the legacy defaults. `LOADGOV_WAIT_KIND`/`LOADGOV_WAIT_PROJECT` change kind and project. Without loadgovd the legacy script runs (`LOADGOV_LEGACY_SCRIPT`, default `$LOADGOV_LEGACY_DIR/<project>/host-wait.sh`); with none the shim exits 3 |
| `wait-headroom.sh <label>` | `loadctl wait --kind impl --project $LOADGOV_HEADROOM_PROJECT --label <label>` (project: that variable, else `LOADGOV_WAIT_PROJECT`, else `default`) (the same fallback rules as `host-wait.sh`): same `HEADROOM_*` lines and exit codes. `HEADROOM_LOAD1`, `LOAD5`, `PSI`, `MEM_GB`, `SWAP_MB`, `MEM_PSI`, `CONFIRM`, `POLL`, `MAX_WAIT` become `--load1-max`, `--load5-max`, `--psi-max`, `--mem-min-gb`, `--swap-max-mb`, `--mem-psi-max`, `--confirm`, `--poll`, `--timeout` |
| `heavy-slot.sh` (sourced) | `heavy_slot_acquire "<project> <label>"` opens fd 9 on `$HEAVY_SLOT_LOCK` and runs `loadctl slot acquire heavy --project P --label L --fd 9 --wait --pid $$`: same `HEAVY_SLOT_OK`/`HEAVY_SLOT_WAIT` lines, the lock lives until the shell exits |

### Slices

`packaging/systemd/build-work.slice` (`CPUAccounting`, `MemoryAccounting`, `CPUQuota=350%`, the Balanced cap that survives a dead daemon or a `daemon-reload`) and one accounting-only child `build-work-<project>.slice` per configured project, generated by `packaging/install.sh` into `/etc/systemd/system`.

### Principals and priority

Priority is system > human > agent. `system` is the daemon's own safety guards (the memory guard's pauses and aborts, the orphan reaper; see Memory guard). A request is **owner** only when it carries `owner: true` **and** the caller passes the default-deny check below; every other request is **agent**. `ceiling_set` and `ceiling_clear` always require the owner claim.

The daemon reads the caller's pid from `SO_PEERCRED` and denies the claim (`owner_denied`, `loadctl` exit 4) when the caller's cgroup (`0::` line of `/proc/<pid>/cgroup`) is under a `build-work` slice (`/build-work.slice…` or a path containing `build-work-`) or starts with one of `[principal] deny_cgroups`, or is unreadable, or when any ancestor's command line matches one of the `[principal] ancestor_deny` regexes (both lists are empty by default; use them for your own dispatchers and loops). These cgroup and ancestor checks, like any token, are audit-grade evidence, not a security boundary: the socket is root-only and a root caller can forge its ancestry.

Rules, all audited:

- Anyone may **tighten** any parameter at any time. Tighter is per parameter: *lower* for `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers` (and `cargo_build_jobs`, `vitest_max_workers`, `gate_shard_budget`), but *higher* for `ram_floor_gb`, because a higher floor makes the memory guard act earlier. **Loosening** is the opposite direction.
- A **human restriction** is active while any of these holds: an owner ceiling is set; an owner hold flag is set (`hold on`, see Slots) or a freeze flag (`freeze`, see Memory guard); the active profile was chosen by the owner and is tighter than the configured default profile (`balanced`) in at least one parameter, i.e. `silent`, `drain` or a tighter custom profile (until it expires or the owner switches to the default or a looser profile; switching to `balanced` or `boost` is not a restriction); an owner override tightened the value effective just before it was set, or is tighter than the active profile (until it expires, is replaced or `reset`). The *owner-only baseline* is the profile, plus owner overrides, capped by the ceiling; it ignores agent overrides.
- An agent **loosening** while a human restriction is active is refused, not clamped; nothing changes: `{"ok": false, "refused": true, "restrictedBy": "owner", "reason": "<the owner's reason>", "ceiling": "<profile or null>"}` (`loadctl` exit 4).
- Agent loosenings and tightenings expire and then reset: default `ttl` 2 h, maximum 12 h (a longer request is capped and the reply has `"ttl_capped": true`). Owner changes are indefinite unless `ttl` or `until` is given. Expiry restores the value that was effective before the override.
- Whenever an owner change leaves a restriction active, on every owner profile switch, and on every expiry that leaves a restriction active or restores a profile, every agent override looser than the owner-only baseline is cancelled at once (audit result `cancelled`), judged from the current state. An agent request is a loosening when it is looser than the owner-only baseline. Expiry of an owner restriction never brings a cancelled agent override back.
- An owner **ceiling** (a profile name) caps `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers`, `cargo_build_jobs`, `vitest_max_workers` and `gate_shard_budget` at no more than that profile's values, and keeps `ram_floor_gb` at no less than it. `load_hold` is its own key and is never derived from `load_max`.

### Slots (heavy and compile)

Two slots arbitrate the legacy flock protocol: `heavy` (`/run/lock/host-heavy-slot.lock`, holder file `host-heavy-slot.holder`) and `compile` (`/run/lock/host-compile-slot.lock`, holder file `host-compile-slot.holder`; the lock name is `[slots] compile_lock` or the daemon's `LOADGOV_COMPILE_LOCK`, a plain `*.lock` file name, and the holder file is the same name with `.holder`). The holder line is the legacy format `<project> <label> pid=<holder pid> since=<UTC>`.

- **Non-blocking ops.** `slot_acquire` never blocks. With `wait: true` a client that gets `queued` calls `slot_acquire` again (every ~2 s, `loadctl` does this) until `granted`; the daemon matches the repeat by `(pid, project, label)`. FIFO: a free slot goes to the queue head, never to a later caller. A waiter that has not polled for 60 s, whose pid is gone or whose `timeout_s` has passed is dropped. `wait: false` answers `granted` or `busy`.
- **Lifetime.** A granted slot is bound to the holder `pid` (default in `loadctl`: its parent shell). It stays held while that process is alive (the pid and its start time, field 22 of `/proc/<pid>/stat`, are recorded at the grant and saved, so a reused pid counts as exited); it is released by `slot_release` or on the first tick after the pid is gone (audit `released: holder exited`). A dropped socket connection does not release it. `SO_PEERCRED` only identifies the requester in the audit log. `loadctl run` re-binds the slot to its job scope with `job_start` (see Jobs): from then on the slot stays held while the scope's `cgroup.procs` is non-empty, even when `loadctl run` itself has died, and is released within a second (the daemon checks slot holders and scopes every 1 s) after the scope empties. A holder whose `loadctl run` is gone while its scope still runs raises one audit event `slot_holder_orphaned`.
- **Legacy holders.** If the real lock is held by a process loadgov did not grant (visible in `/proc/locks`, or a holder line whose pid is alive with the lock file open, or a failed `flock` under enforce), the slot is `held` with `source: legacy` and acquisition waits. A legacy lock is never broken.
- **Client locks always (either mode).** A client that holds the real lock itself replaces a legacy script that always took it: `slot_acquire` with `client_lock: true` (`loadctl slot acquire --fd N`, `loadctl run --kind gate|compile`, the `heavy-slot.sh` and `rustc-slot.sh` shims) gets `lock_path`, `holder_path` and `holder_line` in the `granted` reply whatever `enforce` says; the daemon probes the real lock before granting and the client takes the `flock` and writes the holder line, so a legacy `flock` user is excluded by loadgov clients and the other way round. Without `client_lock` and with enforce off the slot is arbitration-only (`WOULD` events). The CLI therefore refuses `loadctl slot acquire` (exit 2) without `--fd N` unless `--no-lock` is given; with `--no-lock` the line reads `HEAVY_SLOT_OK … (no lock: arbitration only)`.
- **Enforcement** (`[slots] enforce = false` in the config, default off). It governs what the daemon does by itself: the real lock of owner and drain holds and signals (`cancel_holder`). Off (for holds, and for clients without a client lock): the daemon arbitrates only among its own clients, never opens the lock files for locking, never writes holder files, and logs `WOULD flock <path>; WOULD write <holder file>: <line>` audit events (`slot_lock`, result `would`). On: the lock of a client holder lives with the client, like the legacy protocol (the gate shell holds fd 9), so a daemon crash or restart never frees a lock whose gate is still running. The shell opens the lock file (`exec 9>>/run/lock/host-heavy-slot.lock`) and calls `loadctl slot acquire heavy --project P --label L --fd 9`; the daemon probes that the real lock is free (taken and dropped at once) and grants; `loadctl` verifies that fd 9 is open on the slot's lock file (device and inode), takes `flock(9, LOCK_EX|LOCK_NB)` on that inherited descriptor (the shell's own open file description, so the lock outlives `loadctl` and the daemon) and writes the holder line. If a legacy process won the race, `loadctl` gives the slot back and keeps waiting. `slot_acquire` without `client_lock` is refused (`invalid_args`: enforce mode requires --fd N or loadctl run). `loadctl slot release --fd 9` unlocks the descriptor only when the daemon answers `released: true`; after lost bookkeeping (a restart that dropped the holder, a release refused as `owner_denied`) the shell keeps the lock until it exits, which is the safe direction. Only the holder (its pid, or the scope / `loadctl run` parent it was granted for) or an owner may release; anyone else gets `owner_denied` (audited as refused; `loadctl slot release --owner` is the owner claim). Releasing a pid that holds nothing stays a harmless `released: false`. The daemon keeps no flock for client holders; it flocks only for its own holds (owner hold, drain), which a daemon death releases (the Home Assistant side alerts on daemon down). The holder file is left in place like the legacy scripts do. With enforce off the `compile` slot sees legacy holders only through `/proc/locks` (the legacy compile script writes no holder line).
- **Restart.** Holders and holds are saved in `state.json` (`slots`). On start a holder whose process (pid and start time) is alive is re-adopted: only the bookkeeping is re-attached, the client's lock is untouched (audit `adopted`); a dead one is dropped (`released`). The queue is not saved: waiting clients re-poll.
- **Hold.** `hold on` (owner only, no TTL) marks the slots to be held: the daemon takes each as holder `home-assistant hold` as soon as it is free (never preempting the current holder, and ahead of the queue), no new grants are made, and the limits `hold` flag is set (a human restriction, so agent loosenings are refused). `hold off` releases the owner holds and clears the flag. `held_since` of a slot held by a hold is the time the daemon took it (the Home Assistant side can remind every 45 min from it). The drain profile (`hold_heavy_slot = true`) holds the `heavy` slot the same way while it is in force, with origin `profile`: it does not set the `hold` flag, `hold off` does not clear it, and leaving drain releases it.

### Daemon lifecycle

`loadgovd` runs as `Type=notify` with `WatchdogSec=60`. It sends `WATCHDOG=1` only after a tick that completed without an exception. A persistently failing tick therefore stops the keep-alive and systemd restarts the daemon after about 60 s, by design (a restart re-adopts holders and holds from `state.json`); a single failing tick is logged and the loop carries on.

### Files written by the daemon

| Path | Content |
|---|---|
| `/var/lib/loadgov/history.csv` | The 15 s sample ring, about 7 days (see Sample history) |
| `/var/lib/loadgov/backstop.json` | Marker that the backstop wrote limits to a slice, so a later rollback can write `max` back (see Memory backstop); only exists between the first limit write and the rollback |
| `/var/lib/loadgov/state.json` (`--state-dir`) | Profile, overrides, ceiling, hold/freeze, slot holders (`slots`), running `loadctl run` jobs (`jobs`), the paused registry (`memguard`); atomic, restored on start, expired entries dropped on start and every tick |
| `/var/log/loadgov/audit.jsonl` (`--log-dir`) | One JSON line per decision: `ts`, `op`, `principal`, `pid`, `cgroup`, `cmdline` (200 chars), `project`, `reason`, `request`, `result` (`accepted`, `refused`, `expired`, `cancelled`), `before`, `after` (effective limits) |
| `/run/lock/load-governor.env` (`--env-file`, `''` disables) | `KEY=value` lines for shell `source`, rewritten atomically whenever the effective limits change: `MAX_CORES TARGET_PCT LOAD_MAX LOAD_HOLD IMPL_MAX IMPL_MAX_QUIET MEM_MIN_GB LOW_GB CARGO_BUILD_JOBS VITEST_MAX_WORKERS GATE_SHARD_BUDGET PROFILE OWNER_CEILING UPDATED_AT` |

Static profiles come from `/etc/loadgov/loadgov.toml` (`--config`; a missing file means the built-in defaults), see `packaging/config/loadgov.toml.example`.

## REST

An optional HTTP/1.1 listener for Home Assistant, off unless `[rest] enabled = true`. Stdlib `asyncio`, one request per connection (`Connection: close`), JSON bodies only. Limits: header block 8 KiB and body 16 KiB (`413`), malformed request `400`, 10 s read timeout (`408`).

Every REST caller is the **owner** with `source=rest`; requests go through the same code as the socket ops, so the restriction and cancellation rules above apply unchanged. Audit lines carry `principal=owner`, `source=rest` and `peer` (the caller's address; `pid`, `cgroup` and `cmdline` are null).

| Method and path | Body | Same as |
|---|---|---|
| `GET /v1/status` | none | socket `status` (the same `{"ok": true, "status": {…}}` reply) |
| `POST /v1/set` | `set` args without `owner`: `profile`, the five limits, `ttl` or `until`, `reason` (optional here), `project` | `set` as owner |
| `POST /v1/ceiling` | `{"profile", "reason", "ttl"/"until"}` or `{"clear": true, "reason"?}` | `ceiling_set` / `ceiling_clear` |
| `POST /v1/reset` | `params` or `all: true`, `reason` | `reset` |
| `POST /v1/cancel-holder` | `{"slot": "heavy"\|"compile", "confirm": true, "reason"?}` | `cancel_holder` as owner; default reason `REST cancel-holder from <peer>` |
| `POST /v1/action` | `{"action": "profile=silent", "reason"?}` (any profile name) | `set` with that profile as owner; default reason `REST action <action>` |
| `POST /v1/freeze` | `{"project"?, "ttl"?, "reason"?}` | `freeze` as owner; default reason `REST freeze from <peer>`; no owner-change notification |
| `POST /v1/unfreeze` | `{"project"?, "reason"?}` | `unfreeze` as owner; a memory-guard pause below its resume threshold is refused (`409`, `restrictedBy: "system"`) |
| `POST /v1/reap` | `{"reason"?}` | `reap` as owner (audit `source=rest`) |
| `POST /v1/hold` | `{"on": bool, "slot": "heavy"\|"compile"\|"all" (default all), "reason"?}` | `hold` as owner; default reason (on) `REST hold from <peer>`; no owner-change notification |

`reason` is optional on `set`, `ceiling` and `reset` over REST (the socket still requires it); when omitted the audit reason is `REST <set|ceiling|reset> from <peer>`. Replies are the socket replies as JSON. HTTP status: `200` ok, `400` bad JSON, wrong types, unknown fields or `invalid_args`, `409` refused (`refused: true`), `503` `not_ready`, `500` internal. Unknown path `404`, wrong method `405`.

At most `max_connections` connections are served at once, and one source address may hold at most half of them (at least one); any further connection is closed immediately without reading. The header block must arrive within 5 s and the whole request within 10 s (`408`). `Content-Length` must be ASCII digits. Checks, in this order: source address, then the token, then the rate limit.

- **Own addresses:** a source that is loopback or one of the host's own addresses (`/proc/net/fib_trie` `/32 host LOCAL` entries and `/proc/net/if_inet6`, plus the configured `bind`) gets `403`, even with a valid token. If `fib_trie` cannot be read, every request gets `403` and `rest.error` says so (fail closed); a missing `if_inet6` (no IPv6) is accepted.
- **Allowlist:** when `[rest] allow` is non-empty, any other source gets `403`. Empty means the token is the only check.
- **Token:** `Authorization: Bearer <token>`, compared in constant time; missing or wrong is `401` with `WWW-Authenticate: Bearer`. The token is never logged or put in the status.
- **Rate limit:** `rate_per_s` (default 5) requests per second over all callers, burst of one second; more is `429`.

Config `[rest]` (`packaging/config/loadgov.toml.example`): `enabled` (false), `bind` (no default, **required when `enabled = true`**: the host's own LAN address; `0.0.0.0`, `::` and loopback addresses are refused at config load, and so is any address in `172.16.0.0/12`, where docker bridges, `br-*` and `veth*` interfaces get theirs, unless `allow_private_bridge = true`: reading the interface table at start would be racy and docker can add a bridge later, so the range is the robust check), `allow_private_bridge` (false), `port` (7420), `allow` (`[]`), `rate_per_s` (5), `max_connections` (16, range 1-64; one source holds at most half). The token is `LOADGOV_REST_TOKEN=…` in `/etc/loadgov/tokens.env` (`--tokens-file`; template `packaging/config/tokens.env.example`). Generate it with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`; at least 43 characters (32 random bytes). The file must be mode 0600 (group/other access is refused).

**Supervision:** the listener is a separate supervised task. A bind failure (address not assigned yet, port in use), a missing, short or too-open token file never stops the daemon or its engines: `rest.state` is `down` with `rest.error`, and the listener retries with backoff (2 s doubling to 60 s), re-reading the token file each time, until `rest.state` is `up`. With `enabled = false` the state is `disabled` and nothing listens.

## Alerts

The alert engine (`loadgov.engines.watch`) evaluates the host every 30 s (every 6th tick) and keeps the result in the `alerts` status section. It only observes and notifies; it changes no limit and signals nothing.

| Key | Condition | Level | `actions` |
|---|---|---|---|
| `cpu` | busy % averaged over the 30 s interval is above the effective `target_cpu_pct` + 5 for 3 evaluations in a row (legacy: > 70 with target 65) | `warning` | `profile=silent`, `freeze` |
| `memory` | `MemAvailable` < 3000 MB | `critical` | |
| `swap` | swap used grew more than 768 MB over the calm baseline while `MemAvailable` < 6000 MB (`critical` below 3000 MB). The baseline is the lowest swap seen while `MemAvailable` >= 6000 MB: it only moves down until 20 consecutive calm evaluations (10 minutes, the legacy guard's 120 samples at 5 s) have passed; from then on it follows the current swap while the calm lasts | `warning` | |
| `stuck_holder:heavy`, `stuck_holder:compile` | a `loadgov` or legacy holder (not an owner hold) has held the slot for more than 45 min | `warning` | `cancel_holder` |
| `disk` | free space below 25 GB (BUSY) / below 10 GB (LIMIT) | `warning` / `critical` | `prune_caches` |
| `pause`, `abort`, `low_memory`, `resume`, `reap`, `stuck_paused` | raised by the memory guard through `Daemon.emit_event(kind, payload)` (enforce mode only; the shadow writes audit lines, not pushes). `resume` (info) also covers `RELEASED`, `reap` (warning) the JVM reaper, `stuck_paused` (critical, once per entry) an orphaned entry whose tree could not be thawed after 10 rounds (the entry is kept and thawed again every 60 s). Not deduped (the guard rate-limits itself). `payload` may carry `title`, `message`, `actions`, `top3`, `avail_mb`, `swap_mb` | `warning` (`pause`), `critical` | `unfreeze` for `pause` (label "Resume paused workers") |

Dedupe: each key alerts at most once every 10 min while its condition holds; a rise in level (`warning` to `critical`) alerts at once. Only a level above the highest one already sent in the window alerts early, so level flapping cannot bypass the dedupe. A condition must stay clear for 2 evaluations (60 s) before one `resolved` event follows (only if an alert was sent for it); after a `resolved`, a returning condition alerts at once. The Home Assistant side raises the daemon-down alert and the 45-min hold reminders.

Every event is a JSON object. Alert: `type` (`alert`), `key`, `level`, `title`, `message`, `top3` (list of `{pid, comm, pcpu, rss_mb}`), `avail_mb`, `swap_mb`, `profile`, `ceiling`, `actions` (ids; Home Assistant maps them to buttons), `at`. Resolved: `type` (`resolved`), `key`, `title`, `at`. Owner change (`type` `info`, `level` `info`, `key` `owner_change`): `title`, `message`, `who` (`pid`, `cmdline`, `cgroup`), `what` (op), `reason`, `at`. `top3` in alerts is the **interval** CPU share (utime+stime used since the previous 30 s evaluation, so a fresh `rustc` is named); the first evaluation after start falls back to the status `top` (lifetime share). `alerts.events` holds the last 20 events.

**Owner-change notification.** Every accepted owner-level change whose source is not REST (`loadctl --owner` on the host: `set`, `ceiling_set`, `ceiling_clear`, `reset`, `hold`, `cancel_holder`) emits an `owner_change` event naming the caller's pid, command line and cgroup, so an agent-run owner claim shows up on the phone. REST changes (Home Assistant itself) emit none.

### Home Assistant push

`[ha] enabled = false` by default. When enabled, every event is POSTed as JSON (the event plus `"v": 1`) to `<url>/api/webhook/<id>` by one background task: a bounded queue of 64 (the oldest is dropped and counted in `ha_push.dropped`), up to 3 retries after the first attempt with a 1, 4 and 15 s backoff, 5 s timeout per attempt, `2xx` is success. The tick only appends to the queue and never waits for the network. A missing, malformed or group/other-readable webhook id is not retried (`ha_push.state` is `down`, `last_error` says why). The id is `LOADGOV_HA_WEBHOOK_ID=…` in `/etc/loadgov/tokens.env` (mode 0600), never in the TOML; it is read at each delivery and never appears in the status, the audit log or a log line. `url` must be `http://` or `https://` with a host and no credentials, query or fragment; the connection uses no proxy and follows no redirect.

### Legacy alert log

`[compat] alert_log_paths = []` (absolute paths). For each listed file the daemon appends the lines the legacy host watcher wrote, with its own conditions and 10-min dedupe per condition: `ALERT cpu|memory|swap <HH:MM:SS UTC> load=<l> cpu_some10=<p> mem_avail_mb=<n> swap_used_mb=<n> busy=<n>% top: <name>(<pcpu>%,<rss>MB) …` (cpu: busy > 70 % for 3 evaluations; memory: < 3000 MB; swap: > 2048 MB used while < 6000 MB are available). `top` is the interval top 3. The file keeps living after the legacy watcher stops; an unwritable path is logged and skipped.

### Cancel holder

When the holder is a `loadctl run` gate bound to its scope and that `loadctl run` process is gone, the target is the scope: with enforce on the daemon runs `systemctl kill --signal=SIGTERM <unit>` for that one unit (never anything else), provided a process of the scope still holds the lock file; the reply carries `unit`. A live `loadctl run` is signalled as before and forwards the signal to its scope.

`cancel_holder` (socket, owner, default-deny) and `POST /v1/cancel-holder` (REST) send `SIGTERM` to the process that holds `slot`, after `confirm: true`. The target is the holder the slot engine reports: a `loadgov` job holder (pid and start time must still match) or a legacy holder (its pid must still have the lock file open). It is refused, with `invalid_args` and an audit line, when the slot is free or held by an owner hold, when the holder is gone or the pid was reused, and for pid 1, the daemon and a process named `claude`. The signal goes through a pidfd opened before the start-time re-check. Like every signal it needs the enforce flag: with `[slots] enforce = false` nothing is signalled, `signalled` is `false` and the audit result is `would`. The reply names what was signalled; the audit line has `before` (holder line) and `after` (`SIGTERM pid N`).

## CPU governor

Port of the legacy CPU governor script, steering the `cpu.max` of `build.slice/build-work.slice` (`/sys/fs/cgroup/build.slice/build-work.slice`, ADR-0001 "Slice naming"). `[governor] enforce = false` by default: then nothing is written to any cgroup file (AGENTS.md rule 2) and the governor runs as a shadow.

- **Sample.** Every 10 s (monotonic clock, so a wall-clock step cannot stall decisions) the host busy share since the last decision (integer %, from `/proc/stat` jiffies accumulated over the two 5 s daemon ticks; idle = idle + iowait). The total is the sum of every field of the `cpu` line, as the legacy governor sums fields 2..NF, so `guest` and `guest_nice` are in it (the kernel already counts them inside `user`/`nice`; matching the script keeps the shadow comparison exact).
- **Steps.** `TARGET` is the effective `target_cpu_pct`, `MAX` the effective `max_cores`, `MIN` is 1.0. One sample above `TARGET` lowers the cap by 0.5 core (not below `MIN`). Nine consecutive samples below `TARGET - 10` raise it by 0.5 (not above `MAX`). Any other sample resets both counters. `cpu.max` is written as `<cores x 100000> 100000`; the daemon never writes `max`.
- **Start.** The cap starts at the minimum of the cap already in `build-work.slice` (an unlimited `max` counts as `MAX`), the caps of the cgroups in `[compat] mirror_cgroups` that exist, and `MAX`. It only lowers: with enforce on, any target above that value is written down to it (also when a target appears later or a mirrored cgroup is lowered by someone else, whose lower cap is adopted); no target is ever raised at start. A step down never raises the cap (a cap of 0.5 adopted from a mirrored cgroup stays 0.5 on a busy sample).
- **`max_cores` changes.** When the effective `max_cores` (owner, Home Assistant, a profile switch, an expiring override) falls below the cap, the cap is clamped to it at once, in the same call that changed the limit, not at the next sample. A raise of `max_cores` does not jump: the cap climbs at the normal pace.
- **Mirror.** `[compat] mirror_cgroups = []` (default; paths below the cgroup root, no leading `/`) lists cgroups that get the same cap when they exist; a missing cgroup is skipped (`governor.targets[].present` is false). Remove an entry when its cgroup is retired. A missing `build-work.slice` is skipped too, and the governor starts as soon as it appears.
- **Observe mode.** The governor keeps a virtual cap (starting from the real one) and writes an audit line (`op=governor`, `result=would`) `WOULD cap=<c> cores busy=<b>% (<reason>)` only when its decision changes. Enforce mode writes the same line with `result=applied` and no `WOULD`. Reasons: `host above <T>%`, `room again`, `max_cores lowered to <m>`, `start`, `target above cap` (a target, `build-work.slice` or a mirrored cgroup, was found above the cap and is written down to it; `governor.last_reason` shows it too). Under `enforce` the mirrored caps are read on every tick, not only when a decision is made, so a mirrored cgroup that someone raised or recreated is lowered within one tick.
- **Legacy log.** `[compat] governor_log_path = "/abs/path"` appends `<UTC ISO time> cap=<c> cores busy=<b>% (<reason>)` per change, the legacy governor log format (`WOULD ` follows the time in observe mode).
- **Stop and crash.** The daemon does nothing on stop: the last cap stays in `cpu.max`, never `max`. After a restart the governor continues from that value. A failed write leaves the cap unchanged in the engine and is retried at the next decision.
- **Not done.** The governor does not re-write a cap somebody else changed in between, except to lower a target that is above its cap; otherwise it only writes on its own changes.

## Memory guard

Merges the legacy implementer memory guard, a per-project host guard, a JVM-daemon reaper and `guarded`'s floor into `engines/memguard.py` (decisions) and `daemon/memguard_ops.py` (collecting, executing, the registry). It samples on the daemon tick (5 s). `[memguard] enforce = false` (default) and `[reaper] enforce = false`: nothing is frozen, stopped, paused, signalled or terminated (AGENTS.md rule 2); the guard runs as a shadow with a virtual registry and writes audit lines (`op=memguard`, `result=would`) `WOULD PAUSE|RESUME|ABORT|REAP …` (and to `[memguard] log_path` in the legacy memory-guard log style). Shadow events are not pushed to the phone. With enforce on, `pause`, `abort`, `low_memory`, `resume` and `reap` alerts are pushed. Undoing a pause an earlier enforce run made (thaw, `SIGCONT`, `docker unpause`) is always allowed.

**Thresholds.** LOW = the effective `ram_floor_gb` (2.0 to 6.0 GB) in MB; RESUME = `max(6144, LOW + 2048)`, so the resume level is always at least 2 GB above the pause level and a resume cannot be followed by an immediate pause (a floor outside 2.0 to 6.0 is clamped); CRIT = 1500 MB. Pressure = `MemAvailable < LOW`, or swap used grew more than 768 MB over the calm baseline. The baseline only moves down, follows swap after 120 calm samples (10 min), and is reset to the current swap by a pause, an abort and a resume. A project in `[memguard.projects.<name>]` has its own rule for its own work (`low_mb`, `high_mb` (at least 2048 MB above `low_mb`; else the config is refused), `swap_mb`: also pressure when swap used is above it while available is below `high_mb`, `resume_samples`, `containers`: allow-regex of container names, `agent_cwd`: `claude -p` sessions below this path belong to it). There are no built-in projects (`low_mb` and `high_mb` are required, e.g. 2048 / 4096 MB, `swap_mb` 2048, `resume_samples` 12 = a minute at 5 s); every project not listed uses the global rule.

**Decision (one action per sample; 15 s of quiet after a pause or abort, 30 s after a resume).** Under pressure, in this order: (1) JVM build daemons outside every registered job scope are reaped first; (2) the largest tree of the first candidate class that has one is paused: (a) registered `loadctl run` job scopes by `memory.current`: an agent-free `gate` or `compile` scope (no `claude` process in it) is frozen with `cgroup.freeze`; an `impl` or `review` scope (or any scope with a `claude` in it) gets `SIGSTOP` on every process of the scope tree except `claude` and its ancestors (the agent is never frozen and keeps its connection; the nested-child-scope variant is not used); (b) legacy implementer trees: the descendants of the `claude` or `timeout` child of a snapshot shell (`bash <prefix>*` whose file has a line starting with the marker, both from `[memguard] legacy_snapshot = { prefix, marker, project }`; off unless set) and the descendants of `claude -p` sessions whose cwd is below a project's `agent_cwd`, minus every `claude`; (c) containers whose name matches a project's allow-regex, `docker pause`d, never a name matching `[memguard] deny_containers` (default `*-test-db*`, `*-review-*`). Trees whose largest `rustc` has 2048 MB or more are skipped (a stopped rustc keeps its memory). When only such trees are left: `HOLD` (logged first and every minute) while available memory is 1500 MB or more, below it `ABORT`: `SIGTERM` to the largest `rustc` only (never the agent, never `cgroup.kill` on an `impl` scope), and nothing else is touched. Nothing outside these classes is ever paused or stopped. Below 1536 MB every JVM build daemon is reaped, whatever else happens (system).

**Resume.** LIFO, per rule: after `resume_samples` (3) samples with `MemAvailable` at or above the rule's resume level and no pressure, the newest guard pause is released (thaw, `SIGCONT`, `docker unpause`). Human freezes are never resumed by a rule. A resume of a real pause while `[memguard] enforce` is off (a rollback) is written as `RELEASED` and done by the same rule.

**Signalling.** Every signal goes through a pidfd; the process's start time (`/proc/<pid>/stat` field 22) is re-checked after the pidfd is open, and a process that changed is not signalled. `claude` is refused for `SIGSTOP`, `SIGTERM` and `SIGKILL`, pid 1 and the daemon itself always.

**Registry and crash safety.** Every real pause is an entry in `state.json`, written before the pause is made (if it cannot be written, nothing is paused: audit `refused: persist failed`) (`memguard.paused`: `id`, `kind`, `rule`, `how`, `what`, `project`, `targets` as cgroup, pid with start time or container name, `session`, `since`, `reason`, `principal`, `expires`). The daemon never thaws anything when it stops or crashes (there is no `ExecStopPost` and no thaw on exit): paused work stays paused. On start every entry is checked (cgroup still frozen, pids alive with their start time and stopped); a live one is re-adopted (`ADOPTED`) and resumed only by the normal rule; a gone one is dropped (`RELEASED`). `loadgovd --recover --force` (root; the daemon must be stopped: it refuses while the socket answers) is the one thaw-all: it thaws every cgroup, `SIGCONT`s the recorded processes, unpauses the recorded containers, removes the compat marker, clears the freeze flag and the registry, and prints each action; without `--force` it only lists them (exit 2).

**Compat marker.** With `[memguard] pause_marker = { file, project }` set, while any work of that project is really paused `<lock_root>/<file>` contains `paused` (enforce only; headroom reads it). The daemon removes the file only when it wrote it, so a legacy guard's marker is left alone.

**Priority.** Guard pauses are `system`. `unfreeze` (socket, REST, the Home Assistant "Resume paused workers" button) never thaws a guard pause while its rule is not calm (`MemAvailable` below the resume level, or pressure): `{"ok": false, "refused": true, "restrictedBy": "system", "reason": …}` (REST `409`, `loadctl` exit 4); once memory is at the resume level the owner may release it early. Human freezes are separate entries (kind `human`) that the owner can always unfreeze.

**Human freeze.** `freeze` (owner only, default-deny; socket `owner: true` or REST) snapshots the candidate trees of every class (compiling ones included), pauses all of them like the guard does, and sets the limits `freeze` flag (a human restriction; new starts wait on it through headroom). With `project` only that project's trees are paused and the flag is not set. No TTL by default: it lasts until `unfreeze`; with `ttl` it thaws and clears the flag by itself. A reminder every 45 min is Home Assistant's job (the flag is in the status). `unfreeze` thaws the human entries and clears the flag; with enforce off both only write `WOULD` lines and the flag. Work that starts after a `freeze` is not frozen by it.

**JVM reaper (`engines/jvm.py`).** A target is a `java` process with exactly `org.gradle.launcher.daemon.bootstrap.GradleDaemon` or `org.jetbrains.kotlin.daemon.KotlinCompileDaemon` as an argument; nothing else. Reaping is `SIGTERM` through a pidfd. Below 1536 MB available (system) every matching daemon is reaped; `reap` by an agent, `loadctl run --kind gate`'s pre-gate reap and pressure reap only the daemons outside every registered job scope (idle ones, never one that serves a running job); `reap` by the owner (REST, `--owner`) reaps all. Triggers: avail < 1536 MB (all), pressure (the ones outside every job scope first), `reap` (`loadctl reap-daemons`, `POST /v1/reap`, HA "Kill build daemons"), and `loadctl run --kind gate` before it launches when avail is below `[memguard] pre_gate_reap_gb` (8; 0 turns it off; enforce only). With enforce off `reap` answers `would`.

**Pre-gate reaping (default on, enforce only).** `[memguard] pre_gate_reap_gb = 8` is the built-in default and stays: it is "on" in the sense that no key is needed, but it acts only when `[memguard] enforce = true`, only for `loadctl run --kind gate` (not impl, review or compile), only when `MemAvailable` is below 8 GB at that moment, and only on Gradle/Kotlin daemons that are idle, that is outside every registered job scope. A daemon serving a running job is never touched. With enforce off the call answers `skipped` and nothing is signalled. Set `pre_gate_reap_gb = 0` to turn it off (for example if a gate keeps losing a warm daemon it needed); watch the first enforced gates (`docs/deployment.md`, enforce).

**Orphan reaper (`engines/reaper.py`, `[reaper] enforce`).** An entry whose owning session (the implementer shell or `claude -p` pid, or the `loadctl run` pid, with start time) is gone is thawed, then `SIGTERM`ed; if a target is still alive 60 s later `SIGKILL`; when none is left the entry is dropped. Orphan signals are gated by `[reaper] enforce` alone (not `[memguard] enforce`). The `REAP` lines carry the real result per target (`sent`, `gone`, `changed`, `refused`, `error: …`); an entry is dropped once no target is alive, as soon as every target is `gone` or `changed` (nothing is left to signal), or after 10 SIGTERM rounds in which no target could be signalled (`refused`, `changed`, `error`); that last case is audited as `REAPED orphaned …: giving up after 10 rounds …`. Either drop happens only after the tree is confirmed thawed (scope not frozen, no process of the entry in state `T`, container unpaused); otherwise the entry stays registered, a critical `stuck_paused` alert goes out once and the thaw is retried every 60 s. The session recorded for a scope is the `claude` pid (with start time) when one runs in it, else the `loadctl run` pid, so a killed client cannot get a live agent's work reaped. With `[reaper] enforce = false`: one `WOULD REAP` line. Stopped (`T`) processes in governed cgroups that are not in the registry are only reported (`memguard.foreign_stopped`, one `REPORT` line each).

Config (`packaging/config/loadgov.toml.example`): `[memguard] enforce`, `pre_gate_reap_gb`, `log_path`, `[memguard.projects.<name>]`, `[reaper] enforce`; `[backstop]` is below. `loadctl freeze --owner --reason R [--project P] [--ttl T]`, `loadctl unfreeze --owner [--project P]`, `loadctl reap-daemons`.

## Memory backstop

`[backstop]` puts a hard kernel limit under `build-work.slice` for the case that the memory guard above was too slow. The guard pauses and aborts; the backstop is the last line and kills. **Off by default and with no built-in values**: `enabled = false`, and `enabled = true` is a config error unless both `memory_max_mb` and `memory_swap_max_mb` are given (`memory_max_mb` at least 4096; `memory_swap_max_mb` 0 or more). The owner chooses the numbers from the trial-run history (`tools/backstop_sizing.py`, `docs/deployment.md`) and states their source next to them in `loadgov.toml`. `enabled` is the enforce flag (AGENTS.md rule 2): off writes nothing.

- **Apply.** Under `enabled` the daemon writes exactly the configured bytes to `memory.max` and `memory.swap.max` of `/sys/fs/cgroup/build.slice/build-work.slice` and re-asserts them whenever they differ (every tick; the config wins over a hand edit). `max` is never written while enabled. A `memory.max` at or below the slice's current `memory.current` is not written, because the kernel would OOM-kill running builds at once: `backstop.applied` stays false, `backstop.note` says `deferred`, and the write follows when usage has fallen. The marker `<state-dir>/backstop.json` (names the slice) is written before the first limit write; if it cannot be written nothing is written (audit `refused`).
- **Per-scope group kill.** Every `loadctl run` job scope gets `memory.oom.group = 1`, so a kernel OOM kill inside a scope kills that scope's processes together and nothing else. The daemon writes the file when it registers the job (`job_start`; the scope and its first process already exist then) and for re-adopted jobs on the next tick. systemd 259 offers `OOMPolicy=kill` (`systemd-run --scope -p OOMPolicy=kill`) for the same file at scope creation; it is not used here because it would be set for every job whether or not the backstop is on and the client does not know the config. The window between the process start and `job_start` is milliseconds.
- **Detection and the `BACKSTOP` alert.** Each tick compares `oom_kill` and `oom_group_kill` of the slice's `memory.events` (hierarchical: includes every scope below) with the previous reading. An increase emits one critical `backstop` alert (`key: backstop`, fields `scope` = the job unit(s) whose own counter rose or that disappeared since the last tick, `memory_peak_mb` = that scope's `memory.peak`, or the slice's when the killed scope is already gone, `message`), an audit line (`op=backstop`, `result=event`) and updates `backstop.oom_kills` / `last_event`. It is pushed to Home Assistant like the other guard alerts. A kill can only be seen after it happened: the first reading after a daemon start is the baseline.
- **Rollback.** Remove (or `enabled = false`) the `[backstop]` table and restart: the daemon sees its marker, writes `max` to `memory.max` and `memory.swap.max` (the only case `max` is ever written), audits `rollback` and removes the marker. If the limits are also set statically in `packaging/systemd/build-work.slice`, remove the two properties there and `systemctl daemon-reload` too (the commented lines are the template). Job scopes keep `memory.oom.group` until they end.
- **Status and test.** `backstop` object in the status (below); `loadctl status` prints a `backstop` line when enabled. The balloon acceptance runs as `tests/integration/test_backstop.py` on `loadgov-test.slice` only (a test-only target override; the host's `build-work.slice` is never named).

## Sample history

The daemon appends one row every 15 s (clock-driven, whatever the 5 s tick) to `<state-dir>/history.csv` (default `/var/lib/loadgov/history.csv`): a header line, then CSV rows, like `hwmon`'s ring. Columns: `at` (UTC), `level`, `busy_pct`, `cpu_psi60`, `mem_psi60`, `io_psi60` (some avg60), `avail_mb`, `swap_used_mb`, `disk_free_gb`, `cap_cores` (the `build-work.slice` cap, empty when none), `bw_mem_mb`, `bw_swap_mb`, `bw_peak_mb` (`build-work.slice` `memory.current`, `memory.swap.current`, `memory.peak` in MB; empty when the slice or file is absent; `memory.peak` is the high-water mark since the slice was created or the file was reset). About 7 days are kept (40 320 rows): once the file holds 1 000 rows more it is rewritten with the newest 40 320 through a temp file and `rename`, so a reader or a crash sees the old or the new file. A row torn by a crash is skipped when reading. `loadctl history [n] [--csv|--json]` (default 40 rows, a table; `--csv` the file's header and rows, `--json` objects with numbers as numbers) and the socket op `history` read it through the daemon; `loadctl history` with `n` above 5760 or `--all` reads `<state-dir>/history.csv` directly instead (read-only, `--state-dir`, default `/var/lib/loadgov`); `compat/hwmon history [n]` is `loadctl history n --csv` (the columns are loadgov's, not the legacy hwmon ones). The history is observation only and is written whatever the enforce flags say.

## Published files

| Path | Content |
|---|---|
| `/run/loadgovd/status.json` | The latest status object, rewritten atomically every tick (5 s) |
| `/run/loadgovd.json` | Symlink to the above, kept for older readers. If its target does not exist yet (the first sample failed), treat that as "not ready". |

## Status object

Every field below is present in every status (checked by `loadgov.protocol.validate_status` and by `tests/unit/test_protocol_doc.py`, which fails when this table and the code disagree).

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Protocol version (`1`) |
| `at` | string | Sample time, UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `at_ms` | int | The same sample time in milliseconds since the epoch (`loadctl wait` compares samples with it) |
| `host.cores` | int | CPU count |
| `cpu.busy_pct` | number | Share of CPU time busy since the previous tick (`/proc/stat`, idle = idle + iowait) |
| `cpu.psi_some_avg10` | number | `/proc/pressure/cpu` some avg10 |
| `cpu.psi_some_avg60` | number | `/proc/pressure/cpu` some avg60 |
| `cpu.load1` | number | 1-minute load |
| `cpu.load5` | number | 5-minute load |
| `cpu.load1_per_core` | number | `load1 / cores` |
| `memory.avail_mb` | int | `MemAvailable` in MB |
| `memory.total_mb` | int | `MemTotal` in MB |
| `memory.psi_some_avg10` | number | `/proc/pressure/memory` some avg10 |
| `memory.psi_some_avg60` | number | `/proc/pressure/memory` some avg60 |
| `memory.psi_full_avg60` | number | `/proc/pressure/memory` full avg60 |
| `swap.total_mb` | int | `SwapTotal` in MB |
| `swap.used_mb` | int | `SwapTotal − SwapFree` in MB |
| `swap.used_pct` | number | Swap used in % |
| `io.psi_some_avg10` | number | `/proc/pressure/io` some avg10 |
| `io.psi_some_avg60` | number | `/proc/pressure/io` some avg60 |
| `io.psi_full_avg60` | number | `/proc/pressure/io` full avg60 |
| `disk.free_gb` | number | Lowest free space over `disk.paths`, GB |
| `disk.paths` | list | Watched mount points |
| `top` | list | Top 3 processes by CPU share (lifetime, as `ps -o pcpu`) |
| `top[].pid` | int | Process id |
| `top[].comm` | string | Command name |
| `top[].pcpu` | number | CPU share in % |
| `top[].rss_mb` | int | Resident memory, MB |
| `top[].cgroup` | string | cgroup v2 path |
| `jvm_daemons` | int | Running `GradleDaemon` / `KotlinCompileDaemon` processes |
| `heavy_slot.held` | bool | The heavy slot `/run/lock/host-heavy-slot.lock` is held: a live locker is listed in `/proc/locks`, or the pid in the holder line is alive and has the lock file open. Checked read-only; the lock is never taken. |
| `heavy_slot.holder_pid` | int or null | The holder line's `pid=`, when that process is alive and has the lock file open (or is a listed locker) |
| `heavy_slot.lock_pids` | list | Live pids `/proc/locks` lists as lockers. Usually empty for the legacy bash protocol (`exec 9>>lock; flock -n 9`): the kernel records the exited `flock(1)` helper, and on some kernels (observed on 7.x, in the init pid namespace) the entry is then not listed at all, so the holder line's pid is the evidence there. Kernels that still list it make `held` true through this field instead. |
| `heavy_slot.holder` | string or null | Holder line while held by the pid it names |
| `heavy_slot.last_holder` | string or null | Holder line when it does not describe the current holder (legacy scripts never delete the file) |
| `heavy_slot.since` | string or null | `since=` from the holder line while held |
| `heavy_slot.age_s` | int or null | Seconds held |
| `heavy_slot.stuck` | bool | Held longer than 45 minutes |
| `slots.heavy.state` | string | `free`, `held` (a loadgov job or a legacy holder) or `held-by-hold` (owner or drain hold) |
| `slots.heavy.holder` | string or null | Holder line (`<project> <label> pid=<pid> since=<UTC>`); null when free or a legacy holder is unnamed |
| `slots.heavy.source` | string or null | `loadgov` or `legacy` while held |
| `slots.heavy.queue` | int | Waiters in the FIFO queue |
| `slots.heavy.held_since` | string or null | Since when the current holder holds (UTC) |
| `slots.heavy.stuck` | bool | Held longer than 45 minutes |
| `slots.heavy.hold` | string or null | Reason of a hold on this slot (pending or taken) |
| `slots.heavy.hold_origin` | string or null | Who set that hold: `owner` (`hold on`) or `profile` (the drain profile); null without a hold |
| `slots.compile.state` | string | As `slots.heavy.state`, for the compile slot's lock (`/run/lock/host-compile-slot.lock` by default) |
| `slots.compile.holder` | string or null | As `slots.heavy.holder` |
| `slots.compile.source` | string or null | As `slots.heavy.source` |
| `slots.compile.queue` | int | As `slots.heavy.queue` |
| `slots.compile.held_since` | string or null | As `slots.heavy.held_since` |
| `slots.compile.stuck` | bool | As `slots.heavy.stuck` |
| `slots.compile.hold` | string or null | As `slots.heavy.hold` |
| `slots.compile.hold_origin` | string or null | As `slots.heavy.hold_origin` |
| `jobs` | list | Jobs started by `loadctl run` that are still running |
| `jobs[].kind` | string | `impl`, `review`, `gate` or `compile` |
| `jobs[].project` | string | Project of the job |
| `jobs[].label` | string | Label of the job |
| `jobs[].unit` | string | The scope unit, `loadgov-<kind>-<label>-<time>-<pid>.scope` |
| `jobs[].cgroup` | string | cgroup v2 path of the scope |
| `jobs[].pid` | int | The job process |
| `jobs[].slot` | string or null | `heavy` or `compile` while the job holds that slot |
| `jobs[].since` | string | Registration time, UTC |
| `cgroups` | object | `cpu.max` per watched cgroup (each `[compat] mirror_cgroups` entry under its own path; `build-work.slice`, which systemd places at `/sys/fs/cgroup/build.slice/build-work.slice`): `{cpu_max, cores}` (`cores` null = unlimited), or null when the cgroup does not exist |
| `stopped` | list | Processes in state `T` under a mirrored cgroup or `/build.slice/build-work.slice` |
| `stopped[].pid` | int | Process id |
| `stopped[].comm` | string | Command name |
| `stopped[].cgroup` | string | cgroup v2 path |
| `levels.cpu` | string | hwmon level `OK` / `BUSY` / `LIMIT` (LIMIT only when PSI or load agree) |
| `levels.ram` | string | hwmon level for `memory.avail_mb` |
| `levels.swap` | string | hwmon level for `swap.used_pct` |
| `levels.disk` | string | hwmon level for `disk.free_gb` |
| `level` | string | Worst of `levels` |
| `limits.profile` | string | Active profile (`silent`, `balanced`, `boost`, `drain`) |
| `limits.ceiling` | string or null | Profile name of the owner ceiling, or null |
| `limits.ceiling_info` | object or null | The active ceiling: `{profile, reason, principal, set_at, expires_at}` (`expires_at` null = no expiry), or null. Lets a client tell its own ceiling from a human's by the `reason` |
| `limits.overrides` | list | Active overrides, oldest first: `{param, value, principal, reason, set_at, expires_at}` (`expires_at` null = none). Lets a client see which limits somebody set by hand before it sets its own |
| `limits.profile_info` | object or null | Who chose the active profile: `{profile, reason, principal, set_at, expires_at}`, or null when it is the default or was restored after a temporary profile expired |
| `limits.restricted` | bool | A human restriction is active (agent loosenings are refused) |
| `limits.restricted_by` | string or null | `owner` while restricted |
| `limits.freeze` | string or null | The reason of the owner's `freeze` flag (a human restriction), null when none |
| `limits.effective` | object | Effective limits: the five tunables plus `cargo_build_jobs`, `load_hold`, `impl_max_quiet`, `vitest_max_workers`, `gate_shard_budget`, `hold_heavy_slot` |
| `rest.enabled` | bool | `[rest] enabled` |
| `rest.state` | string | `up` (listening), `down` (enabled but not listening; see `rest.error`) or `disabled` |
| `rest.error` | string or null | Why the listener is down (bind failure, token file problem), or that the host's address table is unreadable (requests refused); null otherwise |
| `rest.bind` | string or null | Configured bind address; null while `[rest]` is not configured |
| `rest.port` | int | Listening port (the configured port while down) |
| `alerts.active` | list | Alerts whose condition holds now (see Alerts) |
| `alerts.active[].key` | string | `cpu`, `memory`, `swap`, `stuck_holder:<slot>` or `disk` |
| `alerts.active[].level` | string | `warning` or `critical` |
| `alerts.active[].since` | string | When the condition began, UTC |
| `alerts.events` | list | The last 20 events (alerts, `resolved`, `info`), oldest first; the same objects that are pushed |
| `alerts.events[].type` | string | `alert`, `resolved` or `info` |
| `alerts.events[].key` | string | The alert key, a guard event kind (`pause`, `abort`, `low_memory`, `resume`, `reap`, `stuck_paused`) or `owner_change` |
| `alerts.events[].at` | string | Event time, UTC |
| `ha_push.enabled` | bool | `[ha] enabled` |
| `ha_push.state` | string | `up` (last delivery succeeded or none yet), `down` (the last delivery failed after its retries, or the webhook id is missing) or `disabled` |
| `ha_push.queued` | int | Events waiting for delivery (at most 64) |
| `ha_push.dropped` | int | Events dropped because the queue was full (oldest first) |
| `ha_push.last_ok` | string or null | Time of the last successful delivery, UTC |
| `ha_push.failed` | int | Events that could not be delivered (all attempts failed, or the event could not be encoded) |
| `ha_push.last_error` | string or null | Error class and reason of the last failed delivery (never the URL or the webhook id) |
| `governor.enforce` | bool | `[governor] enforce`; false = shadow, no cgroup write |
| `governor.cap_cores` | number or null | The governor's current cap in cores (the virtual cap in observe mode); null while the target cgroup does not exist |
| `governor.target_pct` | number | Effective `target_cpu_pct` the governor steers to |
| `governor.max_cores` | number | Effective `max_cores`, the upper bound of the cap |
| `governor.min_cores` | number | Lower bound of the cap (1.0) |
| `governor.last_busy_pct` | number or null | Busy % of the last 10 s decision window |
| `governor.last_change_at` | string or null | Time of the last cap change, UTC |
| `governor.last_reason` | string or null | Reason of the last cap change (see CPU governor) |
| `governor.targets` | list | Cgroups the cap is written to: `build.slice/build-work.slice`, plus every `[compat] mirror_cgroups` entry |
| `governor.targets[].path` | string | cgroup path below `/sys/fs/cgroup` |
| `governor.targets[].present` | bool | The cgroup's `cpu.max` exists |
| `governor.targets[].cpu_max` | string or null | Its current content (`<quota or max> <period>`), null when absent |
| `memguard.enforce` | bool | `[memguard] enforce`; false = shadow (`WOULD` lines only) |
| `memguard.reaper_enforce` | bool | `[reaper] enforce` |
| `memguard.low_mb` | int | Global pause level = the effective `ram_floor_gb` in MB |
| `memguard.resume_mb` | int | Global resume level, `max(6144, low + 2048)` |
| `memguard.crit_mb` | int | Abort level (1500) |
| `memguard.baseline_swap_mb` | int | The global rule's calm swap baseline |
| `memguard.paused` | list | The paused registry (virtual entries of the shadow included, `real` false) |
| `memguard.paused[].id` | string | Entry id (`g<n>` guard, `h<n>` human freeze) |
| `memguard.paused[].kind` | string | `guard` (system) or `human` |
| `memguard.paused[].how` | string | `freeze` (cgroup), `sigstop` or `docker` |
| `memguard.paused[].what` | string | `scope <unit>`, `implementer <pid> (<project>)` or `container <name>` |
| `memguard.paused[].project` | string or null | The project of the work |
| `memguard.paused[].since` | string | Pause time, UTC |
| `memguard.paused[].reason` | string | Why (the pressure, or the owner's reason) |
| `memguard.paused[].principal` | string | `system` or `owner` |
| `memguard.paused[].real` | bool | Really paused (enforce); false = a `WOULD` entry of the shadow |
| `memguard.last_decision` | string or null | `PAUSE`, `RESUME`, `RELEASED`, `ABORT`, `HOLD`, `REAP`, `LOW_MEMORY`, `ADOPTED`, `FREEZE`, `UNFREEZE` or `REPORT` |
| `memguard.last_reason` | string or null | The line of the last decision (without `WOULD`) |
| `memguard.last_at` | string or null | Its time, UTC |
| `memguard.cooldown_s` | number | Seconds of quiet left after a pause, abort or resume |
| `memguard.foreign_stopped` | list | Stopped (`T`) processes in governed cgroups that are not in the registry: only reported |
| `memguard.foreign_stopped[].pid` | int | Pid |
| `memguard.foreign_stopped[].comm` | string | Command name |
| `memguard.foreign_stopped[].cgroup` | string | Its cgroup |
| `headroom.io_would_block_count` | int | `headroom` decisions since the daemon started whose io PSI was over `io_psi_max` (`would_block_io`), whether or not the io gate is on: the trial of the io gate counts io-only blocks with it |
| `headroom.io_would_block_last` | string or null | UTC time of the latest such decision |
| `backstop.enabled` | bool | `[backstop] enabled` (the enforce flag of the memory backstop) |
| `backstop.applied` | bool | `memory.max` and `memory.swap.max` of `build-work.slice` hold the configured values |
| `backstop.memory_max_mb` | int or null | Configured `memory.max` in MB (null when not configured) |
| `backstop.memory_swap_max_mb` | int or null | Configured `memory.swap.max` in MB |
| `backstop.oom_kills` | int | OOM kills seen in `build-work.slice` since the daemon started |
| `backstop.last_event` | string or null | `<UTC time> <scope> peak <n> MB` of the last `BACKSTOP` alert |
| `backstop.note` | string or null | Why the limits are not applied (`deferred: usage ... is not below memory.max`, a failed write, ...) |
| `capabilities` | list | What this daemon can do: `freeze` (the `freeze`/`unfreeze` ops and endpoints) and `reap`; the Home Assistant integration enables its freeze, resume and kill-daemons entities from it |
| `daemon.version` | string | loadgov version |
| `daemon.pid` | int | Daemon pid |
| `daemon.started_at` | string | Daemon start time, UTC |
| `daemon.mode` | string | `observe` (nothing is enforced yet) |
| `daemon.tick_s` | number | Seconds between samples |

Level thresholds (busy, limit) are hwmon's: busy % (85, 97), CPU PSI60 (40, 70), load1/core (1.5, 2.5), RAM available (6144, 3584) MB, swap used (85, 95) %, disk free (25, 10) GB.
