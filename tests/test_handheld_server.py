"""Tests for the Handheld-oriented server."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from nanobot.bus.events import OutboundMessage
from nanobot.handheld.server import create_app
from nanobot.session.manager import SessionManager

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)


class FakeLoop:
    def __init__(self, workspace: Path) -> None:
        self.sessions = SessionManager(workspace)
        self.connect_calls = 0
        self.close_calls = 0

    async def _connect_mcp(self) -> None:
        self.connect_calls += 1

    async def close_mcp(self) -> None:
        self.close_calls += 1

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress=None,
        on_stream=None,
        on_stream_end=None,
        hooks=None,
    ):
        hook = hooks[0] if hooks else None
        if hook is not None:
            from nanobot.agent.hook import AgentHookContext

            context = AgentHookContext(iteration=0, messages=[])
            await hook.before_iteration(context)
            await hook.on_stream(context, "hello")
            await hook.on_stream(context, " world")
            context.final_content = "hello world"
            await hook.after_iteration(context)

        session = self.sessions.get_or_create(session_key)
        session.add_message("user", content)
        session.add_message("assistant", "hello world")
        session.metadata.setdefault("title", content[:80])
        self.sessions.save(session)
        return OutboundMessage(channel=channel, chat_id=chat_id, content="hello world")


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_health_is_public_but_status_requires_auth(tmp_path: Path, aiohttp_client) -> None:
    app = create_app(FakeLoop(tmp_path), model_name="test-model", auth_token="secret")
    client = await aiohttp_client(app)

    health = await client.get("/health")
    assert health.status == 200
    assert (await health.json())["status"] == "ok"

    status = await client.get("/status")
    assert status.status == 401


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_status_and_init_status_return_authenticated_snapshots(
    tmp_path: Path, aiohttp_client
) -> None:
    app = create_app(FakeLoop(tmp_path), model_name="test-model", auth_token="secret")
    client = await aiohttp_client(app)
    headers = {"Authorization": "Bearer secret"}

    status = await client.get("/status", headers=headers)
    init_status = await client.get("/init-status", headers=headers)

    assert status.status == 200
    assert (await status.json())["status"] == "healthy"
    assert init_status.status == 200
    assert (await init_status.json())["status"] == "ready"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_websocket_auth_flow_send_message_and_history(
    tmp_path: Path, aiohttp_client
) -> None:
    app = create_app(FakeLoop(tmp_path), model_name="test-model", auth_token="secret")
    client = await aiohttp_client(app)

    websocket = await client.ws_connect("/ws")
    await websocket.send_json({
        "type": "auth",
        "request_id": "req-auth",
        "payload": {"token": "secret"},
    })
    auth_ok = await websocket.receive_json()
    server_status = await websocket.receive_json()
    init_status = await websocket.receive_json()

    assert auth_ok["type"] == "auth_ok"
    assert server_status["type"] == "server_status"
    assert init_status["type"] == "init_status"

    await websocket.send_json({
        "type": "send_message",
        "request_id": "req-1",
        "session_id": "session-alpha",
        "payload": {"text": "hello handheld"},
    })

    events = [await websocket.receive_json() for _ in range(9)]
    event_types = [event["type"] for event in events]

    assert event_types == [
        "session_state",
        "task_status",
        "task_status",
        "message_start",
        "message_delta",
        "message_delta",
        "message_end",
        "session_state",
        "task_status",
    ]

    await websocket.send_json({
        "type": "get_history",
        "request_id": "req-2",
        "session_id": "session-alpha",
    })
    history = await websocket.receive_json()
    assert history["type"] == "history"
    assert [item["role"] for item in history["payload"]["messages"]] == ["user", "assistant"]

    await websocket.send_json({
        "type": "list_sessions",
        "request_id": "req-3",
    })
    sessions = await websocket.receive_json()
    assert sessions["type"] == "sessions_list"
    assert sessions["payload"]["sessions"][0]["session_id"] == "session-alpha"

    await websocket.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_websocket_header_auth_sends_initial_status(tmp_path: Path, aiohttp_client) -> None:
    app = create_app(FakeLoop(tmp_path), model_name="test-model", auth_token="secret")
    client = await aiohttp_client(app)

    websocket = await client.ws_connect("/ws", headers={"Authorization": "Bearer secret"})
    first = await websocket.receive_json()
    second = await websocket.receive_json()
    third = await websocket.receive_json()

    assert [first["type"], second["type"], third["type"]] == [
        "auth_ok",
        "server_status",
        "init_status",
    ]

    await websocket.close()
