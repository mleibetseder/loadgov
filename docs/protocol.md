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
| `starting` | The daemon has not taken its first sample yet (an older daemon sent `not_ready` with the message "no sample yet" for this; the client library accepts both) |
| `not_ready` | The sample is older than 3 x `tick_s` ("stale sample"): the tick loop stalled |

`loadctl` exit codes: `0` ok, `2` usage, `3` daemon not reachable, `4` owner denied or request refused, `5` daemon returned another error.

## Operations

Nothing is enforced unless the matching `enforce` flag is on (`[slots]`, `[governor]`, `[memguard]`, `[reaper]`, `[backstop] enabled`, `[strays]`, all default off; `[strays] enabled` is detection only and reads `/proc`): no cgroup writes, signals, container pauses or `/run/lock` locks without it (AGENTS.md rule 2).

| `op` | Args | Reply |
|---|---|---|
| `status` | none | `{"ok": true, "status": {…}}` |
| `set` | `profile`, `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers`, `max_reviewers`, `max_light`, `ram_floor_gb` (at least one), `ttl` or `until` (not both), `reason` (required), `owner` (bool), `project` | accepted: `{"ok": true, "accepted": true, "principal", "profile", "expires_at", "ttl_s", "ttl_capped", "changed": {param: {before, after}}, "effective"}`; refused: see below |
| `ceiling_set` | `profile`, `reason` (required), `ttl` or `until`, `owner: true` | `{"ok": true, "accepted": true, "ceiling", "expires_at", "effective"}` |
| `ceiling_clear` | `reason`, `owner: true` | `{"ok": true, "cleared": bool, "effective"}` |
| `reset` | `params` (list of limit names) or `all: true`, `reason` (required), `owner: true` | `{"ok": true, "accepted": true, "removed": [override ids], "effective"}`. Removes the owner overrides for `params`; with `all` removes every override, owner and agent. The profile and the ceiling are not touched (use `set --profile`, `ceiling_clear`). Owner only: an owner override with no TTL otherwise stays until reset or replaced. `loadctl reset <param...>\|--all --owner --reason …` |
| `limits` | none | `{"ok": true, "limits": {profile, profile_expires_at, effective, overrides[], ceiling, hold, freeze, restriction}}` |
| `slot_acquire` | `slot` (`heavy`/`compile`), `project`, `label`, `pid` (holder process, required), `wait` (bool), `timeout_s`, `client_lock` (bool; required when enforce is on), `kind` (optional, `impl`/`review`/`gate`/`compile`/`light`/`image`), `key` (optional issue key) | `{"ok": true, "status": "granted"\|"queued"\|"busy"\|"timeout", "slot", "who", "holder", "position"?, "queue"?, "nested"?}`; under enforce a `granted` reply also has `lock_path`, `holder_path`, `holder_line`; see Slots |
| `slot_release` | `slot`, `pid` (required) | `{"ok": true, "slot", "released": bool, "nested"?: true}`; `nested: true` when it ended a nested grant (see Slots) |
| `slot_status` | none | `{"ok": true, "slots": {…as in status}, "queues": {slot: [{project, label, pid}]}}` (the `slots` entries carry `key`, `url` and `waiters`) |
| `hold` | `state` (`on`/`off`), `slot` (`heavy`/`compile`/`all`, default `all`), `reason` (required for `on`), `owner: true` | `{"ok": true, "hold": "on"\|"off", "slots": {…}}`; owner only (`owner_denied` otherwise) |
| `headroom` | `kind` (`impl`, `review`, `gate`, `compile`, `light`, `image`; required), `project` (default `default`), `overrides` (optional object with any of `load1_max`, `load5_max`, `psi_max`, `mem_min_gb`, `swap_max_mb`, `swap_in_max`, `mem_psi_max`: numbers >= 0 that replace the configured value for this call; the profile scaling still applies to them) | `{"ok": true, "kind", "project", "decision": {pass, reasons[], would_block_io, values, paused[], thresholds, factor, reason}, "thresholds", "factor", "timing": {confirm_s, poll_s, max_wait_s}, "sample_at", "sample_at_ms"}`. One decision on the latest sample (see Headroom below). Wrong `kind` or `project` type: `bad_request`; no sample yet: `starting`; one older than 3 x `tick_s`: `not_ready` ("stale sample"). |
| `image_submit` | `project`, `label`, `channel` (`beta`/`release`), `argv` (list of strings), `cwd` (absolute), `env` (object; only `[image] env_allow` names are kept), `key` (optional), `owner` + `reason` (optional: waives the beta interval) | `{"ok": true, "id", "position", "planned_start", "planned_finish", "state"}`; a beta inside its interval: `{"ok": false, "error": "refused", "message", "next_allowed"}`. See The image train |
| `image_list` | none | `{"ok": true, "image": {…as in status, the whole queue}}` |
| `image_cancel` | `id`, `project` (the job's) or `owner: true` | `{"ok": true, "id", "cancelled": true}`; another project: `owner_denied`; a running job: `invalid_args` |
| `image_now` | `id`, `reason` (required), `owner: true` | `{"ok": true, "id", "now": true}`; owner only (`owner_denied` otherwise, audited); the job may start outside the window |
| `image_next` | none (the runner) | `{"ok": true, "job": {id, project, label, channel, key, argv, cwd, env, start_by} or null, "in_window"?}`; marks the job running; refused (`owner_denied`) for a caller inside a governed cgroup |
| `image_done` | `id`, `exit_code`, `duration_s`, `started` (bool, default true) | `{"ok": true, "id", "requeued": bool}`; only the process that got the job from `image_next` |
| `job_start` | `kind`, `project`, `label`, `unit` (`loadgov-*.scope`), `cgroup` (absolute cgroup v2 path ending in `/<unit>`), `pid` (the job process), `owner_pid` (the `loadctl run` process; default: the socket peer), `slot` (`heavy`/`compile`, only when `owner_pid` holds it), `key` (optional issue key) | `{"ok": true, "unit", "slot", "bound": bool}`; registers the job and binds the slot to the scope |
| `job_end` | `unit`, `overrun` (optional, `runtime` or `memory`; only for a `light` job, else ignored; anything else: `invalid_args`), `kind`, `project`, `label` (optional, only with `overrun` for a job the daemon does not know) | `{"ok": true, "unit", "ended": bool, "released": bool}`; removes the job and releases its slot. With `overrun` the daemon audits `light_overrun` first and the `job_end` line's `reason` reads `light_overrun: runtime 600 s exceeded` or `light_overrun: memory limit 1024 MB hit` (the configured values), and counts it for the `light_overrun:<project>` alert. The daemon keeps the last 64 ended jobs for 10 minutes (in memory only: a daemon restart forgets them, and resets the overrun count): when its tick ended the job first (`scope empty`), a late `job_end` with `overrun` still writes the `light_overrun` line and counts it, and a report is audited and counted once per unit. `loadctl` sends `kind` `light`, `project` and `label` with every `overrun`; the daemon uses them only when the unit is neither registered nor remembered (its `job_start` failed, the daemon restarted, or 64 later reports evicted it). It then audits `light_overrun` only for a `loadgov-light-*` unit whose trailing `-<pid>` (the `loadctl run` that made it) equals the pid of the connecting peer; any other peer is refused with `owner_denied` and an audit line with `result` `refused`, and nothing is counted. An accepted late or unregistered report books nothing else (`ended` is false, no `job_stats`) |
| `cancel_holder` | `slot` (`heavy`/`compile`), `confirm: true` (required), `reason`, `owner: true` | `{"ok": true, "slot", "signalled": bool, "signal": "SIGTERM", "pid", "start", "source", "holder", "comm"}`; owner only (`owner_denied` otherwise); see Cancel holder |
| `freeze` | `reason` (required), `project`, `ttl` (`90s`, `15m`, `2h`; default none: until `unfreeze`), `owner: true` | `{"ok": true, "frozen": [entry ids], "failed": [what], "would": bool, "expires_at", "project"}`; owner only (`owner_denied` otherwise); see Memory guard |
| `unfreeze` | `project`, `reason`, `owner: true` | `{"ok": true, "thawed": [ids], "flag_cleared": bool, "still_paused": [ids], "restrictedBy"?: "system"}`, or `{"ok": false, "refused": true, "restrictedBy": "system", "reason", "still_paused"}` when only memory-guard pauses remain and memory is below their resume threshold; owner only |
| `reap` | `reason`, `owner` (bool), `pre_gate` (bool) | `{"ok": true, "reaped": [{pid, start, in_scope, result}], "would": bool}`; any caller (it only frees memory), audited with the caller's principal; `pre_gate: true` is `loadctl run --kind gate`'s call and reaps only with enforce on and `MemAvailable` below `[memguard] pre_gate_reap_gb`, else `{"reaped": [], "skipped": "<why>"}` |
| `stray_kill` | `pid` (required), `start_time` (optional: the stray's field 22; default the one current stray with that pid), `reason`, `owner: true` | `{"ok": true, "killed": true, "signal": "SIGTERM", "message"}`; owner only (`owner_denied` otherwise). Refused with `{"ok": false, "refused": true, "reason"}` when the process is not in the current stray list, is gone, changed (start time or command name), is `claude`, no longer meets the stray conditions, or `[strays] enforce` is off (audited `WOULD KILL`). See Stray guard. `loadctl stray --kill PID [--start-time T] --owner [--reason R]` |
| `history` | `n` (default 40, capped at 5760 = one day, so the daemon never builds a huge reply) | `{"ok": true, "columns": [...], "rows": [[...]]}`: the newest `n` rows of the sample history, oldest first, every cell a string exactly as in `history.csv` (see Sample history) |
| `log` | `n` (default 20, max 1000) | `{"ok": true, "lines": [audit entries, oldest first]}` |

Ranges: `max_cores` 1.0-6.0 step 0.5, `target_cpu_pct` 35-90 step 5, `load_max` 1.0-6.0 step 0.5, `max_implementers` 0-4, `max_reviewers` 0-8, `max_light` 0-8, `ram_floor_gb` 2.0-6.0 step 0.5. `ttl` is `90s`, `15m` or `2h`; `until` is `HH:MM Zone` (`07:00 Europe/Berlin`), the next such time (tomorrow if today's has passed), resolved with the zone's DST rules; without a zone the host's zone is used.

### Headroom

`headroom` answers "is there room for one more heavy job?" on the daemon's latest sample (at most one tick old); `loadctl wait` asks twice, `confirm_s` apart. All comparisons are strict, as in `wait-headroom.sh`. A decision passes when all of these hold: `load1 < load1_max`; `load5 < load5_max` (only when configured); cpu PSI (the effective value, see "CPU pressure and throttling") `< psi_max`; `MemAvailable/1024 >= mem_min_gb`; swap does not block unless swap used `> swap_max_mb` (1024) **and** the swap-in rate `swap.in_rate` (pages/s, averaged over about 60 s) `> swap_in_max` (default 50 pages/s, about 200 KB/s at 4 KiB pages: old, idle swap does not hold work back, active swapping does) and `MemAvailable < 2 x mem_min_gb`; the reason names both values (`swap used 6000 MB > 1024 MB and swapping in 120 pages/s > 50 pages/s`); an unknown rate (first sample, no `/proc/vmstat`, counter reset) counts as 0; memory PSI `some avg60 < mem_psi_max`; not paused; and, only when `[headroom] io_psi_gate = true`, io PSI `some avg60 < io_psi_max` (25). With the gate off `would_block_io` still reports whether io PSI is over its limit.

*CPU pressure and throttling.* cgroup v2 counts the runnable time of a throttled cgroup (a container with `cpu.max`, or loadgov's own cap on `build-work.slice`) as CPU pressure, and it propagates to `/proc/pressure/cpu`. A host with idle cores then looks saturated and its own cap would block its own headroom checks. So CPU PSI only counts as contention while the CPU is busy: `cpu.psi_effective` is `cpu.psi_some_avg60` when `cpu.busy_pct >= [headroom] psi_busy_gate_pct` (default 80), else `0`. It is a hard gate rather than a scaling: it is simple, monotonic (a busier host never counts less) and has no in-between value to explain. `psi_busy_gate_pct = 0` always counts the raw value (the old behaviour). The effective value drives the `levels.cpu` thresholds (BUSY 40, LIMIT 70) and the `psi_max` check (`values.cpu_psi` of a decision is the effective value); the CPU governor and the alerts do not read PSI (the legacy alert line keeps printing the raw `cpu_some10`). The raw values stay in the status and in the history. Memory and io PSI are unchanged. `busy_pct` is the share over the last tick, avg60 is a minute long, so a short idle gap in a real build drops the pressure for that tick; the `confirm_s` double sample and `loadctl wait` polling absorb it. `loadctl status` shows `cpu 62/53 (throttling only, counts 0)` when the two differ.

*Not paused*: no non-empty pause marker file (`[memguard] pause_marker`, `<lock_root>/<file>`; host-wide, any project; none is configured by default); no owner `freeze` (all kinds); no owner `hold` (kinds `gate` and `compile`). `paused[]` names which.

*The review lane.* A `review` (an AI agent reading code) makes little CPU load, so it skips the load, PSI and swap checks above (`load1_max`, `load5_max`, `psi_max`, `swap_max_mb`, `swap_in_max`, `mem_psi_max`; a project's values for them under `kinds.review` are accepted but ignored). It passes when `MemAvailable/1024 >= mem_min_gb` (default 2 GB for reviews, raised by the profile's `ram_floor_gb` like other kinds, and never below the effective `ram_floor_gb` itself, the memory guard's low level; a project's `mem_min_gb`, `poll_s`, `confirm_s` and `max_wait_s` for `review` still apply), the number of registered review jobs (jobs engine, kind `review`, every project) is below the profile limit `max_reviewers` (silent 1, balanced 3, boost 4, drain 0; range 0-8), and it is not paused (a host guard pause file or owner `freeze`, or any memory-guard pause being active, reason `memory guard is pausing work`; an owner `hold` does not stop reviews). `max_reviewers` is set like `max_implementers`: lower is tighter, and an agent raise is refused under a human restriction. The decision's `reason` names the lane (`review lane: 2 of 3 reviewers running, 9.1 GB free`); `reasons[]` holds only what blocks. The cap counts only reviews registered by `loadctl run --kind review`; a bare `loadctl wait --kind review` or MCP `load_wait_headroom` review is not counted. Reviewers that pass the wait within one confirm window (`confirm_s`, default 60 s) before any of them registers can therefore overshoot the cap by the number that passed together.

*Thresholds* are `[headroom]` defaults, then `[headroom.kinds.<kind>]`, then `[projects.<name>.kinds.<kind>]` (see `packaging/config/loadgov.toml.example`). *Profile influence*: load thresholds are multiplied by `effective.load_max / default_profile.load_max` and `mem_min_gb` is raised by `max(0, effective.ram_floor_gb - default_profile.ram_floor_gb)`; `factor` reports `{load, mem_add_gb}` and `thresholds` the applied values. PSI and swap limits do not change with the profile.

`loadctl wait --kind K [--project P] [--label L] [--timeout S] [--confirm S] [--poll S]` prints `HEADROOM_WAIT <label> <state> waited=Ns` to stderr (at most every 300 s), then `HEADROOM_OK <label> <state> waited=Ns` to stdout and exits 0, or `HEADROOM_TIMEOUT <label> <state> after Ns: not started` to stderr and exits 1. Daemon unreachable: exit 3; negative `--timeout`/`--confirm`/`--poll`: exit 2. The confirming query must come from a newer daemon sample (`sample_at`) than the first pass, so one sample can never satisfy both (compared at millisecond resolution through `sample_at_ms` when the daemon sends it). A `starting` or `not_ready` answer (daemon starting or restarting, stale sample) is retried inside the wait budget (120 s when no `--timeout` and no reply has supplied one yet; the client library's own 30 s start-up retry is off for `wait`, so a `--timeout` shorter than 30 s is honoured too) instead of failing with exit 5. After a confirmation sample that fails, the wait looks again at once, like `host-wait.sh`, and only then sleeps a poll. `max_wait_s = 0` (or `--timeout 0`) waits forever; a project may set `poll_s = 120`, `max_wait_s = 0` to wait like a legacy `host-wait.sh` (the defaults are `poll_s = 30`, `max_wait_s = 7200`). A `load1_max`/`load5_max` expression that resolves to <= 0 on the host (e.g. `nproc-2` on 2 cores) makes every decision fail with a `misconfigured:` reason; expressions are validated by shape at config load.

### The light lane

A `light` job is a small, read-only check that should never wait behind a build: it takes no `heavy` or `compile` slot and skips the load, PSI and swap checks (like the review lane). In exchange `loadctl run --kind light` puts it under hard limits it cannot exceed.

*Qualifies:* lint, `shellcheck`, `bash -n`, formatters in check mode, link checks, config validation, small unit tests, read-only verifications, reviewer-style checks. *Never qualifies:* compiles, image builds, full or integration suites, browser or E2E runs, anything that starts containers. Those are `gate` or `compile` jobs.

*Admission* (`headroom` with `kind: light`; `decision.reason` names the lane, `light lane: 2 of 4 running (web-app 1 of 2), 9.1 GB free`; `reasons[]` holds only what blocks):

- `MemAvailable/1024 >= mem_min_gb` (default 2 GB for `light`, raised by the profile's `ram_floor_gb` like other kinds, and never below the effective `ram_floor_gb`; a project's `mem_min_gb`, `poll_s`, `confirm_s` and `max_wait_s` for `light` still apply, its load and PSI keys are accepted but ignored).
- Not paused: a host guard pause file, an owner `freeze` or any memory-guard pause stops it (reason `memory guard is pausing work`); an owner `hold` does not.
- The registered `light` jobs host-wide are below the profile limit `max_light` (silent 2, balanced 4, boost 6, drain 0; range 0-8). `max_light` is set like `max_reviewers`: lower is tighter, an agent raise is refused under a human restriction (`loadctl set --max-light N`, REST and MCP `set` take `max_light`).
- That project's registered `light` jobs are below `[light] max_light_per_project` (default 2, range 1-8). Like reviews, only jobs registered by `loadctl run --kind light` count; a bare `loadctl wait --kind light` or MCP `load_wait_headroom` is not counted. Compat shims take the kind through `LOADGOV_WAIT_KIND=light`.

*Hard limits* (`[light]` in the config; a site may tighten them, the ranges cap how loose they can be):

| Key | Default | Range | Applied as |
|---|---|---|---|
| `cpu_quota_pct` | 100 | 10-200 | `-p CPUQuota=<n>%` on the scope |
| `memory_max_mb` | 1024 | 128-4096 | `-p MemoryMax=<n>M`, and always `-p MemorySwapMax=0` |
| `max_runtime_s` | 600 | 30-1800 | `loadctl` itself kills the scope after this long with `systemctl kill --signal=SIGKILL`; `-p RuntimeMaxSec=<n+30>` is only the backstop (systemd supports it on scopes since version 244; the 30 s grace lets `loadctl` report the overrun first) |
| `max_light_per_project` | 2 | 1-8 | the per-project cap above |

The limits exist only for a job started with `loadctl run --kind light`. A bare command after `loadctl wait --kind light` (or MCP `load_wait_headroom`) is unlimited and not counted: agents use `run`. Light work is short checks; agent sessions (`claude`, `codex` and the like) are not light work, because a runtime kill would end them. Lights that pass the wait within one confirm window (`confirm_s`, default 60 s) before any of them registers can overshoot `max_light` and the per-project cap by the number that passed together.

`loadctl` reads these values from the daemon's status (`light`), so a config reload reaches the next job; only the scope `loadctl` creates is touched, the daemon writes no cgroup file for it. Nothing is applied to the other kinds.

*Overrun.* A light job that exceeds `max_runtime_s`, or whose scope records an OOM kill (`oom_kill` in `memory.events`, polled every second and read once more at exit; a light scope is started without `--collect`, so after the job exits `loadctl` also reads systemd's own `Result` of the scope (`oom-kill`), which holds even when a single-process job was killed and its cgroup, with `memory.events`, is already gone; `loadctl` then stops and resets that one unit itself), makes `loadctl run` print `LIGHT_OVERRUN <label>: runtime 600 s exceeded` or `LIGHT_OVERRUN <label>: memory limit 1024 MB hit` to stderr and exit **76**. It tells the daemon with `job_end` `overrun` (also when `job_start` failed, and whichever side ended the job first, with `kind`, `project` and `label`; a failed report never changes the exit code; for a unit the daemon no longer knows only the `loadctl run` whose pid ends the unit name may report it, and the remembered ended jobs and the overrun count live in memory only); the audit log gets a `light_overrun` line and the `job_end` line carries the reason. Three overruns of one project within 24 h raise the alert `light_overrun:<project>` (see Alerts). Light jobs are listed in `jobs[]` and recorded in `job_stats` like any other kind; the status `light` object and `loadctl status` (`light 2/4`) show the lane.

### The image train

Beta and release container image builds are heavy, slow and not urgent, so they run in a night window instead of when an agent asks: projects **submit** during the day and nothing waits for hours. The owner's rule: builds run only in `[image] window` (default `00:00-06:00` in `timezone`, default `UTC`; the site sets its own zone), a project's beta at most once per `beta_interval_h` (48 h), otherwise only when the owner says `now`. The daemon only arbitrates (it records jobs and says which may start); it never executes anything (AGENTS.md rule 2). A client, `loadctl image runner`, runs them.

*Interface.*

- `loadctl image submit --project P --label L --channel beta|release [--key KEY] [--cwd DIR] -- cmd...` (op `image_submit`) records the job: argv, cwd (default: the caller's), the environment variables named in `[image] env_allow` (default `PATH`, `HOME`, `LANG`, `DOCKER_HOST`, `DOCKER_CONFIG`, `LOADGOV_KEY`; the client sends only those, the daemon filters again), the submitter's pid, cgroup and command line, and the key (`--key`, else `$LOADGOV_KEY`). It is saved in `state.json` and survives a restart. The reply has the id, the position and the planned start and finish. The audit log never gets the environment values or the arguments, only the program name, the cwd and the variable names. At most 50 jobs are queued or running; more is `refused`.
- `loadctl image list [--json]` shows the queue (op `image_list`); `loadctl image cancel ID [--project P | --owner]` removes a queued job. Without `--owner` the daemon only checks that the project the caller names equals the job's project (it cannot know who the caller really is); with `--owner` the default-deny owner claim applies; any other caller gets `owner_denied`. Both are audit-grade evidence (AGENTS.md rule 4), not a security boundary, because everything runs as root. A running job cannot be cancelled.
- `loadctl image now ID --owner --reason "..."` is owner only (the default-deny owner claim of Principals and priority; a caller inside a governed cgroup or below a dispatcher is refused, audited as `image_now` `refused`). It lets the job start outside the window and inside the beta interval (state `now`); the reason is audited as `image_now`. An agent can never grant it. `loadctl image submit --owner --reason` waives only the beta interval at submission, so a beta the owner wants outside the interval can be queued.

*Rules* (`[image]`, validated at load, live on reload):

| Key | Default | Meaning |
|---|---|---|
| `window` | `"00:00-06:00"` | `HH:MM-HH:MM` wall-clock times of `timezone`; the end may be earlier than the start (the window crosses midnight); equal times are refused |
| `timezone` | `"UTC"` | IANA name (`zoneinfo`); a DST change shortens or lengthens that night's window, the clock times stay |
| `beta_interval_h` | 48 (1-720) | a project's beta starts at most this often, measured from the start of its last beta job |
| `max_runtime_min` | 90 (5-1440) | past this a running image job raises `image_overrun:<project>`; it is never killed |
| `max_per_night` | 6 (1-50) | image jobs started per window instance (owner `now` starts do not count) |
| `env_allow` | see above | variable names a job keeps |

One image job runs at a time, in submission order. A job is released (op `image_next`) only inside the window, when fewer than `max_per_night` jobs started in this window, when its beta interval has passed, and when no owner hold or drain profile is active (`image` is one of the hold kinds, like `gate` and `compile`); a job the owner released with `now` skips the window and interval checks but still waits for the one running. A job that runs at the window end runs to its end; no new job starts after the end, the rest stay queued for the next night (state `deferred` when their planned start is a later night). A beta submitted inside the interval is refused (`refused`, with `next_allowed`: the first window moment after the interval), and so is a second queued beta of a project; a release has only the window.

*The runner.* `loadctl image runner` asks the daemon for the next job (`image_next`; refused for a caller inside a governed cgroup), runs it exactly like `loadctl run --kind image --project P --label L --key K -- argv` in the job's cwd with only the allowlisted environment (so the job gets its own scope in the project slice, the heavy slot, `job_start`/`job_end` and `job_stats`), reports the exit code and duration (`image_done`) and asks again while a job is runnable, then exits 0. `--once` runs at most one job. The slot wait and the headroom wait of a job released inside the window give up when the window ends (the runner passes `--start-by`; `loadctl run` then exits 77 and tells the runner in-process that nothing was launched, so a job that did run and exits 77 itself is a started, failed job): nothing is started, the job goes back to the queue and its start no longer counts for the beta interval or the night. A runner that dies leaves a `running` job behind; the daemon drops it (audited `image_done` `failed`) once the runner pid is gone, and does not run it again. `packaging/systemd/loadgov-image-runner.service` is a oneshot unit and `loadgov-image-runner.timer` starts it every 10 minutes: harmless when the queue is empty or the window is closed (the daemon releases nothing and the runner exits at once), and a job submitted at 02:00 does not wait for the next night.

*Heavy slot.* Kind `image` takes the `heavy` slot like `gate`, in the same FIFO queue behind whatever already waits, such as nightly gates; its headroom is the gate's (load, PSI, memory; `[headroom.kinds.image]` and per-project `image` tables can tune it). Never light. The `slots.heavy.waiters[]` and `jobs[]` entries show it with kind `image`.

*Estimates.* The planned start and finish are chained from the window open (or now when the window is open) with the median duration of the project's finished image jobs (`job_stats` for kind `image`), after the running job. Unknown stays unknown: a project without finished image jobs has a null `planned_finish`, and every start behind it is null too; an owner hold or drain makes all of them null. At most 20 queue entries are in the status (`image.queue`; `image.queued` is the full count), with `key` and `url` from `[keys]`. `loadctl status` prints `image train: 3 queued, next 00:40 (web-app beta)` when the queue is not empty (the time in the window's zone); an empty train prints `image train: empty, window 00:00-06:00 UTC, opens in 7 h` (or `..., open`) only while the window is open or opens within 12 hours, and nothing otherwise. A running image job is in the job list with its `eta`.

*Out-of-window builds* (observe only). Every 30 s outside the window the daemon looks at the processes the sampler already reads: a `docker`, `docker-compose`, `docker-buildx` or `buildctl` process whose arguments start a build (`build` or `bake` among the first sub-commands, not `run` or `exec`) and whose cgroup is not inside the scope of a running image job raises `image_out_of_window` (warning). The message names the process and sub-command and never the arguments (they can carry secrets). It is a deliberately small heuristic: an idle BuildKit daemon or container is no build activity and is not looked at, and a build started by something other than these clients goes unseen. The daemon never acts on it.

### Job usage

When a job ends (`job_end`, or the tick finds its scope empty) the daemon records what it used and writes it to the `job_end` audit line: `duration_s`; `cpu_s` (the scope's `cpu.stat` `usage_usec`); `mem_peak_mb` (`memory.peak`, absent when the kernel has none); `host_cpu_s` (root cgroup `usage_usec` over the job's lifetime); `docker_cpu_s` (Docker container CPU attributed to the job). A value the cgroup no longer answers for (ENOENT, ENODEV) falls back to the last sample (job start and end, and every 5 s while it runs); with no sample it is unknown and left out of the line and of the percentiles.

An image build's scope only holds the docker CLI, so `cpu_s` alone is misleading. `docker_cpu_s` integrates `usage_usec` deltas of every `/system.slice/docker-<id>.scope` over the job window, so containers that come and go are counted. Attribution per delta: (1) a job whose project matches the container (compose label `com.docker.compose.project` equal to the project, name prefix `<project>-`, or the project's `[memguard.projects.<p>] containers` regex; read from `/var/lib/docker/containers/<id>/config.v2.json`, no docker CLI; the Docker data root is assumed to be `/var/lib/docker`, and with another root every container counts as unreadable and attribution falls back to the heavy-slot rule) takes it, several matching jobs split it equally; (2) otherwise the job holding the heavy slot takes it (BuildKit builders, containers with an unreadable config). A project match always wins over the heavy-slot fallback, so two jobs never both claim the same CPU. CPU that nothing claims is dropped; CPU a container used between its last sample and its removal is not seen (up to the 5 s interval). Containers started by a job that is not a heavy-slot job and whose name does not match its project are not attributed.

`job_stats` in the status JSON (and `loadctl jobs --stats [--json]`, MCP `load_get_status`) keeps per (project, kind) the count and totals since the first record and percentiles over the last 200 jobs; it is saved in `state.json` and survives a restart. At most 64 (project, kind) pairs are kept: beyond that the least recently updated pair is evicted with its history.

### Project compute
`projects` shows which project uses compute right now. A project appears while it has a running job (`loadctl run`) or a slot waiter, and always when it is configured (`[projects.*]`, `[memguard.projects.*]`); an idle configured project reports zeros for `jobs` and `waiting`. At most every 5 s (like the usage meter) the daemon reads `cpu.stat` (`usage_usec`) and `memory.current` of the project's slice, read-only. The slice is the parent of a running job's scope (the real path from `/proc/<job pid>/cgroup`), else the systemd nesting rule under `/build.slice/build-work.slice` (`build-work-web-app.slice` is `.../build-work-web.slice/build-work-web-app.slice`; the name is sanitised like in `loadctl run`). `cpu_pct` is the `usage_usec` delta since the previous sample over the elapsed seconds (100 = one core). The Docker CPU that the usage meter attributed to the project's jobs in the same interval (see Job usage; no Docker scope is read again) is added, and shown alone as `docker_cpu_pct`. Docker CPU used between the last sample and the end of a job is not counted. A single 5 s `docker_cpu_pct` value can read about 0 or twice the true rate, because the extra meter samples forced at job start and end shift its phase; it averages out over a few samples. A slice counts its child slices, so a project `web` includes `web-app`. `loadctl status` prints a `projects` line, busiest first, without idle projects.

### Jobs and `loadctl run`

`loadctl run --kind K --project P --label L [--key KEY] [--headroom full|level|none] [--floor-mb N --on-floor kill] [--no-wait] -- cmd...` runs `cmd` in its own transient scope and stays its parent (exit code passed through; SIGINT and SIGTERM are forwarded to the scope with `systemctl kill`). Steps, in order:

1. `gate` and `image` take the `heavy` slot and `compile` the `compile` slot (`impl`, `review` and `light` none; a `light` job also gets the scope limits of The light lane), waiting in the FIFO queue (slot first, then the headroom wait).
2. `--headroom full` (default): headroom wait for kind `K` and project `P` while holding the slot: two passing samples, `confirm_s` apart (60 s; 30 s for a `gate` or `image` job that holds the slot), exactly like `loadctl wait`. `HEAVY_SLOT_*` and `HEADROOM_*` lines go to stderr so the job's stdout stays clean. Exit 1 when the wait times out. `--headroom level` waits (forever, polling every 15 s) until the hwmon level is `OK` on 2 samples (`BUSY` keeps waiting), as legacy `guarded` did (`grep -q 'level OK'`); `loadctl level-wait` and `hwmon wait` stay below `LIMIT`, like legacy `hwmon wait`; `--headroom none` takes the slot only, as legacy `rustc-slot.sh` did.
3. Launch (the job inherits the caller's descriptors like the legacy `exec` did, so cargo's jobserver pipes reach rustc; loadctl's own descriptors are close-on-exec; SIGINT and SIGTERM handlers are installed before the launcher starts and a signal that arrives before the scope exists is sent again once it does): `systemd-run --scope --quiet --collect --slice=build-work-<P>.slice --unit=loadgov-<K>-<label>-<UTC time>-<pid> -- nice -n 10 ionice -c 2 -n 7 cmd...`. A `gate` or `compile` job uses `ionice -c 3` instead while a `rustc` runs (as the legacy gate wrapper did). The slice name replaces characters other than letters, digits, `_` and `.` in `P` with `-`.
4. Environment of the job: the caller's, plus `CARGO_BUILD_JOBS` (effective `cargo_build_jobs`; a `gate` gets 3 only when `GATE_JOBS=3`, at least 8 GB are available and the effective value is not 1), `VITEST_MAX_WORKERS`, `GATE_SHARD_BUDGET` (both effective), `GATE_SHARD_THREADS` (the caller's, else 2) and `LOADGOV_JOB=<unit>.scope`.
5. `job_start` registers the job (listed in the status `jobs` section); `job_end` after it exits. If `job_end` never arrives, the job is dropped and its slot released when the scope's `cgroup.procs` is empty. A scope whose `cgroup.procs` cannot be read (not merely missing) counts as alive: the slot stays held and the file is read again every second. The job runs in its own session (`start_new_session`, like legacy `guarded`'s `setsid`), so a terminal Ctrl-C reaches only `loadctl run`, which forwards one signal through the scope.

For `gate` and `compile`, in either `enforce` mode, `loadctl run` opens the lock file itself without `O_CLOEXEC`, takes `flock` after the grant, writes the holder line and the job inherits the descriptor, so the lock lives as long as `loadctl run` or the job (or any process it leaves behind) lives, never only in the daemon. `job_end` frees the slot only when the scope is empty; otherwise the daemon frees it when `cgroup.procs` empties.

`--floor-mb N --on-floor kill` (used by the `guarded` shim): every 2 s `loadctl` reads `MemAvailable` (daemon status; `/proc/meminfo` when the daemon is down); below `N` MB it runs `systemctl kill --signal=SIGKILL <scope>` and `pkill -f` on `GradleDaemon`/`KotlinCompileDaemon` JVMs (nothing else), and exits 137.

Exit codes: the job's own (128+n when a signal n ended it), 137 floor kill, 1 headroom wait timed out, 2 usage, 3 daemon not reachable (nothing started), 5 daemon error, 75 `--no-wait` and the slot is busy or the host has no room, 76 a `light` job exceeded its runtime or memory limit (`LIGHT_OVERRUN`), 77 the hidden `--start-by` time (the image runner passes the window end) came before the job could start: nothing was started, 127 the launcher could not be started. A hidden `--slice` flag (tests) replaces the slice name.

systemd nests a slice under every dash-separated prefix of its name, so `build-work-web-app.slice` lives at `/build.slice/build-work.slice/build-work-web-app.slice` in the cgroup tree (`systemd-cgls`), and `build-work-web-app-ci.slice` below the implicit `build-work-web.slice` and `build-work-web-app.slice`. The daemon takes the real path from `/proc/<job pid>/cgroup`. A project without its own slice unit gets an implicit slice without accounting properties; `packaging/install.sh` writes `build-work-<project>.slice` for every project named in the config (`[projects.*]`, `[memguard.projects.*]`) and for each `--project`. systemd cannot template slice units (`build-work-@.slice` fails to load with `Invalid argument`), so the units are generated, not shipped.

`loadctl level-wait [max_s] [--poll S]` is `hwmon wait`: blocks until the hwmon level is below `LIMIT` on 2 samples (default max 1800 s, polling every 15 s), prints the five hwmon lines, exit 0; at the limit it prints `hwmon wait: still at LIMIT after N s` and exits 1.

`loadctl wait` takes `--load1-max --load5-max --psi-max --mem-min-gb --swap-max-mb --swap-in-max --mem-psi-max N`: each replaces the configured threshold for that call only; the profile scaling (load thresholds times the profile factor, `mem_min_gb` plus the RAM floor difference) is applied to the replacement as it is to a configured value.

**Issue keys.** `--key KEY` (also `loadctl slot acquire --key KEY`) names the work a job or queue item belongs to, for example the issue it implements. The client passes it; the daemon never derives it from the label. When `--key` is absent the environment variable `LOADGOV_KEY` is the default for both commands, so a wrapper script can set it once (an empty value counts as unset). A key matches `^[A-Za-z][A-Za-z0-9_]*-[0-9]+$` and is at most 64 characters; `loadctl` refuses a bad one with exit 2, and the daemon answers `invalid_args` to a `slot_acquire` or `job_start` whose `key` is malformed (`bad_request` when it is not a string). The key goes with the queue item (`slots.<slot>.waiters[].key`), then with the slot holder (`slots.<slot>.key`, saved in `state.json`; a state file without it loads with no key) and the job (`jobs[].key`), and into the `key` of the `request` of the `slot_acquire`, `job_start` and `job_end` audit lines. `loadctl run` also sends its kind with the slot request, so a waiter shows it. Link: `[keys] url` in the config (default empty: every `url` is null) is a template that contains `{key}`, for example `https://tracker.example/issues/{key}`; it must start with `http://` or `https://`, has no spaces and is checked at start and on reload (live, no restart). `url` is the template with `{key}` replaced by the URL-quoted key.

**Estimates.** `jobs[].expected_s` is the `duration_p50_s` of the job's (project, kind) pair in `job_stats` (the median of the last 200 finished jobs), null while there is none. `eta` is `since + expected_s` (`since` is the registration time of the job); `overdue` is true once the job has run longer than that, and then `eta` keeps its past value for consumers to show as "overdue". An estimate is never guessed: without statistics `expected_s`, `eta` are null and `overdue` is false.

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

- Anyone may **tighten** any parameter at any time. Tighter is per parameter: *lower* for `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers`, `max_reviewers`, `max_light` (and `cargo_build_jobs`, `vitest_max_workers`, `gate_shard_budget`), but *higher* for `ram_floor_gb`, because a higher floor makes the memory guard act earlier. **Loosening** is the opposite direction.
- A **human restriction** is active while any of these holds: an owner ceiling is set; an owner hold flag is set (`hold on`, see Slots) or a freeze flag (`freeze`, see Memory guard); the active profile was chosen by the owner and is tighter than the configured default profile (`balanced`) in at least one parameter, i.e. `silent`, `drain` or a tighter custom profile (until it expires or the owner switches to the default or a looser profile; switching to `balanced` or `boost` is not a restriction); an owner override tightened the value effective just before it was set, or is tighter than the active profile (until it expires, is replaced or `reset`). The *owner-only baseline* is the profile, plus owner overrides, capped by the ceiling; it ignores agent overrides.
- An agent **loosening** while a human restriction is active is refused, not clamped; nothing changes: `{"ok": false, "refused": true, "restrictedBy": "owner", "reason": "<the owner's reason>", "ceiling": "<profile or null>"}` (`loadctl` exit 4).
- Agent loosenings and tightenings expire and then reset: default `ttl` 2 h, maximum 12 h (a longer request is capped and the reply has `"ttl_capped": true`). Owner changes are indefinite unless `ttl` or `until` is given. Expiry restores the value that was effective before the override.
- Whenever an owner change leaves a restriction active, on every owner profile switch, and on every expiry that leaves a restriction active or restores a profile, every agent override looser than the owner-only baseline is cancelled at once (audit result `cancelled`), judged from the current state. An agent request is a loosening when it is looser than the owner-only baseline. Expiry of an owner restriction never brings a cancelled agent override back.
- An owner **ceiling** (a profile name) caps `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers`, `max_reviewers`, `max_light`, `cargo_build_jobs`, `vitest_max_workers` and `gate_shard_budget` at no more than that profile's values, and keeps `ram_floor_gb` at no less than it. `load_hold` is its own key and is never derived from `load_max`.

### Slots (heavy and compile)

Two slots arbitrate the legacy flock protocol: `heavy` (`/run/lock/host-heavy-slot.lock`, holder file `host-heavy-slot.holder`) and `compile` (`/run/lock/host-compile-slot.lock`, holder file `host-compile-slot.holder`; the lock name is `[slots] compile_lock` or the daemon's `LOADGOV_COMPILE_LOCK`, a plain `*.lock` file name, and the holder file is the same name with `.holder`). The holder line is the legacy format `<project> <label> pid=<holder pid> since=<UTC>`.

- **Non-blocking ops.** `slot_acquire` never blocks. With `wait: true` a client that gets `queued` calls `slot_acquire` again (every ~2 s, `loadctl` does this) until `granted`; the daemon matches the repeat by `(pid, project, label)`. FIFO: a free slot goes to the queue head, never to a later caller. A waiter that has not polled for 60 s, whose pid is gone or whose `timeout_s` has passed is dropped. `wait: false` answers `granted` or `busy`.
- **Queue view.** `slots.<slot>.waiters` lists the queue in FIFO order (`position` 1 is next). A waiter is published with `project`, `label`, `kind`, `key`, `url`, `pid`, `queued_since` (its first request; later polls do not change it), `wait_s`, `expected_s` and `eta`. `eta` is the **expected start**, computed from the slot's expected free time: now when the slot is free, the holder job's `since + expected_s` (statistics of its project and kind) while it is not overdue, so the first waiter's `eta` equals the holder job's `jobs[].eta` (`since` is the job's registration time; while the holder is granted but its job is not registered yet, the grant time is used); each waiter ahead then adds its own `expected_s`. Whatever is unknown makes the waiter's `eta` and every one behind it null instead of a guess: a hold on the slot, a legacy holder, a holder with unknown kind or statistics, an overdue holder, a waiter ahead without kind or statistics. The count `slots.<slot>.queue` is unchanged. The queue is not saved (see Restart), so after a restart the waiters appear again as they poll; `queued_since` then restarts at their first request to the new daemon.
- **Lifetime.** A granted slot is bound to the holder `pid` (default in `loadctl`: its parent shell). It stays held while that process is alive (the pid and its start time, field 22 of `/proc/<pid>/stat`, are recorded at the grant and saved, so a reused pid counts as exited); it is released by `slot_release` or on the first tick after the pid is gone (audit `released: holder exited`). A dropped socket connection does not release it. `SO_PEERCRED` only identifies the requester in the audit log. `loadctl run` re-binds the slot to its job scope with `job_start` (see Jobs): from then on the slot stays held while the scope's `cgroup.procs` is non-empty, even when `loadctl run` itself has died, and is released within a second (the daemon checks slot holders and scopes every 1 s) after the scope empties. A holder whose `loadctl run` is gone while its scope still runs raises one audit event `slot_holder_orphaned`.
- **Nested requests (re-entrant).** (A pid that holds the slot and asks for it again under another project or label gets a nested grant too; each `slot_release` ends the most recent nested grant of that pid first, and only when none is left does it release the real slot.) A `slot_acquire` for a slot whose loadgov job holder is an **ancestor** of the requester is granted at once, never queued behind it: `{"status": "granted", "nested": true, "holder": …}` without `lock_path`, `holder_path` or `holder_line`. A cargo build inside `loadctl run --kind compile`, whose `RUSTC_WRAPPER` shim runs `loadctl run --kind compile --headroom none`, is the case this exists for. The requester descends from the holder when the process chain of the claimed `pid`, or of the socket peer, from itself upwards (64 levels at most) contains the holder pid, the holder's job pid (`job_start`), each only with the start time recorded at the grant (a reused pid is not the holder), or any process inside the holder's job scope cgroup. A nested grant is bookkeeping only: it takes no flock and writes no holder line, because the real lock is already held by the ancestor's descriptor (`--fd` clients included: `loadctl slot release --fd N` never unlocks the descriptor after a nested release, since it may be the holder's). It is audited as op `slot_acquire`, result `nested`, with the reason naming the holder line and the ancestor; a repeat of the same `(pid, project, label)` is answered again without a second line. `slot_release` of a nested grant (`released: true, nested: true`, also what `loadctl run` does when its job ends) drops that grant only; the slot stays bound to the outer holder. The inner `loadctl run` still starts its command in its own scope under the project slice; `job_start` accepts it without binding the slot (`bound: false`, the job lists no `slot`). Only a loadgov `job` holder can be nested under: an owner or drain hold, a legacy holder or a different slot keeps today's behaviour (a gate inside a compile job still waits for `heavy`). If the outer holder ends while a nested job still runs, the slot is freed by the holder-gone rule above (the scope empties, or the holder pid is gone) and all nested bookkeeping of that slot is dropped with it, so nothing is released twice: the nested job's later `slot_release` answers `released: false`, and the slot may go to the next waiter while the nested job still runs (it holds no lock of its own). The tick also drops a nested grant whose process is gone, so a killed client leaks no count. Nested grants are not saved: after a daemon restart the count starts at 0. `slots.<slot>.nested` counts the live nested grants (`loadctl slot status` prints `nested: N`).
- **Stuck.** `stuck` is true only when the holder has held the slot for more than 45 min (`held_s`) **and** made no CPU progress for 10 min (`idle_s`); a healthy long gate that uses CPU is never stuck, so the `stuck_holder:<slot>` alert and its `cancel_holder` button (see Alerts) only appear for a hung holder. Progress is the CPU time of the holder's job scope (`usage_usec` in the scope's `cpu.stat`, read through the host filesystem) once `loadctl run` bound one, else the utime and stime (own and reaped children) of the holder pid's process tree from `/proc`; a legacy holder named by its holder line is watched the same way. The daemon reads it once a minute from the 1 s tick and keeps only the last reading and the time of the last progress per slot (nothing is saved: after a daemon restart the observation starts again, so `idle_s` counts from the restart). Each reading is compared with the previous one (about a minute apart): CPU seconds divided by the seconds between them below 1 % of one core is no progress, and the holder is idle only while every reading of the last 10 min is below that (one busy minute resets `idle_s`; this errs on the side of not calling a holder stuck, not a true 10-minute average). A falling total (processes ended) counts as progress. Docker CPU counts as progress too: the CPU of Docker scopes (`/system.slice/docker-*.scope`: BuildKit builds, compose test containers) that the usage meter attributed to the holder's job (see Jobs: project match, else the heavy-slot fallback) is added to the scope or process-tree CPU, using the meter's own deltas (no extra reads). A container of another project that is claimed by another job does not count. A holder without a registered job (a pure `--fd` shell gate) has no attributed Docker CPU, because the meter only runs for jobs: its progress is its process tree alone, so a quiet shell holder whose work runs in Docker can show stuck after 45 min and 10 idle minutes; start such gates through `loadctl run --kind gate` to get the attribution. Unreadable data (the cgroup or process is gone, a read error), an owner or drain hold and a free slot give `idle_s: null` and are never stuck. A legacy holder without a holder line (only a kernel lock, no pid) is never watched and never raises the stuck alert; one named by its holder line is watched through its pid tree.
- **Legacy holders.** If the real lock is held by a process loadgov did not grant (visible in `/proc/locks`, or a holder line whose pid is alive with the lock file open, or a failed `flock` under enforce), the slot is `held` with `source: legacy` and acquisition waits. A legacy lock is never broken.
- **Client locks always (either mode).** A client that holds the real lock itself replaces a legacy script that always took it: `slot_acquire` with `client_lock: true` (`loadctl slot acquire --fd N`, `loadctl run --kind gate|compile`, the `heavy-slot.sh` and `rustc-slot.sh` shims) gets `lock_path`, `holder_path` and `holder_line` in the `granted` reply whatever `enforce` says; the daemon probes the real lock before granting and the client takes the `flock` and writes the holder line, so a legacy `flock` user is excluded by loadgov clients and the other way round. Without `client_lock` and with enforce off the slot is arbitration-only (`WOULD` events). The CLI therefore refuses `loadctl slot acquire` (exit 2) without `--fd N` unless `--no-lock` is given; with `--no-lock` the line reads `HEAVY_SLOT_OK … (no lock: arbitration only)`.
- **Enforcement** (`[slots] enforce = false` in the config, default off). It governs what the daemon does by itself: the real lock of owner and drain holds and signals (`cancel_holder`). Off (for holds, and for clients without a client lock): the daemon arbitrates only among its own clients, never opens the lock files for locking, never writes holder files, and logs `WOULD flock <path>; WOULD write <holder file>: <line>` audit events (`slot_lock`, result `would`). On: the lock of a client holder lives with the client, like the legacy protocol (the gate shell holds fd 9), so a daemon crash or restart never frees a lock whose gate is still running. The shell opens the lock file (`exec 9>>/run/lock/host-heavy-slot.lock`) and calls `loadctl slot acquire heavy --project P --label L --fd 9`; the daemon probes that the real lock is free (taken and dropped at once) and grants; `loadctl` verifies that fd 9 is open on the slot's lock file (device and inode), takes `flock(9, LOCK_EX|LOCK_NB)` on that inherited descriptor (the shell's own open file description, so the lock outlives `loadctl` and the daemon) and writes the holder line. If a legacy process won the race, `loadctl` gives the slot back and keeps waiting. `slot_acquire` without `client_lock` is refused (`invalid_args`: enforce mode requires --fd N or loadctl run). `loadctl slot release --fd 9` unlocks the descriptor only when the daemon answers `released: true`; after lost bookkeeping (a restart that dropped the holder, a release refused as `owner_denied`) the shell keeps the lock until it exits, which is the safe direction. Only the holder (its pid, or the scope / `loadctl run` parent it was granted for) or an owner may release; anyone else gets `owner_denied` (audited as refused; `loadctl slot release --owner` is the owner claim). Releasing a pid that holds nothing stays a harmless `released: false`. The daemon keeps no flock for client holders; it flocks only for its own holds (owner hold, drain), which a daemon death releases (the Home Assistant side alerts on daemon down). The holder file is left in place like the legacy scripts do. With enforce off the `compile` slot sees legacy holders only through `/proc/locks` (the legacy compile script writes no holder line).
- **Restart.** Holders and holds are saved in `state.json` (`slots`). On start a holder whose process (pid and start time) is alive is re-adopted: only the bookkeeping is re-attached, the client's lock is untouched (audit `adopted`); a dead one is dropped (`released`). The queue is not saved: waiting clients re-poll.
- **Hold.** `hold on` (owner only, no TTL) marks the slots to be held: the daemon takes each as holder `home-assistant hold` as soon as it is free (never preempting the current holder, and ahead of the queue), no new grants are made, and the limits `hold` flag is set (a human restriction, so agent loosenings are refused). `hold off` releases the owner holds and clears the flag. `held_since` of a slot held by a hold is the time the daemon took it (the Home Assistant side can remind every 45 min from it). The drain profile (`hold_heavy_slot = true`) holds the `heavy` slot the same way while it is in force, with origin `profile`: it does not set the `hold` flag, `hold off` does not clear it, and leaving drain releases it.

### Daemon lifecycle

`loadgovd` runs as `Type=notify`. **Start:** `READY=1` is sent as soon as the socket listens and the saved state is restored (holders and paused work re-adopted, env shim written); the first sample and tick run after it, and `status` answers `starting` until that tick is done. The client library (`loadgov.cli.client.request`, used by every `loadctl` command and the MCP server) retries that reply every 0.5 s for up to 30 s, then fails as "daemon not reachable" (exit 3, so the compat shims fall back); a stale-sample `not_ready` is not retried there. Nothing slow (the `/proc` scan, `docker ps`, which runs in a worker thread and is cached for 5 s so the memory guard uses the last known container list and never waits for docker, the Home Assistant push, which is its own task) can therefore hold up the start. **Watchdog:** `WatchdogSec=120`; a small thread sends `WATCHDOG=1` every quarter of that, but only while the last tick that completed without an exception is at most 300 s old. A tick that is slow because the host thrashes keeps the daemon alive; a hung loop (a deadlock, an endless blocking call) or a persistently failing tick stops the keep-alive, and systemd restarts the daemon after at most about 300 s + 120 s (a restart re-adopts holders and holds from `state.json`). A single failing tick is logged and the loop carries on. **Tick order:** the memory guard runs right after the sample and the limits expiry, ahead of alerts, governor, backstop and strays.

**Reload.** `systemctl reload loadgovd` (SIGHUP) re-reads the same `--config` file. The handler only sets a flag; the main loop reloads between ticks. The file is parsed and validated first: on any error the running config stays untouched and the audit log gets op `reload refused` with the error. On success the new config goes to every engine at once and the audit log gets op `reload` listing the changed keys (dotted paths, e.g. `headroom.swap_max_mb`, `slots_enforce`). These settings are only read at start: a changed value is **not** applied, keeps its running value, and is audited as op `needs restart` with its keys: `default_profile`, `profiles.*`, `compile_lock`, `[rest]` (bind, port, allow, limits), `[ha]`, `compat.mirror_cgroups`, `memguard.projects.*` (a removed project would strand its paused entries). Everything else is live, including headroom thresholds and projects, memguard, reaper, strays, backstop, principal and the `enforce` flags. An `enforce` flag that goes off stops new actions at once; paused work is released by the existing resume rule (undoing a pause is always allowed). A backstop that goes off is rolled back by its next tick, as after a restart. `slots.enforce` changing with an owner or drain hold active brings the hold to the state a restart would give: the real flock is closed (on to off) or taken (off to on; pending while a legacy gate holds the lock). A missing or unreadable config file is `reload refused`. A default never changes. `loadgovd --check-config PATH` validates a file and exits 0, or prints the error and exits 1.

### Files written by the daemon

| Path | Content |
|---|---|
| `/var/lib/loadgov/history.csv` | The 15 s sample ring, about 7 days (see Sample history) |
| `/var/lib/loadgov/backstop.json` | Marker that the backstop wrote limits to a slice, so a later rollback can write `max` back (see Memory backstop); only exists between the first limit write and the rollback |
| `/var/lib/loadgov/state.json` (`--state-dir`) | Profile, overrides, ceiling, hold/freeze, slot holders (`slots`), running `loadctl run` jobs (`jobs`), the paused registry (`memguard`); atomic, restored on start, expired entries dropped on start and every tick |
| `/var/log/loadgov/audit.jsonl` (`--log-dir`) | One JSON line per decision: `ts`, `op`, `principal`, `pid`, `cgroup`, `cmdline` (200 chars), `project`, `reason`, `request`, `result` (`accepted`, `refused`, `expired`, `cancelled`), `before`, `after` (effective limits). Rotated by size (`[audit] max_mb`, default 50, 0 = off; `keep`, default 5): before a line that would exceed `max_mb`, `audit.jsonl` becomes `audit.jsonl.1`, `.1` becomes `.2` and so on, the file past `keep` is deleted. A reader that needs the full history reads `audit.jsonl.<keep>` ... `.1`, then `audit.jsonl` (oldest first); the `log` request reads across the rotation. A rotation error is logged once and appending continues |
| `/run/lock/load-governor.env` (`--env-file`, `''` disables) | `KEY=value` lines for shell `source`, rewritten atomically whenever the effective limits change: `MAX_CORES TARGET_PCT LOAD_MAX LOAD_HOLD IMPL_MAX IMPL_MAX_QUIET MEM_MIN_GB LOW_GB CARGO_BUILD_JOBS VITEST_MAX_WORKERS GATE_SHARD_BUDGET PROFILE OWNER_CEILING UPDATED_AT` |

Static profiles come from `/etc/loadgov/loadgov.toml` (`--config`; a missing file means the built-in defaults), see `packaging/config/loadgov.toml.example`.

## REST

An optional HTTP/1.1 listener for Home Assistant, off unless `[rest] enabled = true`. Stdlib `asyncio`, one request per connection (`Connection: close`), JSON bodies only. Limits: header block 8 KiB and body 16 KiB (`413`), malformed request `400`, 10 s read timeout (`408`).

Every REST caller is the **owner** with `source=rest`; requests go through the same code as the socket ops, so the restriction and cancellation rules above apply unchanged. Audit lines carry `principal=owner`, `source=rest` and `peer` (the caller's address; `pid`, `cgroup` and `cmdline` are null).

| Method and path | Body | Same as |
|---|---|---|
| `GET /v1/status` | none | socket `status` (the same `{"ok": true, "status": {…}}` reply) |
| `POST /v1/set` | `set` args without `owner`: `profile`, `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers`, `max_reviewers`, `max_light`, `ram_floor_gb`, `ttl` or `until`, `reason` (optional here), `project` | `set` as owner |
| `POST /v1/ceiling` | `{"profile", "reason", "ttl"/"until"}` or `{"clear": true, "reason"?}` | `ceiling_set` / `ceiling_clear` |
| `POST /v1/reset` | `params` or `all: true`, `reason` | `reset` |
| `POST /v1/cancel-holder` | `{"slot": "heavy"\|"compile", "confirm": true, "reason"?}` | `cancel_holder` as owner; default reason `REST cancel-holder from <peer>` |
| `POST /v1/action` | `{"action": "profile=silent", "reason"?}` (any profile name) | `set` with that profile as owner; default reason `REST action <action>` |
| `POST /v1/freeze` | `{"project"?, "ttl"?, "reason"?}` | `freeze` as owner; default reason `REST freeze from <peer>`; no owner-change notification |
| `POST /v1/unfreeze` | `{"project"?, "reason"?}` | `unfreeze` as owner; a memory-guard pause below its resume threshold is refused (`409`, `restrictedBy: "system"`) |
| `POST /v1/reap` | `{"reason"?}` | `reap` as owner (audit `source=rest`) |
| `POST /v1/stray/kill` | `{"pid", "start_time"?, "reason"?}` | `stray_kill` as owner (audit `source=rest`; refusals are `409`); default reason `REST stray kill from <peer>`; see Stray guard |
| `POST /v1/hold` | `{"on": bool, "slot": "heavy"\|"compile"\|"all" (default all), "reason"?}` | `hold` as owner; default reason (on) `REST hold from <peer>`; no owner-change notification |
| `POST /v1/image/now` | `{"id": int, "reason": str}` (reason required, non-empty) | `image_now` as owner (audit `op=image_now`, `source=rest`); `404` unknown id, `409` refused (the job is running), `400` bad body; no owner-change notification; see The image train |
| `POST /v1/image/cancel` | `{"id": int}` | `image_cancel` as owner (audit `source=rest`); `404` unknown id, `409` running job; the image queue itself is read from `/v1/status` (`image`) |

`reason` is optional on `set`, `ceiling` and `reset` over REST (the socket still requires it); when omitted the audit reason is `REST <set|ceiling|reset> from <peer>`. Replies are the socket replies as JSON. HTTP status: `200` ok, `400` bad JSON, wrong types, unknown fields or `invalid_args`, `409` refused (`refused: true`), `503` `not_ready`, `500` internal. Unknown path `404`, wrong method `405`; an unknown image job id is `404` (`not_found`).

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
| `stuck_holder:heavy`, `stuck_holder:compile` | a `loadgov` or legacy holder (not an owner hold) has held the slot for more than 45 min **and** made no CPU progress for the last 10 min (see Slots, Stuck); a long hold that still uses CPU is never alerted | `warning` | `cancel_holder` |
| `disk` | a watched path crosses `[levels] disk_busy` (default: below 25 GB free): `warning`, or `disk_limit` (default: below 10 GB free): `critical`. Each threshold is GB free or `"NN%"` used and is checked on its own; the worst path wins and the message names the threshold that fired, e.g. `disk / 81 % used >= 80 %` or `disk / 9.5 GB free < 10 GB`. The alert only observes: the configured thresholds never change `level` or `levels.disk`, so disk never delays or refuses a gate or compile start | `warning` / `critical` | `prune_caches` |
| `pause`, `abort`, `low_memory`, `resume`, `reap`, `stuck_paused` | raised by the memory guard through `Daemon.emit_event(kind, payload)` (enforce mode only; the shadow writes audit lines, not pushes). `resume` (info) also covers `RELEASED`, `reap` (warning) the JVM reaper, `stuck_paused` (critical, once per entry) an orphaned entry whose tree could not be thawed after 10 rounds (the entry is kept and thawed again every 60 s). Not deduped (the guard rate-limits itself). `payload` may carry `title`, `message`, `actions`, `top3`, `avail_mb`, `swap_mb` | `warning` (`pause`), `critical` | `unfreeze` for `pause` (label "Resume paused workers") |
| `light_overrun:<project>` | 3 light jobs of one project hit their runtime or memory limit (`job_end` with `overrun`) within 24 h; the message names the project and the latest labels. It stays active until 24 h after the last overrun, then `resolved` follows. The count lives in memory: a daemon restart resets it to zero | `warning` | |
| `image_overrun:<project>` | an image job of the project has run longer than `[image] max_runtime_min` (default 90 min, measured from its registration). The job is never stopped; the alert resolves when it ends | `warning` | |
| `image_out_of_window` | container build activity outside the image window that is no running image job (see The image train); the message names the process and sub-command (`docker buildx build`), never the arguments. Observe only | `warning` | |
| `stray:<pid>` | a stray process (see Stray guard); raised once per (pid, start time) through `Daemon.emit_event("stray", payload)` with `payload.key = "stray:<pid>"`. `resolved` follows when it is gone or no longer a stray. After an owner kill: an `info` event `stray_killed:<pid>` (title "Stray process killed", `message` names command, project, signal, result and who asked) and `resolved` for `stray:<pid>`. Not deduped (once per process) | `warning` | `kill_stray:<pid>:<start_time>` (label "Kill stray process") |

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

**Thresholds.** LOW = the effective `ram_floor_gb` (2.0 to 6.0 GB) in MB; RESUME = `max(6144, LOW + 2048)`, so the resume level is always at least 2 GB above the pause level and a resume cannot be followed by an immediate pause (a floor outside 2.0 to 6.0 is clamped); CRIT = 1500 MB. Pressure = `MemAvailable < LOW`, or swap used grew more than 768 MB over the calm baseline. The baseline only moves down, follows swap after 120 calm samples (10 min), and is reset to the current swap by a pause, an abort and a resume. A project in `[memguard.projects.<name>]` has its own rule for its own work (`low_mb`, `high_mb` (at least 2048 MB above `low_mb`; else the config is refused), `swap_mb`: also pressure when swap used is above it while available is below `high_mb`, `resume_samples`, `containers`: allow-regex of container names, `agent_cwd`: an absolute path or a non-empty list of them; `claude -p` sessions whose cwd equals one of them or lies below it (path boundary: `/srv/proj` does not match `/srv/project`) belong to it). There are no built-in projects (`low_mb` and `high_mb` are required, e.g. 2048 / 4096 MB, `swap_mb` 2048, `resume_samples` 12 = a minute at 5 s); every project not listed uses the global rule.

**Decision (one action per sample; 15 s of quiet after a pause or abort, 30 s after a resume).** Under pressure, in this order: (1) JVM build daemons outside every registered job scope are reaped first; (2) the largest tree of the first candidate class that has one is paused: (a) registered `loadctl run` job scopes by `memory.current`: an agent-free `gate` or `compile` scope (no `claude` process in it) is frozen with `cgroup.freeze`; an `impl` or `review` scope (or any scope with a `claude` in it) gets `SIGSTOP` on every process of the scope tree except `claude` and its ancestors (the agent is never frozen and keeps its connection; the nested-child-scope variant is not used); (b) legacy implementer trees: the descendants of the `claude` or `timeout` child of a snapshot shell (`bash <prefix>*` whose file has a line starting with the marker, both from `[memguard] legacy_snapshot = { prefix, marker, project }`; off unless set) and the descendants of `claude -p` sessions whose cwd is below a project's `agent_cwd`, minus every `claude`; (c) containers whose name matches a project's allow-regex, `docker pause`d, never a name matching `[memguard] deny_containers` (default `*-test-db*`, `*-review-*`). Trees whose largest `rustc` has 2048 MB or more are skipped (a stopped rustc keeps its memory). When only such trees are left: `HOLD` (logged first and every minute) while available memory is 1500 MB or more, below it `ABORT`: `SIGTERM` to the largest `rustc` only (never the agent, never `cgroup.kill` on an `impl` scope), and nothing else is touched. Nothing outside these classes is ever paused or stopped. Below 1536 MB every JVM build daemon is reaped, whatever else happens (system).

**Critical abort in the reaping sample.** The system reap below 1536 MB does not use up the sample for the abort: when the reap happens and the only candidate is a large compile (no tree can be paused) and `MemAvailable` is below 1500 MB, the largest `rustc` is `SIGTERM`ed in the same sample, after the reap lines (both `WOULD` with enforce off). The abort does not wait for the daemons' memory to come back (no re-read, no sleep in the tick): a daemon frees hundreds of MB, a multi-GB compile is what drives the host into thrashing, and a SIGTERM is cheap. Fall-rate guard: when `MemAvailable` fell by 512 MB or more since the previous sample (at most 15 s old) and the same fall again would end below 1500 MB at the next sample (5 s later), the sample counts as critical and the compile is aborted although `MemAvailable` is still 1500 MB or more (audit text `falling fast, <n>MB projected below 1500MB within 5s`). The 15 s quiet after an abort applies; with a pausable tree the pause is still preferred and an abort never follows a pause. Example: at 1800 MB only HOLD; at 650 MB the JVM daemon is reaped and the large `rustc` aborted in the same sample.

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

## Stray guard

`engines/strays.py` (detection) and `daemon/stray_ops.py` (events, the kill) find processes that outlived their agent and burn CPU outside every governed scope. Config `[strays]` (`packaging/config/loadgov.toml.example`): `enabled` (true; detection only, read-only), `enforce` (false; lets the owner kill send signals, AGENTS.md rule 2), `check_s` (300, at least 60), `min_age_s` (1800, at least 0), `min_cpu_pct` (50, percent of one core, above 0), `exclude_cgroups` (`[]`, `fnmatch` globs on the cgroup path, each starting with `/` or `*`). `[strays] enforce` is not one of the four `daemon.enforce` flags and does not change `daemon.mode`; it is `strays.enforce`.

- **Check.** On the daemon tick, once `check_s` has passed since the last check (the first tick takes the baseline), the guard reads `/proc` through the existing helpers (`Sampler.proc_ticks`, `host/proctree.py`). A process's CPU % over the interval since the previous check is `100 * (utime + stime delta) / CLK_TCK / seconds`, keyed by (pid, start time = stat field 22), so a reused pid is a new process and a process first seen at this check has no interval yet. A process is **hot** while that share is at least `min_cpu_pct`. `hot_since` is the start of the unbroken run of hot intervals it is in (the previous check, for a process that became hot at this one); a cool interval, or a missing process, resets it. The state is not saved: a daemon restart starts the runs again.
- **Stray.** A process hot for at least `min_age_s` is a stray when all of these hold: it is in no registered job scope (`jobs[].cgroup`, nested cgroups included); its cgroup is not under `/system.slice/`, not under `/init.scope`, not a docker, containerd or libpod scope (a path segment `docker`, `containerd`, `cri-containerd`, `crio` or `libpod`, alone or followed by `-<id>`, with or without `.scope`) and matches no `exclude_cgroups` glob; it is not a kernel thread (empty cmdline or ppid 2); its command name is not `claude` and it is not `loadgovd`; and it is **orphaned**: no live `claude` among its ancestors, and (its ppid is 1, or `CLAUDE_PID` in its environment names a pid that is dead or is not a `claude`). `/proc/<pid>/environ` is read as root; an unreadable one counts as empty. An unreadable cgroup is not a stray (it cannot be checked).
- **Record.** Each stray is `{pid, start_time, comm, cmdline (200 characters), cwd, cgroup, project, cpu_pct, hot_since, age_s, orphan, session}`. `project` is the `[memguard.projects.<name>]` whose `agent_cwd` holds the cwd (null when none does); `session` is `{claude_pid, session_id}` from the environment variables `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID` (null when absent). The list is the result of the last check (`strays.last_check`).
- **Events.** A new stray (pid, start time) raises one `stray:<pid>` alert with the action `kill_stray:<pid>:<start_time>`; it is resolved when it is gone or no longer a stray. Both are audited (`op=stray`, `principal=system`, `result` `detected` / `resolved`). A check signals nothing.
- **The kill (owner only, never automatic).** `stray_kill` / `POST /v1/stray/kill` (the REST caller is the owner). The daemon re-verifies the target at that moment: (pid, start time) is in the current list, `/proc/<pid>/stat` still has that start time and command name, it is not `claude`, and the stray conditions hold again. Then, with `[strays] enforce = true`, it sends `SIGTERM` through a pidfd opened before the start-time re-check (`host/signals.py`; pid 1, the daemon and `claude` are refused there too) and, on the tick 30 s later, `SIGKILL` if the process is still alive. With enforce off nothing is signalled: the reply is `refused` and the audit line `WOULD KILL stray ...`. Every request is audited (`op=stray`, `principal=owner`, `result` `applied`, `refused`, `would` or `failed`; the 30 s follow-up `applied` or `terminated`). The phone gets the `stray_killed:<pid>` info event at the `SIGTERM` and a second one with the end result after the 30 s.
- **Status and clients.** The `strays` object in the status (below); `loadctl stray [--json]` lists the strays (`no stray processes` when none), `loadctl status` prints `strays: N` only when N > 0. The MCP server shows `strays` in `load_get_status` and has no kill tool: agents must not kill.

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
| `cpu.psi_effective` | number | CPU PSI that counts: `cpu.psi_some_avg60` when `cpu.busy_pct >= [headroom] psi_busy_gate_pct`, else `0` (throttling raises PSI on idle cores); used by the levels and headroom |
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
| `swap.in_rate` | number | Pages swapped in per second (`pswpin` in `/proc/vmstat`), averaged over about 60 s; 0 while unknown (first sample, counter reset, no `/proc/vmstat`) |
| `swap.out_rate` | number | Pages swapped out per second (`pswpout`), same window |
| `io.psi_some_avg10` | number | `/proc/pressure/io` some avg10 |
| `io.psi_some_avg60` | number | `/proc/pressure/io` some avg60 |
| `io.psi_full_avg60` | number | `/proc/pressure/io` full avg60 |
| `disk.free_gb` | number | Lowest free space over `disk.paths`, GB |
| `disk.total_gb` | number | Size of the path with the lowest free space, GB |
| `disk.used_pct` | number | Used % (as `df`) of the worst path: the one with the worst disk level; with none over a threshold, the one with the least free space |
| `disk.paths` | list | Watched mount points |
| `disk.per_path` | list | One object per watched path |
| `disk.per_path[].path` | string | Mount point |
| `disk.per_path[].free_gb` | number | Free space available to unprivileged users, GB |
| `disk.per_path[].total_gb` | number | Filesystem size, GB |
| `disk.per_path[].used_pct` | number | Used % of the filesystem (as `df`) |
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
| `heavy_slot.stuck` | bool | The same value as `slots.heavy.stuck` (the daemon replaces the sampler's age-only value): held longer than 45 minutes and no CPU progress for 10 minutes |
| `slots.heavy.state` | string | `free`, `held` (a loadgov job or a legacy holder) or `held-by-hold` (owner or drain hold) |
| `slots.heavy.holder` | string or null | Holder line (`<project> <label> pid=<pid> since=<UTC>`); null when free or a legacy holder is unnamed |
| `slots.heavy.source` | string or null | `loadgov` or `legacy` while held |
| `slots.heavy.queue` | int | Waiters in the FIFO queue |
| `slots.heavy.nested` | int | Live nested grants (requests from descendants of the holder, see Slots) |
| `slots.heavy.held_since` | string or null | Since when the current holder holds (UTC) |
| `slots.heavy.held_s` | int or null | Seconds the current holder has held the slot; null when free or the start is unknown |
| `slots.heavy.idle_s` | int or null | Seconds since the holder last made CPU progress (at least 1 % of one core between two samples, 1 min apart; counted from the first sample the daemon took of this holder); null when free, an owner hold, or the CPU data is unreadable |
| `slots.heavy.stuck` | bool | `held_s` above 45 minutes **and** `idle_s` of 10 minutes or more; false when `idle_s` is null (unknown progress is not stuck) |
| `slots.heavy.hold` | string or null | Reason of a hold on this slot (pending or taken) |
| `slots.heavy.hold_origin` | string or null | Who set that hold: `owner` (`hold on`) or `profile` (the drain profile); null without a hold |
| `slots.heavy.key` | string or null | Issue key of the current holder (from `--key` or `LOADGOV_KEY`, see Jobs); null when free, a legacy holder, a hold, or the holder passed none |
| `slots.heavy.url` | string or null | The key's link: `[keys] url` with `{key}` replaced by the URL-quoted key; null without a key or a template |
| `slots.heavy.waiters` | list | The waiting clients in FIFO order, front first (see Slots); empty when none. `queue` is its length |
| `slots.heavy.waiters[].position` | int | 1 is next in line |
| `slots.heavy.waiters[].project` | string | Project of the request |
| `slots.heavy.waiters[].label` | string | Label of the request |
| `slots.heavy.waiters[].kind` | string or null | `impl`, `review`, `gate`, `compile`, `light` or `image` when the client said so (`loadctl run` always does), else null |
| `slots.heavy.waiters[].key` | string or null | Issue key the client passed, else null |
| `slots.heavy.waiters[].url` | string or null | Link of the key, as `slots.heavy.url` |
| `slots.heavy.waiters[].pid` | int | The waiting client's holder pid |
| `slots.heavy.waiters[].queued_since` | string | Time of the first request of this waiter, UTC (polling does not change it) |
| `slots.heavy.waiters[].wait_s` | int | Seconds since `queued_since` |
| `slots.heavy.waiters[].expected_s` | number or null | Median duration (`duration_p50_s`) of finished jobs of this project and `kind`; null without a kind or statistics |
| `slots.heavy.waiters[].eta` | string or null | Expected **start** time, UTC: when the slot is expected free plus the `expected_s` of every waiter ahead. Null when anything it depends on is unknown: a hold or a legacy holder, a holder without statistics or past its estimate (overdue), or an unknown `expected_s` ahead |
| `slots.compile.state` | string | As `slots.heavy.state`, for the compile slot's lock (`/run/lock/host-compile-slot.lock` by default) |
| `slots.compile.holder` | string or null | As `slots.heavy.holder` |
| `slots.compile.source` | string or null | As `slots.heavy.source` |
| `slots.compile.queue` | int | As `slots.heavy.queue` |
| `slots.compile.nested` | int | As `slots.heavy.nested` |
| `slots.compile.held_since` | string or null | As `slots.heavy.held_since` |
| `slots.compile.held_s` | int or null | As `slots.heavy.held_s` |
| `slots.compile.idle_s` | int or null | As `slots.heavy.idle_s` |
| `slots.compile.stuck` | bool | As `slots.heavy.stuck` |
| `slots.compile.hold` | string or null | As `slots.heavy.hold` |
| `slots.compile.hold_origin` | string or null | As `slots.heavy.hold_origin` |
| `slots.compile.key` | string or null | As `slots.heavy.key` |
| `slots.compile.url` | string or null | As `slots.heavy.url` |
| `slots.compile.waiters` | list | As `slots.heavy.waiters` |
| `slots.compile.waiters[].position` | int | As `slots.heavy.waiters[].position` |
| `slots.compile.waiters[].project` | string | As `slots.heavy.waiters[].project` |
| `slots.compile.waiters[].label` | string | As `slots.heavy.waiters[].label` |
| `slots.compile.waiters[].kind` | string or null | As `slots.heavy.waiters[].kind` |
| `slots.compile.waiters[].key` | string or null | As `slots.heavy.waiters[].key` |
| `slots.compile.waiters[].url` | string or null | As `slots.heavy.waiters[].url` |
| `slots.compile.waiters[].pid` | int | As `slots.heavy.waiters[].pid` |
| `slots.compile.waiters[].queued_since` | string | As `slots.heavy.waiters[].queued_since` |
| `slots.compile.waiters[].wait_s` | int | As `slots.heavy.waiters[].wait_s` |
| `slots.compile.waiters[].expected_s` | number or null | As `slots.heavy.waiters[].expected_s` |
| `slots.compile.waiters[].eta` | string or null | As `slots.heavy.waiters[].eta` |
| `jobs` | list | Jobs started by `loadctl run` that are still running |
| `jobs[].kind` | string | `impl`, `review`, `gate`, `compile`, `light` or `image` |
| `jobs[].project` | string | Project of the job |
| `jobs[].label` | string | Label of the job |
| `jobs[].unit` | string | The scope unit, `loadgov-<kind>-<label>-<time>-<pid>.scope` |
| `jobs[].cgroup` | string | cgroup v2 path of the scope |
| `jobs[].pid` | int | The job process |
| `jobs[].slot` | string or null | `heavy` or `compile` while the job holds that slot |
| `jobs[].since` | string | Registration time, UTC |
| `jobs[].key` | string or null | Issue key the client passed (`loadctl run --key`, `LOADGOV_KEY`), else null |
| `jobs[].url` | string or null | Link of the key (`[keys] url`), else null |
| `jobs[].expected_s` | number or null | `duration_p50_s` of the job's project and kind in `job_stats`; null without statistics |
| `jobs[].eta` | string or null | `since` plus `expected_s`, UTC; null when `expected_s` is null. It stays the past time when the job is overdue |
| `jobs[].overdue` | bool | The job has run longer than `expected_s`; false when `expected_s` is null |
| `light.running` | int | Registered jobs of kind `light`, host-wide |
| `light.max` | int | Effective `max_light` (the profile limit) |
| `light.per_project_max` | int | `[light] max_light_per_project` |
| `light.cpu_quota_pct` | int | `[light] cpu_quota_pct`, applied by `loadctl run --kind light` |
| `light.memory_max_mb` | int | `[light] memory_max_mb` |
| `light.max_runtime_s` | int | `[light] max_runtime_s` |
| `image.window` | string | `[image] window`, local `HH:MM-HH:MM` in `image.timezone` |
| `image.timezone` | string | `[image] timezone` (IANA name) |
| `image.in_window` | bool | The window is open now |
| `image.next_window_start` | string or null | Next window start strictly after now, UTC |
| `image.max_runtime_min` | int | `[image] max_runtime_min` |
| `image.beta_interval_h` | int | `[image] beta_interval_h` |
| `image.max_per_night` | int | `[image] max_per_night` |
| `image.env_allow` | list | `[image] env_allow`: the variable names `loadctl image submit` sends and the daemon keeps |
| `image.queued` | int | Jobs waiting (not the running one); the `queue` list is cut to 20 entries |
| `image.running` | int | 1 while an image job is released to the runner, else 0 |
| `image.queue` | list | The running job first, then the waiting ones in submission order (at most 20) |
| `image.queue[].id` | int | Job id (counts up, saved in `state.json`) |
| `image.queue[].position` | int | 1-based place in this list |
| `image.queue[].project` | string | Project |
| `image.queue[].label` | string | Label |
| `image.queue[].key` | string or null | Issue key the submitter passed |
| `image.queue[].url` | string or null | Link of the key (`[keys] url`), else null |
| `image.queue[].channel` | string | `beta` or `release` |
| `image.queue[].submitted` | string | Submission time, UTC |
| `image.queue[].planned_start` | string or null | Planned start, UTC; the actual start for the running job; null when unknown (see The image train) |
| `image.queue[].planned_finish` | string or null | `planned_start` plus the project's median image duration; null when unknown |
| `image.queue[].state` | string | `running`, `now` (released by the owner), `queued` (planned for the first window instance or unknown) or `deferred` (planned for a later night) |
| `job_stats` | list | What finished jobs used, one entry per project and kind (empty until a job ends); see Job usage below |
| `job_stats[].project` | string | Project |
| `job_stats[].kind` | string | `impl`, `review`, `gate`, `compile`, `light` or `image` |
| `job_stats[].count` | int | Jobs recorded since the first one |
| `job_stats[].duration_sum_s` | number | Sum of their durations, s |
| `job_stats[].duration_p50_s` | number or null | Median duration of the last 200 jobs, s |
| `job_stats[].cpu_total_s` | number | Sum of `cpu_s` (the jobs' own scopes), s |
| `job_stats[].cpu_p50_s` | number or null | Median `cpu_s` of the last 200 jobs |
| `job_stats[].cpu_p95_s` | number or null | 95th percentile of `cpu_s` |
| `job_stats[].docker_cpu_total_s` | number | Sum of `docker_cpu_s`, s |
| `job_stats[].docker_cpu_p50_s` | number or null | Median `docker_cpu_s` |
| `job_stats[].docker_cpu_p95_s` | number or null | 95th percentile of `docker_cpu_s` |
| `job_stats[].total_cpu_p50_s` | number or null | Median of `cpu_s + docker_cpu_s` (`total_cpu_s`) |
| `job_stats[].total_cpu_p95_s` | number or null | 95th percentile of `total_cpu_s` |
| `job_stats[].host_cpu_p50_s` | number or null | Median `host_cpu_s` (whole-host CPU during the job) |
| `job_stats[].mem_peak_p50_mb` | number or null | Median `memory.peak` of the scope, MB |
| `job_stats[].mem_peak_p95_mb` | number or null | 95th percentile of the peak, MB |
| `job_stats[].mem_peak_max_mb` | number or null | Largest peak since the first record, MB |
| `projects` | object | CPU and memory per project, keyed by project name (at most 64: projects with a running job or a waiter first, then configured ones); see Project compute below |
| `projects{}.cpu_pct` | number or null | CPU of the project's slice over the last sampling interval, in % of one core (250 = 2.5 cores), plus `docker_cpu_pct`; null on the first sample, an unreadable `cpu.stat` or a counter reset |
| `projects{}.cores` | number or null | `cpu_pct / 100`, 2 decimals, or null |
| `projects{}.docker_cpu_pct` | number | The Docker part of `cpu_pct` alone (0 when none) |
| `projects{}.mem_mb` | int or null | `memory.current` of the slice, MB, or null when unreadable |
| `projects{}.jobs` | int | Running jobs of the project |
| `projects{}.waiting` | int | Slot waiters of the project (both slots) |
| `projects{}.slice` | string | cgroup path of the slice that was read |
| `cgroups` | object | `cpu.max` per watched cgroup (each `[compat] mirror_cgroups` entry under its own path; `build-work.slice`, which systemd places at `/sys/fs/cgroup/build.slice/build-work.slice`): `{cpu_max, cores}` (`cores` null = unlimited), or null when the cgroup does not exist |
| `stopped` | list | Processes in state `T` under a mirrored cgroup or `/build.slice/build-work.slice` |
| `stopped[].pid` | int | Process id |
| `stopped[].comm` | string | Command name |
| `stopped[].cgroup` | string | cgroup v2 path |
| `levels.cpu` | string | hwmon level `OK` / `BUSY` / `LIMIT` (LIMIT only when PSI or load agree; PSI is `cpu.psi_effective`) |
| `levels.ram` | string | hwmon level for `memory.avail_mb` |
| `levels.swap` | string | hwmon level for `swap.used_pct` |
| `levels.disk` | string | hwmon level for the least free space over `disk.paths`: BUSY at or below 25 GB, LIMIT at or below 10 GB free. Fixed; part of `level` |
| `levels.disk_alert` | string | Worst level over `disk.per_path` against `[levels] disk_busy` / `disk_limit` (GB free or % used). Not part of `level`; equal to `levels.disk` with the defaults |
| `level` | string | Worst of `levels.cpu`, `ram`, `swap` and `disk` (never `disk_alert`) |
| `limits.profile` | string | Active profile (`silent`, `balanced`, `boost`, `drain`) |
| `limits.ceiling` | string or null | Profile name of the owner ceiling, or null |
| `limits.ceiling_info` | object or null | The active ceiling: `{profile, reason, principal, set_at, expires_at}` (`expires_at` null = no expiry), or null. Lets a client tell its own ceiling from a human's by the `reason` |
| `limits.overrides` | list | Active overrides, oldest first: `{param, value, principal, reason, set_at, expires_at}` (`expires_at` null = none). Lets a client see which limits somebody set by hand before it sets its own |
| `limits.profile_info` | object or null | Who chose the active profile: `{profile, reason, principal, set_at, expires_at}`, or null when it is the default or was restored after a temporary profile expired |
| `limits.restricted` | bool | A human restriction is active (agent loosenings are refused) |
| `limits.restricted_by` | string or null | `owner` while restricted |
| `limits.freeze` | string or null | The reason of the owner's `freeze` flag (a human restriction), null when none |
| `limits.effective` | object | Effective limits: the seven tunables (including `max_reviewers` and `max_light`) plus `cargo_build_jobs`, `load_hold`, `impl_max_quiet`, `vitest_max_workers`, `gate_shard_budget`, `hold_heavy_slot` |
| `rest.enabled` | bool | `[rest] enabled` |
| `rest.state` | string | `up` (listening), `down` (enabled but not listening; see `rest.error`) or `disabled` |
| `rest.error` | string or null | Why the listener is down (bind failure, token file problem), or that the host's address table is unreadable (requests refused); null otherwise |
| `rest.bind` | string or null | Configured bind address; null while `[rest]` is not configured |
| `rest.port` | int | Listening port (the configured port while down) |
| `alerts.active` | list | Alerts whose condition holds now (see Alerts) |
| `alerts.active[].key` | string | `cpu`, `memory`, `swap`, `stuck_holder:<slot>`, `disk`, `light_overrun:<project>`, `image_overrun:<project>` or `image_out_of_window` |
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
| `memguard.containers_age_s` | number or null | Age of the container list the guard last used (`docker ps` is cached, refreshed at most every 5 s); `null` before the first list or when no project has a `containers` rule |
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
| `backstop.max_events` | int | `memory.events` `max` hits (cgroup reclaim at `memory.max`) in `build-work.slice` since the daemon started |
| `backstop.swap_fail_events` | int | `memory.swap.events` `fail` hits (swap allocation refused at `memory.swap.max`) in `build-work.slice` since the daemon started |
| `backstop.last_event` | string or null | `<UTC time> <scope> peak <n> MB` of the last `BACKSTOP` alert |
| `backstop.note` | string or null | Why the limits are not applied (`deferred: usage ... is not below memory.max`, a failed write, ...) |
| `strays.enabled` | bool | `[strays] enabled` (detection) |
| `strays.enforce` | bool | `[strays] enforce` (the owner kill may send signals) |
| `strays.check_s` | int | `[strays] check_s`, seconds between checks |
| `strays.last_check` | string or null | UTC time of the last check (null before the first) |
| `strays.items` | list | The current strays (empty when disabled) |
| `strays.items[].pid` | int | Pid |
| `strays.items[].start_time` | int | Process start time in clock ticks (stat field 22): with the pid it identifies the process |
| `strays.items[].comm` | string | Command name |
| `strays.items[].cmdline` | string | Command line, truncated to 200 characters |
| `strays.items[].cwd` | string or null | Working directory (null when unreadable) |
| `strays.items[].cgroup` | string | cgroup v2 path |
| `strays.items[].project` | string or null | The project whose `agent_cwd` holds the cwd, else null |
| `strays.items[].cpu_pct` | number | CPU % of one core over the last interval |
| `strays.items[].hot_since` | string | UTC time its current hot run began |
| `strays.items[].age_s` | int | Seconds hot at the last check |
| `strays.items[].orphan` | bool | Always true for a listed stray |
| `strays.items[].session.claude_pid` | int or null | `CLAUDE_PID` from its environment |
| `strays.items[].session.session_id` | string or null | `CLAUDE_CODE_SESSION_ID` from its environment |
| `capabilities` | list | What this daemon can do: `freeze` (the `freeze`/`unfreeze` ops and endpoints) and `reap`; the Home Assistant integration enables its freeze, resume and kill-daemons entities from it |
| `daemon.version` | string | loadgov version |
| `daemon.pid` | int | Daemon pid |
| `daemon.started_at` | string | Daemon start time, UTC |
| `daemon.mode` | string | Derived from the enforce flags: `observe` when none is on, `enforce` when all four are on, `partial` otherwise |
| `daemon.enforce` | object | The four enforce flags (booleans): `governor` (`[governor] enforce`), `slots` (`[slots] enforce`), `memguard` (`[memguard] enforce`), `reaper` (`[reaper] enforce`) |
| `daemon.tick_s` | number | Seconds between samples |
| `daemon.memory_protection_mb` | int | Memory the daemon's cgroup is really protected from reclaim: the least `memory.min` (MB) along its cgroup path (`/proc/self/cgroup`) up to the root; 0 when any level is 0 or unreadable. `loadctl status` warns when it is 0 |

Level thresholds (busy, limit) are hwmon's: busy % (85, 97), CPU PSI60 (40, 70), load1/core (1.5, 2.5), RAM available (6144, 3584) MB, swap used (85, 95) %, disk free (25, 10) GB (the least free path; fixed, this is the disk part of `level`). The disk alert thresholds default to the same (25, 10) GB free and are set with `[levels] disk_busy` and `disk_limit` in the config; they drive only the `disk` alert and `levels.disk_alert`, never `level`: a number is GB free, a string `"80%"` is percent used of the filesystem (above 0, below 100). In the same unit `disk_busy` must warn earlier (more free space, or a lower percent); mixed units are allowed and each is checked on its own, per watched path. They apply on a config reload (SIGHUP) without a restart.
