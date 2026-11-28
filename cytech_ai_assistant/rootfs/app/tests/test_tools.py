import pytest
from anthropic.lib.tools import ToolError

import tools
from ha_client import HAError

STATES = [
    {
        "entity_id": "sensor.comfort_system_status",
        "state": "Trouble",
        "attributes": {"friendly_name": "Comfort System Status"},
    },
    {
        "entity_id": "binary_sensor.cytech_comfort_mqtt_input003",
        "state": "on",
        "attributes": {"friendly_name": "Zone 3"},
    },
    {
        "entity_id": "alarm_control_panel.comfort_alarm",
        "state": "disarmed",
        "attributes": {"friendly_name": "Comfort Alarm"},
    },
]


class FakeHA:
    def __init__(self, states=None, config=None, history=None, log=""):
        self._states = STATES if states is None else states
        self._config = config or {}
        self._history = history or []
        self._log = log
        self.history_calls = []

    def states(self):
        return self._states

    def config(self):
        return self._config

    def state(self, entity_id):
        for item in self._states:
            if item.get("entity_id") == entity_id:
                return item
        raise HAError("Home Assistant has no record for that item.", 404)

    def history(self, entity_id, hours):
        self.history_calls.append((entity_id, hours))
        return self._history

    def error_log(self):
        return self._log


def test_list_entities_filters_domain():
    tools.begin_turn(FakeHA(), None)
    out = tools.list_entities(domain="binary_sensor")

    assert "1 matching entities" in out
    assert "binary_sensor.cytech_comfort_mqtt_input003" in out
    assert "sensor.comfort_system_status" not in out


def test_list_entities_filters_name():
    tools.begin_turn(FakeHA(), None)
    out = tools.list_entities(name_contains="ALARM")

    assert "1 matching entities" in out
    assert "alarm_control_panel.comfort_alarm" in out
    assert "sensor.comfort_system_status" not in out


def test_list_entities_rejects_bad_domain():
    tools.begin_turn(FakeHA(), None)
    with pytest.raises(ToolError, match="domain filter"):
        tools.list_entities(domain="sensor;drop")


def test_get_entity_reads_state():
    tools.begin_turn(FakeHA(), None)
    out = tools.get_entity("sensor.comfort_system_status")

    assert '"state": "Trouble"' in out
    assert "Comfort System Status" in out


def test_get_entity_rejects_bad_id():
    tools.begin_turn(FakeHA(), None)
    with pytest.raises(ToolError, match="not a valid"):
        tools.get_entity("sensor.comfort system")


def test_get_entity_missing_entity():
    tools.begin_turn(FakeHA(), None)
    with pytest.raises(ToolError, match="no record"):
        tools.get_entity("sensor.does_not_exist")


def test_get_entity_redacts_attributes():
    states = [
        {
            "entity_id": "sensor.secretive",
            "state": "1",
            "attributes": {"friendly_name": "Secretive", "password": "hunter2"},
        }
    ]
    tools.begin_turn(FakeHA(states=states), None)
    out = tools.get_entity("sensor.secretive")

    assert "hunter2" not in out
    assert "[redacted]" in out


def test_get_history_flattens_and_clamps():
    points = [[{"last_changed": f"t{i}", "state": f"s{i}"} for i in range(150)]]
    ha = FakeHA(history=points)
    tools.begin_turn(ha, None)

    out = tools.get_history("sensor.comfort_system_status", hours=0)

    assert ha.history_calls == [("sensor.comfort_system_status", 1)]
    assert "100 recent state change(s)" in out
    assert "t149  s149" in out
    assert "t49  s49" not in out


def test_get_history_empty():
    tools.begin_turn(FakeHA(history=[]), None)
    out = tools.get_history("sensor.comfort_system_status", hours=24)

    assert "No state changes" in out


def test_log_errors_filters_noise():
    log = "\n".join(
        [
            "2026-11-27 10:00:00 ERROR something failed",
            "2026-11-27 10:00:01 INFO all good",
            "2026-11-27 10:00:02 WARNING comfort bridge slow",
            "Traceback (most recent call last):",
        ]
    )
    tools.begin_turn(FakeHA(log=log), None)

    out = tools.get_log_errors()

    assert "something failed" in out
    assert "comfort bridge slow" in out
    assert "Traceback" in out
    assert "all good" not in out


def test_log_errors_pattern_and_clamp():
    log = "\n".join(
        f"2026-11-27 10:00:{i:02d} ERROR failure {i}" for i in range(30)
    )
    tools.begin_turn(FakeHA(log=log), None)

    out = tools.get_log_errors(lines=5, pattern="")

    assert "20 matching log line(s)" in out
    assert "failure 29" in out
    assert "failure 9" not in out


def test_log_errors_pattern():
    log = (
        "2026-11-27 10:00:00 ERROR other thing\n"
        "2026-11-27 10:00:01 WARNING Comfort bridge offline"
    )
    tools.begin_turn(FakeHA(log=log), None)

    out = tools.get_log_errors(pattern="comfort")

    assert "Comfort bridge offline" in out
    assert "other thing" not in out


def test_log_errors_none_found():
    tools.begin_turn(FakeHA(log="2026-11-27 10:00:00 INFO quiet"), None)
    out = tools.get_log_errors()
    assert "No ERROR or WARNING lines" in out


def test_system_info_lists_comfort_entities():
    config = {
        "version": "2026.11.1",
        "time_zone": "Europe/London",
        "unit_system": {"temperature": "°C"},
    }
    tools.begin_turn(FakeHA(config=config), None)
    out = tools.get_system_info()

    assert "Home Assistant version: 2026.11.1" in out
    assert "Europe/London" in out
    assert "Entities: 3" in out
    assert "sensor.comfort_system_status = Trouble" in out


def test_system_info_without_comfort():
    states = [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Kitchen"},
        }
    ]
    tools.begin_turn(FakeHA(states=states, config={"version": "1"}), None)
    out = tools.get_system_info()

    assert "No Comfort entities found" in out


def test_emits_tool_events():
    events = []
    tools.begin_turn(FakeHA(), events.append)

    tools.get_entity("sensor.comfort_system_status")

    assert events[0] == {
        "type": "tool",
        "status": "start",
        "name": "get_entity",
        "summary": "sensor.comfort_system_status",
    }
    assert events[-1] == {
        "type": "tool",
        "status": "end",
        "name": "get_entity",
    }


def test_tool_call_limit():
    tools.begin_turn(FakeHA(), None)

    for _ in range(tools.MAX_TOOL_CALLS):
        tools.list_entities()

    with pytest.raises(ToolError, match="limit"):
        tools.list_entities()


def test_missing_ha_client():
    tools.begin_turn(None, None)
    with pytest.raises(ToolError, match="not configured"):
        tools.list_entities()
