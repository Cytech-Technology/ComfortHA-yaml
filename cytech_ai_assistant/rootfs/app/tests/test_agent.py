from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from agent import Agent, Session, UserError
from config import Settings


def make_settings(**overrides):
    values = dict(
        proxy_url="https://proxy.test",
        installation_token="cyc_testtoken1234567890",
        model="claude-sonnet-5-5",
        ha_base_url="http://ha.test/api",
        ha_token="ha-token",
        port=8099,
        data_dir=".",
        log_level="INFO",
    )
    values.update(overrides)
    return Settings(**values)


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block():
    return SimpleNamespace(
        type="tool_use",
        id="toolu_1",
        name="get_entity",
        input={"entity_id": "sensor.x"},
    )


def message(blocks, stop_reason):
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=2,
        cache_creation_input_tokens=1,
    )
    return SimpleNamespace(
        content=blocks, stop_reason=stop_reason, usage=usage
    )


class FakeRunner:
    def __init__(self, messages, responses=None, raises=None):
        self.messages = messages
        self.responses = list(responses or [])
        self.raises = raises
        self.response_calls = 0
        self.closed = False

    def __iter__(self):
        if self.raises is not None:
            raise self.raises
        return iter(self.messages)

    def generate_tool_call_response(self):
        self.response_calls += 1
        return self.responses.pop(0) if self.responses else None

    def close(self):
        self.closed = True


class FakeRunnerFactory:
    def __init__(self, runners):
        self.runners = list(runners)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.runners.pop(0)


class FakeMessages:
    def __init__(self, factory):
        self.tool_runner = factory


class FakeBeta:
    def __init__(self, factory):
        self.messages = FakeMessages(factory)


class FakeClient:
    def __init__(self, factory):
        self.beta = FakeBeta(factory)


def make_agent(settings, factory):
    agent = Agent(settings, ha=object())
    agent._client = FakeClient(factory)
    return agent


def test_successful_turn(settings=None):
    settings = settings or make_settings()
    runner = FakeRunner([message([text_block("Hello")], "end_turn")])
    factory = FakeRunnerFactory([runner])
    agent = make_agent(settings, factory)
    session = Session()
    events = []

    agent.run(session, "hi", events.append)

    texts = [e for e in events if e["type"] == "text"]
    assert [e["text"] for e in texts] == ["Hello"]
    usage = next(e for e in events if e["type"] == "usage")
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cache_read_tokens"] == 2
    assert usage["cache_creation_tokens"] == 1

    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.turns == 1

    kwargs = factory.calls[0]
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "medium"}
    assert kwargs["max_tokens"] == 4096
    assert kwargs["thread"] is False


def test_haiku_omits_thinking():
    settings = make_settings(model="claude-haiku-4-5")
    runner = FakeRunner([message([text_block("Hi")], "end_turn")])
    factory = FakeRunnerFactory([runner])
    agent = make_agent(settings, factory)

    agent.run(Session(), "hi", lambda event: None)

    kwargs = factory.calls[0]
    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


def test_tool_turn_keeps_history_valid():
    tool_result = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "ok",
            }
        ],
    }
    runner = FakeRunner(
        [
            message([tool_block()], "tool_use"),
            message([text_block("Done")], "end_turn"),
        ],
        responses=[tool_result],
    )
    factory = FakeRunnerFactory([runner])
    agent = make_agent(make_settings(), factory)
    session = Session()

    agent.run(session, "check the zone", lambda event: None)

    # One call per yielded message; only the tool_use message returns a result.
    assert runner.response_calls == 2
    assert [m["role"] for m in session.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert session.messages[2] == tool_result


def test_pause_turn_restarts():
    first = FakeRunner([message([text_block("partial")], "pause_turn")])
    second = FakeRunner([message([text_block("finished")], "end_turn")])
    factory = FakeRunnerFactory([first, second])
    agent = make_agent(make_settings(), factory)
    events = []

    agent.run(Session(), "hi", events.append)

    assert len(factory.calls) == 2
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert texts == ["partial", "finished"]


def test_pause_turn_gives_up_after_two_restarts():
    runners = [
        FakeRunner([message([text_block("again")], "pause_turn")])
        for _ in range(3)
    ]
    factory = FakeRunnerFactory(runners)
    agent = make_agent(make_settings(), factory)
    events = []

    agent.run(Session(), "hi", events.append)

    assert len(factory.calls) == 3
    assert any(
        e["type"] == "status" and "could not finish" in e["message"]
        for e in events
    )


def test_truncated_tool_call_is_not_executed():
    runner = FakeRunner([message([tool_block()], "max_tokens")])
    factory = FakeRunnerFactory([runner])
    agent = make_agent(make_settings(), factory)
    session = Session()

    with pytest.raises(UserError, match="too long"):
        agent.run(session, "hi", lambda event: None)

    assert runner.response_calls == 0
    assert runner.closed is True
    assert session.messages == []


def test_refused_tool_call_is_not_executed():
    runner = FakeRunner([message([tool_block()], "refusal")])
    factory = FakeRunnerFactory([runner])
    agent = make_agent(make_settings(), factory)
    session = Session()

    with pytest.raises(UserError, match="rephrase"):
        agent.run(session, "hi", lambda event: None)

    assert runner.response_calls == 0
    assert runner.closed is True
    assert session.messages == []


def _api_error(cls, status, headers=None):
    request = httpx2.Request("POST", "https://proxy.test/v1/messages")
    response = httpx2.Response(
        status,
        request=request,
        headers=headers or {},
        json={"type": "error", "error": {"type": "x", "message": "x"}},
    )
    return cls("error", response=response, body={})


def test_invalid_token_is_friendly():
    runner = FakeRunner(
        [], raises=_api_error(anthropic.AuthenticationError, 401)
    )
    agent = make_agent(make_settings(), FakeRunnerFactory([runner]))

    with pytest.raises(UserError, match="not valid"):
        agent.run(Session(), "hi", lambda event: None)


def test_quota_is_friendly():
    error = _api_error(
        anthropic.RateLimitError, 429, headers={"retry-after": "3600"}
    )
    agent = make_agent(make_settings(), FakeRunnerFactory([]))

    text = agent._friendly_error(error)

    assert "monthly AI allowance" in text
    assert "resets on" in text
    assert "(UTC)" in text


def test_exhausted_turns_start_a_new_conversation():
    settings = make_settings()
    runner = FakeRunner([message([text_block("ok")], "end_turn")])
    factory = FakeRunnerFactory([runner])
    agent = make_agent(settings, factory)
    session = Session()
    session.turns = 20
    events = []

    agent.run(session, "hi", events.append)

    assert session.turns == 1
    assert len(session.messages) == 2
    assert any(
        e["type"] == "status" and "length limit" in e["message"]
        for e in events
    )
