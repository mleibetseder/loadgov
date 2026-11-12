# ADR-0001: Python daemon, unix-socket protocol, systemd slices, cgroup freeze

- Status: accepted
- Date: 2026-11-10
- Context: the initial design of loadgov

## Context

The build host this was designed for (a small multi-core build host with limited RAM and swap; cgroup v2 + systemd) had hung under a runaway load with no memory available, and ran loud and hot under sustained CPU use. A set of shell loops (governor, memory guards, headroom waits, slot locks) protected it. They were racy, untestable, and each project carried its own copy. They are to be replaced by one daemon, `loadgovd`, with a CLI, an MCP server and a Home Assistant integration.

## Decisions

### Language: Python, stdlib-only runtime

The daemon is Python (≥ 3.13; the host runs 3.14) using only the standard library at runtime (`asyncio`, `tomllib`, `http.server`, `json`, a raw `sd_notify` socket). It is installed with `uv` into its own pinned environment under `/opt/loadgov`, independent of apt upgrades. The MCP SDK is an optional extra used only by `loadgov-mcp`.

Why: the existing `hwmon` monitor is already Python; a 5 s control loop has no performance need for a compiled language; there is no compile step on a host whose CPU we are trying to protect; `pytest` makes the decision logic testable with fake `/proc` and `/sys` trees.

Rejected: Rust (compile cost and slower iteration, no gain for a 5 s loop), Go (not installed), Bash (status quo).

### Transport: unix socket with newline-delimited JSON

`loadctl` and `loadgov-mcp` talk to the daemon over `/run/loadgovd/loadgovd.sock` (mode 0600, root). Each request is one JSON line `{"v":1,"op":…,"args":{…}}`, each reply one line `{"ok":…}`. `SO_PEERCRED` gives the caller's pid, from which the daemon reads `/proc/<pid>/cgroup` and the ancestor command lines for the audit log and for principal detection.

A slot lives as long as its job scope is non-empty, not as long as the client connection: a dropped client whose job still runs keeps the slot.

Rejected: REST on loopback for local callers (needs tokens, loses peer credentials), D-Bus (heavier, no benefit), file polling (status quo, racy). REST exists only for Home Assistant on the LAN address.

### cgroups: systemd slices under `build-work.slice`

All governed work runs under `build-work.slice` with one child slice per project (`build-work-<project>.slice`, for example `build-work-web-app.slice`; systemd's naming, since `build-work.slice/web-app.slice` is not a valid child name). The slices are generated from the configured projects: systemd cannot template slice units. Each `loadctl run` job is a transient scope (`systemd-run --scope`). The daemon writes `cpu.max` of `build-work.slice` directly on each governor step; the slice unit carries a static `CPUQuota` so a dead daemon or a `daemon-reload` still leaves a cap. A legacy raw cgroup can be mirrored until it drains (`[compat] mirror_cgroups`).

**Slice naming.** In a systemd slice name every dash is a level of hierarchy: `build-work-web-app.slice` is a child of `build-work.slice`, which is itself a child of `build.slice`. In the cgroup tree `build-work.slice` therefore lives at `/sys/fs/cgroup/build.slice/build-work.slice` and a job scope at `build.slice/build-work.slice/build-work-web-app.slice/<unit>.scope` (`build-work-web-app-ci.slice` sits below the implicit `build-work-web.slice` and `build-work-web-app.slice`). The slices are not renamed; the status key stays `build-work.slice`, the watcher and the stopped-process prefix use the real path, and the governor writes `cpu.max` at `build.slice/build-work.slice`.

Rejected: raw cgroups under the root (fights systemd, no per-job scopes), `systemctl set-property` on every step (a D-Bus round trip every 10 s for no benefit).

### Pausing: cgroup freeze for agent-free scopes, never the agent

- `gate` and `compile` scopes contain no agent: pause = `cgroup.freeze=1` (atomic for the whole tree, covers children started later).
- `impl` scopes contain the agent (`claude -p`): the agent is never frozen or killed. Pause = `SIGSTOP` of the tree minus the agent (the legacy rule), or freeze of a nested child scope that holds only the build commands.
- Abort = `SIGTERM` of the largest `rustc` only. `cgroup.kill` is used only on agent-free scopes.
- Legacy processes outside any scope: `SIGSTOP` tree fallback, detected the way a legacy memory guard does (`[memguard] legacy_snapshot`). Containers: `docker pause`.
- Per-job memory comes from `memory.current` instead of summing `VmRSS`.

Rejected: `SIGSTOP` only (misses children spawned later), `memory.high` throttling (reclaim stalls, no clear resume rule).

## Consequences

- The decision logic is pure functions over a `Sample` and a `State`, tested against fixture trees; only thin adapters touch `/proc`, `/sys/fs/cgroup`, signals and Docker.
- Everything that writes to the host sits behind per-engine `enforce` flags that default to off.
- Integration tests that create cgroups need root and run on the host only (`LOADGOV_INTEGRATION=1`), on a private `loadgov-test.slice`.
