# loadgov-mcp

A stdio MCP server that lets an agent read the load governor and request limit changes. It is an optional extra: the daemon and `loadctl` stay stdlib-only.

```bash
uv sync --extra mcp          # or: pip install 'loadgov[mcp]'
loadgov-mcp                  # speaks MCP on stdin/stdout; --socket or $LOADGOV_SOCKET overrides the socket
```

## Tools

| Tool | What it does |
|---|---|
| `load_get_status` | Compact snapshot: cpu/psi/memory/swap/io, levels, effective limits, profile, ceiling and `ceiling_info`, restriction, slots, jobs, `capabilities`, memory-guard/paused sections when the daemon reports them |
| `load_set_limits` | `set` as the agent principal. Needs `reason`; optional `profile`, `max_cores`, `target_cpu_pct`, `load_max`, `max_implementers`, `ram_floor_gb`, `ttl` or `until`, `agent_id`, `project`. `agent_id` is folded into the audit reason. Returns the daemon reply verbatim, including `refused: true`, `restrictedBy` and the owner's reason |
| `load_wait_headroom` | `kind` (`impl`, `review`, `gate`, `compile`), `timeout_sec`, optional `project`. Same two-sample rule as `loadctl wait`. Returns `result: ok` or `timeout` with the last decision `reasons`. One call waits at most 1800 s: call again, or use `loadctl wait` for longer waits |
| `load_explain` | Why the limits are what they are: profile and who chose it, ceiling, overrides (who, why, expiry), restriction flags, and a `why` list in plain text |
| `load_reap_idle_daemons`, `load_freeze`, `load_unfreeze` | Listed only when the daemon's status `capabilities` contains `reap` (`load_reap_idle_daemons`) or `freeze` (`load_freeze` and `load_unfreeze`; there is no separate unfreeze capability). Freeze and unfreeze are owner-only on the daemon and MCP never claims owner, so expect `owner_denied`. The server re-checks on every `tools/list` (and at startup), so a client that refreshes its tool list sees the change. The daemon's reply is returned as it is; a refusal is never turned into success |

If `loadgovd` is not running, every tool fails with `loadgovd not reachable ...` (no trace). The skill tells agents to continue without it then (no governed slot, no wait).

## Register it (documented, not applied by the installer)

Claude Code:

```bash
claude mcp add loadgov -- loadgov-mcp
```

or in the MCP JSON (`.mcp.json` / `~/.claude.json`):

```json
{ "mcpServers": { "loadgov": { "command": "loadgov-mcp", "args": [] } } }
```

opencode (`opencode.json`):

```json
{ "mcp": { "loadgov": { "type": "local", "command": ["loadgov-mcp"], "enabled": true } } }
```

codex (`~/.codex/config.toml`):

```toml
[mcp_servers.loadgov]
command = "loadgov-mcp"
```

Use the full path of the `loadgov-mcp` executable if the client does not inherit your `PATH`. The socket is root-only (`0600`), so the client must run as root.

## Security

- stdio only; no network listener.
- Every request goes out as the **agent** principal. The server never sends `owner: true` and has no tool for the ceiling, `reset`, `hold` or `--owner`: owner actions are impossible through MCP.
- An agent may lower limits at any time (TTL 2 h by default, 12 h at most) and may raise them only while no human restriction is active. A refused raise comes back as `refused: true`; the agent reports it and does not retry.
- Everything runs as root, so the daemon's principal checks are audit-grade evidence, not a security boundary (`AGENTS.md`, rule 4).
