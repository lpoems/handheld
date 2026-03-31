"""Handheld-focused HTTP + WebSocket server."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aiohttp import WSMsgType, web
from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext

AGENT_LOOP_KEY = web.AppKey("agent_loop", Any)
AUTH_TOKEN_KEY = web.AppKey("auth_token", str)
SERVICE_STATE_KEY = web.AppKey("service_state", Any)
SESSION_LOCKS_KEY = web.AppKey("session_locks", dict[str, asyncio.Lock])
LOG_SINK_ID_KEY = web.AppKey("log_sink_id", int)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_bearer_token(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    return token or None


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "code": status}},
        status=status,
    )


def _safe_title(text: str, limit: int = 80) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return "New Session"
    return clean[:limit]


@dataclass(slots=True)
class ServiceState:
    """Mutable status snapshot for the Handheld service."""

    model_name: str
    init_phase: str = "starting"
    server_status: str = "starting"
    last_error: str | None = None
    init_detail: str | None = None
    started_at: float = field(default_factory=time.time)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=200))

    def record_log(self, message: str) -> None:
        self.logs.append(message.rstrip())

    def set_init_phase(self, phase: str, detail: str | None = None) -> None:
        self.init_phase = phase
        self.init_detail = detail
        if phase == "ready":
            self.server_status = "healthy"
        elif phase == "failed":
            self.server_status = "degraded"
        logger.info("Handheld init status={} detail={}", phase, detail or "")

    def set_error(self, error: str) -> None:
        self.last_error = error
        self.server_status = "degraded"
        self.init_phase = "failed"
        logger.error("Handheld server error: {}", error)

    def status_payload(self) -> dict[str, Any]:
        return {
            "status": self.server_status,
            "model": self.model_name,
            "uptime_seconds": max(0, int(time.time() - self.started_at)),
            "last_error": self.last_error,
            "timestamp": _utc_now(),
        }

    def init_payload(self) -> dict[str, Any]:
        return {
            "status": self.init_phase,
            "detail": self.init_detail,
            "timestamp": _utc_now(),
        }


class HandheldStreamingHook(AgentHook):
    """Bridge agent lifecycle callbacks into Handheld WebSocket events."""

    def __init__(
        self,
        emit_event,
        *,
        session_id: str,
        request_id: str | None,
        message_id: str,
        task_id: str,
    ) -> None:
        self._emit_event = emit_event
        self._session_id = session_id
        self._request_id = request_id
        self._message_id = message_id
        self._task_id = task_id
        self.started = False

    def wants_streaming(self) -> bool:
        return True

    async def before_iteration(self, context: AgentHookContext) -> None:
        if context.iteration == 0:
            await self._emit_event(
                "task_status",
                request_id=self._request_id,
                session_id=self._session_id,
                message_id=self._message_id,
                task_id=self._task_id,
                payload={"status": "running"},
            )

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        if not self.started:
            self.started = True
            await self._emit_event(
                "message_start",
                request_id=self._request_id,
                session_id=self._session_id,
                message_id=self._message_id,
                task_id=self._task_id,
                payload={},
            )
        await self._emit_event(
            "message_delta",
            request_id=self._request_id,
            session_id=self._session_id,
            message_id=self._message_id,
            task_id=self._task_id,
            payload={"text": delta},
        )

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._emit_event(
            "task_status",
            request_id=self._request_id,
            session_id=self._session_id,
            message_id=self._message_id,
            task_id=self._task_id,
            payload={
                "status": "waiting_tool",
                "detail": ", ".join(tc.name for tc in context.tool_calls) or None,
            },
        )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if context.error:
            await self._emit_event(
                "task_status",
                request_id=self._request_id,
                session_id=self._session_id,
                message_id=self._message_id,
                task_id=self._task_id,
                payload={"status": "failed", "detail": context.error},
            )
        elif context.final_content is None and context.tool_events:
            await self._emit_event(
                "task_status",
                request_id=self._request_id,
                session_id=self._session_id,
                message_id=self._message_id,
                task_id=self._task_id,
                payload={"status": "running"},
            )


async def _emit_ws_event(
    websocket: web.WebSocketResponse,
    event_type: str,
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    await websocket.send_json({
        "type": event_type,
        "request_id": request_id,
        "session_id": session_id,
        "message_id": message_id,
        "task_id": task_id,
        "timestamp": _utc_now(),
        "payload": payload or {},
    })


def _serialize_history(session_id: str, session) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index, message in enumerate(session.messages):
        item = dict(message)
        item.setdefault("id", f"{session_id}:{index}")
        messages.append(item)
    return messages


def _build_session_summary(session_info: dict[str, Any], session) -> dict[str, Any]:
    metadata = dict(session_info.get("metadata") or {})
    title = metadata.get("title") or _safe_title(
        next(
            (
                str(message.get("content") or "")
                for message in session.messages
                if message.get("role") == "user"
            ),
            "",
        )
    )
    if metadata.get("title") != title:
        session.metadata["title"] = title
    return {
        "session_id": session.key,
        "title": title,
        "created_at": session_info.get("created_at"),
        "updated_at": session.updated_at.isoformat(),
        "message_count": len(session.messages),
    }


async def handle_health(request: web.Request) -> web.Response:
    state: ServiceState = request.app[SERVICE_STATE_KEY]
    return web.json_response({"status": "ok", "service_status": state.server_status})


async def handle_status(request: web.Request) -> web.Response:
    state: ServiceState = request.app[SERVICE_STATE_KEY]
    return web.json_response(state.status_payload())


async def handle_init_status(request: web.Request) -> web.Response:
    state: ServiceState = request.app[SERVICE_STATE_KEY]
    return web.json_response(state.init_payload())


async def handle_logs(request: web.Request) -> web.Response:
    state: ServiceState = request.app[SERVICE_STATE_KEY]
    raw_limit = request.query.get("limit", "50")
    try:
        limit = max(1, min(int(raw_limit), len(state.logs) or 1))
    except ValueError:
        limit = 50
    return web.json_response({"logs": list(state.logs)[-limit:]})


async def handle_websocket(request: web.Request) -> web.StreamResponse:
    state: ServiceState = request.app[SERVICE_STATE_KEY]
    agent_loop = request.app[AGENT_LOOP_KEY]
    auth_token = request.app[AUTH_TOKEN_KEY]
    session_locks: dict[str, asyncio.Lock] = request.app[SESSION_LOCKS_KEY]

    websocket = web.WebSocketResponse(heartbeat=30.0)
    await websocket.prepare(request)

    authenticated = _extract_bearer_token(request) == auth_token

    async def emit(
        event_type: str,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
        message_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await _emit_ws_event(
            websocket,
            event_type,
            request_id=request_id,
            session_id=session_id,
            message_id=message_id,
            task_id=task_id,
            payload=payload,
        )

    if authenticated:
        await emit("auth_ok", payload={"status": "authenticated"})
        await emit("server_status", payload=state.status_payload())
        await emit("init_status", payload=state.init_payload())

    async for message in websocket:
        if message.type == WSMsgType.ERROR:
            logger.warning("WebSocket error: {}", websocket.exception())
            break
        if message.type != WSMsgType.TEXT:
            continue

        try:
            data = json.loads(message.data)
        except json.JSONDecodeError:
            await emit("error", payload={"message": "Invalid JSON body"})
            continue

        event_type = data.get("type")
        request_id = data.get("request_id")
        session_id = data.get("session_id") or data.get("payload", {}).get("session_id")
        payload = data.get("payload") or {}

        if not authenticated:
            if event_type != "auth":
                await emit("error", request_id=request_id, payload={"message": "Authentication required"})
                continue
            if payload.get("token") != auth_token:
                await emit("error", request_id=request_id, payload={"message": "Invalid token"})
                continue
            authenticated = True
            await emit("auth_ok", request_id=request_id, payload={"status": "authenticated"})
            await emit("server_status", request_id=request_id, payload=state.status_payload())
            await emit("init_status", request_id=request_id, payload=state.init_payload())
            continue

        if event_type == "ping":
            await emit("server_status", request_id=request_id, payload=state.status_payload())
            continue

        if event_type == "get_server_status":
            await emit("server_status", request_id=request_id, payload=state.status_payload())
            await emit("init_status", request_id=request_id, payload=state.init_payload())
            continue

        if event_type == "list_sessions":
            items = []
            for info in agent_loop.sessions.list_sessions():
                session = agent_loop.sessions.get_or_create(info["key"])
                items.append(_build_session_summary(info, session))
            await emit("sessions_list", request_id=request_id, payload={"sessions": items})
            continue

        if event_type == "get_history":
            if not session_id:
                await emit("error", request_id=request_id, payload={"message": "session_id is required"})
                continue
            session = agent_loop.sessions.get_or_create(session_id)
            await emit(
                "history",
                request_id=request_id,
                session_id=session_id,
                payload={"messages": _serialize_history(session_id, session)},
            )
            continue

        if event_type != "send_message":
            await emit("error", request_id=request_id, payload={"message": f"Unsupported event type: {event_type}"})
            continue

        content = payload.get("text")
        if not isinstance(content, str) or not content.strip():
            await emit("error", request_id=request_id, session_id=session_id, payload={"message": "payload.text is required"})
            continue

        session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        lock = session_locks.setdefault(session_id, asyncio.Lock())

        async with lock:
            session = agent_loop.sessions.get_or_create(session_id)
            if not session.metadata.get("title"):
                session.metadata["title"] = _safe_title(content)
                agent_loop.sessions.save(session)
            await emit(
                "session_state",
                request_id=request_id,
                session_id=session_id,
                payload={
                    "title": session.metadata.get("title"),
                    "updated_at": session.updated_at.isoformat(),
                },
            )
            await emit(
                "task_status",
                request_id=request_id,
                session_id=session_id,
                message_id=message_id,
                task_id=task_id,
                payload={"status": "queued"},
            )

            hook = HandheldStreamingHook(
                emit,
                session_id=session_id,
                request_id=request_id,
                message_id=message_id,
                task_id=task_id,
            )

            try:
                response = await agent_loop.process_direct(
                    content=content,
                    session_key=session_id,
                    channel="handheld",
                    chat_id=session_id,
                    hooks=[hook],
                )
                final_text = ((response.content if response else None) or "").strip()
                if not hook.started:
                    await emit(
                        "message_start",
                        request_id=request_id,
                        session_id=session_id,
                        message_id=message_id,
                        task_id=task_id,
                        payload={},
                    )
                    await emit(
                        "message_delta",
                        request_id=request_id,
                        session_id=session_id,
                        message_id=message_id,
                        task_id=task_id,
                        payload={"text": final_text},
                    )
                await emit(
                    "message_end",
                    request_id=request_id,
                    session_id=session_id,
                    message_id=message_id,
                    task_id=task_id,
                    payload={},
                )
                session = agent_loop.sessions.get_or_create(session_id)
                await emit(
                    "session_state",
                    request_id=request_id,
                    session_id=session_id,
                    payload={
                        "title": session.metadata.get("title") or _safe_title(content),
                        "updated_at": session.updated_at.isoformat(),
                    },
                )
                await emit(
                    "task_status",
                    request_id=request_id,
                    session_id=session_id,
                    message_id=message_id,
                    task_id=task_id,
                    payload={"status": "done"},
                )
            except Exception as exc:
                state.set_error(str(exc))
                logger.exception("Handheld message failed for session {}", session_id)
                await emit(
                    "task_status",
                    request_id=request_id,
                    session_id=session_id,
                    message_id=message_id,
                    task_id=task_id,
                    payload={"status": "failed", "detail": str(exc)},
                )
                await emit(
                    "error",
                    request_id=request_id,
                    session_id=session_id,
                    message_id=message_id,
                    task_id=task_id,
                    payload={"message": str(exc)},
                )

    return websocket


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in {"/health", "/ws"}:
        return await handler(request)
    auth_token = request.app[AUTH_TOKEN_KEY]
    if _extract_bearer_token(request) != auth_token:
        return _json_error(401, "Unauthorized")
    return await handler(request)


def create_app(
    agent_loop,
    *,
    model_name: str,
    auth_token: str,
    log_buffer_size: int = 200,
) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    state = ServiceState(model_name=model_name)
    state.logs = deque(maxlen=log_buffer_size)

    app[AGENT_LOOP_KEY] = agent_loop
    app[AUTH_TOKEN_KEY] = auth_token
    app[SERVICE_STATE_KEY] = state
    app[SESSION_LOCKS_KEY] = {}

    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/init-status", handle_init_status)
    app.router.add_get("/logs", handle_logs)
    app.router.add_get("/ws", handle_websocket)

    async def on_startup(_app: web.Application) -> None:
        sink_id = logger.add(state.record_log, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")
        _app[LOG_SINK_ID_KEY] = sink_id
        try:
            state.set_init_phase("loading_config")
            state.set_init_phase("loading_runtime")
            await agent_loop._connect_mcp()
            state.set_init_phase("recovering_sessions")
            for info in agent_loop.sessions.list_sessions():
                agent_loop.sessions.get_or_create(info["key"])
            state.set_init_phase("ready")
        except Exception as exc:
            state.set_error(str(exc))
            raise

    async def on_cleanup(_app: web.Application) -> None:
        sink_id = _app.get(LOG_SINK_ID_KEY)
        if sink_id is not None:
            logger.remove(sink_id)
        await agent_loop.close_mcp()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
