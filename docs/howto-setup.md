# How to set up loadgov by hand

A step-by-step checklist for setting up loadgov manually: the daemon on the build host, the REST port for
Home Assistant, and the Home Assistant integration. Each step has a check, so you know it worked before
going on, and a way back. Values in `<angle brackets>` are yours; see the README "Configure for your site"
table for every setting. The wider install → observe → enforce path is in [deployment.md](deployment.md).

Everything here keeps loadgov in **observe mode**: no `enforce` flag is turned on, so nothing is throttled,
paused or locked by the daemon itself.

Run every command as root (`sudo` is written out below). The daemon's socket is root-only, so `loadctl` without
`sudo` reports "not reachable" even when the daemon runs fine.

## 0. Before you start

- A Linux build host with systemd and cgroup v2, root access, and `uv` installed.
- A checkout of this repository on the build host.
- Optional: Home Assistant with HACS, on the same network as the build host.
- Do it while no heavy build runs: `cat /run/lock/host-heavy-slot.holder` should not name a running job,
  and `uptime` should show a calm load.

## 1. Install the daemon

```bash
cd <checkout>
sudo bash packaging/install.sh
```

What it does: installs Python and loadgov into `/opt/loadgov`, links `loadctl` into `/usr/local/bin`, installs
the compat shims (root-owned) into `/opt/loadgov/compat`, writes the systemd units (`loadgovd.service`,
`build-work.slice` and one slice per configured project) and starts `loadgovd`. It never overwrites an
existing `/etc/loadgov/loadgov.toml`. Re-running it always rebuilds the package from the checkout; check with `grep`
for a recent change in `/opt/loadgov/.venv/lib/python*/site-packages/loadgov`.

**Check:**

```bash
systemctl is-active loadgovd          # active
sudo loadctl status                   # level OK/BUSY/LIMIT, "guard observe", slots free
ls -l /opt/loadgov/compat             # root root, not group/world-writable
```

**Back:** `sudo systemctl disable --now loadgovd` (nothing else depends on it; every shim falls back).

## 2. Configure

Edit `/etc/loadgov/loadgov.toml` (start from `packaging/config/loadgov.toml.example`). For a first setup only
fill in your projects (`[projects.<name>]`) and leave every `enforce` flag `false`. If the file does not exist
yet, copy the example; otherwise keep a backup first:

```bash
if [ -e /etc/loadgov/loadgov.toml ]; then
  sudo cp -p /etc/loadgov/loadgov.toml /etc/loadgov/loadgov.toml.bak-$(date -u +%Y%m%dT%H%M%SZ)
else
  sudo install -m 0644 packaging/config/loadgov.toml.example /etc/loadgov/loadgov.toml
fi
sudo systemctl restart loadgovd && sudo loadctl status
```

A config error makes the daemon refuse to start (`sudo journalctl -u loadgovd -n 20` names the key). A new
project gets its `build-work-<name>.slice` only when `sudo bash packaging/install.sh` runs again (a restart is
not enough).

## 3. Turn on the REST port for Home Assistant

1. **Create a token** (root only, never commit it):

   ```bash
   sudo sh -c 'umask 077; printf "LOADGOV_REST_TOKEN=%s\n" "$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")" > /etc/loadgov/tokens.env'
   sudo ls -l /etc/loadgov/tokens.env    # -rw------- root root
   ```

2. **Enable REST** in `/etc/loadgov/loadgov.toml`, bound to the build host's LAN address (never `0.0.0.0`):

   ```toml
   [rest]
   enabled = true
   bind = "<build host LAN IP>"
   port = 7420
   # allow = ["<Home Assistant IP>"]   # add in step 5
   ```

3. `sudo systemctl restart loadgovd`

**Check:**

```bash
sudo loadctl status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["rest"])'   # state: up
curl -s -o /dev/null -w '%{http_code}\n' http://<build host LAN IP>:7420/v1/status         # 403 from the host itself, by design
```

**Back:** set `enabled = false` and restart.

## 4. Install the Home Assistant integration

1. **HACS:** HACS → menu (⋮) → *Custom repositories* → add `https://github.com/mleibetseder/loadgov`, category
   **Integration** → find *loadgov* → *Download*.
   *Manual alternative:* copy `custom_components/loadgov/` to `<config>/custom_components/loadgov/`.
2. **Restart Home Assistant** (Settings → System → Restart). The log shows the usual "custom integration …
   has not been tested" warning; that is expected.
3. **Add the integration:** Settings → Devices & services → *Add integration* → search **loadgov** (it is listed
   as "Build Host Load Governor"), then enter:
   - Host: `<build host LAN IP>`
   - Port: `7420`
   - Token: the value after `LOADGOV_REST_TOKEN=` (`sudo cat /etc/loadgov/tokens.env`)

**Check:** a device "Build Host Load Governor" with about 27 entities: `sensor.host_busy_pct`,
`select.host_load_profile`, `number.host_max_cores`, … with live values. `sensor.host_daemon_features` lists
`freeze, reap`. Smart triggers stay off until you turn them on under the integration's *Configure*.

**Back:** delete the integration entry in Settings → Devices & services, then remove it in HACS.

## 5. Lock the REST port to Home Assistant's address

Home Assistant polls every 10 s, so its address shows up in the connections to the port within seconds:

```bash
for i in 1 2 3 4; do ss -tnH state time-wait '( sport = :7420 )' | awk '{print $4}' | sed 's/:[0-9]*$//'; sleep 3; done | sort | uniq -c
```

Put that address into `[rest] allow = ["<Home Assistant IP>"]` and run `sudo systemctl restart loadgovd`. Then
check both, about a minute later:

- `sudo journalctl -u loadgovd --since "-1 min" | grep -c refused` prints `0` (the `sudo` matters: without it
  the journal may be unreadable and an empty result proves nothing);
- in Home Assistant the "Build Host Load Governor" entities still show live values (not *unavailable*).

**Back:** remove the `allow` line (token-only) and restart.

## 6. Optional: alerts and phone buttons

The drafts in `ha/` (webhook alerts, phone action buttons, daemon-down alert, hold reminder) are installed
by hand following [ha/README.md](../ha/README.md) — package, secrets, then the automations — and are tested
with the README's curl step before relying on them.

## 7. Let your agents and loops use it

- Agents and scripts wait with `loadctl wait --kind <impl|review|gate|compile> --project <name> --label <what>`
  and run heavy jobs with `loadctl run --kind gate|compile --project <name> --label <what> -- <command>`.
- **Never wrap a command that takes a slot itself** in `loadctl run` (for example a gate script that already
  calls `loadctl run --kind gate`, or a build whose `RUSTC_WRAPPER` is the compile shim): the inner call
  waits for the slot its parent holds, forever.
- Existing scripts can point at the shims in `/opt/loadgov/compat` instead (see the README table for their
  environment variables). Replace a running loop script only between runs, by copying to a temporary name and
  renaming, and keep the old file as a backup.

## 8. Later: enforce

Only after a trial period in observe mode, flip one `enforce` flag at a time as described in
[deployment.md](deployment.md) §4. Setting every `enforce = false` plus `sudo systemctl restart loadgovd`
returns to observe mode (paused work is released by the normal resume rule). To thaw everything the daemon
paused at once, stop it first (the recovery refuses while the daemon runs), then start it again:

```bash
sudo systemctl stop loadgovd
sudo /opt/loadgov/.venv/bin/loadgovd --recover --force
sudo systemctl start loadgovd
```
