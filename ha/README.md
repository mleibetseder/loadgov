# Home Assistant drafts for loadgov

**Nothing here is active.** These are drafts for Home Assistant 2026.10. The owner reviews and applies them; nothing has been written to any Home Assistant instance. They are the webhook, notification and REST side that works without the custom integration.

## Files

| File | What it does |
|---|---|
| `packages/loadgov.yaml` | `rest_command.loadgov_action` (`POST /v1/action`), `loadgov_cancel_holder` (`POST /v1/cancel-holder`, `confirm: true` fixed), `loadgov_set` (`POST /v1/set`). A `rest` poll of `GET /v1/status` every 30 s gives `sensor.loadgovd_level` (state = `level`, attributes `at`, `limits`, `slots`, `daemon`, `rest`, `ha_push`; unavailable when the daemon does not answer) and `binary_sensor.loadgovd_hold_active` (a `slots.<slot>.hold` is set; drives the reminder) and `binary_sensor.loadgovd_restriction_active` (`limits.restricted`, informational only). |
| `automations/loadgov_alerts.yaml` | Webhook (`POST`, `local_only`) from loadgovd. Alert: every phone in the `phones` variable, tag `loadgov-<key>`, group `loadgov`, tap opens `alert_path`, high priority for `critical`, buttons from `actions`, only those the daemon can execute today: Throttle to Silent and Cancel hung gate. `resolved`: `clear_notification` with the same tag. `owner_change`/`info`: notification without buttons. Every event goes to `record_script` when one is set. |
| `automations/loadgov_notification_actions.yaml` | Handles `LOADGOV:*` button taps, calls the matching rest_command, reports the HTTP status on every phone and in `record_script` when one is set. Mode `queued`. |
| `automations/loadgov_daemon_down.yaml` | `sensor.loadgovd_level` unavailable for 2 min gives a critical "loadgovd down - paused work stays paused"; also checked 2 min after HA starts, so a daemon already down then is caught. Cleared when the sensor leaves `unavailable`. `input_boolean.loadgov_down_alerted` (in the package) records that an alert was sent: no duplicate alert, and "loadgovd back" goes to the history only after a real alert. |
| `automations/loadgov_hold_reminder.yaml` | While `binary_sensor.loadgovd_hold_active` is on, a reminder every 45 min naming the held slot, the hold reason and since when, without buttons; stops and clears when it turns off. A silent profile or an owner ceiling is not a hold. |
| `secrets.example.yaml` | Placeholders for the three secrets. |

Button ids offered now: `LOADGOV:profile=silent` (Throttle to Silent (2.0 cores), `POST /v1/action`) and `LOADGOV:cancel_holder:<slot>` (Cancel hung gate, `POST /v1/cancel-holder`). Prepared but commented out, to be enabled with one line each in `loadgov_alerts.yaml` (label) and `loadgov_notification_actions.yaml` (`passthrough`): `freeze` (Freeze heavy builds) and `unfreeze` (Resume paused workers) when the daemon supports it, `prune_caches` (Prune build caches) when it lands. Alerts whose `actions` list those ids simply get no button for them today.

## Custom integration `loadgov`

`custom_components/loadgov/` (HACS layout; copy the folder to `<config>/custom_components/`, restart, add **Build Host Load Governor** under Settings, Devices and services; enter the daemon's host (the `[rest] bind` address, no default), port `7420` and the REST bearer token). Install it on a running instance only after review. It replaces the `rest` poll of `packages/loadgov.yaml` (keep the package for the webhook alerts and `rest_command`s; the integration's own entities are `host_*`, the package's are `loadgovd_*`, so both can run side by side).

- One device, polled every 10 s (`GET /v1/status`); all entities go `unavailable` when the daemon does not answer.
- `select.host_load_profile`; numbers `host_max_cores`, `host_target_cpu_pct`, `host_load_max`, `host_max_implementers`, `host_ram_floor_gb` (capped at 6.0); `switch.host_load_gate_hold` (`POST /v1/hold`, slot `heavy`); sensors and binary sensors `host_*`.
- `switch.host_instant_quiet_freeze`, `button.host_kill_build_daemons` and `button.host_resume_paused_workers` stay **unavailable** until the daemon reports the feature (`capabilities` containing `freeze`/`reap` in the status, or a `limits.freeze` field). `sensor.host_daemon_features` (diagnostic) says what is missing: "requires loadgovd freeze (see the daemon's `capabilities`)". Their endpoints (`/v1/freeze`, `/v1/unfreeze`, `/v1/reap`, see docs/protocol.md) are unverified until the daemon supports it.
- Services `loadgov.set_profile`, `loadgov.set_ceiling` (both with `until` or `ttl`, optional `reason`), `loadgov.clear_ceiling`. A refusal (409) raises with the daemon's reason; a rejected token starts reauth.
- Options flow (Configure): presence zone (`zone.home` preset), TV media players, quiet-hours switch, load and CPU sensors of a remote host with their thresholds, notify targets, and an on/off per trigger (all off). Everything except the home zone starts empty; a trigger with an empty entity does nothing, and the remote-host trigger needs a threshold. Saving reloads the entry.
- Smart triggers (`triggers.py`; each runs only when its toggle is on, entities only from the options): **presence** (zone count 0 for 10 min: profile `boost` ttl 30m, re-armed every 15 min; someone home: `balanced` at once; stops re-arming when the daemon profile is no longer the one it set), **TV quiet** (any chosen player `playing`: ceiling `silent` ttl 15m, re-armed every 10 min; none playing for 5 min: ceiling cleared, only if the active ceiling's reason carries its tag), **quiet hours** (switch on: ceiling `silent` until 07:00 in the HA time zone; off: cleared under the same ownership check; re-applied at start when already on), **remote host load** (load or CPU of the watched sensors above its threshold for 10 min: `max_implementers=1 max_cores=2.5` ttl 15m, re-armed every 10 min; nothing is sent on clear, the TTL restores the limits; it reads `limits.overrides` and only lowers params nobody set by hand and whose effective value is above the target, never loosening and replacing agent raises (system > human > agent), so a human slider value, tighter or looser, is left alone). Every restriction expires on its own, so a Home Assistant restart heals itself; at start and on reload each enabled trigger evaluates once. `sensor.host_triggers` (diagnostic) shows enabled, active, last action and last error per trigger. Refusals (409), 503 and connection errors are logged once per change and retried at the next re-arm. Ownership is read from the daemon: every trigger reason starts with `HA trigger:<presence|tv|quiet_hours|remote_load>:` and the status carries `limits.ceiling_info`, `limits.profile_info` and `limits.overrides` (profile, reason, principal, set_at, expires_at). A trigger clears or replaces only what carries its own tag; a human ceiling or profile is never overwritten or cleared (diagnostics say `human ceiling active`), and after a restart a trigger still recognises its own. Needs a loadgovd that reports these status fields. Presence boosts only from a `balanced` that nobody set (`profile_info` null) or from its own boost.
- `dashboard/loadgov.yaml`: sections view for the device.
- Not exposed: "last change by" (the status has no audit field).

Tests: `cd ha && uv run pytest -q` (separate uv project, `pytest-homeassistant-custom-component` pinned to the HA 2026.10.4 line; mocked REST, no network).

## Known gaps (read before applying)

- Freeze, Resume and Prune buttons are not offered: the daemon's `/v1/action` accepts only `profile=<name>` (`docs/protocol.md` "REST"). `cancel_holder` only signals when `[slots] enforce = true`.
- No Release hold / Resume button on the reminder and no `reset` command: `/v1/reset all` does not release a hold and clears unrelated overrides. Releasing holds and freezes from the phone arrives with `/v1/hold`.
- The status has no freeze field and no "who set the hold" field; the reminder names the slot, the hold reason (`slots.<slot>.hold`) and `held_since`. A commented hook marks where the freeze flag goes.
- "since ... min" in the reminder is the sensor's `last_changed`; a HA restart resets it.
- Secret deviation: YAML cannot write `Bearer !secret x`, so the header uses the secret `loadgov_auth_header` (`"Bearer <token>"`). `loadgov_token` holds the raw token for reference only.
- Notification channels `loadgov` and `loadgov-critical` are created on the phone on first use.

## Open checks before install (owner)

1. Packages enabled in `configuration.yaml` (`homeassistant: packages: !include_dir_named packages`)? Not verified, no read access to it.
2. Notify action names of your phones (Developer Tools -> Actions): they go into the `phones` variable of each automation (placeholder `notify.YOUR_PHONE`).
3. `rest_command` behaviour on 4xx in 2026.10: confirm it returns `{status, content}` through `response_variable` and does not raise (try `loadgov_cancel_holder` with an unknown slot against a staging token, or a wrong token for a 401). If it raises, the outcome notification reports "No reply" for refusals.

## Install

1. On the loadgov host generate the token (`packaging/config/tokens.env.example`): `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, put it in `/etc/loadgov/tokens.env` (mode 0600) as `LOADGOV_REST_TOKEN`. Generate the webhook id: `python3 -c 'import secrets; print("loadgov-alert-" + secrets.token_urlsafe(24))'`, put it in the same file as `LOADGOV_HA_WEBHOOK_ID`. Enable `[rest]` and `[ha]` in `loadgov.toml` (`[ha] url` = the HA base URL; add the HA address to `[rest] allow`).
2. Add `loadgov_token`, `loadgov_auth_header` and `loadgov_webhook_id` to HA's `secrets.yaml` (see `secrets.example.yaml`). Never commit it.
3. Back up HA, then copy `packages/loadgov.yaml` into `/config/packages/` (configuration.yaml needs `homeassistant: packages: !include_dir_named packages`; check whether packages are already enabled first). Run Developer Tools -> YAML -> Check configuration, then restart (new `rest:` and `rest_command:` entries need it).
4. Create the four automations (Settings -> Automations -> Create -> Edit in YAML, paste one item without the leading dash), or append them to `automations.yaml`. In `loadgov_alerts.yaml` replace `webhook_id` with the generated id: a webhook trigger cannot read `!secret` in a UI automation so the real id lives only in HA's automation storage and `tokens.env`, never in git.
5. Site values, set in the `variables:` block at the top of each automation: `phones` (one notify action per phone), `alert_path` (the dashboard path a tap opens, default `/`) and `record_script` (optional; a script with the fields `title` and `message` that records every event, empty = none). In `packages/loadgov.yaml` replace `YOUR_LOADGOV_HOST` with the daemon's address.

## Test plan (after applying, with the owner present)

1. Status: Developer Tools -> States: `sensor.loadgovd_level` has a level, `binary_sensor.loadgovd_restriction_active` is `off` on a calm host.
2. Webhook: from the HA host (it is `local_only`):
   `curl -sS -X POST -H 'Content-Type: application/json' -d @tests/unit/golden/ha-alert-cpu.json http://127.0.0.1:8123/api/webhook/<id>` returns 200; your phones show "CPU busy" with two buttons; the record script (if set) was called.
3. Resolve: same call with `{"v":1,"type":"resolved","key":"cpu","title":"CPU busy","at":"2025-11-10T18:45:00Z"}` clears the phone notification.
4. Owner change: `{"v":1,"type":"info","level":"info","key":"owner_change","title":"Owner change","message":"loadctl set by pid 4242"}` gives a notification without buttons.
5. Buttons: tap "Throttle to Silent" (the Cancel hung gate button exists only on a `stuck_holder:<slot>` alert) (changes the active profile, so only with the owner present). Read the trace of "loadgov: notification actions" and the outcome notification (HTTP 200). Do not test `cancel_holder` on a real holder.
6. Daemon down (also restart HA while the daemon is stopped: the alert comes 2 min after start): stop `loadgovd` (or block the REST port) for over 2 min, expect the critical notification, then start it and expect the clear.
7. Reminder: needs a slot hold (`slots.<slot>.hold` set; the hold op is later work), so test by pointing a temporary copy of the sensor at a stub; a profile switch must NOT produce a reminder. Confirm in the trace of "loadgov: hold reminder" that the notify steps ran and the message has the held slot, reason and times (the since values are ISO strings converted with `as_datetime`). To not wait 45 min, temporarily set the `wait_template` timeout to 1 minute in the copy you test with, or run the automation once with the hold stub active.
8. Recorder: consider excluding `sensor.loadgovd_level` attributes from long-term history (large attributes, 30 s polling).
