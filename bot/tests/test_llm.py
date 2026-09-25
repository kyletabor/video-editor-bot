import json

import pytest

from clipbot import llm


def test_chunk_text_never_splits_a_block():
    blocks = [f"[0:00:{i:02d}] K: " + "x" * 90 for i in range(10)]
    text = "\n\n".join(blocks)
    chunks = llm.chunk_text(text, limit=250)
    assert len(chunks) > 1
    assert "\n\n".join(chunks) == text
    for c in chunks:
        assert len(c) <= 250 and all(b in blocks for b in c.split("\n\n"))


def test_validate_specs_drops_nonsense():
    specs = [
        {"start": 10, "end": 40, "title": "ok", "why": "Decision", "score": 8},
        {"start": 50, "end": 55, "title": "too short", "why": "", "score": 9},
        {"start": 100, "end": 90, "title": "backwards", "why": "", "score": 9},
        {"start": 3000, "end": 3030, "title": "past the end", "why": "", "score": 9},
        {"start": "x", "end": 30, "title": "bad number", "why": "", "score": 9},
        {"start": 200, "end": 230, "title": "t" * 200, "why": "w" * 200, "score": "nope"},
    ]
    out = llm.validate_specs(specs, duration=1000)
    assert [m["start"] for m in out] == [10, 200]
    assert len(out[1]["title"]) == 80 and len(out[1]["why"]) == 120 and out[1]["score"] == 5.0


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, moments, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [_Block(json.dumps({"moments": moments}))]


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

        client = self

        class _Messages:
            def create(self, **kw):
                client.requests.append(kw)
                return client.responses.pop(0)

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


def test_propose_moments_sends_structured_request_and_validates():
    good = {"start": 30, "end": 60, "title": "Decision on the schema", "why": "Decision", "score": 9}
    bad = {"start": 5, "end": 9, "title": "too short", "why": "", "score": 10}
    client = _FakeClient([_Resp([good, bad])])
    logs = []
    out = llm.propose_moments("[0:00:00] K: hello.", minutes=4, duration=600, client=client, log=logs.append)
    assert out == [dict(good, start=30.0, end=60.0, score=9.0)]
    req = client.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["format"]["schema"] == llm.SCHEMA
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["messages"][0]["role"] == "user" and "[0:00:00] K: hello." in req["messages"][0]["content"]
    assert any("proposed 2 moments" in line for line in logs)


def test_propose_moments_chunks_long_outlines():
    blocks = "\n\n".join(f"[0:{i:02d}:00] K: " + "words " * 200 for i in range(60))
    client = _FakeClient([_Resp([]) for _ in range(10)])
    llm.propose_moments(blocks, minutes=4, duration=3600, client=client)
    assert 2 <= len(client.requests) <= 10
    assert all(len(r["messages"][0]["content"]) < llm.CHUNK_CHARS + 2000 for r in client.requests)


def test_propose_moments_refusal_or_truncation_raises():
    with pytest.raises(RuntimeError, match="declined"):
        llm.propose_moments("x", minutes=4, duration=600, client=_FakeClient([_Resp([], "refusal")]))
    with pytest.raises(RuntimeError, match="cut off"):
        llm.propose_moments("x", minutes=4, duration=600, client=_FakeClient([_Resp([], "max_tokens")]))


def test_has_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert not llm.has_credentials()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llm.has_credentials()
