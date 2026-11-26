"""The host id: derivation, config, persistence, and where it shows up (status, audit, alerts, loadctl)."""

import json
import os

import pytest
from test_loadctl import status

from loadgov.cli import loadctl
from loadgov.config import Config, ConfigError, check_host_id, derive_host_id, load_config, parse_config
from loadgov.daemon.main import Daemon
from loadgov.daemon.state import StateStore
from loadgov.engines.watch import Watch
from loadgov.protocol import validate_status

T0 = 1_762_800_000.0


@pytest.mark.parametrize(
    ("hostname", "want"),
    [
        ("build-a", "build-a"),
        ("Build-A", "build-a"),
        ("build_a.Example.NET", "build-a-example-net"),
        ("  web app  ", "web-app"),
        ("--x--", "x"),
        ("ünï", "n"),
        ("", "host"),
        ("...", "host"),
        ("a" * 80, "a" * 63),
        ("a" * 62 + "." + "b" * 5, "a" * 62),  # cut at 63 characters, no trailing dash
    ],
)
def test_default_id_from_the_hostname(hostname, want):
    got = derive_host_id(hostname)
    assert got == want
    assert check_host_id(got) == got


@pytest.mark.parametrize("bad", ["", "Build", "a b", "a.b", "a_b", "x" * 64, 7, None, True])
def test_config_validates_the_id(bad):
    with pytest.raises(ConfigError, match="host.id"):
        check_host_id(bad)


def test_config_parses_the_host_table():
    assert parse_config('[host]\nid = "build-a"\n').host.id == "build-a"
    assert parse_config("").host.id is None
    with pytest.raises(ConfigError, match="host"):
        parse_config('[host]\nname = "x"\n')
    with pytest.raises(ConfigError, match="host.id"):
        parse_config('[host]\nid = "Bad Id"\n')


def make(fake_host, tmp_path, cfg=None, hostname="Build-A.example.net"):
    host = fake_host("calm")
    d = Daemon(
        host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
        compat_link=None, run_dir=str(tmp_path / "run"), config=cfg or Config(), clock=lambda: T0,
        tokens_file=str(tmp_path / "tokens.env"), hostname=hostname,
    )  # fmt: skip
    os.makedirs(d.run_dir, exist_ok=True)
    d.sampler.prime(host.prev_stat)
    return d


def audit_rows(d):
    with open(d.audit.path) as f:
        return [json.loads(line) for line in f]


def saved(tmp_path):
    return json.loads((tmp_path / "state" / "state.json").read_text())


def test_first_start_derives_and_persists_the_id(fake_host, tmp_path):
    d = make(fake_host, tmp_path)
    assert d.host_id == "build-a-example-net"
    assert d._save_state()
    assert saved(tmp_path)["host_id"] == "build-a-example-net"
    assert not os.path.exists(d.audit.path)  # nothing to audit on a first start


def test_saved_id_is_kept_when_the_hostname_changes(fake_host, tmp_path):
    make(fake_host, tmp_path)._save_state()
    d = make(fake_host, tmp_path, hostname="renamed")
    assert d.host_id == "build-a-example-net"
    assert not os.path.exists(d.audit.path)


def test_config_id_wins_and_the_change_is_audited_once(fake_host, tmp_path):
    make(fake_host, tmp_path)._save_state()
    cfg = parse_config('[host]\nid = "web-app"\n')
    d = make(fake_host, tmp_path, cfg)
    assert d.host_id == "web-app"
    (row,) = audit_rows(d)
    assert (row["op"], row["before"], row["after"], row["host"]) == (
        "host_id_changed", "build-a-example-net", "web-app", "web-app",
    )  # fmt: skip
    d._save_state()
    assert saved(tmp_path)["host_id"] == "web-app"
    again = make(fake_host, tmp_path, cfg)  # the same config on the next start: nothing new
    assert again.host_id == "web-app"
    assert len(audit_rows(again)) == 1


def test_config_id_on_a_first_start_is_not_a_change(fake_host, tmp_path):
    d = make(fake_host, tmp_path, parse_config('[host]\nid = "build-a"\n'))
    assert d.host_id == "build-a"
    assert not os.path.exists(d.audit.path)


def test_old_state_without_an_id_is_filled_in(fake_host, tmp_path):
    make(fake_host, tmp_path)._save_state()
    body = saved(tmp_path)
    del body["host_id"]
    (tmp_path / "state" / "state.json").write_text(json.dumps(body))
    assert StateStore(str(tmp_path / "state")).load_host_id() is None
    d = make(fake_host, tmp_path)
    assert d.host_id == "build-a-example-net"
    assert not os.path.exists(d.audit.path)
    d._save_state()
    assert saved(tmp_path)["host_id"] == "build-a-example-net"


def test_status_carries_the_id_and_validates(fake_host, tmp_path):
    d = make(fake_host, tmp_path)
    s = d.tick()
    assert s["host"]["id"] == "build-a-example-net"
    assert isinstance(s["host"]["cores"], int)
    assert validate_status(s) == []
    assert (
        d.dispatch({"v": 1, "op": "status", "args": {}}, 0)["status"]["host"]["id"] == "build-a-example-net"
    )


def test_every_audit_line_carries_the_host(fake_host, tmp_path):
    d = make(fake_host, tmp_path)
    d.audit.append({"ts": "t", "op": "set", "host": "spoofed"})
    d._audit_reload("reload", "no changes")
    assert [r["host"] for r in audit_rows(d)] == ["build-a-example-net"] * 2


def test_alert_events_and_active_alerts_carry_the_host():
    w = Watch(lambda: T0)
    w.host = "build-a"
    assert w.emit_event("pause")["host"] == "build-a"
    assert w.resolved("k", "t")["host"] == "build-a"
    assert w.info("k", "t", "m")["host"] == "build-a"
    assert w.owner_change({}, "set", "r")["host"] == "build-a"
    s = {"memory": {"avail_mb": 100}, "swap": {"used_mb": 0}, "disk": {"free_gb": 99.0},
         "slots": {}, "heavy_slot": {"stuck": False}}  # fmt: skip
    events = w.evaluate(s, 10, 65, "balanced", None, [])
    assert events and all(e["host"] == "build-a" for e in events)
    summary = w.summary()
    assert summary["active"] and all(a["host"] == "build-a" for a in summary["active"])
    assert all(e["host"] == "build-a" for e in summary["events"])


def test_ha_push_body_has_the_host():
    w = Watch(lambda: T0)
    w.host = "build-a"
    body = json.loads(json.dumps({"v": 1, **w.emit_event("pause")}))
    assert body["host"] == "build-a"


def test_loadctl_status_first_line_names_the_host(fake_host):
    st = status(fake_host("calm"))
    st["host"]["id"] = "build-a"
    head = loadctl.format_status(st).splitlines()[0]
    assert head.startswith("loadgov build-a 20")
    assert head.endswith("level " + st["level"])
    assert loadctl.format_hwmon(st).startswith("hwmon 20")  # the legacy format is unchanged
    del st["host"]["id"]  # an older daemon
    assert loadctl.format_status(st).startswith("loadgov 20")


def test_reload_keeps_the_running_host_id(fake_host, tmp_path):
    cfg_file = tmp_path / "cfg.toml"
    cfg_file.write_text('[host]\nid = "one"\n')
    d = make(fake_host, tmp_path, load_config(str(cfg_file)))
    d.config_path = str(cfg_file)
    cfg_file.write_text('[host]\nid = "two"\n')
    d.reload_config()
    assert d.host_id == "one"
    assert d.config.host.id == "one"
    assert any(r["op"] == "needs restart" and "host" in r["reason"] for r in audit_rows(d))
