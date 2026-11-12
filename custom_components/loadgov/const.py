"""Constants of the loadgov integration."""

from __future__ import annotations

DOMAIN = "loadgov"
NAME = "Build Host Load Governor"

DEFAULT_PORT = 7420
SCAN_INTERVAL_S = 10
REQUEST_TIMEOUT_S = 10
HA_REASON = "Home Assistant"

# The four built-in profiles of loadgovd (docs/protocol.md) and their labels in Home Assistant.
PROFILE_LABELS = {
    "silent": "Silent (Fan Off)",
    "balanced": "Balanced (Default)",
    "boost": "Boost (Away)",
    "drain": "Drain (Hold New Work)",
}

# Options flow keys (read by the smart triggers, triggers.py).
CONF_PRESENCE_ZONE = "presence_zone"
CONF_TV_PLAYERS = "tv_media_players"
CONF_QUIET = "quiet_hours_switch"
CONF_REMOTE_LOAD = "remote_load_sensor"
CONF_REMOTE_CPU = "remote_cpu_sensor"
CONF_NOTIFY = "notify_targets"
CONF_REMOTE_LOAD_MAX = "remote_load_threshold"
CONF_REMOTE_CPU_MAX = "remote_cpu_threshold"
CONF_TRIGGER_PRESENCE = "trigger_presence"
CONF_TRIGGER_TV = "trigger_tv_quiet"
CONF_TRIGGER_QUIET = "trigger_quiet_hours"
CONF_TRIGGER_REMOTE = "trigger_remote_load"

# Only the presence zone has a default (`zone.home` exists in every Home Assistant). Every other entity and
# threshold starts empty: a trigger with an empty entity does nothing.
OPTION_DEFAULTS = {
    CONF_PRESENCE_ZONE: "zone.home",
    CONF_TV_PLAYERS: [],
    CONF_QUIET: None,
    CONF_REMOTE_LOAD: None,
    CONF_REMOTE_CPU: None,
    CONF_NOTIFY: [],
    CONF_REMOTE_LOAD_MAX: None,
    CONF_REMOTE_CPU_MAX: None,
    CONF_TRIGGER_PRESENCE: False,
    CONF_TRIGGER_TV: False,
    CONF_TRIGGER_QUIET: False,
    CONF_TRIGGER_REMOTE: False,
}

# Daemon features that are not implemented everywhere yet. A feature is on when the status lists it
# in `capabilities`, or (freeze) when `limits` carries a `freeze` field.
FEATURE_FREEZE = "freeze"
FEATURE_REAP = "reap"
REQUIRES_FREEZE = "requires a loadgovd with freeze support"
