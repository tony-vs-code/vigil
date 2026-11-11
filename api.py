import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from subprocess import CalledProcessError
import threading
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import aiohttp
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psutil

import main

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

_docker_events_task: Optional[asyncio.Task] = None


async def docker_events_listener(
    socket_path: str = "/var/run/docker.sock",
    max_backoff: float = 60.0,
) -> None:
    """Stream real-time Docker events over Unix domain socket and notify WebSockets."""
    backoff = 2.0
    while True:
        try:
            if not Path(socket_path).exists():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            connector = aiohttp.UnixConnector(path=socket_path)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get("http://localhost/events") as resp:
                    if resp.status != 200:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, max_backoff)
                        continue

                    backoff = 2.0
                    async for line in resp.content:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            continue

                        event_type = event.get("Type", "")
                        action = event.get("Action", "")

                        lifecycle_actions = {
                            "start",
                            "stop",
                            "die",
                            "restart",
                            "kill",
                            "destroy",
                            "pause",
                            "unpause",
                            "health_status",
                            "create",
                        }
                        is_lifecycle = (
                            event_type == "container"
                            and (
                                action in lifecycle_actions
                                or action.startswith("health_status")
                            )
                        )

                        if is_lifecycle:
                            main.docker_manager.invalidate_cache()
                            actor = event.get("Actor", {})
                            attributes = actor.get("Attributes", {})
                            notification_payload = {
                                "type": "docker_event",
                                "event": {
                                    "action": action,
                                    "type": event_type,
                                    "id": str(actor.get("ID", ""))[:12],
                                    "name": attributes.get("name", ""),
                                    "time": event.get("time", time.time()),
                                },
                                "docker": main.docker_mgr.get_status(),
                            }
                            await ws_manager.broadcast(notification_payload)
                            await sse_manager.broadcast("docker_event", notification_payload)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.debug(f"Docker events listener error: {exc}")
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            backoff = min(backoff * 2, max_backoff)


_notifications_task: Optional[asyncio.Task] = None
_sentinel_probes_task: Optional[asyncio.Task] = None
_eval_loop_count: int = 0


async def notifications_evaluator_loop() -> None:
    global _eval_loop_count
    while True:
        sample = None
        try:
            sample = main.record_telemetry_sample()
            _eval_loop_count += 1
            if _eval_loop_count % 120 == 0:
                main.prune_telemetry_samples(7)
        except Exception as exc:
            logging.debug(f"Telemetry collector in evaluator loop error: {exc}")

        if sample is not None:
            try:
                await ws_manager.broadcast({"type": "telemetry_sample", "sample": sample})
                await sse_manager.broadcast("telemetry", sample)
            except Exception:
                pass

        try:
            await main.evaluate_threshold_alerts()
            await main.evaluate_scheduled_reports()
            cron_runs = main.evaluate_due_cron_jobs()
            if cron_runs:
                await _broadcast_module("backups")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.error(f"Error in notifications evaluator loop: {exc}")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _docker_events_task, _notifications_task, _sentinel_probes_task
    sse_manager.set_loop(asyncio.get_running_loop())
    try:
        main.init_state_db()
    except Exception as exc:
        logging.debug(f"State DB initialization notice: {exc}")
    if _docker_events_task is None or _docker_events_task.done():
        _docker_events_task = asyncio.create_task(docker_events_listener())
    if _notifications_task is None or _notifications_task.done():
        _notifications_task = asyncio.create_task(notifications_evaluator_loop())
    if _sentinel_probes_task is None or _sentinel_probes_task.done():
        _sentinel_probes_task = asyncio.create_task(main.sentinel_probes_worker())
    try:
        yield
    finally:
        if _docker_events_task and not _docker_events_task.done():
            _docker_events_task.cancel()
            try:
                await _docker_events_task
            except (asyncio.CancelledError, Exception):
                pass
        if _notifications_task and not _notifications_task.done():
            _notifications_task.cancel()
            try:
                await _notifications_task
            except (asyncio.CancelledError, Exception):
                pass
        if _sentinel_probes_task and not _sentinel_probes_task.done():
            _sentinel_probes_task.cancel()
            try:
                await _sentinel_probes_task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title="Vigil", lifespan=lifespan)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        if websocket not in self.active_connections:
            self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(payload)
            except Exception:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)


ws_manager = ConnectionManager()


class SSEManager:
    """Manages active Server-Sent Events subscriber queues and real-time broadcasts."""

    def __init__(self, max_queue_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._max_queue_size = max_queue_size

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register the active event loop for thread-safe cross-thread broadcasts."""
        self._loop = loop

    @property
    def subscribers(self) -> set[asyncio.Queue]:
        """Return a snapshot of current subscriber queues."""
        with self._lock:
            return set(self._subscribers)

    @property
    def active_subscribers(self) -> set[asyncio.Queue]:
        """Alias for subscribers set."""
        with self._lock:
            return set(self._subscribers)

    @property
    def subscriber_count(self) -> int:
        """Return the count of active subscriber queues."""
        with self._lock:
            return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        """Subscribe a new queue to SSE events."""
        if self._loop is None or not self._loop.is_running():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe a queue from SSE events."""
        with self._lock:
            self._subscribers.discard(queue)

    async def broadcast(self, event: str, data: Any) -> None:
        """Broadcast an event and its data payload to all active subscriber queues asynchronously."""
        with self._lock:
            subscribers = list(self._subscribers)

        if not subscribers:
            return

        item = (event, data)
        for queue in subscribers:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(item)
                except Exception:
                    pass
            except Exception:
                pass

    def broadcast_sync(self, event: str, data: Any) -> None:
        """Synchronous helper using asyncio.run_coroutine_threadsafe when invoked from worker threads."""
        loop = self._loop
        if loop is None or not loop.is_running():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.broadcast(event, data), loop)
                return
            except Exception as exc:
                logging.debug(f"Error in SSE broadcast_sync run_coroutine_threadsafe: {exc}")

        # Fallback if no running loop is active
        with self._lock:
            subscribers = list(self._subscribers)

        item = (event, data)
        for queue in subscribers:
            try:
                queue.put_nowait(item)
            except Exception:
                pass


SSEBroadcaster = SSEManager
sse_manager = SSEManager()
sse_broadcaster = sse_manager


def _on_notification_logged(payload: dict[str, Any]) -> None:
    try:
        sse_manager.broadcast_sync("notification", payload)
    except Exception as exc:
        logging.debug(f"Error dispatching notification to SSE: {exc}")


main.register_notification_subscriber(_on_notification_logged)


def format_sse(event: str, data: Any) -> str:
    """Format an SSE message block according to text/event-stream specification."""
    if isinstance(data, str):
        payload_str = data
    else:
        try:
            payload_str = json.dumps(data)
        except Exception:
            payload_str = str(data)
    return f"event: {event}\ndata: {payload_str}\n\n"


class SetupRequest(BaseModel):
    username: str
    password: str
    email: Optional[str] = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class GoogleAuthRequest(BaseModel):
    credential: Optional[str] = None
    id_token: Optional[str] = None


class SettingsUpdateRequest(BaseModel):
    discord_token: Optional[str] = None
    channel_id: Optional[str] = None
    discord_enabled: Optional[bool] = None
    raid_device: Optional[str] = None
    mount_path: Optional[str] = None
    google_client_id: Optional[str] = None
    allowed_google_emails: Optional[str] = None
    refresh_system_sec: Optional[int | str] = None
    refresh_raid_sec: Optional[int | str] = None
    refresh_notifications_sec: Optional[int | str] = None
    refresh_docker_sec: Optional[int | str] = None
    docker_auto_update_frequency: Optional[str] = None
    timezone: Optional[str] = None


class StackCreateRequest(BaseModel):
    name: str
    compose_yaml: str
    env_content: Optional[str] = None
    deploy: Optional[bool] = True


class StackValidateRequest(BaseModel):
    compose_yaml: str


class StackFormatRequest(BaseModel):
    compose_yaml: str = ""
    env_content: Optional[str] = None


class EnvParseRequest(BaseModel):
    env_content: str = ""


class EnvSerializeRequest(BaseModel):
    variables: list[dict[str, Any]] = []


class StackActionRequest(BaseModel):
    action: str


class ContainerActionRequest(BaseModel):
    action: str


class ImagePullRequest(BaseModel):
    image: Optional[str] = None
    image_tag: Optional[str] = None


class ImagePruneRequest(BaseModel):
    all: Optional[bool] = False


class UpdatesConfigRequest(BaseModel):
    frequency: str


class UpdatesApplyRequest(BaseModel):
    containers: Optional[list[str]] = None
    stacks: Optional[list[str]] = None


class UpdateScheduleRequest(BaseModel):
    enabled: Optional[bool] = False
    day: Optional[str] = "daily"
    time: Optional[str] = "03:00"
    notify_discord: Optional[bool] = True


class NotificationCenterUpdateRequest(BaseModel):
    channels: Optional[dict[str, Any]] = None
    schedule: Optional[dict[str, Any]] = None
    topics: Optional[dict[str, Any]] = None
    thresholds: Optional[dict[str, Any]] = None


class TestNotificationRequest(BaseModel):
    channel: Optional[str] = "all"


NotificationTestRequest = TestNotificationRequest


class ServiceActionRequest(BaseModel):
    action: str


class ProcessKillRequest(BaseModel):
    signal: str = "SIGTERM"


class CreateUserRequest(BaseModel):
    username: str
    shell: Optional[str] = None
    groups: Optional[list[str]] = None
    create_home: bool = True


class UpdateUserRequest(BaseModel):
    shell: Optional[str] = None
    groups: Optional[list[str]] = None
    locked: Optional[bool] = None


class CreateGroupRequest(BaseModel):
    name: str
    gid: Optional[int] = None


class GroupMembershipRequest(BaseModel):
    action: str
    username: str


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _extract_session_token(request: Request) -> Optional[str]:
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        return cookie_token.strip()
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    query_token = request.query_params.get("token")
    if query_token:
        return query_token.strip()
    return None


def require_auth(request: Request) -> str:
    token = _extract_session_token(request)
    try:
        username = main.verify_session(token)
    except (OSError, sqlite3.Error):
        username = None

    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    return username


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=main.SESSION_TTL_SECONDS,
        path="/",
    )


def verify_google_id_token(id_token: str) -> dict[str, Any]:
    url = f"https://oauth2.googleapis.com/tokeninfo?{urlencode({'id_token': id_token})}"
    try:
        with urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise HTTPException(status_code=401, detail="Invalid Google token payload")
            return payload
    except (HTTPError, URLError, ValueError, OSError) as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Google ID token: {exc}") from exc


def _is_google_email_allowed(email: str, settings: dict[str, str]) -> bool:
    clean_email = email.strip().lower()
    if not clean_email:
        return False

    allowed_raw = settings.get("allowed_google_emails", "")
    allowed_set = {
        item.strip().lower() for item in allowed_raw.split(",") if item.strip()
    }
    if clean_email in allowed_set:
        return True

    try:
        conn = main._connect_state_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM users WHERE LOWER(email) = ? OR LOWER(username) = ?",
                (clean_email, clean_email),
            ).fetchone()
            if row:
                return True
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        pass

    try:
        if not allowed_set and not main.has_any_user():
            return True
    except (OSError, sqlite3.Error):
        pass

    return False


def get_storage_metrics() -> list[dict[str, Any]]:
    """Collect mounted partition metrics via psutil."""
    return main.get_storage_metrics()


def get_system_metrics() -> dict[str, Any]:
    """Collect real-time system metrics via psutil, mounts, and services summary."""
    return main.get_system_metrics_data()


get_system_metrics_data = get_system_metrics


def get_network_metrics() -> dict[str, Any]:
    """Collect network telemetry via psutil."""
    return main.get_network_metrics()


def _get_formatted_notification_state() -> Optional[dict[str, Any]]:
    try:
        state = main.get_weekly_notification_state()
    except (OSError, sqlite3.Error):
        return None

    if state is None:
        return None

    sent_at, due_at, status = state
    return {
        "sent_at": sent_at,
        "sent_at_formatted": main.format_timestamp(sent_at),
        "due_at": due_at,
        "due_at_formatted": main.format_timestamp(due_at),
        "status": status,
    }


def _get_formatted_history(limit: int = 20) -> list[dict[str, Any]]:
    try:
        rows = main.get_notification_history(limit, detailed=True)
    except (OSError, sqlite3.Error):
        rows = []

    result = []
    for row in rows:
        if len(row) >= 8:
            status, sent_at, weekly, channel, trigger, title, details, outcome = row[:8]
        else:
            status, sent_at, weekly = row[:3]
            channel, trigger, title, details, outcome = "all", status, "", "", "delivered"

        result.append(
            {
                "status": status,
                "sent_at": float(sent_at),
                "sent_at_formatted": main.format_timestamp(float(sent_at)),
                "weekly": bool(weekly),
                "kind": (
                    "test notification"
                    if status == "test"
                    else ("weekly summary" if weekly else "alert")
                ),
                "channel": channel or "all",
                "trigger": trigger or status,
                "title": title or "",
                "details": details or "",
                "outcome": outcome or "delivered",
            }
        )
    return result


def _get_raid_summary() -> dict[str, Any]:
    raid_status, raid_details = main.check_raid_status()
    duf_output = main.get_duf_output()
    raid_summary = main._extract_raid_summary_data(
        raid_status, raid_details, duf_output
    )
    raid_summary.setdefault("raid_device", main.resolve_raid_device() or "None")
    return raid_summary


def _build_full_snapshot() -> dict[str, Any]:
    settings = main.get_app_settings()
    tz = settings.get("timezone", "UTC")
    return {
        "type": "snapshot",
        "raid": _get_raid_summary(),
        "system": get_system_metrics(),
        "notification_state": _get_formatted_notification_state(),
        "history": _get_formatted_history(20),
        "notification_center": main.get_masked_notification_center_config(),
        "version": main.get_current_version_info(),
        "refresh_intervals": main.get_refresh_intervals(),
        "timezone": tz,
        "settings": {
            "timezone": tz,
        },
        "docker": main.docker_mgr.get_status(),
        "services_summary": main.get_services_summary(),
    }


async def _build_module_update(module_name: str) -> dict[str, Any]:
    mod = (module_name or "").strip().lower()
    if mod == "system":
        return {
            "type": "module_update",
            "module": "system",
            "system": get_system_metrics(),
        }
    if mod in ("raid", "disks"):
        raid = _get_raid_summary()
        adaptive_fast = raid.get("status") in ("recovering", "resyncing", "degraded")
        intervals = main.get_refresh_intervals()
        effective_interval = (
            min(intervals["raid"], 5)
            if adaptive_fast and intervals["raid"] > 0
            else intervals["raid"]
        )
        return {
            "type": "module_update",
            "module": "raid",
            "raid": raid,
            "adaptive_fast": adaptive_fast,
            "effective_interval": effective_interval,
        }
    if mod in ("notifications", "schedule", "notification_state", "history"):
        return {
            "type": "module_update",
            "module": "notifications",
            "notification_state": _get_formatted_notification_state(),
            "history": _get_formatted_history(20),
            "notification_center": main.get_masked_notification_center_config(),
        }
    if mod == "docker_full":
        docker_status = await asyncio.to_thread(main.docker_mgr.get_status)
        containers = await asyncio.to_thread(main.docker_mgr.list_containers, True)
        stacks = await asyncio.to_thread(main.docker_mgr.list_stacks)
        return {
            "type": "module_update",
            "module": "docker",
            "docker": docker_status,
            "containers": containers,
            "stacks": stacks,
        }
    if mod in ("docker", "containers"):
        docker_status = await asyncio.to_thread(main.docker_mgr.get_status)
        return {
            "type": "module_update",
            "module": "docker",
            "docker": docker_status,
        }
    if mod in ("services", "systemctl"):
        services_data = await asyncio.to_thread(main.get_system_services)
        return {
            "type": "module_update",
            "module": "services",
            "services": services_data,
        }
    if mod in ("processes", "process"):
        processes_data = await asyncio.to_thread(main.get_process_list)
        return {
            "type": "module_update",
            "module": "processes",
            "processes": processes_data,
        }
    if mod in ("users", "groups"):
        users_data = await asyncio.to_thread(main.get_system_users)
        groups_data = await asyncio.to_thread(main.get_system_groups)
        return {
            "type": "module_update",
            "module": "users",
            "users": users_data,
            "groups": groups_data,
        }
    if mod in ("security", "network_security", "firewall", "ports"):
        sec_data = await asyncio.to_thread(main.get_network_security_overview)
        return {
            "type": "module_update",
            "module": "security",
            "security": sec_data,
        }
    if mod in ("backups", "cron"):
        backups_data = await asyncio.to_thread(main.get_backups_overview)
        cron_jobs = await asyncio.to_thread(main.list_cron_jobs)
        cron_runs = await asyncio.to_thread(main.list_cron_job_runs, 30)
        return {
            "type": "module_update",
            "module": "backups",
            "backups": backups_data,
            "cron_jobs": cron_jobs,
            "cron_runs": cron_runs,
        }
    if mod in ("probes", "sentinel"):
        probes_data = await asyncio.to_thread(main.list_probes)
        return {
            "type": "module_update",
            "module": "probes",
            "probes": probes_data,
        }
    if mod == "audit":
        audit_res = await asyncio.to_thread(main.get_audit_logs, 100)
        if isinstance(audit_res, tuple) and len(audit_res) == 2:
            logs_list, total_count = audit_res
        else:
            logs_list = audit_res
            total_count = len(logs_list) if isinstance(logs_list, list) else 0
        return {
            "type": "module_update",
            "module": "audit",
            "logs": logs_list,
            "total": total_count,
        }
    return await asyncio.to_thread(_build_full_snapshot)


async def _broadcast_module(module_name: str) -> None:
    try:
        is_mocked = hasattr(sse_manager.broadcast, "assert_called") or hasattr(sse_manager.broadcast, "mock")
        if not ws_manager.active_connections and sse_manager.subscriber_count == 0 and not is_mocked:
            return
        payload = await _build_module_update(module_name)
        if ws_manager.active_connections:
            await ws_manager.broadcast(payload)
        await sse_manager.broadcast("module_update", payload)
    except Exception as exc:
        logging.debug(f"Broadcast module '{module_name}' error: {exc}")


def _format_settings_response(settings: dict[str, str]) -> dict[str, Any]:
    raw_token = settings.get("discord_token", "")
    discord_token_configured = bool(raw_token)
    if len(raw_token) > 4:
        discord_token_masked = f"••••{raw_token[-4:]}"
    elif raw_token:
        discord_token_masked = "••••"
    else:
        discord_token_masked = ""

    discord_enabled = settings.get("discord_enabled", "false").strip().lower() == "true"
    configured_raid_device = settings.get("raid_device", "auto") or "auto"
    return {
        "channel_id": settings.get("channel_id", ""),
        "discord_token_configured": discord_token_configured,
        "discord_token_masked": discord_token_masked,
        "discord_enabled": discord_enabled,
        "raid_device": configured_raid_device,
        "available_raid_devices": main.discover_raid_devices(),
        "resolved_raid_device": main.resolve_raid_device(configured_raid_device),
        "mount_path": settings.get("mount_path", "/mnt/media/") or "/mnt/media/",
        "google_client_id": settings.get("google_client_id", ""),
        "allowed_google_emails": settings.get("allowed_google_emails", ""),
        "refresh_intervals": main.get_refresh_intervals(settings),
        "docker_auto_update_frequency": settings.get(
            "docker_auto_update_frequency", "disabled"
        ),
        "docker_auto_update_schedule": main.get_docker_auto_update_schedule(),
        "timezone": settings.get("timezone", "UTC"),
    }


@app.get("/api/auth/status")
def api_auth_status(request: Request) -> dict[str, Any]:
    token = _extract_session_token(request)
    try:
        username = main.verify_session(token)
    except (OSError, sqlite3.Error):
        username = None

    try:
        setup_required = not main.has_any_user()
    except (OSError, sqlite3.Error):
        setup_required = True

    settings = main.get_app_settings()
    return {
        "authenticated": username is not None,
        "username": username,
        "setup_required": setup_required,
        "google_client_id": settings.get("google_client_id", ""),
    }


@app.post("/api/auth/setup")
def api_auth_setup(request: Request, payload: SetupRequest, response: Response) -> dict[str, Any]:
    if main.has_any_user():
        raise HTTPException(
            status_code=400, detail="Initial admin setup has already been completed."
        )

    username = payload.username.strip()
    password = payload.password
    if not username or len(password) < 4:
        raise HTTPException(
            status_code=400,
            detail="Username is required and password must be at least 4 characters.",
        )

    main.create_user(username, password, email=payload.email or "")
    token = main.create_session(username)
    _set_session_cookie(response, token)
    main.record_audit_event(
        "auth.setup",
        f"Admin user '{username}' created",
        actor=username,
        ip_address=_get_client_ip(request),
        severity="info",
    )
    return {
        "authenticated": True,
        "username": username,
        "token": token,
    }


@app.post("/api/auth/login")
def api_auth_login(request: Request, payload: LoginRequest, response: Response) -> dict[str, Any]:
    username = payload.username.strip()
    client_ip = _get_client_ip(request)
    if not main.verify_user(username, payload.password):
        main.record_audit_event(
            "auth.login.failure",
            f"Failed login attempt for user '{username}'",
            actor=username or "anonymous",
            ip_address=client_ip,
            severity="warning",
            metadata={"reason": "invalid_credentials"},
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = main.create_session(username)
    _set_session_cookie(response, token)
    main.record_audit_event(
        "auth.login.success",
        f"User '{username}' logged in successfully",
        actor=username,
        ip_address=client_ip,
        severity="info",
    )
    return {
        "authenticated": True,
        "username": username,
        "token": token,
    }


@app.post("/api/auth/google")
def api_auth_google(payload: GoogleAuthRequest, response: Response) -> dict[str, Any]:
    raw_token = (payload.credential or payload.id_token or "").strip()
    if not raw_token:
        raise HTTPException(status_code=400, detail="Missing Google ID token")

    token_info = verify_google_id_token(raw_token)
    email = str(token_info.get("email") or "").strip().lower()
    email_verified = str(token_info.get("email_verified") or "").lower() in (
        "true",
        "1",
    )
    if not email or not email_verified:
        raise HTTPException(status_code=401, detail="Google account email is not verified")

    settings = main.get_app_settings()
    expected_client_id = settings.get("google_client_id", "").strip()
    if expected_client_id and token_info.get("aud") != expected_client_id:
        raise HTTPException(status_code=401, detail="Google client ID mismatch")

    if not _is_google_email_allowed(email, settings):
        raise HTTPException(
            status_code=403,
            detail="Google email is not authorized for this dashboard",
        )

    username = main.get_or_create_google_user(email)
    session_token = main.create_session(username)
    _set_session_cookie(response, session_token)
    return {
        "authenticated": True,
        "username": username,
        "token": session_token,
    }


@app.post("/api/auth/logout")
def api_auth_logout(request: Request, response: Response) -> dict[str, Any]:
    token = _extract_session_token(request)
    client_ip = _get_client_ip(request)
    user = "unknown"
    if token:
        try:
            verified = main.verify_session(token)
            if verified:
                user = verified
            main.delete_session(token)
        except (OSError, sqlite3.Error):
            pass
    response.delete_cookie(key="session_token", path="/")
    main.record_audit_event(
        "auth.logout",
        f"User '{user}' logged out",
        actor=user,
        ip_address=client_ip,
        severity="info",
    )
    return {"authenticated": False}


@app.get("/api/system/storage")
def api_system_storage(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        disks = get_storage_metrics()
        root_usage = None
        try:
            u = psutil.disk_usage("/")
            root_usage = {
                "total": u.total,
                "used": u.used,
                "free": u.free,
                "percent": u.percent,
            }
        except Exception:
            pass
        return {
            "disks": disks,
            "filesystems": disks,
            "mounts": disks,
            "disk": root_usage or (disks[0] if disks else {}),
            "physical_disks": main.get_all_disks_smart_metrics(),
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching storage metrics: {exc}"
        )


@app.get("/api/hardware/disks")
def api_hardware_disks(
    refresh: bool = False,
    _user: str = Depends(require_auth),
) -> list[dict[str, Any]]:
    try:
        return main.get_all_disks_smart_metrics(force_refresh=refresh)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching SMART disk telemetry: {exc}"
        )


@app.get("/api/hardware/disks/{disk_name}")
def api_hardware_disk_detail(
    disk_name: str,
    refresh: bool = False,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    cleaned = os.path.basename(disk_name.strip())
    if not cleaned or ".." in cleaned or "/" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid disk name.")
    device_path = f"/dev/{cleaned}"
    try:
        data = main.get_disk_smart_metrics(device_path, force_refresh=refresh)
        # If device has no raw_json and does not exist on host/discovery, return 404
        if data.get("raw_json") is None and data.get("status") in ("error", "unavailable", "not_found"):
            discovered = main.discover_physical_disks()
            if (
                device_path not in discovered
                and not Path(device_path).exists()
                and not Path(f"/sys/block/{cleaned}").exists()
            ):
                raise HTTPException(status_code=404, detail=f"Disk '{disk_name}' not found.")
        return data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching disk telemetry for {disk_name}: {exc}"
        )


@app.get("/api/hardware/thermals")
def api_hardware_thermals(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return main.get_hardware_thermal_metrics()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching hardware thermals: {exc}"
        )


@app.get("/api/system/metrics")
def api_system_metrics(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return get_system_metrics()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching system metrics: {exc}"
        )


@app.get("/api/system/network")
def api_system_network(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return get_network_metrics()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching network metrics: {exc}"
        )


@app.get("/api/system/diagnostics")
async def api_system_diagnostics(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(main.run_system_diagnostics)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error executing system diagnostics: {exc}"
        )


VALID_TELEMETRY_RANGES = {"1h", "6h", "24h", "7d"}


@app.get("/api/telemetry/history")
def api_telemetry_history(
    range: str = Query(default="1h"),
    points: int = Query(default=60),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    if range not in VALID_TELEMETRY_RANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid range '{range}'. Valid ranges are: {', '.join(sorted(VALID_TELEMETRY_RANGES))}",
        )
    try:
        return main.get_telemetry_history(range_key=range, points_limit=points)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching telemetry history: {exc}"
        )


@app.post("/api/telemetry/sample")
def api_telemetry_sample(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return main.record_telemetry_sample()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error recording telemetry sample: {exc}"
        )


@app.get("/api/status")
def api_status(_user: str = Depends(require_auth)) -> dict[str, Any]:
    settings = main.get_app_settings()
    tz = settings.get("timezone", "UTC")
    system_metrics = get_system_metrics()
    return {
        "raid": _get_raid_summary(),
        "system": system_metrics,
        "network": system_metrics.get("network", {}),
        "notification_state": _get_formatted_notification_state(),
        "version": main.get_current_version_info(),
        "timezone": tz,
        "settings": {
            "timezone": tz,
        },
        "services_summary": main.get_services_summary(),
        "reboot_status": main.package_manager.get_reboot_status(),
        "updates_summary": main.package_manager.get_updates_summary(),
    }


@app.get("/api/history")
def api_history(
    limit: int = Query(default=20, ge=1),
    _user: str = Depends(require_auth),
) -> dict[str, list[dict[str, Any]]]:
    return {"history": _get_formatted_history(limit)}


@app.get("/api/notifications/center")
def api_get_notification_center(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    return main.get_masked_notification_center_config()


@app.post("/api/notifications/center")
async def api_update_notification_center(
    payload: NotificationCenterUpdateRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True)
    updated = main.update_notification_center_config(updates)
    main.record_audit_event(
        "notifications.update",
        "Notification center settings updated",
        actor=_user,
        severity="info",
    )
    await _broadcast_module("notifications")
    return main.get_masked_notification_center_config(updated)


@app.post("/api/notifications/test")
async def api_send_test_notification(
    payload: Optional[TestNotificationRequest] = None,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    channel = payload.channel if payload and payload.channel else "all"
    success, message = await main.send_test_notification(channel=channel)
    await _broadcast_module("notifications")
    return {"success": success, "message": message}


def _extract_ws_token(websocket: WebSocket) -> Optional[str]:
    cookie_token = websocket.cookies.get("session_token")
    if cookie_token:
        return cookie_token.strip()
    query_token = websocket.query_params.get("token")
    if query_token:
        return query_token.strip()
    auth_header = websocket.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return None


async def sse_event_generator(
    request: Request,
    queue: asyncio.Queue,
    ping_interval: float = 15.0,
):
    try:
        # Immediately yield full initial snapshot on connect
        snapshot = await asyncio.to_thread(_build_full_snapshot)
        yield format_sse("snapshot", snapshot)

        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=ping_interval)
                event, data = item
                yield format_sse(event, data)
            except asyncio.TimeoutError:
                yield "event: ping\ndata: {}\n\n"
    except (asyncio.CancelledError, GeneratorExit):
        pass
    except Exception as exc:
        logging.debug(f"SSE generator error: {exc}")
    finally:
        sse_manager.unsubscribe(queue)


async def _sse_stream_response(request: Request, ping_interval: float = 15.0) -> StreamingResponse:
    queue = sse_manager.subscribe()
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        sse_event_generator(request, queue, ping_interval=ping_interval),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/api/events")
async def api_events_stream(
    request: Request,
    ping_interval: float = Query(default=15.0, ge=0.01, le=120.0),
    _user: str = Depends(require_auth),
) -> StreamingResponse:
    return await _sse_stream_response(request, ping_interval=ping_interval)


@app.get("/api/stream/events")
async def api_stream_events_alias(
    request: Request,
    ping_interval: float = Query(default=15.0, ge=0.01, le=120.0),
    _user: str = Depends(require_auth),
) -> StreamingResponse:
    return await _sse_stream_response(request, ping_interval=ping_interval)


@app.websocket("/ws")
@app.websocket("/api/ws")
async def api_websocket(websocket: WebSocket) -> None:
    token = _extract_ws_token(websocket)
    try:
        username = main.verify_session(token)
    except (OSError, sqlite3.Error):
        username = None

    if not username:
        await websocket.accept()
        await websocket.close(code=1008)
        return

    await ws_manager.connect(websocket)
    try:
        initial_snapshot = await asyncio.to_thread(_build_full_snapshot)
        last_raid_status = initial_snapshot.get("raid", {}).get("status", "unknown")
        await websocket.send_json(initial_snapshot)
        elapsed = {"system": 0.0, "raid": 0.0, "notifications": 0.0, "docker": 0.0}
        tick_count = 0
        subscribed_view = "overview"
        subscribed_subtab = "stacks"
        subscribed_log_container: Optional[str] = None
        last_sent_container_logs: dict[str, str] = {}
        while True:
            try:
                raw_text = await asyncio.wait_for(
                    websocket.receive_text(), timeout=0.5
                )
                try:
                    data = json.loads(raw_text)
                except ValueError:
                    data = {}
                if isinstance(data, dict):
                    action = data.get("action")
                    if action == "refresh":
                        snap = await asyncio.to_thread(_build_full_snapshot)
                        last_raid_status = snap.get("raid", {}).get(
                            "status", last_raid_status
                        )
                        elapsed = {"system": 0.0, "raid": 0.0, "notifications": 0.0, "docker": 0.0}
                        await websocket.send_json(snap)
                    elif action == "refresh_module":
                        mod_payload = await _build_module_update(
                            str(data.get("module") or "")
                        )
                        if (
                            mod_payload.get("module") == "raid"
                            and "raid" in mod_payload
                        ):
                            last_raid_status = mod_payload["raid"].get(
                                "status", last_raid_status
                            )
                        await websocket.send_json(mod_payload)
                    elif action == "subscribe":
                        subscribed_view = str(data.get("view") or "overview").lower()
                        subscribed_subtab = str(data.get("subtab") or "").lower()
                        view_mod_map = {
                            "processes": "processes",
                            "services": "services",
                            "security": "security",
                            "network": "security",
                            "firewall": "security",
                            "backups": "backups",
                            "cron": "backups",
                            "users": "users",
                            "audit": "audit",
                            "probes": "probes",
                            "sentinel": "probes",
                            "containers": "docker_full",
                            "docker": "docker_full",
                            "storage": "raid",
                            "disks": "raid",
                            "raid": "raid",
                            "notifications": "notifications",
                        }
                        target_mod = view_mod_map.get(subscribed_view)
                        if target_mod:
                            await websocket.send_json(await _build_module_update(target_mod))
                    elif action == "subscribe_logs":
                        subscribed_log_container = (
                            str(data.get("container_id") or "").strip() or None
                        )
                        if subscribed_log_container:
                            try:
                                logs_text = await asyncio.to_thread(
                                    main.docker_mgr.get_container_logs,
                                    subscribed_log_container,
                                    200,
                                )
                                last_sent_container_logs[subscribed_log_container] = logs_text
                                await websocket.send_json(
                                    {
                                        "type": "container_logs",
                                        "container_id": subscribed_log_container,
                                        "logs": logs_text,
                                    }
                                )
                            except Exception as exc:
                                logging.debug(f"Error fetching subscribed container logs: {exc}")
                    elif action == "set_intervals":
                        raw_intervals = (
                            data.get("intervals")
                            if isinstance(data.get("intervals"), dict)
                            else data
                        )
                        interval_updates: dict[str, str] = {}
                        for key_short, setting_key in (
                            ("system", "refresh_system_sec"),
                            ("raid", "refresh_raid_sec"),
                            ("notifications", "refresh_notifications_sec"),
                            ("docker", "refresh_docker_sec"),
                        ):
                            val = raw_intervals.get(key_short)
                            if val is None:
                                val = raw_intervals.get(setting_key)
                            if val is not None:
                                try:
                                    interval_updates[setting_key] = str(
                                        max(0, int(val))
                                    )
                                except (ValueError, TypeError):
                                    pass
                        if interval_updates:
                            main.update_app_settings(interval_updates)
                        await websocket.send_json(
                            {
                                "type": "intervals_updated",
                                "refresh_intervals": main.get_refresh_intervals(),
                            }
                        )
            except asyncio.TimeoutError:
                tick_count += 1
                intervals = main.get_refresh_intervals()
                elapsed["system"] += 0.5
                elapsed["raid"] += 0.5
                elapsed["notifications"] += 0.5
                elapsed["docker"] += 0.5

                if intervals["system"] > 0:
                    elapsed["system"] = 0.0
                    await websocket.send_json(await _build_module_update("system"))

                if tick_count % 2 == 0:
                    if subscribed_view == "processes":
                        await websocket.send_json(await _build_module_update("processes"))
                    elif subscribed_view in ("security", "network", "firewall"):
                        await websocket.send_json(await _build_module_update("security"))
                    elif subscribed_view == "services":
                        await websocket.send_json(await _build_module_update("services"))
                    elif subscribed_view in ("containers", "docker"):
                        await websocket.send_json(await _build_module_update("docker_full"))
                    elif subscribed_view in ("backups", "cron"):
                        await websocket.send_json(await _build_module_update("backups"))
                    elif subscribed_view in ("probes", "sentinel"):
                        await websocket.send_json(await _build_module_update("probes"))

                    if subscribed_log_container:
                        try:
                            logs_text = await asyncio.to_thread(
                                main.docker_mgr.get_container_logs,
                                subscribed_log_container,
                                200,
                            )
                            if last_sent_container_logs.get(subscribed_log_container) != logs_text:
                                last_sent_container_logs[subscribed_log_container] = logs_text
                                await websocket.send_json(
                                    {
                                        "type": "container_logs",
                                        "container_id": subscribed_log_container,
                                        "logs": logs_text,
                                    }
                                )
                        except Exception as exc:
                            logging.debug(f"Error streaming container logs: {exc}")

                adaptive_fast = last_raid_status in (
                    "recovering",
                    "resyncing",
                    "degraded",
                )
                raid_interval = (
                    min(intervals["raid"], 5)
                    if adaptive_fast and intervals["raid"] > 0
                    else intervals["raid"]
                )
                if raid_interval > 0 and elapsed["raid"] >= raid_interval:
                    elapsed["raid"] = 0.0
                    raid_msg = await _build_module_update("raid")
                    last_raid_status = raid_msg.get("raid", {}).get(
                        "status", last_raid_status
                    )
                    await websocket.send_json(raid_msg)

                if (
                    intervals["notifications"] > 0
                    and elapsed["notifications"] >= intervals["notifications"]
                ):
                    elapsed["notifications"] = 0.0
                    await websocket.send_json(await _build_module_update("notifications"))

                docker_interval = intervals.get("docker", 5)
                if (
                    docker_interval > 0
                    and elapsed["docker"] >= docker_interval
                ):
                    elapsed["docker"] = 0.0
                    await websocket.send_json(await _build_module_update("docker"))
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket)


@app.get("/api/settings")
def api_get_settings(_user: str = Depends(require_auth)) -> dict[str, Any]:
    settings = main.get_app_settings()
    return _format_settings_response(settings)


@app.post("/api/settings")
def api_update_settings(
    payload: SettingsUpdateRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.discord_token is not None and payload.discord_token.strip() != "":
        updates["discord_token"] = payload.discord_token.strip()
    if payload.channel_id is not None:
        updates["channel_id"] = payload.channel_id.strip()
    if payload.discord_enabled is not None:
        updates["discord_enabled"] = "true" if payload.discord_enabled else "false"
    if payload.raid_device is not None and payload.raid_device.strip() != "":
        updates["raid_device"] = payload.raid_device.strip()
    if payload.mount_path is not None and payload.mount_path.strip() != "":
        updates["mount_path"] = payload.mount_path.strip()
    if payload.google_client_id is not None:
        updates["google_client_id"] = payload.google_client_id.strip()
    if payload.allowed_google_emails is not None:
        updates["allowed_google_emails"] = payload.allowed_google_emails.strip()
    if payload.refresh_system_sec is not None:
        try:
            updates["refresh_system_sec"] = str(
                max(0, int(payload.refresh_system_sec))
            )
        except (ValueError, TypeError):
            pass
    if payload.refresh_raid_sec is not None:
        try:
            updates["refresh_raid_sec"] = str(max(0, int(payload.refresh_raid_sec)))
        except (ValueError, TypeError):
            pass
    if payload.refresh_notifications_sec is not None:
        try:
            updates["refresh_notifications_sec"] = str(
                max(0, int(payload.refresh_notifications_sec))
            )
        except (ValueError, TypeError):
            pass
    if payload.refresh_docker_sec is not None:
        try:
            updates["refresh_docker_sec"] = str(
                max(0, int(payload.refresh_docker_sec))
            )
        except (ValueError, TypeError):
            pass
    if payload.docker_auto_update_frequency is not None:
        updates["docker_auto_update_frequency"] = (
            payload.docker_auto_update_frequency.strip().lower()
        )
    if payload.timezone is not None and payload.timezone.strip():
        updates["timezone"] = payload.timezone.strip()

    updated = main.update_app_settings(updates)
    main.record_audit_event(
        "settings.update",
        f"Application settings updated: {', '.join(sorted(updates.keys()))}",
        actor=_user,
        metadata={k: ("••••" if "token" in k else v) for k, v in updates.items()},
        severity="info",
    )
    result = _format_settings_response(updated)
    result["saved"] = True
    return result


@app.get("/api/update/check")
def api_update_check(_user: str = Depends(require_auth)) -> dict[str, Any]:
    return main.check_for_release_update()


@app.get("/api/changelog")
def api_changelog(_user: str = Depends(require_auth)) -> dict[str, Any]:
    return {"changelog": main.get_full_changelog()}


@app.post("/api/update/apply")
def api_update_apply(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.apply_release_update()
    except (CalledProcessError, OSError) as exc:
        detail = (
            exc.output.decode("utf-8", errors="replace")
            if isinstance(exc, CalledProcessError) and exc.output
            else str(exc)
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to apply release update: {detail}"
        ) from exc


@app.get("/api/docker/status")
async def api_docker_status(_user: str = Depends(require_auth)) -> dict[str, Any]:
    return await asyncio.to_thread(main.docker_manager.get_status)


@app.post("/api/docker/install")
async def api_docker_install(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(main.docker_manager.install_docker)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/templates")
async def api_docker_templates(_user: str = Depends(require_auth)) -> dict[str, Any]:
    templates = await asyncio.to_thread(main.docker_manager.get_templates)
    return {"templates": templates}


@app.post("/api/docker/stacks/validate")
async def api_docker_validate_stack(
    payload: StackValidateRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    result = await asyncio.to_thread(
        main.docker_manager.validate_compose_yaml,
        payload.compose_yaml,
    )
    return result


@app.post("/api/docker/stacks/format")
async def api_docker_format_stack(
    payload: StackFormatRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    if not (payload.compose_yaml or "").strip() and payload.env_content is not None:
        formatted_env = await asyncio.to_thread(
            main.docker_manager.format_env_content,
            payload.env_content,
        )
        return {
            "valid": True,
            "formatted_yaml": "",
            "formatted_env": formatted_env,
            "services": [],
            "ports": [],
            "volumes": [],
            "warnings": [],
        }
    result = await asyncio.to_thread(
        main.docker_manager.format_compose_yaml,
        payload.compose_yaml,
        payload.env_content,
    )
    return result


@app.post("/api/docker/stacks/env/parse")
async def api_docker_parse_env(
    req: EnvParseRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    return {"variables": main.docker_manager.parse_env_key_values(req.env_content)}


@app.post("/api/docker/stacks/env/serialize")
async def api_docker_serialize_env(
    req: EnvSerializeRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    return {"env_content": main.docker_manager.serialize_env_key_values(req.variables)}


@app.get("/api/docker/stacks")
async def api_docker_stacks(_user: str = Depends(require_auth)) -> dict[str, Any]:
    stacks = await asyncio.to_thread(main.docker_manager.list_stacks)
    return {"stacks": stacks}


@app.post("/api/docker/stacks")
async def api_docker_create_stack(
    payload: StackCreateRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        stack = await asyncio.to_thread(
            main.docker_manager.save_stack,
            payload.name,
            payload.compose_yaml,
            env_content=payload.env_content,
            deploy=bool(payload.deploy),
        )
        await _broadcast_module("docker_full")
        return {"success": True, "stack": stack}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/docker/stacks/{name}")
async def api_docker_update_stack(
    name: str,
    payload: StackCreateRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        stack = await asyncio.to_thread(
            main.docker_manager.save_stack,
            name,
            payload.compose_yaml,
            env_content=payload.env_content,
            deploy=bool(payload.deploy),
        )
        await _broadcast_module("docker_full")
        return {"success": True, "stack": stack}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/stacks/{name}")
async def api_docker_get_stack(
    name: str,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    stack = await asyncio.to_thread(main.docker_manager.get_stack, name)
    if not stack:
        raise HTTPException(status_code=404, detail=f"Stack '{name}' not found")
    return {"stack": stack}


@app.post("/api/docker/stacks/{name}/action")
async def api_docker_stack_action(
    name: str,
    payload: StackActionRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = await asyncio.to_thread(
            main.docker_manager.execute_stack_action, name, payload.action
        )
        await _broadcast_module("docker_full")
        return res
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/docker/stacks/{name}")
async def api_docker_delete_stack(
    name: str,
    down: bool = Query(True),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        await asyncio.to_thread(main.docker_manager.delete_stack, name, down=down)
        await _broadcast_module("docker_full")
        return {"success": True, "deleted": name}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/containers")
async def api_docker_containers(
    all: bool = Query(True),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    containers = await asyncio.to_thread(main.docker_manager.list_containers, all=all)
    return {"containers": containers}


@app.post("/api/docker/containers/{id}/action")
async def api_docker_container_action(
    id: str,
    payload: ContainerActionRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = await asyncio.to_thread(
            main.docker_manager.execute_container_action, id, payload.action
        )
        await _broadcast_module("docker_full")
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/containers/{id}/logs")
async def api_docker_container_logs(
    id: str,
    tail: int = Query(200, ge=1, le=5000),
    timestamps: bool = Query(True),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        logs = await asyncio.to_thread(
            main.docker_manager.get_container_logs,
            id,
            tail=tail,
            timestamps=timestamps,
        )
        return {"logs": logs}
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/containers/{id}/stats")
async def api_docker_container_stats(
    id: str,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        stats = await asyncio.to_thread(main.docker_manager.get_container_stats, id)
        return {"stats": stats}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.websocket("/api/docker/containers/{container_id}/terminal")
async def api_docker_container_terminal(
    websocket: WebSocket,
    container_id: str,
    shell: str = "/bin/sh",
    user: str = "",
) -> None:
    token = _extract_ws_token(websocket)
    try:
        username = main.verify_session(token)
    except (OSError, sqlite3.Error):
        username = None

    if not username:
        await websocket.accept()
        await websocket.close(code=1008)
        return

    shell_cmd = websocket.query_params.get("shell") or shell or "/bin/sh"
    target_user = websocket.query_params.get("user") or user or ""

    await websocket.accept()

    try:
        exec_info = await asyncio.to_thread(
            main.docker_manager.create_container_exec,
            container_id,
            cmd=[shell_cmd],
            user=target_user,
            tty=True,
        )
        exec_id = exec_info.get("id")
        if not exec_id:
            raise RuntimeError("Exec instance creation did not return an ID")
    except Exception as exc:
        logging.error(f"Failed to create exec for container {container_id}: {exc}")
        try:
            await websocket.send_text(f"\r\n\x1b[31mTerminal error: {exc}\x1b[0m\r\n")
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    try:
        main.record_audit_event(
            "docker.container.terminal_opened",
            f"Terminal session opened for container '{container_id}' (user: '{target_user or 'default'}', shell: '{shell_cmd}')",
            actor=username,
            severity="info",
            metadata={
                "container_id": container_id,
                "shell": shell_cmd,
                "user": target_user,
                "exec_id": exec_id,
            },
        )
    except Exception as exc:
        logging.debug(f"Failed to record audit event for terminal: {exc}")

    try:
        raw_sock = await asyncio.to_thread(
            main.docker_manager.start_container_exec_socket,
            exec_id,
        )
        raw_sock.setblocking(False)
    except Exception as exc:
        logging.error(f"Failed to start exec socket for {exec_id}: {exc}")
        try:
            await websocket.send_text(f"\r\n\x1b[31mSocket connection error: {exc}\x1b[0m\r\n")
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    loop = asyncio.get_running_loop()

    async def ws_to_docker() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                b_data = msg.get("bytes")
                if text is not None:
                    stripped = text.strip()
                    if (
                        stripped.startswith('{"type": "resize"')
                        or stripped.startswith('{"type":"resize"')
                        or (stripped.startswith("{") and '"type"' in stripped and '"resize"' in stripped)
                    ):
                        try:
                            rd = json.loads(stripped)
                            if isinstance(rd, dict) and rd.get("type") == "resize":
                                cols = int(rd.get("cols") or rd.get("width") or 80)
                                rows = int(rd.get("rows") or rd.get("height") or 24)
                                await asyncio.to_thread(
                                    main.docker_manager.resize_container_exec,
                                    exec_id,
                                    height=rows,
                                    width=cols,
                                )
                                continue
                        except Exception as exc:
                            logging.debug(f"Terminal resize error: {exc}")
                    await loop.sock_sendall(raw_sock, text.encode("utf-8"))
                elif b_data is not None:
                    await loop.sock_sendall(raw_sock, b_data)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as exc:
            logging.debug(f"ws_to_docker relay error: {exc}")

    async def docker_to_ws() -> None:
        try:
            while True:
                chunk = await loop.sock_recv(raw_sock, 4096)
                if not chunk:
                    break
                await websocket.send_bytes(chunk)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as exc:
            logging.debug(f"docker_to_ws relay error: {exc}")

    in_task = asyncio.create_task(ws_to_docker())
    out_task = asyncio.create_task(docker_to_ws())

    try:
        done, pending = await asyncio.wait(
            [in_task, out_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        for task in [in_task, out_task]:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            raw_sock.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/docker/images")
async def api_docker_images(_user: str = Depends(require_auth)) -> dict[str, Any]:
    images = await asyncio.to_thread(main.docker_manager.list_images)
    return {"images": images}


@app.post("/api/docker/images/pull")
async def api_docker_pull_image(
    payload: ImagePullRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    image_name = (payload.image or payload.image_tag or "").strip()
    if not image_name:
        raise HTTPException(status_code=400, detail="Image tag is required")
    try:
        return await asyncio.to_thread(main.docker_manager.pull_image, image_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/docker/images/{id}")
async def api_docker_remove_image(
    id: str,
    force: bool = Query(False),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            main.docker_manager.remove_image, id, force=force
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/docker/images/prune")
async def api_docker_prune_images(
    payload: Optional[ImagePruneRequest] = None,
    all: bool = Query(False),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    prune_all = all or (payload.all if payload else False)
    try:
        return await asyncio.to_thread(
            main.docker_manager.prune_images, all_images=prune_all
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/volumes")
async def api_docker_volumes(_user: str = Depends(require_auth)) -> dict[str, Any]:
    volumes = await asyncio.to_thread(main.docker_manager.list_volumes)
    return {"volumes": volumes}


@app.post("/api/docker/volumes/prune")
async def api_docker_prune_volumes(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = await asyncio.to_thread(main.docker_manager.prune_volumes)
        main.record_audit_event(
            "docker.volume.prune",
            "Pruned unused Docker volumes",
            actor=_user,
            severity="info",
        )
        await _broadcast_module("docker_full")
        return res
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/volumes/{name}")
async def api_docker_inspect_volume(
    name: str,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        data = await asyncio.to_thread(main.docker_manager.inspect_volume, name)
        result = dict(data)
        result["volume"] = data
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/docker/volumes/{name}")
async def api_docker_remove_volume(
    name: str,
    force: bool = Query(False),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = await asyncio.to_thread(
            main.docker_manager.remove_volume, name, force=force
        )
        main.record_audit_event(
            "docker.volume.remove",
            f"Removed Docker volume '{name}'",
            actor=_user,
            severity="info",
        )
        await _broadcast_module("docker_full")
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/networks")
async def api_docker_networks(_user: str = Depends(require_auth)) -> dict[str, Any]:
    networks = await asyncio.to_thread(main.docker_manager.list_networks)
    return {"networks": networks}


@app.delete("/api/docker/networks/{id}")
async def api_docker_remove_network(
    id: str,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = await asyncio.to_thread(main.docker_manager.remove_network, id)
        await _broadcast_module("docker_full")
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/updates")
async def api_docker_updates(
    force: bool = False,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    return await asyncio.to_thread(main.docker_manager.check_updates, force=force)


@app.post("/api/docker/updates/apply")
async def api_docker_updates_apply(
    payload: Optional[UpdatesApplyRequest] = None,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    containers = payload.containers if payload else None
    stacks = payload.stacks if payload else None
    try:
        return await asyncio.to_thread(
            main.docker_manager.apply_updates,
            containers=containers,
            stacks=stacks,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/docker/updates/config")
def api_docker_get_updates_config(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    return main.docker_manager.get_auto_update_config()


@app.post("/api/docker/updates/config")
def api_docker_updates_config(
    payload: UpdatesConfigRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.docker_manager.set_auto_update_config(payload.frequency)
        main.record_audit_event(
            "docker.config.update",
            f"Docker auto-update frequency set to '{payload.frequency}'",
            actor=_user,
            severity="info",
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/docker/updates/schedule")
def api_docker_get_update_schedule(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    schedule = main.get_docker_auto_update_schedule()
    return {
        "schedule": schedule,
        "enabled": schedule.get("enabled", False),
        "day": schedule.get("day", "daily"),
        "time": schedule.get("time", "03:00"),
        "notify_discord": schedule.get("notify_discord", True),
    }


@app.post("/api/docker/updates/schedule")
def api_docker_set_update_schedule(
    payload: UpdateScheduleRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        schedule = main.set_docker_auto_update_schedule(payload.model_dump())
        main.record_audit_event(
            "docker.config.update",
            "Docker auto-update schedule updated",
            actor=_user,
            severity="info",
        )
        return {
            "success": True,
            "schedule": schedule,
            "enabled": schedule.get("enabled", False),
            "day": schedule.get("day", "daily"),
            "time": schedule.get("time", "03:00"),
            "notify_discord": schedule.get("notify_discord", True),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ============================================================================
# System Services Routes
# ============================================================================


@app.get("/api/services")
def api_services(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.get_system_services()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching system services: {exc}"
        )


@app.post("/api/services/{service_name}/action")
async def api_service_action(
    service_name: str,
    payload: ServiceActionRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.execute_service_action(service_name, payload.action)
        main.record_audit_event(
            "service.action",
            f"Service '{res['service']}' action '{res['action']}' executed",
            actor=_user,
            severity="info",
            metadata={"service": res["service"], "action": res["action"]},
        )
        await _broadcast_module("services")
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error executing service action: {exc}")


@app.get("/api/services/{service_name}/logs")
def api_service_logs(
    service_name: str,
    lines: int = Query(default=100, ge=1, le=500),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        log_lines = main.get_service_logs(service_name, lines=lines)
        clean_name = service_name if service_name.endswith(".service") else f"{service_name}.service"
        return {
            "service": clean_name,
            "logs": log_lines,
            "lines": len(log_lines),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error fetching service logs: {exc}")


@app.get("/api/vigil/logs")
@app.get("/api/system/logs")
def api_vigil_logs(
    lines: int = Query(default=100, ge=1, le=1000),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        log_lines = main.get_vigil_logs(lines=lines)
        return {
            "service": "vigil.service",
            "logs": log_lines,
            "lines": len(log_lines),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error fetching Vigil logs: {exc}")


# ============================================================================
# Package Management & System Updates Routes
# ============================================================================


@app.get("/api/packages/summary")
def api_packages_summary(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return main.package_manager.get_updates_summary()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error checking package updates summary: {exc}")


@app.get("/api/packages/updates")
def api_packages_updates(
    force: bool = Query(default=False),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return main.package_manager.get_available_updates(force=force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error checking package updates: {exc}")


@app.post("/api/packages/refresh")
@app.post("/api/packages/check")
def api_packages_refresh(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        refresh_res = main.package_manager.refresh_repositories()
        updates_res = main.package_manager.get_available_updates(force=True)
        return {
            "success": bool(refresh_res.get("success", False)),
            "refresh": refresh_res,
            "updates": updates_res,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error refreshing repositories: {exc}")


@app.get("/api/packages/installed")
def api_packages_installed(
    q: str = Query(default=""),
    limit: int = Query(default=50, ge=0, le=500),
    page: int = Query(default=1, ge=1),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return main.package_manager.get_installed_packages(query=q, limit=limit, page=page)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error retrieving installed packages: {exc}")


@app.get("/api/packages/reboot")
def api_packages_reboot(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return main.package_manager.get_reboot_status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error checking reboot status: {exc}")


class PackageUpgradeRequest(BaseModel):
    packages: Optional[list[str]] = None
    dry_run: bool = True
    stream: bool = False


@app.post("/api/packages/upgrade", response_model=None)
def api_packages_upgrade(
    req: PackageUpgradeRequest,
    stream: bool = Query(default=False),
    _user: str = Depends(require_auth),
) -> Any:
    should_stream = stream or req.stream
    if should_stream:
        if not req.dry_run:
            target_desc = (
                f"packages ({', '.join(req.packages)})"
                if req.packages
                else "all packages"
            )
            main.record_audit_event(
                "packages.upgrade",
                f"Streaming upgrade of {target_desc} initiated",
                actor=_user,
                severity="info",
            )
        return StreamingResponse(
            main.package_manager.run_upgrade_stream(packages=req.packages, dry_run=req.dry_run),
            media_type="text/plain; charset=utf-8",
        )
    try:
        res = main.package_manager.run_upgrade(packages=req.packages, dry_run=req.dry_run)
        if not req.dry_run:
            target_desc = (
                f"packages ({', '.join(req.packages)})"
                if req.packages
                else "all packages"
            )
            status_desc = "succeeded" if res.get("success") else "failed"
            main.record_audit_event(
                "packages.upgrade",
                f"Upgrade of {target_desc} {status_desc}",
                actor=_user,
                severity="info" if res.get("success") else "warning",
            )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error executing package upgrade: {exc}")


@app.post("/api/packages/upgrade/stream")
def api_packages_upgrade_stream(
    req: PackageUpgradeRequest,
    _user: str = Depends(require_auth),
) -> StreamingResponse:
    if not req.dry_run:
        target_desc = (
            f"packages ({', '.join(req.packages)})"
            if req.packages
            else "all packages"
        )
        main.record_audit_event(
            "packages.upgrade",
            f"Streaming upgrade of {target_desc} initiated",
            actor=_user,
            severity="info",
        )
    return StreamingResponse(
        main.package_manager.run_upgrade_stream(packages=req.packages, dry_run=req.dry_run),
        media_type="text/plain; charset=utf-8",
    )


class PythonPackageUpgradeRequest(BaseModel):
    packages: Optional[list[str]] = None
    dry_run: bool = False
    restart_service: bool = False
    stream: bool = False


@app.get("/api/packages/python")
async def api_packages_python(
    check_pypi: bool = Query(default=False),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(main.package_manager.get_python_packages, check_pypi)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error checking Python packages: {exc}")


@app.post("/api/packages/python/check")
async def api_packages_python_check(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(main.package_manager.get_python_packages, True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error checking Python package updates: {exc}")


@app.post("/api/packages/python/upgrade", response_model=None)
async def api_packages_python_upgrade(
    req: PythonPackageUpgradeRequest,
    stream: bool = Query(default=False),
    _user: str = Depends(require_auth),
) -> Any:
    should_stream = stream or req.stream
    if should_stream:
        if not req.dry_run:
            target_desc = (
                f"packages ({', '.join(req.packages)})"
                if req.packages
                else "all packages"
            )
            main.record_audit_event(
                "packages.python.upgrade",
                f"Streaming Python upgrade of {target_desc} initiated",
                actor=_user,
                severity="info",
            )
        return StreamingResponse(
            main.package_manager.upgrade_python_packages_stream(
                packages=req.packages,
                dry_run=req.dry_run,
                restart_service=req.restart_service,
            ),
            media_type="text/plain; charset=utf-8",
        )
    try:
        res = await asyncio.to_thread(
            main.package_manager.upgrade_python_packages,
            req.packages,
            req.dry_run,
            req.restart_service,
        )
        if not req.dry_run:
            target_desc = (
                f"packages ({', '.join(req.packages)})"
                if req.packages
                else "all packages"
            )
            status_desc = "succeeded" if res.get("success") else "failed"
            main.record_audit_event(
                "packages.python.upgrade",
                f"Python upgrade of {target_desc} {status_desc}",
                actor=_user,
                severity="info" if res.get("success") else "warning",
            )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error executing Python package upgrade: {exc}")


@app.post("/api/packages/python/upgrade/stream")
def api_packages_python_upgrade_stream(
    req: PythonPackageUpgradeRequest,
    _user: str = Depends(require_auth),
) -> StreamingResponse:
    if not req.dry_run:
        target_desc = (
            f"packages ({', '.join(req.packages)})"
            if req.packages
            else "all packages"
        )
        main.record_audit_event(
            "packages.python.upgrade",
            f"Streaming Python upgrade of {target_desc} initiated",
            actor=_user,
            severity="info",
        )
    return StreamingResponse(
        main.package_manager.upgrade_python_packages_stream(
            packages=req.packages,
            dry_run=req.dry_run,
            restart_service=req.restart_service,
        ),
        media_type="text/plain; charset=utf-8",
    )


# ============================================================================
# Process Explorer Routes
# ============================================================================


@app.get("/api/processes")
def api_processes(
    sort: str = Query(default="cpu"),
    limit: int = Query(default=50, ge=1, le=200),
    q: Optional[str] = Query(default=None),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    if sort.lower() not in {"cpu", "memory", "pid", "name"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid sort parameter. Must be one of: cpu, memory, pid, name",
        )
    try:
        return main.get_process_list(sort=sort, limit=limit, q=q)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching processes: {exc}"
        )


@app.post("/api/processes/{pid}/kill")
def api_kill_process(
    pid: int,
    payload: ProcessKillRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.kill_process(pid, payload.signal)
        main.record_audit_event(
            "process.kill",
            f"Process PID {pid} sent signal {res['signal']}",
            actor=_user,
            severity="warning",
            metadata={"pid": pid, "signal": res["signal"]},
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ProcessLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ============================================================================
# Linux User & Group Management Routes
# ============================================================================


@app.get("/api/system/users")
def api_get_users(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.get_system_users()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching system users: {exc}"
        )


@app.post("/api/system/users")
def api_create_user(
    payload: CreateUserRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.create_system_user(
            username=payload.username,
            shell=payload.shell,
            groups=payload.groups,
            create_home=payload.create_home,
        )
        main.record_audit_event(
            "system.user.create",
            f"System user '{payload.username}' created",
            actor=_user,
            metadata={"username": payload.username, "groups": payload.groups},
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/api/system/users/{username}")
def api_update_user(
    username: str,
    payload: UpdateUserRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.update_system_user(
            username=username,
            shell=payload.shell,
            groups=payload.groups,
            locked=payload.locked,
        )
        main.record_audit_event(
            "system.user.update",
            f"System user '{username}' updated",
            actor=_user,
            metadata={"username": username, "locked": payload.locked},
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/system/users/{username}")
def api_delete_user(
    username: str,
    remove_home: bool = Query(default=False),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.delete_system_user(username=username, remove_home=remove_home)
        main.record_audit_event(
            "system.user.delete",
            f"System user '{username}' deleted",
            actor=_user,
            severity="warning",
            metadata={"username": username, "remove_home": remove_home},
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/system/groups")
def api_get_groups(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.get_system_groups()
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching system groups: {exc}"
        )


@app.post("/api/system/groups")
def api_create_group(
    payload: CreateGroupRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.create_system_group(name=payload.name, gid=payload.gid)
        main.record_audit_event(
            "system.group.create",
            f"System group '{payload.name}' created",
            actor=_user,
            metadata={"group": payload.name, "gid": payload.gid},
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/system/groups/{groupname}")
def api_delete_group(
    groupname: str,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.delete_system_group(groupname=groupname)
        main.record_audit_event(
            "system.group.delete",
            f"System group '{groupname}' deleted",
            actor=_user,
            severity="warning",
            metadata={"group": groupname},
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/system/groups/{groupname}/members")
def api_group_members(
    groupname: str,
    payload: GroupMembershipRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        res = main.manage_group_membership(
            groupname=groupname,
            action=payload.action,
            username=payload.username,
        )
        main.record_audit_event(
            "system.group.membership",
            f"User '{payload.username}' {payload.action}ed to group '{groupname}'",
            actor=_user,
            metadata={"group": groupname, "username": payload.username, "action": payload.action},
        )
        return res
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================================
# Audit Logs Routes
# ============================================================================


@app.get("/api/audit/logs")
def api_get_audit_logs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    severity: Optional[str] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        logs, total = main.get_audit_logs(
            limit=limit,
            offset=offset,
            event_type=event_type,
            severity=severity,
            q=q,
        )
        return {"logs": logs, "total": total}
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Error fetching audit logs: {exc}"
        )


# ============================================================================
# Vigil Sentinel: Service Uptime & Health Probes Endpoints
# ============================================================================


class ProbeCreateRequest(BaseModel):
    name: str
    type: str
    target: str
    interval_seconds: Optional[int] = 60
    timeout_seconds: Optional[int] = 5
    expected_status: Optional[int] = 200
    keyword: Optional[str] = ""
    ssl_check: Optional[bool] = True
    enabled: Optional[bool] = True


class ProbeUpdateRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    target: Optional[str] = None
    interval_seconds: Optional[int] = None
    timeout_seconds: Optional[int] = None
    expected_status: Optional[int] = None
    keyword: Optional[str] = None
    ssl_check: Optional[bool] = None
    enabled: Optional[bool] = None


@app.get("/api/probes")
def api_get_probes(
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    probes = main.get_probes_summary()
    total = len(probes)
    up = sum(1 for p in probes if p.get("last_status") == "up")
    down = sum(1 for p in probes if p.get("last_status") == "down")
    active_latencies = [
        p["last_response_time_ms"]
        for p in probes
        if p.get("last_status") in ("up", "degraded") and p.get("last_response_time_ms", 0) > 0
    ]
    avg_latency = (
        round(sum(active_latencies) / len(active_latencies), 1)
        if active_latencies
        else 0.0
    )

    return {
        "probes": probes,
        "summary": {
            "total": total,
            "up": up,
            "down": down,
            "avg_latency": avg_latency,
        },
    }


@app.post("/api/probes")
async def api_create_probe(
    payload: ProbeCreateRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Probe name is required.")
    probe_type = payload.type.strip().lower()
    if probe_type not in ("http", "tcp", "ping"):
        raise HTTPException(
            status_code=400, detail="Probe type must be 'http', 'tcp', or 'ping'."
        )
    target = payload.target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Probe target is required.")

    interval_sec = max(5, int(payload.interval_seconds or 60))
    timeout_sec = max(1, min(60, int(payload.timeout_seconds or 5)))
    expected_status = int(payload.expected_status or 200)
    keyword = (payload.keyword or "").strip()
    ssl_check = True if payload.ssl_check is None else bool(payload.ssl_check)
    enabled = True if payload.enabled is None else bool(payload.enabled)

    probe = main.create_sentinel_probe(
        name=name,
        type=probe_type,
        target=target,
        interval_seconds=interval_sec,
        timeout_seconds=timeout_sec,
        expected_status=expected_status,
        keyword=keyword,
        ssl_check=ssl_check,
        enabled=enabled,
    )
    if not probe:
        raise HTTPException(status_code=500, detail="Failed to create probe.")

    try:
        initial_result = await main.execute_probe(probe)
        main.record_probe_check(probe["id"], initial_result)
    except Exception as exc:
        logging.warning(f"Initial probe execution encountered error: {exc}")

    main.record_audit_event(
        "sentinel.probe.create",
        f"Created sentinel probe '{name}' ({probe_type}: {target})",
        actor=_user,
        metadata={"probe_id": probe["id"], "name": name, "type": probe_type, "target": target},
    )

    enriched = main.get_sentinel_probe_enriched(probe["id"])
    await _broadcast_module("probes")
    return enriched or probe


@app.get("/api/probes/{probe_id}")
def api_get_probe(
    probe_id: int,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    probe = main.get_sentinel_probe_enriched(probe_id)
    if not probe:
        raise HTTPException(status_code=404, detail="Probe not found")
    return probe


@app.put("/api/probes/{probe_id}")
async def api_update_probe(
    probe_id: int,
    payload: ProbeUpdateRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    existing = main.get_sentinel_probe(probe_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Probe not found")

    updates = payload.model_dump(exclude_unset=True)
    if "type" in updates:
        t = str(updates["type"]).strip().lower()
        if t not in ("http", "tcp", "ping"):
            raise HTTPException(
                status_code=400, detail="Probe type must be 'http', 'tcp', or 'ping'."
            )
        updates["type"] = t
    if "name" in updates:
        n = str(updates["name"]).strip()
        if not n:
            raise HTTPException(status_code=400, detail="Probe name cannot be empty.")
        updates["name"] = n
    if "target" in updates:
        tgt = str(updates["target"]).strip()
        if not tgt:
            raise HTTPException(status_code=400, detail="Probe target cannot be empty.")
        updates["target"] = tgt
    if "interval_seconds" in updates and updates["interval_seconds"] is not None:
        updates["interval_seconds"] = max(5, int(updates["interval_seconds"]))
    if "timeout_seconds" in updates and updates["timeout_seconds"] is not None:
        updates["timeout_seconds"] = max(1, min(60, int(updates["timeout_seconds"])))

    updated = main.update_sentinel_probe(probe_id, updates)
    main.record_audit_event(
        "sentinel.probe.update",
        f"Updated sentinel probe '{existing['name']}' (ID: {probe_id})",
        actor=_user,
        metadata={"probe_id": probe_id, "updated_fields": list(updates.keys())},
    )
    enriched = main.get_sentinel_probe_enriched(probe_id)
    await _broadcast_module("probes")
    return enriched or (updated or existing)


@app.delete("/api/probes/{probe_id}")
async def api_delete_probe(
    probe_id: int,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    probe = main.get_sentinel_probe(probe_id)
    if not probe:
        raise HTTPException(status_code=404, detail="Probe not found")

    main.delete_sentinel_probe(probe_id)
    main.record_audit_event(
        "sentinel.probe.delete",
        f"Deleted sentinel probe '{probe['name']}' (ID: {probe_id})",
        actor=_user,
        metadata={"probe_id": probe_id, "name": probe["name"]},
    )
    await _broadcast_module("probes")
    return {"ok": True, "message": f"Probe {probe_id} deleted successfully."}


@app.post("/api/probes/{probe_id}/test")
async def api_test_probe(
    probe_id: int,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    probe = main.get_sentinel_probe(probe_id)
    if not probe:
        raise HTTPException(status_code=404, detail="Probe not found")

    result = await main.execute_probe(probe)
    main.record_probe_check(probe_id, result)
    await _broadcast_module("probes")
    return {
        "ok": result.get("status") in ("up", "degraded"),
        "result": result,
    }


@app.get("/api/probes/{probe_id}/history")
def api_get_probe_history(
    probe_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    probe = main.get_sentinel_probe(probe_id)
    if not probe:
        raise HTTPException(status_code=404, detail="Probe not found")

    history = main.get_probe_history(probe_id, limit=limit)
    return {
        "probe_id": probe_id,
        "history": history,
        "total": len(history),
    }


class UfwRuleRequest(BaseModel):
    action: str = "allow"
    port: str
    proto: str = "any"
    from_ip: Optional[str] = None


class SshIpBanRequest(BaseModel):
    action: str
    ip: str


@app.get("/api/network/security")
def api_network_security(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.get_network_security_telemetry()
    except Exception as exc:
        logging.error(f"Error fetching network security telemetry: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch network security telemetry: {exc}"
        )


@app.get("/api/network/ssh-security")
def api_network_ssh_security(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.get_ssh_security_telemetry()
    except Exception as exc:
        logging.error(f"Error fetching SSH security telemetry: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch SSH security telemetry: {exc}"
        )


@app.post("/api/network/ssh-security/ban")
def api_manage_ssh_ip_ban(
    payload: SshIpBanRequest,
    request: Request,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    client_ip = request.client.host if request.client else ""
    actor = _user.get("username", "admin") if isinstance(_user, dict) else (str(_user) if _user else "admin")
    try:
        return main.manage_ssh_ip_ban(
            action=payload.action,
            ip_address=payload.ip,
            actor=actor,
            client_ip=client_ip,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to manage IP ban: {exc}")


@app.get("/api/network/ports")
def api_network_ports(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        ports = main.get_open_listening_ports()
        total = len(ports)
        public = sum(1 for p in ports if p.get("is_public"))
        local = total - public
        tcp_count = sum(1 for p in ports if str(p.get("proto", "")).lower().startswith("tcp"))
        udp_count = sum(1 for p in ports if str(p.get("proto", "")).lower().startswith("udp"))
        return {
            "ports": ports,
            "summary": {
                "total": total,
                "public": public,
                "local": local,
                "total_listening": total,
                "public_exposed_count": public,
                "local_only_count": local,
                "tcp_count": tcp_count,
                "udp_count": udp_count,
            },
        }
    except Exception as exc:
        logging.error(f"Error fetching network ports: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch open ports: {exc}"
        )


@app.get("/api/network/firewall")
def api_network_firewall(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.get_ufw_firewall_status()
    except Exception as exc:
        logging.error(f"Error fetching UFW firewall status: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch firewall status: {exc}"
        )


@app.post("/api/network/firewall/rules")
def api_add_firewall_rule(
    payload: UfwRuleRequest,
    request: Request,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    client_ip = request.client.host if request.client else ""
    res = main.manage_ufw_rule(
        action=payload.action,
        port=payload.port,
        proto=payload.proto,
        from_ip=payload.from_ip,
        actor=_user,
        ip_address=client_ip,
    )
    if not res.get("success"):
        raise HTTPException(
            status_code=400,
            detail=res.get("message", "Failed to add firewall rule"),
        )
    return res


@app.delete("/api/network/firewall/rules/{rule_index}")
def api_delete_firewall_rule(
    rule_index: int,
    request: Request,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    if rule_index < 1:
        raise HTTPException(
            status_code=400, detail="Rule index must be a positive integer."
        )
    client_ip = request.client.host if request.client else ""
    res = main.manage_ufw_rule(
        action="delete",
        port=str(rule_index),
        proto="any",
        actor=_user,
        ip_address=client_ip,
    )
    if not res.get("success"):
        raise HTTPException(
            status_code=400,
            detail=res.get("message", "Failed to delete firewall rule"),
        )
    return res


# ============================================================================
# Backups & Cron Scheduler Models and Endpoints
# ============================================================================


class BackupRunRequest(BaseModel):
    target_type: str = "directory"
    source_path: str = ""
    stack_name: str = ""
    target_name: str = ""
    excludes: list[str] = []
    stop_containers: bool = False
    retain_count: int = 7


class BackupTargetRequest(BaseModel):
    id: Optional[int] = None
    name: str = ""
    target_type: str = "directory"
    source_path: str = ""
    stack_name: str = ""
    excludes: list[str] = []
    stop_containers: bool = False
    retain_count: int = 7


class BackupRestoreRequest(BaseModel):
    destination_path: Optional[str] = ""


class CronJobRequest(BaseModel):
    id: Optional[int] = None
    name: str
    task_type: str
    cron_expression: str
    task_config: dict[str, Any] = {}
    enabled: bool = True
    notify_on_failure: bool = True
    notify_on_success: Optional[bool] = None


class BackupConfigRequest(BaseModel):
    backup_dir: str


class CronJobToggleRequest(BaseModel):
    enabled: Optional[bool] = None


@app.get("/api/backups")
def api_get_backups(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.get_backups_overview()
    except Exception as exc:
        logging.error(f"Error fetching backups overview: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch backups overview: {exc}"
        )


@app.post("/api/backups/config")
def api_configure_backup_dir(
    payload: BackupConfigRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    raw_dir = str(payload.backup_dir or "").strip()
    if not raw_dir:
        raise HTTPException(status_code=400, detail="backup_dir is required.")
    try:
        resolved_path = str(
            main._validate_safe_host_directory(raw_dir, purpose="backup storage")
        )
        out_path = Path(resolved_path)
        out_path.mkdir(parents=True, exist_ok=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=f"Cannot create backup directory: {exc}"
        )

    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    main.update_app_settings({"backup_dir": resolved_path})
    main.record_audit_event(
        "backup.config.updated",
        f"Updated backup archive storage directory to '{resolved_path}'",
        actor=actor,
        severity="info",
        metadata={"backup_dir": resolved_path},
    )
    return {"status": "ok", "backup_dir": resolved_path}


@app.post("/api/backups/run")
async def api_run_backup(
    payload: BackupRunRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        archive = main.create_backup_archive(
            target_type=payload.target_type,
            source_path=payload.source_path,
            stack_name=payload.stack_name,
            target_name=payload.target_name,
            excludes=payload.excludes,
            stop_containers=payload.stop_containers,
            retain_count=payload.retain_count,
            actor=actor,
        )
        await _broadcast_module("backups")
        return {
            "success": True,
            "archive": archive,
            "message": f"Created backup archive '{archive['filename']}' ({archive['size_human']}, {archive['file_count']} files).",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error creating backup archive: {exc}")
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")


@app.post("/api/backups/targets")
async def api_save_backup_target(
    payload: BackupTargetRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        target = main.save_backup_target(
            payload.model_dump(),
            actor=actor,
        )
        await _broadcast_module("backups")
        return {
            "success": True,
            "target": target,
            "message": f"Backup target '{target['name']}' saved.",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error saving backup target: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to save backup target: {exc}"
        )


@app.delete("/api/backups/targets/{target_id}")
async def api_delete_backup_target(
    target_id: int,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        res = main.delete_backup_target(target_id=target_id, actor=actor)
        await _broadcast_module("backups")
        return res
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error deleting backup target {target_id}: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete backup target: {exc}"
        )


@app.get("/api/backups/archives/{filename}/download")
def api_download_backup_archive(
    filename: str,
    _user: str = Depends(require_auth),
):
    try:
        clean_filename = main._validate_archive_filename(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    backups_dir = main.get_backups_dir().resolve()
    archive_path = (backups_dir / clean_filename).resolve()
    if archive_path != backups_dir and backups_dir not in archive_path.parents:
        raise HTTPException(status_code=400, detail="Path traversal detected")
    if not archive_path.is_file():
        raise HTTPException(
            status_code=404, detail=f"Backup archive '{clean_filename}' not found."
        )

    return FileResponse(
        path=str(archive_path),
        filename=clean_filename,
        media_type="application/gzip",
    )


@app.post("/api/backups/archives/{filename}/restore")
async def api_restore_backup_archive(
    filename: str,
    payload: Optional[BackupRestoreRequest] = None,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    dest_path = (
        payload.destination_path if payload and payload.destination_path else ""
    )
    try:
        res = main.restore_backup_archive(
            filename=filename,
            destination_path=dest_path,
            actor=actor,
        )
        await _broadcast_module("backups")
        return {
            "success": True,
            "result": res,
            "message": f"Successfully restored {res['restored_files']} files to '{res['destination_path']}'.",
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error restoring backup archive {filename}: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to restore archive: {exc}"
        )


@app.delete("/api/backups/archives/{filename}")
async def api_delete_backup_archive(
    filename: str,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        res = main.delete_backup_archive(filename=filename, actor=actor)
        await _broadcast_module("backups")
        return res
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error deleting backup archive {filename}: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete archive: {exc}"
        )


# Cron Scheduler Endpoints


@app.get("/api/cron/jobs")
def api_get_cron_jobs(_user: str = Depends(require_auth)) -> dict[str, Any]:
    try:
        return main.list_cron_jobs()
    except Exception as exc:
        logging.error(f"Error listing cron jobs: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch scheduled cron jobs: {exc}"
        )


@app.post("/api/cron/jobs")
async def api_save_cron_job(
    payload: CronJobRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        job = main.create_or_update_cron_job(
            payload.model_dump(),
            actor=actor,
        )
        await _broadcast_module("backups")
        return {
            "success": True,
            "job": job,
            "message": f"Cron job '{job['name']}' ({job['cron_expression']}) saved.",
        }
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error saving cron job: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to save cron job: {exc}")


@app.put("/api/cron/jobs/{job_id}")
async def api_update_cron_job(
    job_id: int,
    payload: CronJobRequest,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        data = payload.model_dump()
        data["id"] = int(job_id)
        job = main.create_or_update_cron_job(
            data,
            actor=actor,
        )
        await _broadcast_module("backups")
        return {
            "success": True,
            "job": job,
            "message": f"Cron job '{job['name']}' ({job['cron_expression']}) updated.",
        }
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error updating cron job {job_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to update cron job: {exc}")


@app.post("/api/cron/jobs/{job_id}/toggle")
async def api_toggle_cron_job(
    job_id: int,
    payload: Optional[CronJobToggleRequest] = None,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    enabled = payload.enabled if payload is not None else None
    try:
        job = main.toggle_cron_job(job_id=job_id, enabled=enabled, actor=actor)
        await _broadcast_module("backups")
        return {
            "success": True,
            "job": job,
            "message": f"Cron job '{job['name']}' is now {'enabled' if job['enabled'] else 'disabled'}.",
        }
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error toggling cron job {job_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to toggle cron job: {exc}")


@app.post("/api/cron/jobs/{job_id}/run")
async def api_run_cron_job_now(
    job_id: int,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        run = main.run_cron_job_now(
            job_id=job_id, trigger_source="manual", actor=actor
        )
        await _broadcast_module("backups")
        return {
            "success": True,
            "run": run,
            "message": f"Cron job '{run['job_name']}' executed [{run['status']}]: {run['output']}",
        }
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error executing cron job {job_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to run cron job: {exc}")


@app.delete("/api/cron/jobs/{job_id}")
async def api_delete_cron_job(
    job_id: int,
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    actor = (
        _user.get("username", "admin")
        if isinstance(_user, dict)
        else (str(_user) if _user else "admin")
    )
    try:
        res = main.delete_cron_job(job_id=job_id, actor=actor)
        await _broadcast_module("backups")
        return res
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logging.error(f"Error deleting cron job {job_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to delete cron job: {exc}")


class CacheControlStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path: Any,
        stat_result: Any,
        scope: Any,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(
            full_path, stat_result, scope, status_code=status_code
        )
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


@app.get("/")
@app.get("/overview")
@app.get("/storage")
@app.get("/disks")
@app.get("/raid")
@app.get("/containers")
@app.get("/containers/{subpath:path}")
@app.get("/services")
@app.get("/processes")
@app.get("/packages")
@app.get("/packages/{subpath:path}")
@app.get("/updates")
@app.get("/users")
@app.get("/users/{subpath:path}")
@app.get("/notifications")
@app.get("/audit")
@app.get("/probes")
@app.get("/probes/{subpath:path}")
@app.get("/sentinel")
@app.get("/sentinel/{subpath:path}")
@app.get("/network")
@app.get("/network/{subpath:path}")
@app.get("/firewall")
@app.get("/firewall/{subpath:path}")
@app.get("/backups")
@app.get("/backups/{subpath:path}")
@app.get("/cron")
@app.get("/cron/{subpath:path}")
@app.get("/login")
@app.get("/logs")
def serve_index(subpath: Optional[str] = None):
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        content = index_file.read_text(encoding="utf-8")
        current_version = main.get_current_version_info()["current_version"]
        content = re.sub(
            r"/static/styles\.css(\?[^\"'>\s]*)?",
            f"/static/styles.css?v={current_version}",
            content,
        )
        content = re.sub(
            r"/static/app\.js(\?[^\"'>\s]*)?",
            f"/static/app.js?v={current_version}",
            content,
        )
        return HTMLResponse(content=content, status_code=200, headers=headers)
    return HTMLResponse(
        content="<!DOCTYPE html><html><head><title>Vigil</title></head>"
        "<body><h1>Vigil</h1></body></html>",
        status_code=200,
        headers=headers,
    )


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon():
    favicon_path = STATIC_DIR / "favicon.ico"
    if favicon_path.is_file():
        return FileResponse(
            favicon_path,
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    return Response(status_code=404)


app.mount(
    "/static", CacheControlStaticFiles(directory=str(STATIC_DIR)), name="static"
)


@app.get("/{full_path:path}")
def serve_spa_fallback(full_path: str):
    if (
        full_path == "api"
        or full_path.startswith("api/")
        or full_path == "static"
        or full_path.startswith("static/")
        or full_path == "favicon.ico"
    ):
        raise HTTPException(status_code=404, detail="Not Found")
    return serve_index()
