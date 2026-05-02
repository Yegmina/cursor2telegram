import pytest

from cursor2telegram.streaming import StreamAggregator, TextBuffer


@pytest.mark.asyncio
async def test_stream_aggregator_text_chunks():
    agg = StreamAggregator(throttle_ms=10)
    await agg.feed({"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "Hi "}}})
    await agg.feed({"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "there"}}})
    await agg.feed({"update": {"sessionUpdate": "stop", "stopReason": "end_turn"}})
    await agg.close()
    events = []
    async for ev in agg.events():
        events.append(ev)
    kinds = [e.kind for e in events]
    assert kinds[:2] == ["text_chunk", "text_chunk"]
    assert events[0].text == "Hi "
    assert events[1].text == "there"
    assert "stop" in kinds


@pytest.mark.asyncio
async def test_stream_aggregator_tool_call():
    agg = StreamAggregator()
    await agg.feed(
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "abc",
                "name": "ShellExec",
                "args": {"cmd": "ls"},
            }
        }
    )
    await agg.feed(
        {
            "update": {
                "sessionUpdate": "tool_call_end",
                "toolCallId": "abc",
                "result": {"text": "file1\nfile2"},
            }
        }
    )
    await agg.close()
    events = [ev async for ev in agg.events()]
    assert events[0].kind == "tool_call_start"
    assert events[0].data["name"] == "ShellExec"
    assert events[1].kind == "tool_call_end"
    assert events[1].data["result"]["text"] == "file1\nfile2"


@pytest.mark.asyncio
async def test_stream_aggregator_current_acp_shell_update_shape():
    agg = StreamAggregator()
    await agg.feed(
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "title": "Terminal",
                "kind": "execute",
                "status": "pending",
                "rawInput": {},
            }
        }
    )
    await agg.feed(
        {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tool-1",
                "status": "completed",
                "rawOutput": {
                    "exitCode": 0,
                    "stdout": "/workspace\nACP_TOOL_OK\n",
                    "stderr": "",
                },
            }
        }
    )
    await agg.close()
    events = [ev async for ev in agg.events()]
    assert events[0].kind == "tool_call_start"
    assert events[0].data["name"] == "Terminal"
    assert events[1].kind == "tool_call_end"
    assert events[1].data["result"]["stdout"] == "/workspace\nACP_TOOL_OK\n"


def test_text_buffer_throttle():
    import time

    buf = TextBuffer(throttle_s=0.1)
    buf.append("hello ")
    # First flush is intentionally immediate so the user sees text fast.
    assert buf.should_flush()
    assert buf.flush() == "hello "
    # After flushing, the throttle window applies.
    buf.append("world")
    assert not buf.should_flush()
    time.sleep(0.11)
    assert buf.should_flush()
    assert buf.flush() == "world"
    assert not buf.should_flush()
