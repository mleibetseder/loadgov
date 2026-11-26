# Deployment guide: install, configure, observe, enforce

How to put `loadgovd` on a Linux build host, let it watch, compare it with what protects the host today, and turn
on enforcement one flag at a time. Every command runs as root on the host. Protocol and field names:
[protocol.md](protocol.md). The settings, in one table: [README, "Configure for your site"](../README.md).

**The rules of this guide**

- One step at a time; the next step starts when the go criterion of the current one is met.
- Every `enforce` flag stays `false` until its step says otherwise. A step is one config line and one
  `systemctl reload loadgovd` (or `restart`; `loadgovd --check-config /etc/loadgov/loadgov.toml` checks the file first; settings
  that only apply at start are listed in [protocol.md, Daemon lifecycle](protocol.md#daemon-lifecycle)).
- Run heavy commands with `nice -n 10` and never while a heavy job holds `/run/lock/host-heavy-slot.lock`.
- Nothing is changed on a Home Assistant instance without its owner's approval.

For a hands-on checklist of the same first steps (daemon, REST, Home Assistant), see
[howto-setup.md](howto-setup.md). Once the daemon runs, projects adopt the light lane and the image train as
described in [adopting.md](adopting.md).

## 1. Install

Requirements: Linux with cgroup v2 and systemd, [`uv`](https://docs.astral.sh/uv/), root.

```bash
git clone <this repository> /opt/loadgov-src && cd /opt/loadgov-src
nice -n 10 packaging/install.sh --no-start        # /opt/loadgov venv, /usr/local/bin/loadctl, systemd units
```

`install.sh` is idempotent and never turns on an enforce flag. Re-running it always rebuilds the `loadgov`
package from the checkout (even at an unchanged version); to check, `grep` a recent change in
`/opt/loadgov/.venv/lib/python*/site-packages/loadgov`. It installs the daemon into `/opt/loadgov/.venv`
(pinned interpreter, not editable), links `loadctl`, installs `loadgovd.service` and `build-work.slice`, installs and enables `loadgov-image-runner.timer` (with its service; it starts
the runner of the image train every 10 minutes and does nothing while the image queue is empty, see `docs/protocol.md` "The image train"; the night window, its time zone and the beta interval are the `[image]` values, which the owner can override at run time with `loadctl set --image-window ... --owner --reason ...` and clear with `--image-clear`; the override lives in `state.json` and survives a restart and a reload), and
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

Compile memory admission (`[compile] memory_admission = true`, default off): a compile waits for
`expected peak + the memory guard's low floor` of free RAM instead of starting into a full host and being
killed (at most `max_memory_wait_s`, default 900 s, then it starts anyway). It learns the peak per crate from finished `loadctl run --kind compile --label <crate>` jobs, so
turn it on after the host has built for a while (until then every crate counts `default_peak_mb`); the
compile slot stays 1 and the guard floors do not change. See `docs/protocol.md` (Slots).

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

**Survives thrash.** The unit reserves memory for the daemon (`MemoryMin=96M`, RSS is about 38 MB), raises its
CPU and IO weight, and keeps `OOMScoreAdjust=-900`. Check after an install or unit change:
`systemctl show loadgovd -p MemoryMin -p CPUWeight -p WatchdogUSec -p TimeoutStartUSec`. `MemoryMin` only
protects as far as the parent slices allow (cgroup v2 caps a child's protection at its parents'), so
`packaging/install.sh` installs `/etc/systemd/system/system.slice.d/50-loadgov-memorymin.conf` (`[Slice]
MemoryMin=96M`) before its `daemon-reload`. It leaves `system.slice` alone when it already has `MemoryMin` of
96M or more from another drop-in (it never lowers an admin's value), and refreshes its own file on a re-run.
Verify: read `memory.min` of `loadgovd.service` and each parent
(`cat /sys/fs/cgroup/system.slice/loadgovd.service/memory.min /sys/fs/cgroup/system.slice/memory.min`) and
check that the first one is not 0 and that no parent in the chain is 0 (a parent at 0 caps the protection
at 0). `loadctl status --json | jq .daemon.memory_protection_mb` shows the least value along the daemon's
cgroup path, and `loadctl status` prints a `warn  memory protection 0 MB` line when it is 0 (then set
`MemoryMin=` on the slice that is 0). Rollback: delete the drop-in file and `systemctl daemon-reload`.

**Go.** `mode` is `observe`, `governor.enforce` is `false`, `loadctl status` answers, `journalctl -u loadgovd -n 50`
shows no traceback, and nothing under `/sys/fs/cgroup` changed.

## 3. Observe

In observe mode the daemon samples the host, makes every decision a real run would make and records it as
`WOULD ...` audit lines (`/var/log/loadgov/audit.jsonl`) without writing a cgroup file, sending a signal, pausing a
container or taking a lock.

The audit log rotates by size (`[audit] max_mb`, `keep`; docs/protocol.md "Files written by the daemon"): older lines are in
`audit.jsonl.1` (newest) to `audit.jsonl.N`. `tools/shadow_compare.py --audit` reads one file, so for a window that spans a
rotation concatenate them oldest first, e.g. `cat audit.jsonl.2 audit.jsonl.1 audit.jsonl > /tmp/audit-all.jsonl`.

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
loadctl slot status --json | jq 'map_values(.state)'
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
5. **`[strays] enforce = true`** (optional): detection (`[strays] enabled`, on by default) only reads `/proc` and
   lists orphaned CPU hogs in `loadctl stray` and on the phone. `enforce` only lets the owner's kill (`loadctl stray
   --kill PID --owner`, the phone's Kill button) send `SIGTERM`, then `SIGKILL` 30 s later; the daemon never
   kills a stray on its own. Watch `loadctl stray` for a few days first and add `exclude_cgroups` for anything
   that is a legitimate long runner.
6. **`[backstop]`** (optional, after at least seven days of history): a hard `memory.max` under
   `build-work.slice`, sized from the history with `loadctl history --all --csv | python3 tools/backstop_sizing.py -`.
   The daemon applies it with `systemctl set-property --runtime build-work.slice MemoryMax=… MemorySwapMax=…`
   (and starts the slice first when it is inactive after boot), because systemd overwrites a value written only
   to the cgroup file whenever it re-realizes the slice, for example when a light-lane scope with resource
   properties starts below it. Check after the restart: `loadctl status` shows `backstop … applied`, `systemctl
   show build-work.slice -p MemoryMax -p MemorySwapMax` shows the configured sizes and `loadctl status --json`
   has `backstop.via` `systemd` (`file` means the systemctl call failed; the note says why). When usage is
   already at or above `memory_max_mb`, the limit starts as a cap at usage + `cap_margin_mb` (default 512) and
   steps down to the target as usage falls (one `backstop_cap` warning). Rollback as above; the daemon also
   lifts the runtime properties (`MemoryMax=infinity MemorySwapMax=infinity`).

   **Per-project caps** (optional, off by default): the build-work limit alone lets one runaway project fill
   build-work.slice, and the kernel then OOM-kills other projects' jobs too. `[backstop] project_memory_max_mb`
   confines each project to its own `build-work-<project>.slice` (`MemoryMax`, with `MemoryHigh` at
   `project_memory_high_pct`, default 90 %, so it is throttled first); `[projects.<name>] memory_max_mb`
   overrides it per project, 0 = no cap. Size it from the per-project peaks in the history and keep it at
   least 1024 and at most `memory_max_mb`. Check: `loadctl status` shows a `backstop projects:` line, `systemctl
   show build-work-<project>.slice -p MemoryMax -p MemoryHigh` the sizes. A nested name (`a-b`) is also under
   its parent's (`a`) cap. Removing the keys (or the flag) lifts the caps with `MemoryMax=infinity
   MemoryHigh=infinity`.

   **Leaked tmpfs memory** (read-only, also with the flag off): files a job writes to a tmpfs such as `/tmp`
   stay charged to its slice after it ends, and a job killed by SIGKILL never runs its cleanup. The status shows
   the `shmem` share of build-work.slice and of each project slice, a `backstop_leak` warning names a slice
   that holds more than `leak_alert_mb` (1024) of shmem with no process for `leak_alert_min` (5) minutes (page
   cache, `file_mb`, is reclaimable and does not count), and the leftover scan names big tmpfs directories
   with the jobs that likely wrote them (`backstop.leftovers`, the `job_end` audit line). The scan is bounded
   (a job end at most 8 directories and 2000 entries, a tick at most 64 and 20000, 256 top-level entries
   per mount per step) and resumes on the next tick. The daemon never deletes them: check the named directory and remove it as its
   owner.

7. **`[ui]`, the per-host web UI** (optional; Queue, Runs, a run's jobs and live log). Read-only, no token, its own
   listener: protect it with a reverse proxy that does the sign-in, on the LAN only.
   1. Build the app (the build output is never committed): see `ui/README.md`.
   2. `sudo packaging/install.sh` copies a build found under `ui/dist/*/browser` to `/opt/loadgov/ui`; or copy
      the build there yourself (root-owned, `index.html` at the top), or point `[ui] dir` at it.
   3. In `/etc/loadgov/loadgov.toml` set `[ui] enabled = true`, `bind` (the host's LAN address, as for `[rest]`),
      `allow = ["<the proxy's address>"]` (required; never a whole network you do not trust) and, optionally,
      `tempo_url` for the "Fleet view: in tempo" link. `loadgovd --check-config` warns when no build is installed.
      `[ui]` is read at start: `systemctl restart loadgovd`.
   4. Configure the proxy to require sign-in and to forward everything (`/` and `/api/`) to `http://<bind>:<port>`
      (default port 7421), with the app at the root of its host name. Do not expose the port itself beyond the
      proxy. The daemon trusts the proxy's address and nothing else (`docs/protocol.md` "Web UI": audit-grade, not
      a security boundary); use `https://` at the proxy.
      The proxy is the only client, so it may hold `max_connections // 2` connections at once (8 of the default 16);
      the app's first page load fetches many files at once, so raise `[ui] rate_per_s` and `max_connections`
      if assets come back `429` or are dropped (ILM-39 sizes the burst).
   5. Check: `loadctl status` shows no `warn  ui` line, `curl -s http://<bind>:7421/api/config` from the proxy
      host returns the host id, and the same request from any other address returns `403`.
   Rollback: `enabled = false` and restart.

## 5. Panic and rollback

```bash
sed -i -E 's/^(enforce)[[:space:]]*=[[:space:]]*true/\1 = false/' /etc/loadgov/loadgov.toml
systemctl restart loadgovd
loadctl status --json | jq '.stopped'          # nothing should stay stopped
loadgovd --recover --force                      # only with the daemon stopped: thaws the whole registry
```

`systemctl disable --now loadgovd` removes the daemon; the last cpu cap stays in place, and the lock files and
client scopes are untouched.

Downgrading to a daemon older than the queue view: the new `key` and `job_kind` fields in `state.json` are unknown to it, so it drops the saved holder and job bookkeeping on start (client locks stay held by their clients).

Downgrading to a daemon older than the job endings (ILM-33): the saved running jobs carry the new `oom0` and `oom` fields, which the older daemon cannot load, so it drops them on start (their scopes keep running; the slot rule frees what they held).

## REST clients: tokens and scopes

Every REST caller has its own token and a scope. Home Assistant keeps the single `LOADGOV_REST_TOKEN` line: it is
client `default`, scope `owner`, and nothing changes for it. A service that only reads gets a named entry with
`scope=read`: a read token can call `GET` endpoints only, and every write path answers `403` `read_only`.

**Create a read token for a client** (neutral name `fleet`). Generate the token and keep it out of git and chat:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Add one line with that token to `/etc/loadgov/tokens.env` (mode 0600, owner root), next to the existing ones:

```
LOADGOV_REST_CLIENT=fleet <the-new-token> scope=read
```

Names are unique and `[a-z0-9-]{1,32}` (`default` belongs to the single-token line); a token is at least 43
characters and belongs to one client. Then reload the daemon (`systemctl reload loadgovd`, SIGHUP): it re-reads the
file, and a bad file is refused and logged while the working tokens stay. Check with the client's token:
`GET /v1/runs?limit=1` answers `200` and `POST /v1/set` answers `403` `read_only`. Give the token to the client
through its own secret store.

**Rotate it.** Generate a new token and add it as a second entry (`fleet-2`), reload, switch the client over, then
delete the old line and reload again: there is no gap. (Replacing the token in the one line works too, but the old
token stops at the reload, before the client has the new one.) The audit log's `client` field shows which names
still call.

**Remove a client** by deleting its line and reloading. A change of `scope` takes effect the same way.

## Multi-host readiness

loadgov runs one `loadgovd` per host, and each daemon decides only for its own host: its limits, slots, memory
guard and alerts never look at another machine. Every host has an id (`[host] id`, default the hostname; see
the README "Configure for your site"), and status, audit lines and alerts carry it. Adding a host is
configuration: install the package there and set its own `[host] id`, REST and `[ha]` options. The fleet view that collects
every host is read-only (docs/protocol.md, Fleet); see "Add a host to the fleet" below.

## Add a host to the fleet

The fleet view shows this host and every host listed under `[[fleet]]`; it only reads, each host's own `loadgovd`
decides for that host (docs/protocol.md, Fleet). Adding a host is configuration, no code change. The addresses
below are documentation addresses.

1. **Install loadgov on the new host** (sections 1 to 3 above). It runs in observe mode until you turn enforcement
   on.
2. **Set its `[host]` table** in `/etc/loadgov/loadgov.toml`: `id = "build-b"` (the id the viewers will use) and
   `mode = "govern"` for a host whose own daemon enforces, or `"monitor"` for one that is only watched (every
   enforce flag is forced off). Restart `loadgovd` for a new id.
3. **Enable its REST surface with a token** (`[rest] enabled = true`, `bind = "192.0.2.20"`, and `allow` containing
   the viewing host's address), and put a new `LOADGOV_REST_TOKEN` in its `/etc/loadgov/tokens.env` (mode 0600).
4. **Add it on the viewing host(s):** in the viewer's `loadgov.toml`

   ```toml
   [[fleet]]
   id = "build-b"
   url = "http://192.0.2.20:7420"
   token_env = "LOADGOV_TOKEN_BUILD_B"
   mode = "govern"
   ```

   and, in the viewer's `/etc/loadgov/tokens.env`, `LOADGOV_TOKEN_BUILD_B=<the new host's token>`. The token is never
   in the TOML. Check with `loadgovd --check-config`, then reload (`systemctl reload loadgovd` or SIGHUP; `[[fleet]]`
   is live on reload).
5. **Verify:** `loadctl fleet status` lists the host with its level, load per core, slots and image queue, and `ok`
   with an age. `DOWN` names the reason (`timeout`, `connection refused`, `HTTP 401` for a wrong token or a source
   address that is not in `allow`); `id mismatch` means the host reports a different `[host] id` than the registry `id`.

REST is plain HTTP with a bearer token: use `https://` behind a reverse proxy if the path is not trusted.

A future infrastructure role `loadgov` is meant to do steps 1 to 3 and the viewer entry from one inventory. It does
not exist yet; until then follow the steps above.
