# Deployment guide: install, configure, observe, enforce

How to put `loadgovd` on a Linux build host, let it watch, compare it with what protects the host today, and turn
on enforcement one flag at a time. Every command runs as root on the host. Protocol and field names:
[protocol.md](protocol.md). The settings, in one table: [README, "Configure for your site"](../README.md).

**The rules of this guide**

- One step at a time; the next step starts when the go criterion of the current one is met.
- Every `enforce` flag stays `false` until its step says otherwise. A step is one config line and one
  `systemctl restart loadgovd`.
- Run heavy commands with `nice -n 10` and never while a heavy job holds `/run/lock/host-heavy-slot.lock`.
- Nothing is changed on a Home Assistant instance without its owner's approval.

For a hands-on checklist of the same first steps (daemon, REST, Home Assistant), see
[howto-setup.md](howto-setup.md).

## 1. Install

Requirements: Linux with cgroup v2 and systemd, [`uv`](https://docs.astral.sh/uv/), root.

```bash
git clone <this repository> /opt/loadgov-src && cd /opt/loadgov-src
nice -n 10 packaging/install.sh --no-start        # /opt/loadgov venv, /usr/local/bin/loadctl, systemd units
```

`install.sh` is idempotent and never turns on an enforce flag. It installs the daemon into `/opt/loadgov/.venv`
(pinned interpreter, not editable), links `loadctl`, installs `loadgovd.service` and `build-work.slice`, and
writes one `build-work-<project>.slice` for every project named in `/etc/loadgov/loadgov.toml`
(`[projects.*]` and `[memguard.projects.*]`) and for every `--project NAME` you pass. Slices are generated
because systemd cannot template slice units. If a `site/loadgov.toml` exists in the checkout and
`/etc/loadgov/loadgov.toml` does not, it is installed once and never overwritten. If the checkout carries a
`legacy/` folder, it is copied to `/opt/loadgov/legacy` as an optional fallback for the shims in `compat/`
(the shims themselves are installed to `/opt/loadgov/compat`, e.g. `RUSTC_WRAPPER=/opt/loadgov/compat/rustc-slot.sh`);
without it the shims degrade as described in their headers.

Create the state and log directories and the secrets file:

```bash
install -d -m 0755 /etc/loadgov /var/log/loadgov
install -m 0600 packaging/config/tokens.env.example /etc/loadgov/tokens.env   # fill in the token
```

## 2. Configure

Start from [`packaging/config/loadgov.toml.example`](../packaging/config/loadgov.toml.example): every block is
documented, and a missing file means the built-in defaults (all enforce flags off). Copy it to
`/etc/loadgov/loadgov.toml` and set at least:

- your **projects**: `[projects.<name>]` for every project that runs governed work (this is what makes
  `install.sh` write its slice), with `[projects.<name>.kinds.<kind>]` for thresholds that differ from the
  defaults;
- the **shadow logs**, so that the observe step has something to compare: `[compat] governor_log_path`,
  `alert_log_paths` and `[memguard] log_path`, each a new file (never a file another tool still writes);
- optional site rules: `[memguard.projects.<name>]`, `[memguard] deny_containers`, `[principal]`,
  `[compat] mirror_cgroups`, `[slots] compile_lock`.

Defaults to know before you rely on them: the `[principal]` deny lists start empty (only `build-work*` cgroups are
refused an owner claim, so list your own dispatchers and loops), and with `LOADGOV_HEAVY_CRATES` unset the
`rustc-slot.sh` shim puts no crate behind the compile slot. `loadgovd --recover --force` works even when the
config is broken (it then cannot remove a configured pause marker).

Then start it in observe mode:

```bash
systemctl enable --now loadgovd
systemctl --no-pager status loadgovd | head -5
loadctl status --json | jq '{mode: .daemon.mode, gov: .governor.enforce, rest: .rest.state}'
```

**Go.** `mode` is `observe`, `governor.enforce` is `false`, `loadctl status` answers, `journalctl -u loadgovd -n 50`
shows no traceback, and nothing under `/sys/fs/cgroup` changed.

## 3. Observe

In observe mode the daemon samples the host, makes every decision a real run would make and records it as
`WOULD ...` audit lines (`/var/log/loadgov/audit.jsonl`) without writing a cgroup file, sending a signal, pausing a
container or taking a lock.

If you already run loops that do the same work (a CPU governor, memory guards, an alert sampler), compare them for
at least three days, including a full build or test gate, with `tools/shadow_compare.py`:

```bash
python3 -I tools/shadow_compare.py \
  --audit /var/log/loadgov/audit.jsonl \
  --mem-guard-log /path/to/guard.log --governor-log /path/to/governor.log \
  --alerts-log /path/to/alerts.log --shadow-alerts /var/log/loadgov/host-alerts.shadow.log \
  --since "$(date -u -d '3 days ago' +%F)" --window-s 120 --tolerance 0.2 --abs-tolerance 2
```

Exit 0 means every class is within tolerance per day, 1 a mismatch, 2 no data. `--episode-project NAME` collapses
the per-tree pause lines of a project whose old guard logged one line per episode. Read the unmatched lines by hand
before trusting a pass. The script documents its inputs in its header.

Route the work through the daemon while you observe, and check the headroom and slot decisions:

```bash
loadctl wait --kind impl --project <name> --timeout 600
loadctl run --kind gate --project <name> --label nightly -- <your build command>
loadctl slot status --json | jq '.slots | map_values(.state)'
```

`loadctl run` always takes the real lock for a client (the one exception to "observe writes nothing", see
AGENTS.md rule 2), so existing lock users keep working. The shims in `compat/` put the same behaviour behind the
names of older scripts; their environment is in the README table.

## 4. Enforce, one flag at a time

Order, each with a day of watching and its rollback (set the flag back to `false`, `systemctl restart loadgovd`):

1. **`[governor] enforce = true`**: the daemon writes `cpu.max` of `build-work.slice` (and every
   `mirror_cgroups` entry). It only lowers the cap at start, so the host never gets more cores than before. The
   last cap stays when the daemon stops; it is never set to `max`.
2. **`[memguard] enforce = true`**, then a day later **`[reaper] enforce = true`**: pause and resume work under
   memory pressure (cgroup freeze for agent-free scopes, `SIGSTOP` for the rest, `docker pause` for allowed
   containers; the agent process itself is never frozen). Paused work stays paused if the daemon dies;
   `loadgovd --recover --force` (daemon stopped) thaws everything. Turning `enforce` off releases paused work by
   the normal resume rule.
3. **`[slots] enforce = true`**, only when both slots are free (`loadctl slot status`): owner and drain holds and
   `cancel_holder` take the real locks.
4. **`[rest]` and `[ha]`**, the Home Assistant side ([ha/README.md](../ha/README.md)): set `[rest] bind` to the host's
   LAN address (required, never `0.0.0.0`), fill `[rest] allow` with the Home Assistant address, put the token
   and the webhook id into `tokens.env`, then `enabled = true`.
5. **`[backstop]`** (optional, after at least seven days of history): a hard `memory.max` under
   `build-work.slice`, sized from the history with `loadctl history --all --csv | python3 tools/backstop_sizing.py -`.

## 5. Panic and rollback

```bash
sed -i -E 's/^(enforce)[[:space:]]*=[[:space:]]*true/\1 = false/' /etc/loadgov/loadgov.toml
systemctl restart loadgovd
loadctl status --json | jq '.stopped'          # nothing should stay stopped
loadgovd --recover --force                      # only with the daemon stopped: thaws the whole registry
```

`systemctl disable --now loadgovd` removes the daemon; the last cpu cap stays in place, and the lock files and
client scopes are untouched.
