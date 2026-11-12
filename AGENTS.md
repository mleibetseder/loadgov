# AGENTS.md

Rules for anyone (human or agent) changing this repository.

## What this repo is

`loadgov`: a host-wide load governor for the build host. Architecture decisions live in `docs/decisions/`, the wire protocol and the behaviour of each engine in `docs/protocol.md`, the install and cut-over path in `docs/deployment.md`.

## Layout

- `src/loadgov/`: the Python package (daemon, CLI, MCP server).
- `tests/unit/`: fast tests with fake `/proc` and `/sys` trees; no root, no host access.
- `tests/integration/`: host tests; run only with `LOADGOV_INTEGRATION=1`, as root, on a private `loadgov-test.slice`.
- `legacy/`: optional, not part of the public tree: the shell and Python scripts `loadgovd` replaces, where a site keeps them. Do not edit them; they are the reference for current behaviour and thresholds. Where a legacy README and its script disagree, the script wins.
- `docs/`: deployment guide, protocol, MCP server, ADRs. `custom_components/loadgov/`: the Home Assistant integration (HACS layout, root `hacs.json`). `ha/`: its tests (separate uv project) and the drafts. `packaging/`, `compat/`, `tools/`: install, shims, helper scripts.

## Commands

```bash
uv sync
uv run pytest tests/unit
uv run ruff check && uv run ruff format --check
```

The host is the machine being protected. Run heavy commands with `nice -n 10` and do not start them while a heavy gate holds `/run/lock/host-heavy-slot.lock`.

## Safety rules

1. Priority is **system > human > agent**. Safety guards beat owner settings; owner restrictions beat agent requests.
2. The daemon itself never writes to `/sys/fs/cgroup`, sends signals, pauses containers or takes `/run/lock/*` locks (owner and drain holds) unless the matching `enforce` flag is on. The one exception is a momentary probe: for a client-lock request (`slot acquire --fd`, `loadctl run`) the daemon opens the lock file, takes the flock non-blocking and drops it at once to learn whether it is free; it never keeps it. Defaults are off. Client tools that replace a legacy lock user (`loadctl slot acquire --fd`, `loadctl run --kind gate|compile` and the `compat/` shims built on them) always take the same lock the legacy script took, in either mode. *Undoing* a pause or freeze the daemon made earlier (cgroup thaw, `SIGCONT`, `docker unpause`) is always allowed, whatever `enforce` says, so turning `enforce` off releases paused work by the resume rule; `loadgovd --recover --force` is the explicit owner thaw-all.
3. Unit tests never touch the real host: inject `proc_root` / `sys_root` and use the fakes.
4. Owner claims (`--owner`) are default-deny and checked by cgroup and process ancestry (`docs/protocol.md`); these checks and tokens are audit-grade evidence, not a security boundary, because everything runs as root.
5. Never freeze or kill the agent process (`claude`) itself.
6. Never change the live Home Assistant instance from this repo's work without the owner's approval. `ha/` holds drafts.
7. Keep the runtime dependency-free (stdlib only); dev tools go in the `dev` dependency group.

## Pull requests

One logical change per pull request, titled after what it changes. Include the test output in the description. Site specific values (addresses, project names, lock names, paths, entity ids) never go into code, tests, docs or drafts: they are configuration (README "Configure for your site").
