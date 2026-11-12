"""The drafted automations parse, and the daemon-down one clears its alert whenever the sensor is healthy."""

from pathlib import Path

import jinja2
import yaml

AUTOMATIONS = Path(__file__).resolve().parents[1] / "automations"


def load(name):
    return yaml.safe_load((AUTOMATIONS / name).read_text())


def test_every_automation_parses():
    for path in sorted(AUTOMATIONS.glob("*.yaml")):
        assert yaml.safe_load(path.read_text()), path.name


def render(text, ctx):
    return jinja2.Environment().from_string(text).render(**ctx) if isinstance(text, str) else text


def holds(cond, trigger_id, states, ctx):
    kind = cond["condition"]
    if kind == "trigger":
        ids = cond["id"] if isinstance(cond["id"], list) else [cond["id"]]
        return trigger_id in ids
    if kind == "state":
        want = cond["state"] if isinstance(cond["state"], list) else [cond["state"]]
        return states[cond["entity_id"]] in want
    if kind == "template":
        return render(cond["value_template"], ctx).strip() == "True"
    if kind == "not":
        return not all(holds(c, trigger_id, states, ctx) for c in cond["conditions"])
    raise AssertionError(kind)


class Item(dict):
    """`repeat.item` in a template."""


def run(steps, trigger_id, states, done, ctx):
    """A tiny evaluator of the automation actions: choose / if / parallel / repeat and the services."""
    for step in steps:
        if "choose" in step:
            for opt in step["choose"]:
                if all(holds(c, trigger_id, states, ctx) for c in opt["conditions"]):
                    run(opt["sequence"], trigger_id, states, done, ctx)
                    break
        elif "if" in step:
            if all(holds(c, trigger_id, states, ctx) for c in step["if"]):
                run(step["then"], trigger_id, states, done, ctx)
            else:
                run(step.get("else", []), trigger_id, states, done, ctx)
        elif "parallel" in step:
            run(step["parallel"], trigger_id, states, done, ctx)
        elif "repeat" in step:
            assert step["repeat"]["for_each"] == "{{ phones }}"  # the only for_each the drafts use
            for phone in ctx["phones"]:
                run(step["repeat"]["sequence"], trigger_id, states, done, {**ctx, "repeat": Item(item=phone)})
        elif "action" in step:
            act = render(step["action"], ctx)
            if act == "input_boolean.turn_on":
                states["input_boolean.loadgov_down_alerted"] = "on"
            elif act == "input_boolean.turn_off":
                states["input_boolean.loadgov_down_alerted"] = "off"
            done.append("clear" if step.get("data", {}).get("message") == "clear_notification" else act)


PHONES = ["notify.phone_a", "notify.phone_b"]


def outcome(trigger_id, sensor, flag, phones=None, record_script=""):
    (auto,) = load("loadgov_daemon_down.yaml")
    ctx = {"phones": phones or PHONES, "record_script": record_script, "alert_path": "/"}
    states = {"sensor.loadgovd_level": sensor, "input_boolean.loadgov_down_alerted": flag}
    done = []
    run(auto["actions"], trigger_id, states, done, ctx)
    return done, states["input_boolean.loadgov_down_alerted"]


def test_daemon_down_alerts_once_and_never_clears_while_still_down():
    for trig in ("down", "start_check"):
        done, flag = outcome(trig, "unavailable", "off")
        assert "input_boolean.turn_on" in done and "notify.phone_a" in done and flag == "on"
        assert "notify.phone_b" in done
        # restart during an outage / unavailable -> unknown -> unavailable: already alerted, still down
        for sensor in ("unavailable", "unknown"):
            done, flag = outcome(trig, sensor, "on")
            assert done == [] and flag == "on", (trig, sensor, done)


def test_every_notify_and_record_target_is_a_variable_with_a_placeholder_default():
    for path in sorted(AUTOMATIONS.glob("*.yaml")):
        (auto,) = yaml.safe_load(path.read_text())
        assert auto["variables"]["phones"] == ["notify.YOUR_PHONE"], path.name
        assert auto["variables"]["record_script"] == "" and auto["variables"]["alert_path"] == "/"
        text = path.read_text()
        assert "notify.mobile_app" not in text and "script.record_" not in text, path.name


def test_the_record_script_is_optional():
    done, _ = outcome("down", "unavailable", "off")
    assert "script.record_events" not in done and len(done) == 3  # turn_on and two phones
    done, _ = outcome("down", "unavailable", "off", record_script="script.record_events")
    assert "script.record_events" in done


def test_daemon_down_clears_when_the_sensor_is_healthy():
    done, flag = outcome(  # HA started while a stale alert was left behind
        "start_check", "OK", "on", record_script="script.record_events"
    )
    assert "clear" in done and "input_boolean.turn_off" in done and flag == "off"
    assert "script.record_events" in done
    done, flag = outcome("start_check", "OK", "off", record_script="script.record_events")  # no false "back"
    assert "input_boolean.turn_off" not in done and "script.record_events" not in done
    assert "clear" in done
    back = next(t for t in load("loadgov_daemon_down.yaml")[0]["triggers"] if t["id"] == "back")
    assert back["from"] == ["unavailable", "unknown"] and back["not_to"] == ["unavailable", "unknown"]
