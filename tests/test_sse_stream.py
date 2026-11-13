import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import api
import main
from test_main import asgi_get


def parse_sse_block(block: str) -> dict[str, str]:
    evt = {"event": "message", "data": ""}
    for line in block.split("\n"):
        if line.startswith("event:"):
            evt["event"] = line[6:].strip()
        elif line.startswith("data:"):
            evt["data"] = line[5:].strip()
    return evt


async def asgi_sse_client(
    app,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    max_events: int = 1,
    on_event: any = None,
    timeout: float = 3.0,
):
    parsed = urlsplit(path)
    req_headers = [(b"host", b"testserver")]
    if headers:
        for k, v in headers.items():
            req_headers.append((k.lower().encode("latin1"), v.encode("latin1")))
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req_headers.append((b"cookie", cookie_str.encode("latin1")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": parsed.path or "/api/events",
        "raw_path": (parsed.path or "/api/events").encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "headers": req_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    status_code: int = 500
    resp_headers: dict[str, str] = {}
    events: list[dict[str, str]] = []
    disconnect_event = asyncio.Event()
    buffer = ""

    async def receive():
        await disconnect_event.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status_code, buffer
        if message["type"] == "http.response.start":
            status_code = message["status"]
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin1").lower()] = v.decode("latin1")
            if status_code != 200:
                disconnect_event.set()
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"").decode("utf-8")
            if chunk:
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    parsed_evt = parse_sse_block(block)
                    if parsed_evt and (parsed_evt["event"] or parsed_evt["data"]):
                        events.append(parsed_evt)
                        if on_event:
                            res = on_event(parsed_evt, len(events))
                            if asyncio.iscoroutine(res):
                                await res
                        if len(events) >= max_events:
                            disconnect_event.set()
                            break

    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        disconnect_event.set()

    return status_code, resp_headers, events


class TestSSEStreaming(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_sse.sqlite3"
        self._orig_db = main.RAID_STATE_DB
        main.RAID_STATE_DB = self.db_path
        main.init_state_db()
        main.create_user("sseuser", "password123", email="sse@test.local")
        self.token = main.create_session("sseuser")
        self.auth_cookies = {"session_token": self.token}
        self.auth_headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        main.RAID_STATE_DB = self._orig_db
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. SSEManager Core Unit Tests
    # -------------------------------------------------------------------------
    def test_sse_manager_subscribe_and_unsubscribe(self):
        mgr = api.SSEManager()
        self.assertEqual(mgr.subscriber_count, 0)
        self.assertEqual(len(mgr.subscribers), 0)

        q1 = mgr.subscribe()
        self.assertEqual(mgr.subscriber_count, 1)
        self.assertIn(q1, mgr.subscribers)

        q2 = mgr.subscribe()
        self.assertEqual(mgr.subscriber_count, 2)
        self.assertIn(q2, mgr.subscribers)

        mgr.unsubscribe(q1)
        self.assertEqual(mgr.subscriber_count, 1)
        self.assertNotIn(q1, mgr.subscribers)
        self.assertIn(q2, mgr.subscribers)

        mgr.unsubscribe(q2)
        self.assertEqual(mgr.subscriber_count, 0)

    def test_sse_manager_async_broadcast(self):
        async def _test():
            mgr = api.SSEManager()
            q1 = mgr.subscribe()
            q2 = mgr.subscribe()

            await mgr.broadcast("telemetry", {"cpu": 35.5, "memory": 60.0})

            evt1, data1 = await q1.get()
            self.assertEqual(evt1, "telemetry")
            self.assertEqual(data1, {"cpu": 35.5, "memory": 60.0})

            evt2, data2 = await q2.get()
            self.assertEqual(evt2, "telemetry")
            self.assertEqual(data2, {"cpu": 35.5, "memory": 60.0})

            mgr.unsubscribe(q1)
            mgr.unsubscribe(q2)

        asyncio.run(_test())

    def test_sse_manager_broadcast_sync(self):
        async def _test():
            mgr = api.SSEManager()
            loop = asyncio.get_running_loop()
            mgr.set_loop(loop)
            q = mgr.subscribe()

            mgr.broadcast_sync("docker_event", {"action": "start", "container": "web"})
            # Allow event loop turn
            await asyncio.sleep(0.01)

            evt, data = await q.get()
            self.assertEqual(evt, "docker_event")
            self.assertEqual(data, {"action": "start", "container": "web"})
            mgr.unsubscribe(q)

        asyncio.run(_test())

    def test_sse_manager_queue_full_drops_oldest(self):
        async def _test():
            mgr = api.SSEManager(max_queue_size=2)
            q = mgr.subscribe()

            await mgr.broadcast("evt1", {"idx": 1})
            await mgr.broadcast("evt2", {"idx": 2})
            # Third message causes queue full -> drops oldest
            await mgr.broadcast("evt3", {"idx": 3})

            item1 = await q.get()
            item2 = await q.get()
            self.assertEqual(item1[0], "evt2")
            self.assertEqual(item2[0], "evt3")
            mgr.unsubscribe(q)

        asyncio.run(_test())

    def test_format_sse_helper(self):
        msg1 = api.format_sse("ping", {})
        self.assertEqual(msg1, "event: ping\ndata: {}\n\n")

        msg2 = api.format_sse("telemetry", {"cpu": 10.0})
        self.assertEqual(msg2, 'event: telemetry\ndata: {"cpu": 10.0}\n\n')

        msg3 = api.format_sse("raw", "plain-text-payload")
        self.assertEqual(msg3, "event: raw\ndata: plain-text-payload\n\n")

    # -------------------------------------------------------------------------
    # 2. HTTP Endpoint Authentication & Headers Tests
    # -------------------------------------------------------------------------
    def test_get_events_unauthenticated_returns_401(self):
        resp = asgi_get(api.app, "/api/events")
        self.assertEqual(resp.status_code, 401)

        alias_resp = asgi_get(api.app, "/api/stream/events")
        self.assertEqual(alias_resp.status_code, 401)

    def test_get_events_headers_and_snapshot(self):
        async def _test():
            status, headers, events = await asgi_sse_client(
                api.app,
                "/api/events",
                cookies=self.auth_cookies,
                max_events=1,
            )
            self.assertEqual(status, 200)
            self.assertIn("text/event-stream", headers.get("content-type", ""))
            self.assertEqual(headers.get("cache-control"), "no-cache")
            self.assertEqual(headers.get("connection"), "keep-alive")
            self.assertEqual(headers.get("x-accel-buffering"), "no")

            self.assertGreaterEqual(len(events), 1)
            snapshot_event = events[0]
            self.assertEqual(snapshot_event["event"], "snapshot")
            data = json.loads(snapshot_event["data"])
            self.assertEqual(data["type"], "snapshot")
            self.assertIn("system", data)
            self.assertIn("raid", data)
            self.assertIn("notification_state", data)
            self.assertIn("services_summary", data)

        asyncio.run(_test())

    def test_get_stream_events_alias(self):
        async def _test():
            status, headers, events = await asgi_sse_client(
                api.app,
                "/api/stream/events",
                headers=self.auth_headers,
                max_events=1,
            )
            self.assertEqual(status, 200)
            self.assertEqual(events[0]["event"], "snapshot")

        asyncio.run(_test())

    def test_auth_via_query_param_token(self):
        async def _test():
            status, headers, events = await asgi_sse_client(
                api.app,
                f"/api/events?token={self.token}",
                max_events=1,
            )
            self.assertEqual(status, 200)
            self.assertEqual(events[0]["event"], "snapshot")

        asyncio.run(_test())

    # -------------------------------------------------------------------------
    # 3. Real-Time Event Dispatch & Keepalive Pings
    # -------------------------------------------------------------------------
    def test_get_events_receives_broadcasts(self):
        async def _test():
            async def on_event(evt, count):
                if count == 1:
                    # After receiving initial snapshot, broadcast telemetry
                    await api.sse_manager.broadcast("telemetry", {"cpu_percent": 12.3})
                elif count == 2:
                    # Broadcast docker event
                    await api.sse_manager.broadcast(
                        "docker_event",
                        {"event": {"action": "restart", "name": "monitored-service"}},
                    )
                elif count == 3:
                    # Broadcast module update
                    await api.sse_manager.broadcast(
                        "module_update",
                        {"module": "raid", "status": "clean"},
                    )

            status, _, events = await asgi_sse_client(
                api.app,
                "/api/events",
                cookies=self.auth_cookies,
                max_events=4,
                on_event=on_event,
                timeout=4.0,
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(events), 4)

            self.assertEqual(events[0]["event"], "snapshot")

            self.assertEqual(events[1]["event"], "telemetry")
            telemetry_data = json.loads(events[1]["data"])
            self.assertEqual(telemetry_data.get("cpu_percent"), 12.3)

            self.assertEqual(events[2]["event"], "docker_event")
            docker_data = json.loads(events[2]["data"])
            self.assertEqual(docker_data["event"]["action"], "restart")

            self.assertEqual(events[3]["event"], "module_update")
            mod_data = json.loads(events[3]["data"])
            self.assertEqual(mod_data.get("module"), "raid")

        asyncio.run(_test())

    def test_get_events_keepalive_ping(self):
        async def _test():
            status, _, events = await asgi_sse_client(
                api.app,
                "/api/events?ping_interval=0.05",
                cookies=self.auth_cookies,
                max_events=2,
                timeout=2.0,
            )
            self.assertEqual(status, 200)
            self.assertGreaterEqual(len(events), 2)
            self.assertEqual(events[0]["event"], "snapshot")
            self.assertEqual(events[1]["event"], "ping")
            self.assertEqual(json.loads(events[1]["data"]), {})

        asyncio.run(_test())

    def test_disconnect_unsubscribes_queue(self):
        async def _test():
            before_subscribers = api.sse_manager.subscriber_count
            status, _, events = await asgi_sse_client(
                api.app,
                "/api/events",
                cookies=self.auth_cookies,
                max_events=1,
            )
            self.assertEqual(status, 200)
            # After client completes and disconnects, subscriber count should return to previous
            await asyncio.sleep(0.05)
            self.assertEqual(api.sse_manager.subscriber_count, before_subscribers)

        asyncio.run(_test())

    def test_record_notification_triggers_sse_broadcast(self):
        async def _test():
            async def on_event(evt, count):
                if count == 1:
                    # snapshot received; trigger a notification in main
                    main.record_notification(
                        "alert_disk",
                        time.time(),
                        False,
                        title="Disk Alert",
                        details="Root filesystem > 90%",
                    )

            status, _, events = await asgi_sse_client(
                api.app,
                "/api/events",
                cookies=self.auth_cookies,
                max_events=2,
                on_event=on_event,
                timeout=3.0,
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["event"], "snapshot")
            self.assertEqual(events[1]["event"], "notification")
            notif_data = json.loads(events[1]["data"])
            self.assertEqual(notif_data["status"], "alert_disk")
            self.assertEqual(notif_data["title"], "Disk Alert")
            self.assertEqual(notif_data["details"], "Root filesystem > 90%")

        asyncio.run(_test())

    # -------------------------------------------------------------------------
    # 4. Live Telemetry Tick & Dedicated Streaming Worker Tests
    # -------------------------------------------------------------------------
    def test_get_live_telemetry_tick_schema(self):
        tick = main.get_live_telemetry_tick()
        self.assertIsInstance(tick, dict)
        self.assertIn("cpu", tick)
        self.assertIn("memory", tick)
        self.assertIn("load_avg", tick)
        self.assertIn("timestamp", tick)

        # CPU block
        self.assertIn("percent", tick["cpu"])
        self.assertIsInstance(tick["cpu"]["percent"], (int, float))
        self.assertIn("cores", tick["cpu"])
        self.assertIsInstance(tick["cpu"]["cores"], list)
        self.assertGreater(len(tick["cpu"]["cores"]), 0)
        for core_val in tick["cpu"]["cores"]:
            self.assertIsInstance(core_val, (int, float))

        # Memory block
        mem = tick["memory"]
        for key in ("percent", "used", "total", "available", "free", "cached"):
            self.assertIn(key, mem)
        self.assertIsInstance(mem["percent"], (int, float))
        self.assertIsInstance(mem["used"], int)
        self.assertIsInstance(mem["total"], int)
        self.assertIsInstance(mem["available"], int)
        self.assertIsInstance(mem["free"], int)
        self.assertIsInstance(mem["cached"], int)

        # Load avg and timestamp
        self.assertIsInstance(tick["load_avg"], list)
        self.assertIsInstance(tick["timestamp"], float)
        self.assertGreater(tick["timestamp"], 0.0)

    def test_get_live_telemetry_tick_non_blocking_performance(self):
        # Warmup
        main.get_live_telemetry_tick()

        iterations = 25
        t0 = time.perf_counter()
        for _ in range(iterations):
            tick = main.get_live_telemetry_tick()
            self.assertIn("cpu", tick)
        duration = time.perf_counter() - t0
        avg_ms = (duration / iterations) * 1000.0
        # Must execute non-blocking, well below 5.0ms per tick (typically <0.5ms)
        self.assertLess(avg_ms, 5.0, f"Tick collection took too long: {avg_ms:.2f}ms")

    def test_get_live_telemetry_tick_error_fallbacks(self):
        from unittest.mock import patch
        with patch("psutil.cpu_percent", side_effect=RuntimeError("psutil cpu error")):
            with patch("psutil.virtual_memory", side_effect=RuntimeError("psutil mem error")):
                with patch("os.getloadavg", side_effect=OSError("loadavg unsupported")):
                    tick = main.get_live_telemetry_tick()
                    self.assertEqual(tick["cpu"]["percent"], 0.0)
                    self.assertEqual(tick["cpu"]["cores"], [0.0])
                    self.assertEqual(tick["memory"]["percent"], 0.0)
                    self.assertEqual(tick["memory"]["used"], 0)
                    self.assertEqual(tick["load_avg"], [])
                    self.assertGreater(tick["timestamp"], 0.0)

    def test_live_telemetry_stream_worker_broadcasts_when_subscribers_exist(self):
        async def _test():
            q = api.sse_manager.subscribe()
            task = asyncio.create_task(api.live_telemetry_stream_worker(interval=0.05))
            try:
                event, sample = await asyncio.wait_for(q.get(), timeout=1.5)
                self.assertEqual(event, "telemetry")
                self.assertIn("cpu", sample)
                self.assertIn("memory", sample)
                self.assertIn("timestamp", sample)
                self.assertIsInstance(sample["cpu"]["percent"], (int, float))
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                api.sse_manager.unsubscribe(q)

        asyncio.run(_test())

    def test_live_telemetry_stream_worker_idle_when_no_subscribers(self):
        async def _test():
            from unittest.mock import patch
            # Ensure subscribers set is clear
            for q in list(api.sse_manager.subscribers):
                api.sse_manager.unsubscribe(q)
            api.ws_manager.active_connections.clear()

            with patch.object(main, "get_live_telemetry_tick") as mock_tick:
                task = asyncio.create_task(api.live_telemetry_stream_worker(interval=0.03))
                await asyncio.sleep(0.1)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                mock_tick.assert_not_called()

        asyncio.run(_test())

    def test_live_telemetry_stream_worker_default_interval(self):
        import inspect
        sig = inspect.signature(api.live_telemetry_stream_worker)
        self.assertEqual(sig.parameters["interval"].default, 0.25)

    def test_get_live_telemetry_tick_non_blocking_latency(self):
        # Warmup
        main.get_live_telemetry_tick()
        start = time.perf_counter()
        for _ in range(20):
            tick = main.get_live_telemetry_tick()
            self.assertIn("cpu", tick)
            self.assertIn("memory", tick)
        elapsed = time.perf_counter() - start
        avg_ms = (elapsed / 20) * 1000
        # Must be fast and non-blocking (<10ms per tick even on loaded CI)
        self.assertLess(avg_ms, 10.0, f"Average tick duration {avg_ms:.2f}ms exceeds threshold")

    def test_live_state_hub_coordination(self):
        # Test main.live_state_hub
        hub_tick = main.live_state_hub.tick()
        self.assertIsNotNone(hub_tick)
        self.assertEqual(main.live_state_hub.latest_tick, hub_tick)

        # Test api.live_state_hub
        for q in list(api.sse_manager.subscribers):
            api.sse_manager.unsubscribe(q)
        api.ws_manager.active_connections.clear()
        self.assertFalse(api.live_state_hub.has_active_subscribers)

        q = api.sse_manager.subscribe()
        try:
            self.assertTrue(api.live_state_hub.has_active_subscribers)
        finally:
            api.sse_manager.unsubscribe(q)

    def test_api_telemetry_live_endpoint(self):
        resp = asgi_get(api.app, "/api/telemetry/live", headers=self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("cpu", data)
        self.assertIn("memory", data)
        self.assertIn("load_avg", data)
        self.assertIn("timestamp", data)

    def test_get_live_telemetry_tick_ewma_smoothing(self):
        main.reset_live_telemetry_ewma()
        try:
            # 1. First tick sets initial baseline without prior history
            with (
                patch("psutil.cpu_percent", side_effect=[100.0, [100.0, 80.0]]),
                patch("psutil.virtual_memory", return_value=MagicMock(percent=50.0, used=100, total=200, available=100, free=50, cached=50)),
                patch("os.getloadavg", return_value=[1.0, 1.0, 1.0]),
            ):
                tick1 = api.get_live_telemetry_tick()
                self.assertEqual(tick1["cpu"]["percent"], 100.0)
                self.assertEqual(tick1["cpu"]["cores"], [100.0, 80.0])

            # 2. Second tick raw values: 0.0% and [0.0, 0.0]
            # EWMA (alpha = 0.5):
            # cpu_percent: 0.5 * 0.0 + 0.5 * 100.0 = 50.0
            # cores: [0.5 * 0.0 + 0.5 * 100.0, 0.5 * 0.0 + 0.5 * 80.0] = [50.0, 40.0]
            with (
                patch("psutil.cpu_percent", side_effect=[0.0, [0.0, 0.0]]),
                patch("psutil.virtual_memory", return_value=MagicMock(percent=50.0, used=100, total=200, available=100, free=50, cached=50)),
                patch("os.getloadavg", return_value=[1.0, 1.0, 1.0]),
            ):
                tick2 = api.get_live_telemetry_tick()
                self.assertEqual(tick2["cpu"]["percent"], 50.0)
                self.assertEqual(tick2["cpu"]["cores"], [50.0, 40.0])

            # 3. Third tick raw values: 0.0% and [0.0, 0.0]
            # EWMA (alpha = 0.5):
            # cpu_percent: 0.5 * 0.0 + 0.5 * 50.0 = 25.0
            # cores: [0.5 * 0.0 + 0.5 * 50.0, 0.5 * 0.0 + 0.5 * 40.0] = [25.0, 20.0]
            with (
                patch("psutil.cpu_percent", side_effect=[0.0, [0.0, 0.0]]),
                patch("psutil.virtual_memory", return_value=MagicMock(percent=50.0, used=100, total=200, available=100, free=50, cached=50)),
                patch("os.getloadavg", return_value=[1.0, 1.0, 1.0]),
            ):
                tick3 = api.get_live_telemetry_tick()
                self.assertEqual(tick3["cpu"]["percent"], 25.0)
                self.assertEqual(tick3["cpu"]["cores"], [25.0, 20.0])
        finally:
            main.reset_live_telemetry_ewma()

    def test_live_telemetry_stream_worker_broadcasts_consistent_payload(self):
        async def _test():
            q = api.sse_manager.subscribe()
            mock_ws = AsyncMock()
            api.ws_manager.active_connections.append(mock_ws)
            task = asyncio.create_task(api.live_telemetry_stream_worker(interval=0.03))
            try:
                # 1. SSE received payload
                event, sse_payload = await asyncio.wait_for(q.get(), timeout=1.5)
                self.assertEqual(event, "telemetry")
                self.assertEqual(sse_payload["type"], "telemetry")
                self.assertIn("data", sse_payload)
                self.assertIn("sample", sse_payload)
                self.assertIn("cpu", sse_payload)
                self.assertIn("memory", sse_payload)

                # 2. WS received payload
                await asyncio.sleep(0.06)
                mock_ws.send_json.assert_called()
                ws_payload = mock_ws.send_json.call_args[0][0]
                self.assertEqual(ws_payload["type"], "telemetry")
                self.assertIn("data", ws_payload)
                self.assertIn("sample", ws_payload)
                self.assertEqual(ws_payload["data"], ws_payload["sample"])
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                api.sse_manager.unsubscribe(q)
                api.ws_manager.disconnect(mock_ws)

        asyncio.run(_test())

    def test_notifications_evaluator_loop_skips_telemetry_when_live_worker_streaming(self):
        async def _test():
            mock_ws = AsyncMock()
            api.ws_manager.active_connections.append(mock_ws)
            q = api.sse_manager.subscribe()

            fake_sample = {
                "timestamp": 123456789.0,
                "cpu_percent": 15.0,
                "memory_percent": 30.0,
            }

            try:
                # Case 1: When actively streaming, notifications_evaluator_loop should NOT broadcast telemetry
                with (
                    patch.object(main, "record_telemetry_sample", return_value=fake_sample),
                    patch.object(main, "evaluate_threshold_alerts", return_value=None),
                    patch.object(main, "evaluate_scheduled_reports", return_value=None),
                    patch.object(main, "evaluate_due_cron_jobs", return_value=[]),
                    patch.object(api, "is_live_telemetry_streaming", return_value=True),
                    patch("asyncio.sleep", side_effect=asyncio.CancelledError),
                ):
                    try:
                        await api.notifications_evaluator_loop()
                    except asyncio.CancelledError:
                        pass

                mock_ws.send_json.assert_not_called()
                self.assertTrue(q.empty())

                # Case 2: When NOT actively streaming, notifications_evaluator_loop DOES broadcast telemetry
                with (
                    patch.object(main, "record_telemetry_sample", return_value=fake_sample),
                    patch.object(main, "evaluate_threshold_alerts", return_value=None),
                    patch.object(main, "evaluate_scheduled_reports", return_value=None),
                    patch.object(main, "evaluate_due_cron_jobs", return_value=[]),
                    patch.object(api, "is_live_telemetry_streaming", return_value=False),
                    patch("asyncio.sleep", side_effect=asyncio.CancelledError),
                ):
                    try:
                        await api.notifications_evaluator_loop()
                    except asyncio.CancelledError:
                        pass

                mock_ws.send_json.assert_called()
                sent_msg = mock_ws.send_json.call_args[0][0]
                self.assertEqual(sent_msg["type"], "telemetry")
                self.assertIn("data", sent_msg)
                self.assertFalse(q.empty())
                event, data = q.get_nowait()
                self.assertEqual(event, "telemetry")
            finally:
                api.sse_manager.unsubscribe(q)
                api.ws_manager.disconnect(mock_ws)

        asyncio.run(_test())

    # -------------------------------------------------------------------------
    # 5. Frontend Node VM SSE Stream Execution & In-Place DOM Patching Tests
    # -------------------------------------------------------------------------
    def test_frontend_node_vm_sse_stream_initialization_and_handlers(self):
        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id, tagName = 'DIV') {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  let _text = '';
  const el = {{
    id: id,
    tagName: tagName.toUpperCase(),
    style: {{}},
    dataset: {{}},
    children: children,
    addEventListener: () => {{}},
    appendChild: (child) => {{ children.push(child); child.parentElement = el; return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      child.parentElement = null;
      return child;
    }},
    remove: () => {{ if (el.parentElement) el.parentElement.removeChild(el); }},
    insertBefore: (child, ref) => {{
      const idx = children.indexOf(ref);
      if (idx !== -1) children.splice(idx, 0, child);
      else children.push(child);
      child.parentElement = el;
      return child;
    }},
    get firstChild() {{ return children[0] || null; }},
    querySelector: (sel) => findInTree(el, sel),
    querySelectorAll: (sel) => findAllInTree(el, sel),
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) {{ classes.delete(cls); return false; }}
          classes.add(cls); return true;
        }}
        if (force) classes.add(cls); else classes.delete(cls);
        return Boolean(force);
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    removeAttribute: (name) => {{ delete attributes[name]; }},
    value: '',
    disabled: false,
    parentElement: null,
    get textContent() {{
      if (children.length > 0) return children.map(c => c.textContent).join(' ');
      return _text;
    }},
    set textContent(val) {{
      _text = String(val);
      children.length = 0;
    }},
    get innerHTML() {{ return _text; }},
    set innerHTML(val) {{ _text = String(val); }}
  }};
  return el;
}}

function matchesSel(el, sel) {{
  if (!el) return false;
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) return el.classList && el.classList.contains(sel.slice(1));
  if (el.tagName && el.tagName.toLowerCase() === sel.toLowerCase()) return true;
  return false;
}}

function findInTree(root, sel) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) return ch;
    const found = findInTree(ch, sel);
    if (found) return found;
  }}
  return null;
}}

function findAllInTree(root, sel, out = []) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) out.push(ch);
    findAllInTree(ch, sel, out);
  }}
  return out;
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement('', tag),
  querySelector: (sel) => {{
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) return elements[k];
      const found = findInTree(elements[k], sel);
      if (found) return found;
    }}
    return null;
  }},
  querySelectorAll: (sel) => {{
    const res = [];
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) res.push(elements[k]);
      findAllInTree(elements[k], sel, res);
    }}
    return res;
  }},
  addEventListener: () => {{}}
}};

class MockEventSource {{
  constructor(url) {{
    this.url = url;
    this.listeners = {{}};
    MockEventSource.instances.push(this);
  }}
  addEventListener(evt, fn) {{
    if (!this.listeners[evt]) this.listeners[evt] = [];
    this.listeners[evt].push(fn);
  }}
  removeEventListener(evt, fn) {{
    if (this.listeners[evt]) {{
      this.listeners[evt] = this.listeners[evt].filter(f => f !== fn);
    }}
  }}
  close() {{
    this.closed = true;
  }}
  trigger(evt, data) {{
    if (evt === 'open' && this.onopen) this.onopen({{ type: 'open' }});
    if (evt === 'error' && this.onerror) this.onerror({{ type: 'error' }});
    const list = this.listeners[evt] || [];
    for (const fn of list) {{
      fn({{ type: evt, data: typeof data === 'string' ? data : JSON.stringify(data) }});
    }}
  }}
}}
MockEventSource.instances = [];

const appJs = fs.readFileSync({json.dumps(str(js_path))}, 'utf-8');

let intervalCount = 0;
let lastIntervalMs = 0;

const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    location: {{ protocol: 'http:', host: 'localhost:8000', hash: '', pathname: '/' }},
    addEventListener: () => {{}},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    setInterval: (fn, ms) => {{
      intervalCount++;
      lastIntervalMs = ms;
      return intervalCount;
    }},
    clearInterval: () => {{}},
    requestAnimationFrame: (cb) => cb(),
    EventSource: MockEventSource,
  }},
  localStorage: {{ getItem: () => 'sample_jwt_token', setItem: () => {{}}, removeItem: () => {{}} }},
  history: {{ replaceState: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: (fn, ms) => {{
    intervalCount++;
    lastIntervalMs = ms;
    return intervalCount;
  }},
  clearInterval: () => {{}},
  requestAnimationFrame: (cb) => cb(),
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  Headers: Headers,
  URLSearchParams: URLSearchParams,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }})
}});

vm.runInContext(appJs, ctx);

// Setup indicator DOM elements
const indicator = getEl('live-stream-indicator');
const streamText = getEl('live-stream-text');

// 1. Initially isAuthenticated = true & initialize SSE stream
vm.runInContext('isAuthenticated = true; initSSEStream();', ctx);

const instance = MockEventSource.instances[0];
if (!instance) throw new Error("MockEventSource not instantiated");
if (!instance.url.includes('/api/events')) throw new Error("URL missing /api/events: " + instance.url);
if (!instance.url.includes('token=sample_jwt_token')) throw new Error("URL missing token query: " + instance.url);

// Check event listeners registered
const reqEvents = ['snapshot', 'telemetry', 'docker_event', 'module_update', 'notification', 'ping'];
for (const ev of reqEvents) {{
  if (!instance.listeners[ev] || instance.listeners[ev].length === 0) {{
    throw new Error("Missing listener for event: " + ev);
  }}
}}

// 3. Trigger open event
instance.trigger('open');
if (!vm.runInContext('sseConnected', ctx)) throw new Error("Expected sseConnected to be true on open");
if (vm.runInContext('VigilLiveStore.getState("connection")', ctx) !== 'live') throw new Error("Expected live connection in VigilLiveStore");
if (!indicator.classList.contains('stream-active')) throw new Error("Expected stream-active on indicator");
if (streamText.textContent !== 'Live Stream') throw new Error("Expected 'Live Stream' text, got: " + streamText.textContent);
// Background polling completely eliminated when live
if (vm.runInContext('refreshTimerId', ctx) !== null) throw new Error("Expected refreshTimerId to be null when live stream connected, got: " + vm.runInContext('refreshTimerId', ctx));

// 4. Trigger error event
instance.trigger('error');
if (vm.runInContext('sseConnected', ctx)) throw new Error("Expected sseConnected to be false on error");
if (vm.runInContext('VigilLiveStore.getState("connection")', ctx) !== 'reconnecting') throw new Error("Expected reconnecting connection in VigilLiveStore");
if (!indicator.classList.contains('stream-reconnecting')) throw new Error("Expected stream-reconnecting on indicator");
if (!streamText.textContent.includes('Reconnecting')) throw new Error("Expected Reconnecting text, got: " + streamText.textContent);
if (lastIntervalMs !== 10000) throw new Error("Expected normal polling safety interval of 10000ms, got: " + lastIntervalMs);
if (vm.runInContext('refreshTimerId', ctx) === null) throw new Error("Expected refreshTimerId to be set on error for safety polling");

console.log("SUCCESS_INIT_AND_HANDLERS");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\nOutput: {res.stdout}")
        self.assertIn("SUCCESS_INIT_AND_HANDLERS", res.stdout)

    def test_frontend_node_vm_sse_fine_grained_telemetry_patching(self):
        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id, tagName = 'DIV') {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  let _text = '';
  const el = {{
    id: id,
    tagName: tagName.toUpperCase(),
    style: {{}},
    dataset: {{}},
    children: children,
    addEventListener: () => {{}},
    appendChild: (child) => {{ children.push(child); child.parentElement = el; return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      child.parentElement = null;
      return child;
    }},
    remove: () => {{ if (el.parentElement) el.parentElement.removeChild(el); }},
    insertBefore: (child, ref) => {{
      const idx = children.indexOf(ref);
      if (idx !== -1) children.splice(idx, 0, child);
      else children.push(child);
      child.parentElement = el;
      return child;
    }},
    get firstChild() {{ return children[0] || null; }},
    querySelector: (sel) => findInTree(el, sel),
    querySelectorAll: (sel) => findAllInTree(el, sel),
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) {{ classes.delete(cls); return false; }}
          classes.add(cls); return true;
        }}
        if (force) classes.add(cls); else classes.delete(cls);
        return Boolean(force);
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    removeAttribute: (name) => {{ delete attributes[name]; }},
    value: '',
    disabled: false,
    parentElement: null,
    get textContent() {{
      if (children.length > 0) return children.map(c => c.textContent).join(' ');
      return _text;
    }},
    set textContent(val) {{
      _text = String(val);
      children.length = 0;
    }},
    get innerHTML() {{ return _text; }},
    set innerHTML(val) {{ _text = String(val); }}
  }};
  return el;
}}

function matchesSel(el, sel) {{
  if (!el) return false;
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) return el.classList && el.classList.contains(sel.slice(1));
  if (el.tagName && el.tagName.toLowerCase() === sel.toLowerCase()) return true;
  return false;
}}

function findInTree(root, sel) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) return ch;
    const found = findInTree(ch, sel);
    if (found) return found;
  }}
  return null;
}}

function findAllInTree(root, sel, out = []) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) out.push(ch);
    findAllInTree(ch, sel, out);
  }}
  return out;
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement('', tag),
  querySelector: (sel) => {{
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) return elements[k];
      const found = findInTree(elements[k], sel);
      if (found) return found;
    }}
    return null;
  }},
  querySelectorAll: (sel) => {{
    const res = [];
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) res.push(elements[k]);
      findAllInTree(elements[k], sel, res);
    }}
    return res;
  }},
  addEventListener: () => {{}}
}};

class MockEventSource {{
  constructor(url) {{
    this.url = url;
    this.listeners = {{}};
    MockEventSource.instances.push(this);
  }}
  addEventListener(evt, fn) {{
    if (!this.listeners[evt]) this.listeners[evt] = [];
    this.listeners[evt].push(fn);
  }}
  removeEventListener() {{}}
  close() {{ this.closed = true; }}
  trigger(evt, data) {{
    if (evt === 'open' && this.onopen) this.onopen({{ type: 'open' }});
    const list = this.listeners[evt] || [];
    for (const fn of list) {{
      fn({{ type: evt, data: typeof data === 'string' ? data : JSON.stringify(data) }});
    }}
  }}
}}
MockEventSource.instances = [];

const appJs = fs.readFileSync({json.dumps(str(js_path))}, 'utf-8');

const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    location: {{ protocol: 'http:', host: 'localhost:8000', hash: '', pathname: '/' }},
    addEventListener: () => {{}},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    setInterval: () => 1,
    clearInterval: () => {{}},
    requestAnimationFrame: (cb) => cb(),
    EventSource: MockEventSource,
  }},
  localStorage: {{ getItem: () => 'token', setItem: () => {{}}, removeItem: () => {{}} }},
  history: {{ replaceState: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {{}},
  requestAnimationFrame: (cb) => cb(),
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  Headers: Headers,
  URLSearchParams: URLSearchParams,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }})
}});

vm.runInContext(appJs, ctx);

vm.runInContext('isAuthenticated = true; initSSEStream();', ctx);
const sse = MockEventSource.instances[0];
sse.trigger('open');

// Setup telemetry DOM elements
const cpuVal = getEl('cpu-percent-value');
const cpuBar = getEl('cpu-progress-bar');
const cpuCores = getEl('cpu-cores-badge');
const cpuLoad = getEl('cpu-load-detail');
const cpuTemp = getEl('cpu-temp-badge');

const memVal = getEl('memory-percent-value');
const memBar = getEl('memory-progress-bar');
const memDetail = getEl('memory-detail');
const memTotal = getEl('memory-total-badge');

const diskVal = getEl('disk-percent-value');
const diskBar = getEl('disk-progress-bar');
const diskDetail = getEl('disk-detail');

// Trigger telemetry event
sse.trigger('telemetry', {{
  cpu: {{ percent: 45.2, cores: [42.1, 48.3] }},
  memory: {{ percent: 62.4, used_gb: 10.0, total_gb: 16.0 }},
  disk: {{ percent: 78.9, used_gb: 394.5, total_gb: 500.0 }},
  load_avg: [1.25, 1.10, 0.95],
  thermal: 54
}});

// Validate in-place DOM patching
if (cpuVal.textContent !== '45.2%') throw new Error("cpuVal mismatch: " + cpuVal.textContent);
if (cpuBar.style.width !== '45.2%') throw new Error("cpuBar mismatch: " + cpuBar.style.width);
if (cpuCores.textContent !== '2 Cores') throw new Error("cpuCores mismatch: " + cpuCores.textContent);
if (!cpuLoad.textContent.includes('1.25, 1.10, 0.95')) throw new Error("cpuLoad mismatch: " + cpuLoad.textContent);
if (cpuTemp.textContent !== '54°C') throw new Error("cpuTemp mismatch: " + cpuTemp.textContent);

if (memVal.textContent !== '62.4%') throw new Error("memVal mismatch: " + memVal.textContent);
if (memBar.style.width !== '62.4%') throw new Error("memBar mismatch: " + memBar.style.width);
if (!memDetail.textContent.includes('10.0 GB') || !memDetail.textContent.includes('16.0 GB')) {{
  throw new Error("memDetail mismatch: " + memDetail.textContent);
}}

if (diskVal.textContent !== '78.9%') throw new Error("diskVal mismatch: " + diskVal.textContent);
if (diskBar.style.width !== '78.9%') throw new Error("diskBar mismatch: " + diskBar.style.width);
if (!diskDetail.textContent.includes('394.5 GB') || !diskDetail.textContent.includes('500.0 GB')) {{
  throw new Error("diskDetail mismatch: " + diskDetail.textContent);
}}

// Validate VigilLiveStore and telemetry rolling buffers
const storeTelem = vm.runInContext('VigilLiveStore.getState("telemetry")', ctx);
if (!storeTelem || storeTelem.cpu.percent !== 45.2) {{
  throw new Error("VigilLiveStore telemetry state mismatch");
}}

const coreBuffers = vm.runInContext('cpuCoresHistoryBuffers', ctx);
if (!Array.isArray(coreBuffers) || coreBuffers.length !== 2) {{
  throw new Error("Expected 2 core buffers, got: " + JSON.stringify(coreBuffers));
}}
if (coreBuffers[0][coreBuffers[0].length - 1] !== 42.1 || coreBuffers[1][coreBuffers[1].length - 1] !== 48.3) {{
  throw new Error("Core buffer values mismatch: " + JSON.stringify(coreBuffers));
}}

const memBuffer = vm.runInContext('memoryHistoryBuffer', ctx);
if (!Array.isArray(memBuffer) || memBuffer[memBuffer.length - 1] !== 62.4) {{
  throw new Error("Memory history buffer mismatch: " + JSON.stringify(memBuffer));
}}

console.log("SUCCESS_TELEMETRY_PATCHING");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\nOutput: {res.stdout}")
        self.assertIn("SUCCESS_TELEMETRY_PATCHING", res.stdout)

    def test_frontend_node_vm_sse_docker_event_and_notification_patching(self):
        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id, tagName = 'DIV') {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  let _text = '';
  const el = {{
    id: id,
    tagName: tagName.toUpperCase(),
    style: {{}},
    dataset: {{}},
    children: children,
    addEventListener: () => {{}},
    appendChild: (child) => {{ children.push(child); child.parentElement = el; return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      child.parentElement = null;
      return child;
    }},
    remove: () => {{ if (el.parentElement) el.parentElement.removeChild(el); }},
    insertBefore: (child, ref) => {{
      const idx = children.indexOf(ref);
      if (idx !== -1) children.splice(idx, 0, child);
      else children.push(child);
      child.parentElement = el;
      return child;
    }},
    get firstChild() {{ return children[0] || null; }},
    querySelector: (sel) => findInTree(el, sel),
    querySelectorAll: (sel) => findAllInTree(el, sel),
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) {{ classes.delete(cls); return false; }}
          classes.add(cls); return true;
        }}
        if (force) classes.add(cls); else classes.delete(cls);
        return Boolean(force);
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    removeAttribute: (name) => {{ delete attributes[name]; }},
    value: '',
    disabled: false,
    parentElement: null,
    get textContent() {{
      if (children.length > 0) return children.map(c => c.textContent).join(' ');
      return _text;
    }},
    set textContent(val) {{
      _text = String(val);
      children.length = 0;
    }},
    get innerHTML() {{ return _text; }},
    set innerHTML(val) {{ _text = String(val); }}
  }};
  return el;
}}

function matchesSel(el, sel) {{
  if (!el) return false;
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) return el.classList && el.classList.contains(sel.slice(1));
  if (el.tagName && el.tagName.toLowerCase() === sel.toLowerCase()) return true;
  return false;
}}

function findInTree(root, sel) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) return ch;
    const found = findInTree(ch, sel);
    if (found) return found;
  }}
  return null;
}}

function findAllInTree(root, sel, out = []) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) out.push(ch);
    findAllInTree(ch, sel, out);
  }}
  return out;
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement('', tag),
  querySelector: (sel) => {{
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) return elements[k];
      const found = findInTree(elements[k], sel);
      if (found) return found;
    }}
    return null;
  }},
  querySelectorAll: (sel) => {{
    const res = [];
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) res.push(elements[k]);
      findAllInTree(elements[k], sel, res);
    }}
    return res;
  }},
  addEventListener: () => {{}}
}};

class MockEventSource {{
  constructor(url) {{
    this.url = url;
    this.listeners = {{}};
    MockEventSource.instances.push(this);
  }}
  addEventListener(evt, fn) {{
    if (!this.listeners[evt]) this.listeners[evt] = [];
    this.listeners[evt].push(fn);
  }}
  removeEventListener() {{}}
  close() {{ this.closed = true; }}
  trigger(evt, data) {{
    if (evt === 'open' && this.onopen) this.onopen({{ type: 'open' }});
    const list = this.listeners[evt] || [];
    for (const fn of list) {{
      fn({{ type: evt, data: typeof data === 'string' ? data : JSON.stringify(data) }});
    }}
  }}
}}
MockEventSource.instances = [];

const appJs = fs.readFileSync({json.dumps(str(js_path))}, 'utf-8');

let toasts = [];
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    location: {{ protocol: 'http:', host: 'localhost:8000', hash: '', pathname: '/' }},
    addEventListener: () => {{}},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    setInterval: () => 1,
    clearInterval: () => {{}},
    requestAnimationFrame: (cb) => cb(),
    EventSource: MockEventSource,
  }},
  showToast: (t) => toasts.push(t),
  localStorage: {{ getItem: () => 'token', setItem: () => {{}}, removeItem: () => {{}} }},
  history: {{ replaceState: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {{}},
  requestAnimationFrame: (cb) => cb(),
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  Headers: Headers,
  URLSearchParams: URLSearchParams,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }})
}});

vm.runInContext(appJs, ctx);

vm.runInContext('isAuthenticated = true; initSSEStream();', ctx);
const sse = MockEventSource.instances[0];
sse.trigger('open');

// 1. Setup Docker Containers Table and Toasts
const toastContainer = getEl('toast-container');
const dockerTbody = getEl('docker-containers-tbody');
const containerRow = mockDoc.createElement('tr');
const tdName = mockDoc.createElement('td');
tdName.textContent = 'nginx-web';
const tdId = mockDoc.createElement('td');
tdId.textContent = 'c12345678901';
const tdStatus = mockDoc.createElement('td');
const badge = mockDoc.createElement('span');
badge.classList.add('status-badge', 'status-clean');
badge.textContent = 'running';
tdStatus.appendChild(badge);

containerRow.appendChild(tdName);
containerRow.appendChild(tdId);
containerRow.appendChild(tdStatus);
dockerTbody.appendChild(containerRow);

// Trigger docker_event: container died
sse.trigger('docker_event', {{
  type: 'docker_event',
  event: {{
    action: 'die',
    id: 'c12345678901',
    name: 'nginx-web'
  }}
}});

if (!badge.classList.contains('status-failed')) {{
  throw new Error("Expected status-failed on container badge, got: " + badge.className);
}}
if (toastContainer.children.length === 0 || !toastContainer.children[0].className.includes('toast-danger')) {{
  throw new Error("Expected danger toast for stopped container");
}}

// 2. Setup Notification History Table
const notifTbody = getEl('notification-history-tbody');
const emptyTr = mockDoc.createElement('tr');
const emptyTd = mockDoc.createElement('td');
emptyTd.classList.add('empty-state');
emptyTd.textContent = 'Loading...';
emptyTr.appendChild(emptyTd);
notifTbody.appendChild(emptyTr);

const drawerBadge = getEl('notif-drawer-badge');
drawerBadge.textContent = '0';

// Trigger notification event
sse.trigger('notification', {{
  trigger: 'alert_disk',
  title: 'Disk Warning: /dev/sda1 > 90%',
  channel: 'Discord',
  success: true
}});

if (notifTbody.children.length !== 1) {{
  throw new Error("Expected 1 row in notification history, got: " + notifTbody.children.length);
}}
const firstRowText = notifTbody.children[0].textContent;
if (!firstRowText.includes('ALERT_DISK') || !firstRowText.includes('Disk Warning')) {{
  throw new Error("Notification row missing alert content: " + firstRowText);
}}
if (drawerBadge.textContent !== '1') {{
  throw new Error("Expected drawerBadge to be 1, got: " + drawerBadge.textContent);
}}

// 3. Trigger ping event
sse.trigger('ping', {{}});

console.log("SUCCESS_DOCKER_AND_NOTIFICATION");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\nOutput: {res.stdout}")
        self.assertIn("SUCCESS_DOCKER_AND_NOTIFICATION", res.stdout)

    def test_frontend_node_vm_vigil_live_store_pubsub_and_graph_rolling(self):
        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id, tagName = 'DIV') {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  let _text = '';
  const el = {{
    id: id,
    tagName: tagName.toUpperCase(),
    style: {{}},
    dataset: {{}},
    children: children,
    addEventListener: () => {{}},
    appendChild: (child) => {{ children.push(child); child.parentElement = el; return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      child.parentElement = null;
      return child;
    }},
    remove: () => {{ if (el.parentElement) el.parentElement.removeChild(el); }},
    insertBefore: (child, ref) => {{
      const idx = children.indexOf(ref);
      if (idx !== -1) children.splice(idx, 0, child);
      else children.push(child);
      child.parentElement = el;
      return child;
    }},
    get firstChild() {{ return children[0] || null; }},
    querySelector: (sel) => findInTree(el, sel),
    querySelectorAll: (sel) => findAllInTree(el, sel),
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force === undefined) {{
          if (classes.has(cls)) {{ classes.delete(cls); return false; }}
          classes.add(cls); return true;
        }}
        if (force) classes.add(cls); else classes.delete(cls);
        return Boolean(force);
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{
      classes.clear();
      (val || '').split(/\\s+/).filter(Boolean).forEach(c => classes.add(c));
    }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    removeAttribute: (name) => {{ delete attributes[name]; }},
    value: '',
    disabled: false,
    parentElement: null,
    get textContent() {{
      if (children.length > 0) return children.map(c => c.textContent).join(' ');
      return _text;
    }},
    set textContent(val) {{
      _text = String(val);
      children.length = 0;
    }},
    get innerHTML() {{ return _text; }},
    set innerHTML(val) {{ _text = String(val); }}
  }};
  return el;
}}

function matchesSel(el, sel) {{
  if (!el) return false;
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) return el.classList && el.classList.contains(sel.slice(1));
  if (el.tagName && el.tagName.toLowerCase() === sel.toLowerCase()) return true;
  return false;
}}

function findInTree(root, sel) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) return ch;
    const found = findInTree(ch, sel);
    if (found) return found;
  }}
  return null;
}}

function findAllInTree(root, sel, out = []) {{
  for (const ch of root.children) {{
    if (matchesSel(ch, sel)) out.push(ch);
    findAllInTree(ch, sel, out);
  }}
  return out;
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) {{
    elements[id] = createMockElement(id);
  }}
  return elements[id];
}}

const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement('', tag),
  querySelector: (sel) => {{
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) return elements[k];
      const found = findInTree(elements[k], sel);
      if (found) return found;
    }}
    return null;
  }},
  querySelectorAll: (sel) => {{
    const res = [];
    for (const k of Object.keys(elements)) {{
      if (matchesSel(elements[k], sel)) res.push(elements[k]);
      findAllInTree(elements[k], sel, res);
    }}
    return res;
  }},
  addEventListener: () => {{}}
}};

class MockEventSource {{
  constructor(url) {{
    this.url = url;
    this.listeners = {{}};
    MockEventSource.instances.push(this);
  }}
  addEventListener(evt, fn) {{
    if (!this.listeners[evt]) this.listeners[evt] = [];
    this.listeners[evt].push(fn);
  }}
  removeEventListener(evt, fn) {{
    if (this.listeners[evt]) {{
      this.listeners[evt] = this.listeners[evt].filter(f => f !== fn);
    }}
  }}
  close() {{
    this.closed = true;
  }}
  trigger(evt, data) {{
    if (evt === 'open' && this.onopen) this.onopen({{ type: 'open' }});
    if (evt === 'error' && this.onerror) this.onerror({{ type: 'error' }});
    const list = this.listeners[evt] || [];
    for (const fn of list) {{
      fn({{ type: evt, data: typeof data === 'string' ? data : JSON.stringify(data) }});
    }}
  }}
}}
MockEventSource.instances = [];

let fetchCalls = 0;
const appJs = fs.readFileSync({json.dumps(str(js_path))}, 'utf-8');

const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    location: {{ protocol: 'http:', host: 'localhost:8000', hash: '', pathname: '/' }},
    addEventListener: () => {{}},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    setInterval: () => 1,
    clearInterval: () => {{}},
    requestAnimationFrame: (cb) => cb(),
    EventSource: MockEventSource,
  }},
  localStorage: {{ getItem: () => 'token', setItem: () => {{}}, removeItem: () => {{}} }},
  history: {{ replaceState: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {{}},
  requestAnimationFrame: (cb) => cb(),
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  Headers: Headers,
  URLSearchParams: URLSearchParams,
  fetch: async (url) => {{
    fetchCalls++;
    return {{
      ok: true,
      status: 200,
      json: async () => ({{ raid: {{ status: 'clean' }}, system: {{ cpu_percent: 10 }} }})
    }};
  }}
}});

vm.runInContext(appJs, ctx);

(async () => {{
  // 1. Verify VigilLiveStore global interface
  const store = vm.runInContext('VigilLiveStore', ctx);
  if (!store || typeof store.subscribe !== 'function' || typeof store.getState !== 'function' || typeof store.dispatch !== 'function') {{
    throw new Error("VigilLiveStore interface is incomplete");
  }}

  if (store.getState('connection') !== 'disconnected') {{
    throw new Error("Initial connection state should be disconnected, got: " + store.getState('connection'));
  }}

  // 2. Test Pub/Sub & Replay & Wildcard
  let directReceived = null;
  const unsub = store.subscribe('custom_slice', (data) => {{
    directReceived = data;
  }});

  let wildcardReceived = null;
  store.subscribe('*', (slice, data) => {{
    wildcardReceived = {{ slice, data }};
  }});

  store.dispatch('custom_slice', {{ count: 42 }});
  if (!directReceived || directReceived.count !== 42) {{
    throw new Error("Direct subscriber failed to receive dispatched value");
  }}
  if (!wildcardReceived || wildcardReceived.slice !== 'custom_slice' || wildcardReceived.data.count !== 42) {{
    throw new Error("Wildcard subscriber failed to receive dispatched value");
  }}

  // Immediate replay for new subscriber
  let replayReceived = null;
  store.subscribe('custom_slice', (data) => {{
    replayReceived = data;
  }});
  if (!replayReceived || replayReceived.count !== 42) {{
    throw new Error("Immediate replay failed on existing state");
  }}

  // Unsubscribe test
  unsub();
  directReceived = null;
  store.dispatch('custom_slice', {{ count: 99 }});
  if (directReceived !== null) {{
    throw new Error("Unsubscribed listener still received event");
  }}

  // 3. Test Continuous Telemetry Waveform Rolling
  store.dispatch('telemetry', {{
    cpu: {{ percent: 20.0, cores: [10.0, 30.0] }},
    memory: {{ percent: 40.0, used: 4000, total: 10000 }},
    disk: {{ percent: 50.0 }}
  }});
  store.dispatch('telemetry', {{
    cpu: {{ percent: 25.0, cores: [15.0, 35.0] }},
    memory: {{ percent: 45.0, used: 4500, total: 10000 }},
    disk: {{ percent: 50.0 }}
  }});
  store.dispatch('telemetry', {{
    cpu: {{ percent: 30.0, cores: [20.0, 40.0] }},
    memory: {{ percent: 50.0, used: 5000, total: 10000 }},
    disk: {{ percent: 50.0 }}
  }});

  const coreBufs = vm.runInContext('cpuCoresHistoryBuffers', ctx);
  if (coreBufs.length !== 2) throw new Error("Expected 2 core buffers, got: " + coreBufs.length);
  if (coreBufs[0].slice(-3).join(',') !== '10,15,20') {{
    throw new Error("Core 0 buffer waveform mismatch: " + coreBufs[0]);
  }}
  if (coreBufs[1].slice(-3).join(',') !== '30,35,40') {{
    throw new Error("Core 1 buffer waveform mismatch: " + coreBufs[1]);
  }}

  const memBuf = vm.runInContext('memoryHistoryBuffer', ctx);
  if (memBuf.slice(-3).join(',') !== '40,45,50') {{
    throw new Error("Memory history buffer waveform mismatch: " + memBuf);
  }}

  // 4. Test Background Polling Elimination when Connection is Live
  store.dispatch('connection', 'live');
  const initialFetchCount = fetchCalls;
  await vm.runInContext('isAuthenticated = true; refreshDashboard({{ manual: false }});', ctx);
  if (fetchCalls !== initialFetchCount) {{
    throw new Error("Background polling refreshDashboard executed fetch even though live stream connected!");
  }}

  // But manual refresh STILL works:
  await vm.runInContext('refreshDashboard({{ manual: true }});', ctx);
  if (fetchCalls <= initialFetchCount) {{
    throw new Error("Manual refresh failed to execute fetch");
  }}

  // 5. Test Instant Tab Switching (Zero Spinners)
  store.dispatch('docker', {{
    containers: [
      {{ id: 'cont1', name: 'app_prod', status: 'running', state: 'running' }}
    ],
    stacks: []
  }});
  store.dispatch('raid', {{
    status: 'clean',
    level: 'raid1'
  }});

  vm.runInContext('switchViewTab("containers", false);', ctx);
  const contTbody = getEl('docker-containers-tbody');
  if (!contTbody || !contTbody.innerHTML.includes('app_prod')) {{
    throw new Error("Containers view was not instantly rendered from VigilLiveStore cache");
  }}

  vm.runInContext('switchViewTab("storage", false);', ctx);
  const raidStatusText = getEl('raid-status-text') || getEl('raid-status-badge');
  if (!raidStatusText || !raidStatusText.textContent.toUpperCase().includes('CLEAN')) {{
    throw new Error("Storage view was not instantly rendered from VigilLiveStore cache: " + raidStatusText.textContent);
  }}

  console.log("SUCCESS_VIGIL_LIVE_STORE_AND_GRAPH_ROLLING");
}})().catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\nOutput: {res.stdout}")
        self.assertIn("SUCCESS_VIGIL_LIVE_STORE_AND_GRAPH_ROLLING", res.stdout)

    def test_app_js_syntax_validity(self):
        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"
        res = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"node --check failed: {res.stderr}")

