# loadgov

A host-wide load governor for a Linux build server: CPU and fan throttling, RAM and swap protection, compile
serialization and heavy-slot coordination, for work that agents and CI start on the same machine.

One daemon, `loadgovd`, with a `loadctl` CLI, an optional MCP server (`loadgov-mcp`) and a Home Assistant
integration (`ha/`). The daemon and the CLI use only the Python standard library. The architecture decisions
are in [ADR-0001](docs/decisions/ADR-0001-language-ipc-cgroups.md), the wire protocol and the behaviour of each
engine in [docs/protocol.md](docs/protocol.md).

**Status: it starts in observe mode.** Every `enforce` flag is off by default; the daemon logs `WOULD ...`
decisions and writes nothing to cgroups, signals nothing and takes no locks until you flip a flag. The
install, configure, observe and enforce path is in [docs/deployment.md](docs/deployment.md);
`tools/shadow_compare.py` compares loadgov's `WOULD` decisions with the logs of a loop it replaces.

## Development

```bash
uv sync            # create .venv with dev tools
uv run pytest      # unit tests (a fake /proc and /sys, no root, no host access)
uv run ruff check && uv run ruff format --check
```

See [AGENTS.md](AGENTS.md) for the rules that apply when changing this repo.

## Configure for your site

Nothing site specific is built in: no addresses, no project names, no lock names, no paths. Every value below is
set by you, in one of four places: the daemon's TOML file (`/etc/loadgov/loadgov.toml`, annotated in
[packaging/config/loadgov.toml.example](packaging/config/loadgov.toml.example)), the secrets file
(`/etc/loadgov/tokens.env`, mode 0600, [example](packaging/config/tokens.env.example)), the environment of the
client shims in `compat/`, and Home Assistant (the integration's options and `secrets.yaml`, see
[ha/README.md](ha/README.md)). The examples use documentation addresses (`192.0.2.0/24`) and invented names.

| What | Where | Example |
|---|---|---|
| Address the REST surface listens on (required when REST is on; never `0.0.0.0` or loopback) | TOML `[rest] bind` | `bind = "192.0.2.10"` |
| REST port, allowed source addresses | TOML `[rest] port`, `allow` | `allow = ["192.0.2.20"]` |
| REST bearer token | `tokens.env` `LOADGOV_REST_TOKEN`; HA secrets `loadgov_token`, `loadgov_auth_header` | `LOADGOV_REST_TOKEN=<32 random bytes>` |
| Home Assistant base URL for alert pushes | TOML `[ha] url` | `url = "http://ha.example:8123"` |
| Home Assistant webhook id | `tokens.env` `LOADGOV_HA_WEBHOOK_ID`; HA secret `loadgov_webhook_id` and the `webhook_id` of the alert automation | `LOADGOV_HA_WEBHOOK_ID=loadgov-alert-<random>` |
| The projects that run governed work (one `build-work-<project>.slice` each, made by `packaging/install.sh`) | TOML `[projects.<name>]`, `[memguard.projects.<name>]`, or `install.sh --project <name>` | `[projects.web-app]` |
| Headroom thresholds of a project, per kind (`impl`, `review`, `gate`, `compile`) | TOML `[projects.<name>.kinds.<kind>]` | `load1_max = "nproc"`, `poll_s = 120` |
| Memory-guard rule of a project, its container names and the working directory of its agents | TOML `[memguard.projects.<name>]` | `low_mb = 2048`, `containers = '^(ci-|test-)'`, `agent_cwd = "/srv/work/web-app"` |
| Containers that are never paused | TOML `[memguard] deny_containers` | `["*-test-db*", "*-review-*"]` (the default) |
| Implementer trees of a legacy dispatcher (a snapshot shell and its usage line) | TOML `[memguard] legacy_snapshot` | `{ prefix = "/tmp/dispatcher-snap.", marker = "# Usage: dispatch.sh", project = "web-app" }` |
| A host-wide pause file that another tool reads (`loadctl wait` honours it) | TOML `[memguard] pause_marker` | `{ file = "web-app-guard.paused", project = "web-app" }` |
| The compile slot's lock file under `/run/lock` (the heavy slot is always `host-heavy-slot.lock`) | TOML `[slots] compile_lock`, or the daemon's environment `LOADGOV_COMPILE_LOCK` | `compile_lock = "compile-slot.lock"` |
| Which crates the `RUSTC_WRAPPER` shim puts behind the compile slot, and their project | environment `LOADGOV_HEAVY_CRATES`, `LOADGOV_COMPILE_PROJECT` | `LOADGOV_HEAVY_CRATES="app_backend app_worker"` |
| Project and kind of the legacy-compatible wait shims, and the optional legacy fallback script | environment `LOADGOV_HOSTWAIT_PROJECT` (host-wait shim), `LOADGOV_HEADROOM_PROJECT` (wait-headroom shim), `LOADGOV_WAIT_PROJECT` (shared fallback), `LOADGOV_WAIT_KIND`, `LOADGOV_LEGACY_SCRIPT`, `LOADGOV_LEGACY_DIR` | `LOADGOV_WAIT_PROJECT=web-app` |
| Who may not claim `--owner`: extra cgroups and dispatcher command lines (build-work cgroups are always refused) | TOML `[principal] deny_cgroups`, `ancestor_deny` | `deny_cgroups = ["/legacy-loop"]`, `ancestor_deny = ['dispatch-.*\.sh']` |
| Other cgroups that follow the governor's cap | TOML `[compat] mirror_cgroups` | `mirror_cgroups = ["legacy-loop"]` |
| Log files that keep the lines of loops being replaced | TOML `[compat] alert_log_paths`, `governor_log_path`, `[memguard] log_path` | `alert_log_paths = ["/var/log/loadgov/host-alerts.log"]` |
| Profiles, ceilings, the hard memory backstop | TOML `[profiles.*]`, `[backstop]` | `memory_max_mb = 12288` |
| HA integration: daemon host, port and token (no defaults) | the integration's config flow | `192.0.2.10`, `7420`, the token |
| HA integration: presence zone, TV media players, quiet-hours switch, sensors and thresholds of a remote host's load, notify targets, and an on/off per trigger | the integration's options (all triggers off; only `zone.home` is preset) | `remote_load_sensor = sensor.other_host_load`, `remote_load_threshold = 6.0` |
| HA drafts: the phones, the page a notification opens, an optional script that records events | the `variables:` block at the top of each file in `ha/automations/` | `phones: [notify.mobile_app_my_phone]`, `record_script: ""` |
| HA drafts: the daemon's address in the REST commands and the sensor | `YOUR_LOADGOV_HOST` in `ha/packages/loadgov.yaml` | `http://192.0.2.10:7420` |

## Layout

| Path | What it is |
|---|---|
| `src/loadgov/` | the daemon (`loadgovd`), the CLI (`loadctl`) and the MCP server, standard library only |
| `compat/` | shell shims that keep older scripts working on top of `loadctl` (optional) |
| `packaging/` | systemd units, the install script, the example configuration |
| `ha/` | Home Assistant integration, package, automations and dashboard drafts |
| `skills/loadgov/` | an agent skill that explains `loadctl` and the MCP tools to coding agents |
| `tools/` | `shadow_compare.py` (observe-mode comparison), `backstop_sizing.py` |
| `docs/` | deployment guide, protocol reference, MCP server, decisions |

## License

MIT, see [LICENSE](LICENSE).
