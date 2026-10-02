import asyncio
import json

import httpx
import pytest

from experiments.task_state.bounded_inference import _generate
from experiments.task_state.prototype import InvalidProposalError

MODEL = "llama3.1:8b"


class Stream(httpx.AsyncByteStream):
    def __init__(self, parts, stall=False):
        self.parts = parts
        self.stall = stall
        self.closed = False
        self.sent = 0

    async def __aiter__(self):
        for part in self.parts:
            if self.stall:
                await asyncio.sleep(2)
            self.sent += 1
            yield part

    async def aclose(self):
        self.closed = True


def wire(**values):
    return json.dumps({"model": MODEL, **values}).encode() + b"\n"


def install(monkeypatch, stream, stall_connect=False):
    client_type = httpx.AsyncClient

    async def handler(request):
        if stall_connect:
            await asyncio.sleep(2)
        assert str(request.url) == "http://127.0.0.1:11434/api/generate"
        return httpx.Response(200, stream=stream)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handler)),
    )


def request(deadline=1):
    return _generate(
        MODEL, "synthetic", schema={}, options={}, deadline_seconds=deadline, think=None
    )


def test_normal_chunks_are_assembled_only_after_verified_terminal(monkeypatch):
    body = wire(response='{"events":', done=False) + wire(
        response="[]}",
        done=True,
        done_reason="stop",
    )
    stream = Stream([body[:15], body[15:-1], body[-1:]])
    install(monkeypatch, stream)
    assert asyncio.run(request())["response"] == '{"events":[]}'
    assert stream.closed


@pytest.mark.parametrize(
    "parts,reason",
    [
        ([wire(response="private", done=False)], "generation_end_unobserved"),
        ([wire(done=True, done_reason="length")], "generation_incomplete"),
        ([wire(done=True, done_reason="other")], "generation_stop_unverified"),
        ([wire(model="other")], "response_model_mismatch"),
        ([b"x" * 524289], "model_output_budget"),
        ([wire(response={})], "generation_invalid_fragment"),
    ],
)
def test_invalid_stream_never_returns_a_candidate(monkeypatch, parts, reason):
    stream = Stream(parts)
    install(monkeypatch, stream)
    with pytest.raises(InvalidProposalError, match=reason):
        asyncio.run(request())
    assert stream.closed


@pytest.mark.parametrize("connect", [False, True])
def test_deadline_cancels_connection_or_first_byte_and_never_accepts_late_reply(
    monkeypatch, connect
):
    stream = Stream([wire(done=True, done_reason="stop", response="private")], stall=not connect)
    install(monkeypatch, stream, stall_connect=connect)

    async def check():
        started = asyncio.get_running_loop().time()
        with pytest.raises(InvalidProposalError, match="generation_deadline_unobserved"):
            await request(0.03)
        await asyncio.sleep(0.04)
        assert stream.sent == 0
        assert asyncio.get_running_loop().time() - started < 0.5

    asyncio.run(check())
    if not connect:
        assert stream.closed


def test_external_cancellation_propagates_without_partial_acceptance(monkeypatch):
    stream = Stream([wire(done=True, done_reason="stop")], stall=True)
    install(monkeypatch, stream)

    async def check():
        task = asyncio.create_task(request())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.sent == 0

    asyncio.run(check())
    assert stream.closed
