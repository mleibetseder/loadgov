# loadgov commands

```bash
loadctl status [--json]                                  # host load, levels, limits, slots
loadctl limits [--json]                                  # effective limits, overrides, ceiling, restriction
loadctl wait --kind impl|review|gate|compile [--project P] [--timeout S]   # exit 0 ok, 1 timeout, 3 no daemon
loadctl run --kind gate --project P --label L -- cargo test                # governed job in a slot
loadctl set --max-cores 2 --ttl 2h --reason "fan too loud" [--project P]  # lower
loadctl set --max-implementers 3 --until "07:00 Europe/Berlin" --reason "night run"  # raise: refused under a human restriction
loadctl slot status [--json]
loadctl log -n 20                                        # audit lines
```

Limits: `--profile`, `--max-cores`, `--target-cpu`, `--load-max`, `--max-impl`, `--ram-floor-gb`; timing `--ttl 90s|15m|2h` or `--until 'HH:MM Zone'`; `--reason` is required.

MCP equivalents: `load_get_status`, `load_explain` (status and limits), `load_wait_headroom` (wait), `load_set_limits` (set). `loadctl run` has no MCP tool.

Exit codes: `0` ok, `2` usage, `3` daemon not reachable, `4` refused or owner denied, `5` other daemon error. `loadctl wait`: `1` = `HEADROOM_TIMEOUT`.

Owner-only (the human, `--owner`): `ceiling`, `reset`, `hold`, owner `set`. Not for agents without an explicit instruction from the user in the same conversation.
