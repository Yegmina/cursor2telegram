"""ACP client integration tests using a mock Python ACP server."""

from __future__ import annotations

import asyncio
import sys
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock

import pytest

from cursor2telegram.acp import AcpClient, AcpError, initialize, session_new, session_prompt


@pytest.fixture
def mock_acp_server(tmp_path):
    script = tmp_path / "fake_acp.py"
    script.write_text(
        dedent(
            '''
            import json, sys, threading, time

            def write(msg):
                sys.stdout.write(json.dumps(msg) + "\\n")
                sys.stdout.flush()

            def stream_response(sid):
                # Simulate streaming text + a tool call + final stop.
                for chunk in ["Hello ", "world!"]:
                    write({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {"sessionUpdate": "agent_message_chunk",
                                        "content": {"text": chunk}}
                        }
                    })
                    time.sleep(0.01)
                write({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {"sessionUpdate": "tool_call",
                                    "toolCallId": "t1",
                                    "name": "EchoTool",
                                    "args": {"x": 1}}
                    }
                })
                write({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {"sessionUpdate": "tool_call_end",
                                    "toolCallId": "t1",
                                    "result": {"text": "ok"}}
                    }
                })

            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                method = msg.get("method")
                if method == "initialize":
                    write({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
                elif method == "authenticate":
                    write({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}})
                elif method == "session/new":
                    write({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "S1"}})
                elif method == "session/prompt":
                    sid = msg["params"]["sessionId"]
                    threading.Thread(target=stream_response, args=(sid,), daemon=True).start()
                    time.sleep(0.05)
                    write({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
                elif method == "session/cancel":
                    pass
                elif method == "session/throw":
                    write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32001, "message": "nope"}})
                else:
                    write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not implemented"}})
            '''
        ).lstrip()
    )
    return [sys.executable, str(script)]


@pytest.mark.asyncio
async def test_initialize_session_prompt_streams(mock_acp_server):
    client = AcpClient(mock_acp_server)
    received = []

    async def on_update(_method, params):
        received.append(params)

    client.on("session/update", on_update)
    async with client:
        init = await initialize(client, client_name="test", client_version="0")
        assert init["protocolVersion"] == 1
        new = await session_new(client, cwd=".")
        sid = new["sessionId"]
        result = await session_prompt(client, session_id=sid, text="hi")
        assert result["stopReason"] == "end_turn"
        # Give the streamer a moment to land all messages.
        await asyncio.sleep(0.1)
    kinds = [r["update"]["sessionUpdate"] for r in received]
    assert "agent_message_chunk" in kinds
    assert "tool_call" in kinds
    assert "tool_call_end" in kinds


@pytest.mark.asyncio
async def test_error_response_raises(mock_acp_server):
    client = AcpClient(mock_acp_server)
    async with client:
        with pytest.raises(AcpError):
            await client.request("session/throw", {})


@pytest.mark.asyncio
async def test_session_new_omits_mcp_servers_when_none(tmp_path):
    client = MagicMock()
    client.request = AsyncMock(return_value={"sessionId": "sid-1"})
    cwd = str(tmp_path / "workspace")

    await session_new(client, cwd=cwd)
    client.request.assert_awaited_once_with("session/new", {"cwd": cwd})

    client.request.reset_mock()
    await session_new(client, cwd=cwd, mcp_servers=[])
    client.request.assert_awaited_once_with(
        "session/new", {"cwd": cwd, "mcpServers": []}
    )


@pytest.mark.asyncio
async def test_server_to_client_request_handler(tmp_path):
    """Verify AcpClient can answer server-to-client JSON-RPC requests."""

    script = tmp_path / "server_request.py"
    script.write_text(
        '''
import json, sys, time

def write(msg):
    sys.stdout.write(json.dumps(msg) + "\\n")
    sys.stdout.flush()

# Wait for one client message (initialize) then issue our own request to client.
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        write({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
        # Now send a request *to* the client.
        write({"jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
               "params": {"toolCallId": "tc-1", "description": "test"}})
        # Read its response.
        for line2 in sys.stdin:
            resp = json.loads(line2)
            if resp.get("id") == 99 and "result" in resp:
                outcome = resp["result"].get("outcome", {}).get("optionId")
                # Echo it back so the test can verify.
                write({"jsonrpc": "2.0", "method": "test/echo", "params": {"got": outcome}})
                sys.stdout.flush()
                time.sleep(0.05)
                sys.exit(0)
        break
'''
    )

    client = AcpClient([sys.executable, str(script)])
    received: dict[str, str] = {}

    async def handle_perm(_method: str, params: dict):
        return {"outcome": {"outcome": "selected", "optionId": "allow-once"}}

    async def echo_back(_method: str, params: dict):
        received["got"] = params["got"]

    client.on_request("session/request_permission", handle_perm)
    client.on("test/echo", echo_back)
    async with client:
        await client.request("initialize", {"protocolVersion": 1})
        await asyncio.sleep(0.4)
    assert received.get("got") == "allow-once"
