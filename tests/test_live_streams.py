import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import api
import main
from test_main import asgi_delete, asgi_post, asgi_put, asgi_ws


class TestLiveStreamsBackend(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "live_streams_test.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("streamadmin", "streampass123", email="stream@test.local")
        self.session_token = main.create_session("streamadmin")
        self.auth_cookies = {"session_token": self.session_token}
        self.auth_headers = {"Authorization": f"Bearer {self.session_token}"}
        api.ws_manager.active_connections.clear()

    def tearDown(self):
        api.ws_manager.active_connections.clear()
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    # =========================================================================
    # 1. Fast-Path Sub-Second Metrics & Caching
    # =========================================================================
    def test_get_system_metrics_data_includes_timestamp_float(self):
        before = time.time()
        with (
            patch.object(main, "get_storage_metrics", return_value=[]),
            patch.object(main, "get_services_summary", return_value={"running": 1, "failed": 0, "total": 1}),
            patch.object(main, "get_network_metrics", return_value={"total_bytes_sent": 100, "total_bytes_recv": 200, "interfaces": []}),
            patch.object(main, "get_all_disks_smart_metrics", return_value=[]),
            patch.object(main, "get_hardware_thermal_metrics", return_value={"cpu_package_c": 45.0, "cores": [], "sensors": [], "fans": []}),
        ):
            data = main.get_system_metrics_data()
        after = time.time()

        self.assertIn("timestamp", data)
        self.assertIsInstance(data["timestamp"], float)
        self.assertGreaterEqual(data["timestamp"], before - 1.0)
        self.assertLessEqual(data["timestamp"], after + 1.0)

    def test_get_system_services_cache_and_invalidation_on_execute_service_action(self):
        orig_cache = dict(main._SERVICES_CACHE)
        try:
            cached_payload = {
                "services": [
                    {
                        "unit": "nginx.service",
                        "name": "nginx",
                        "load_state": "loaded",
                        "active_state": "active",
                        "sub_state": "running",
                        "description": "Cached Nginx Server",
                    }
                ],
                "summary": {"total": 1, "running": 1, "failed": 0, "inactive": 0},
                "failed_services": [],
                "systemctl_available": True,
            }
            main._SERVICES_CACHE["ts"] = time.time()
            main._SERVICES_CACHE["data"] = cached_payload

            # Unmocked call within TTL should return cached payload without invoking subprocess
            result = main.get_system_services(force_refresh=False)
            self.assertEqual(result["services"][0]["description"], "Cached Nginx Server")
            self.assertIsNot(result, cached_payload)  # Returned as a deep copy

            # Executing a service action must invalidate _SERVICES_CACHE
            mock_proc = MagicMock(returncode=0, stdout="restarted", stderr="")
            with (
                patch("shutil.which", return_value="/bin/systemctl"),
                patch("subprocess.run", return_value=mock_proc),
            ):
                act_res = main.execute_service_action("nginx.service", "restart")
                self.assertEqual(act_res["status"], "ok")
                self.assertEqual(act_res["service"], "nginx.service")
                self.assertEqual(act_res["action"], "restart")

            self.assertEqual(main._SERVICES_CACHE["ts"], 0.0)
            self.assertIsNone(main._SERVICES_CACHE["data"])
        finally:
            main._SERVICES_CACHE.clear()
            main._SERVICES_CACHE.update(orig_cache)

    # =========================================================================
    # 2. WebSocket View Multiplexing & Log Streaming (/api/ws)
    # =========================================================================
    def test_websocket_view_multiplexing_and_log_streaming(self):
        fake_snapshot = {
            "type": "snapshot",
            "raid": {"status": "clean"},
            "system": {"timestamp": time.time(), "cpu_percent": 12.5},
        }
        fake_processes = {
            "processes": [{"pid": 101, "name": "python3", "cpu_percent": 5.5, "memory_percent": 1.2}],
            "summary": {"total_processes": 1, "running_count": 1, "sleeping_count": 0},
        }
        fake_services = {
            "services": [{"unit": "ssh.service", "name": "ssh", "active_state": "active", "sub_state": "running"}],
            "summary": {"total": 1, "running": 1, "failed": 0, "inactive": 0},
        }
        fake_security = {
            "ports": [{"proto": "tcp", "ip": "0.0.0.0", "port": 22, "is_public": True}],
            "summary": {"total": 1, "public": 1, "local": 0},
            "firewall": {"available": True, "enabled": True, "rules": []},
            "ssh_security": {"active_sessions": [], "banned_ips": [], "top_offenders": []},
        }
        fake_backups = {
            "backup_dir": "/var/backups",
            "targets": [],
            "archives": [],
            "summary": {"total_archives": 0, "total_targets": 0},
        }
        fake_cron_jobs = {"jobs": [{"id": 1, "name": "Nightly Backup", "cron_expression": "0 2 * * *", "enabled": True}]}
        fake_cron_runs = [{"id": 10, "job_id": 1, "job_name": "Nightly Backup", "status": "success"}]
        fake_probes = [{"id": 1, "name": "Gateway", "type": "ping", "target": "1.1.1.1", "last_status": "up"}]
        fake_audit = ([{"id": 1, "event_type": "auth.login.success", "actor": "streamadmin"}], 1)
        fake_users = {"users": [{"username": "streamadmin", "uid": 1000, "is_system": False}], "summary": {"total_users": 1}}
        fake_groups = {"groups": [{"name": "sudo", "gid": 27, "members": ["streamadmin"]}], "summary": {"total_groups": 1}}
        fake_docker_status = {"installed": True, "running": True, "containers_running": 2}
        fake_containers = [{"id": "c123", "name": "web-1", "image": "nginx:alpine", "state": "running", "status": "Up 2 hours"}]
        fake_stacks = [{"name": "media", "status": "running", "running_count": 2, "containers_count": 2, "managed": True}]

        client_msgs = [
            {"action": "subscribe", "view": "processes"},
            {"action": "subscribe", "view": "services"},
            {"action": "subscribe", "view": "security"},
            {"action": "subscribe", "view": "backups"},
            {"action": "subscribe", "view": "probes"},
            {"action": "subscribe", "view": "audit"},
            {"action": "subscribe", "view": "users"},
            {"action": "subscribe", "view": "containers", "subtab": "stacks"},
            {"action": "subscribe_logs", "container_id": "c123"},
        ]

        with (
            patch.object(api, "_build_full_snapshot", return_value=fake_snapshot),
            patch.object(main, "get_process_list", return_value=fake_processes),
            patch.object(main, "get_system_services", return_value=fake_services),
            patch.object(main, "get_network_security_overview", return_value=fake_security),
            patch.object(main, "get_backups_overview", return_value=fake_backups),
            patch.object(main, "list_cron_jobs", return_value=fake_cron_jobs),
            patch.object(main, "list_cron_job_runs", return_value=fake_cron_runs),
            patch.object(main, "list_probes", return_value=fake_probes, create=True),
            patch.object(main, "get_audit_logs", return_value=fake_audit),
            patch.object(main, "get_system_users", return_value=fake_users),
            patch.object(main, "get_system_groups", return_value=fake_groups),
            patch.object(main.docker_mgr, "get_status", return_value=fake_docker_status),
            patch.object(main.docker_mgr, "list_containers", return_value=fake_containers),
            patch.object(main.docker_mgr, "list_stacks", return_value=fake_stacks),
            patch.object(main.docker_mgr, "get_container_logs", return_value="2026-11-06T12:00:00Z Server listening on 80\n"),
        ):
            ws_res = asgi_ws(
                api.app,
                f"/api/ws?token={self.session_token}",
                client_messages=client_msgs,
            )

        self.assertTrue(ws_res.accepted)
        # First message is snapshot, followed by 8 module_updates and 1 container_logs
        self.assertGreaterEqual(len(ws_res.messages), 10)
        self.assertEqual(ws_res.messages[0]["type"], "snapshot")

        by_module = {}
        container_log_msgs = []
        for m in ws_res.messages[1:]:
            if m.get("type") == "module_update":
                by_module[m.get("module")] = m
            elif m.get("type") == "container_logs":
                container_log_msgs.append(m)

        # Verify each subscribed view returned the expected module_update payload
        self.assertIn("processes", by_module)
        self.assertEqual(by_module["processes"]["processes"], fake_processes)

        self.assertIn("services", by_module)
        self.assertEqual(by_module["services"]["services"], fake_services)

        self.assertIn("security", by_module)
        self.assertEqual(by_module["security"]["security"], fake_security)

        self.assertIn("backups", by_module)
        self.assertEqual(by_module["backups"]["backups"], fake_backups)
        self.assertEqual(by_module["backups"]["cron_jobs"], fake_cron_jobs)
        self.assertEqual(by_module["backups"]["cron_runs"], fake_cron_runs)

        self.assertIn("probes", by_module)
        self.assertEqual(by_module["probes"]["probes"], fake_probes)

        self.assertIn("audit", by_module)
        self.assertEqual(by_module["audit"]["logs"], fake_audit[0])
        self.assertEqual(by_module["audit"]["total"], 1)

        self.assertIn("users", by_module)
        self.assertEqual(by_module["users"]["users"], fake_users)
        self.assertEqual(by_module["users"]["groups"], fake_groups)

        self.assertIn("docker", by_module)
        self.assertEqual(by_module["docker"]["docker"], fake_docker_status)
        self.assertEqual(by_module["docker"]["containers"], fake_containers)
        self.assertEqual(by_module["docker"]["stacks"], fake_stacks)

        # Verify container_logs streaming response
        self.assertEqual(len(container_log_msgs), 1)
        self.assertEqual(container_log_msgs[0]["type"], "container_logs")
        self.assertEqual(container_log_msgs[0]["container_id"], "c123")
        self.assertIn("Server listening on 80", container_log_msgs[0]["logs"])

    # =========================================================================
    # 3. Instant Event-Driven Broadcasts (_broadcast_module)
    # =========================================================================
    def test_broadcast_module_sends_to_active_websocket_connections_on_mutations(self):
        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()
        api.ws_manager.active_connections.append(mock_ws)

        # 3a. Service action triggers _broadcast_module("services")
        with (
            patch.object(
                main,
                "execute_service_action",
                return_value={"status": "ok", "service": "ssh.service", "action": "restart", "output": ""},
            ),
            patch.object(
                main,
                "get_system_services",
                return_value={"services": [{"unit": "ssh.service", "active_state": "active"}], "summary": {"total": 1, "running": 1, "failed": 0, "inactive": 0}},
            ),
        ):
            resp = asgi_post(
                api.app,
                "/api/services/ssh.service/action",
                json_body={"action": "restart"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)

        self.assertTrue(mock_ws.send_json.called)
        sent_payload = mock_ws.send_json.call_args[0][0]
        self.assertEqual(sent_payload["type"], "module_update")
        self.assertEqual(sent_payload["module"], "services")

        # 3b. Cron job mutation triggers _broadcast_module("backups")
        mock_ws.send_json.reset_mock()
        with (
            patch.object(
                main,
                "create_or_update_cron_job",
                return_value={"id": 1, "name": "Daily Prune", "cron_expression": "0 3 * * *", "enabled": True},
            ),
            patch.object(main, "get_backups_overview", return_value={"archives": [], "targets": []}),
            patch.object(main, "list_cron_jobs", return_value={"jobs": [{"id": 1, "name": "Daily Prune"}]}),
            patch.object(main, "list_cron_job_runs", return_value=[]),
        ):
            resp = asgi_post(
                api.app,
                "/api/cron/jobs",
                json_body={
                    "name": "Daily Prune",
                    "task_type": "docker_prune",
                    "cron_expression": "0 3 * * *",
                    "task_config": {},
                },
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)

        self.assertTrue(mock_ws.send_json.called)
        sent_payload = mock_ws.send_json.call_args[0][0]
        self.assertEqual(sent_payload["type"], "module_update")
        self.assertEqual(sent_payload["module"], "backups")

        # 3c. Probe mutation triggers _broadcast_module("probes")
        mock_ws.send_json.reset_mock()
        with (
            patch.object(
                main,
                "create_sentinel_probe",
                return_value={"id": 5, "name": "DNS Check", "type": "ping", "target": "8.8.8.8"},
            ),
            patch.object(main, "execute_probe", new=AsyncMock(return_value={"status": "up", "response_time_ms": 8.2})),
            patch.object(main, "record_probe_check"),
            patch.object(
                main,
                "get_sentinel_probe_enriched",
                return_value={"id": 5, "name": "DNS Check", "type": "ping", "target": "8.8.8.8", "last_status": "up"},
            ),
            patch.object(main, "list_probes", return_value=[{"id": 5, "name": "DNS Check"}], create=True),
        ):
            resp = asgi_post(
                api.app,
                "/api/probes",
                json_body={"name": "DNS Check", "type": "ping", "target": "8.8.8.8"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)

        self.assertTrue(mock_ws.send_json.called)
        sent_payload = mock_ws.send_json.call_args[0][0]
        self.assertEqual(sent_payload["type"], "module_update")
        self.assertEqual(sent_payload["module"], "probes")

        # 3d. Docker container and stack actions trigger _broadcast_module("docker_full")
        mock_ws.send_json.reset_mock()
        with (
            patch.object(main.docker_manager, "execute_container_action", return_value={"success": True, "action": "restart"}),
            patch.object(main.docker_mgr, "get_status", return_value={"installed": True, "running": True}),
            patch.object(main.docker_mgr, "list_containers", return_value=[{"id": "c123", "name": "web"}]),
            patch.object(main.docker_mgr, "list_stacks", return_value=[{"name": "stack1"}]),
        ):
            resp = asgi_post(
                api.app,
                "/api/docker/containers/c123/action",
                json_body={"action": "restart"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)

        self.assertTrue(mock_ws.send_json.called)
        sent_payload = mock_ws.send_json.call_args[0][0]
        self.assertEqual(sent_payload["type"], "module_update")
        self.assertEqual(sent_payload["module"], "docker")
        self.assertIn("containers", sent_payload)
        self.assertIn("stacks", sent_payload)

        mock_ws.send_json.reset_mock()
        with (
            patch.object(main.docker_manager, "execute_stack_action", return_value={"success": True, "action": "restart"}),
            patch.object(main.docker_mgr, "get_status", return_value={"installed": True, "running": True}),
            patch.object(main.docker_mgr, "list_containers", return_value=[{"id": "c123", "name": "web"}]),
            patch.object(main.docker_mgr, "list_stacks", return_value=[{"name": "stack1"}]),
        ):
            resp = asgi_post(
                api.app,
                "/api/docker/stacks/stack1/action",
                json_body={"action": "restart"},
                cookies=self.auth_cookies,
            )
            self.assertEqual(resp.status_code, 200)

        self.assertTrue(mock_ws.send_json.called)
        sent_payload = mock_ws.send_json.call_args[0][0]
        self.assertEqual(sent_payload["type"], "module_update")
        self.assertEqual(sent_payload["module"], "docker")


class TestLiveStreamsZeroClutterAndNodeVM(unittest.TestCase):
    # =========================================================================
    # 4. Zero-Clutter CSS & DOM Contracts
    # =========================================================================
    def test_zero_clutter_css_hides_manual_refresh_and_interval_selectors(self):
        root = Path(__file__).resolve().parent.parent
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        required_hidden_selectors = [
            "#refresh-btn",
            ".btn-module-refresh",
            "#docker-interval-select",
            "#notifications-interval-select",
            "#backups-refresh-btn",
            "#btn-refresh-packages",
            "#live-stream-indicator",
            "#auto-refresh-indicator",
        ]

        # Extract the Zero-Clutter rule block at the end of styles.css
        zero_clutter_idx = css.find("Zero-Clutter Live Stream UI")
        self.assertNotEqual(zero_clutter_idx, -1, "Missing Zero-Clutter Live Stream UI CSS block in static/styles.css")
        zero_clutter_section = css[zero_clutter_idx:]

        for selector in required_hidden_selectors:
            self.assertIn(
                selector,
                zero_clutter_section,
                f"Expected selector '{selector}' in Zero-Clutter CSS block",
            )
        self.assertIn("display: none !important;", zero_clutter_section)

    # =========================================================================
    # 5. Frontend Node VM Live Stream Execution
    # =========================================================================
    def test_frontend_node_vm_live_streams_and_dropdown_preservation(self):
        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  let openDropdownPresent = false;
  return {{
    id: id,
    style: {{}},
    dataset: {{}},
    children: children,
    scrollHeight: 200,
    clientHeight: 200,
    scrollTop: 0,
    setOpenDropdown: (val) => {{ openDropdownPresent = Boolean(val); }},
    querySelector: (sel) => {{
      if (sel === '.actions-dropdown-menu.show' && openDropdownPresent) {{
        return {{ classList: {{ contains: () => true }} }};
      }}
      return null;
    }},
    querySelectorAll: () => [],
    appendChild: (child) => {{ children.push(child); return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      return child;
    }},
    addEventListener: () => {{}},
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
    innerHTML: '',
    textContent: ''
  }};
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
  createElement: (tag) => createMockElement(tag),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

class MockWebSocket {{
  constructor(url) {{
    this.url = url;
    this.readyState = MockWebSocket.OPEN;
    this.sent = [];
  }}
  send(data) {{
    this.sent.push(JSON.parse(data));
  }}
  close() {{
    this.readyState = MockWebSocket.CLOSED;
  }}
}}
MockWebSocket.OPEN = 1;
MockWebSocket.CLOSED = 3;

const appJs = fs.readFileSync({json.dumps(str(js_path))}, 'utf-8');

const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    WebSocket: MockWebSocket,
    location: {{ protocol: 'http:', host: 'localhost:8000', hash: '', pathname: '/' }},
    addEventListener: () => {{}},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {{}},
    requestAnimationFrame: (cb) => cb(),
  }},
  WebSocket: MockWebSocket,
  localStorage: {{ getItem: () => 'tok123', setItem: () => {{}}, removeItem: () => {{}} }},
  history: {{ replaceState: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {{}},
  requestAnimationFrame: (cb) => cb(),
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  Headers: typeof Headers !== 'undefined' ? Headers : class MockHeaders {{}},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }})
}});

vm.runInContext(appJs, ctx);

// Set up an open WebSocket connection
vm.runInContext(`
  isAuthenticated = true;
  wsConnection = new window.WebSocket("ws://localhost:8000/api/ws");
`, ctx);

// 1. Test sendWsSubscription and sendWsLogSubscription
ctx.sendWsSubscription("processes", "");
ctx.sendWsSubscription("containers", "stacks");
ctx.sendWsLogSubscription("c123");
ctx.sendWsLogSubscription(null);

const sentMessages = vm.runInContext("wsConnection.sent", ctx);
if (sentMessages.length !== 4) {{
  throw new Error("Expected 4 WebSocket messages sent, got " + sentMessages.length);
}}
if (sentMessages[0].action !== "subscribe" || sentMessages[0].view !== "processes") {{
  throw new Error("Unexpected first subscription message: " + JSON.stringify(sentMessages[0]));
}}
if (sentMessages[1].action !== "subscribe" || sentMessages[1].view !== "containers" || sentMessages[1].subtab !== "stacks") {{
  throw new Error("Unexpected second subscription message: " + JSON.stringify(sentMessages[1]));
}}
if (sentMessages[2].action !== "subscribe_logs" || sentMessages[2].container_id !== "c123") {{
  throw new Error("Unexpected log subscription message: " + JSON.stringify(sentMessages[2]));
}}
if (sentMessages[3].action !== "subscribe_logs" || sentMessages[3].container_id !== null) {{
  throw new Error("Unexpected log unsubscription message: " + JSON.stringify(sentMessages[3]));
}}

// 2. Test handleWebSocketMessage for container_logs
vm.runInContext(`activeLogContainerId = "c123";`, ctx);
const logsViewer = getEl("container-logs-viewer");
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "container_logs",
  container_id: "c123",
  logs: "line1\\nline2\\nready"
}}));
if (logsViewer.textContent !== "line1\\nline2\\nready") {{
  throw new Error("container_logs viewer not updated: " + logsViewer.textContent);
}}

// 3. Test handleWebSocketMessage for module_update across all modules
// 3a. services
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "services",
  services: {{
    services: [{{ unit: "nginx.service", name: "nginx", active_state: "active", sub_state: "running", description: "Nginx Web" }}],
    summary: {{ total: 5, running: 4, failed: 1, inactive: 0 }}
  }}
}}));
if (getEl("services-total-count").textContent !== "5") {{
  throw new Error("services-total-count not updated: " + getEl("services-total-count").textContent);
}}
if (!getEl("services-table-body").innerHTML.includes("nginx.service")) {{
  throw new Error("services-table-body did not render nginx.service");
}}

// 3b. processes
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "processes",
  processes: {{
    processes: [{{ pid: 4242, name: "uvicorn", username: "root", cpu_percent: 14.2, memory_percent: 3.1, status: "running", num_threads: 4 }}],
    summary: {{ total_processes: 128, running_count: 3, sleeping_count: 125, top_cpu_process: "uvicorn", top_memory_process: "uvicorn" }}
  }}
}}));
if (getEl("proc-total-count").textContent !== "128") {{
  throw new Error("proc-total-count not updated: " + getEl("proc-total-count").textContent);
}}
if (!getEl("processes-table-body").innerHTML.includes("uvicorn")) {{
  throw new Error("processes-table-body did not render uvicorn");
}}

// 3c. users & groups
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "users",
  users: {{
    users: [{{ username: "devops", uid: 1001, gid: 1001, name: "DevOps", home: "/home/devops", shell: "/bin/bash", is_system: false, groups: ["sudo"] }}],
    summary: {{ total_users: 10, regular_users_count: 1, system_users_count: 9 }}
  }},
  groups: {{
    groups: [{{ name: "devops", gid: 1001, members: ["devops"], is_system: false }}],
    summary: {{ total_groups: 20, regular_groups_count: 1 }}
  }}
}}));
if (getEl("users-total-count").textContent !== "10") {{
  throw new Error("users-total-count not updated: " + getEl("users-total-count").textContent);
}}
if (!getEl("users-table-body").innerHTML.includes("devops")) {{
  throw new Error("users-table-body did not render devops");
}}

// 3d. audit
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "audit",
  logs: [{{ id: 77, timestamp: 1700000000, event_type: "service.action", actor: "admin", severity: "info", ip_address: "127.0.0.1", description: "Restarted ssh.service" }}],
  total: 1
}}));
const auditBody = getEl("audit-table-body");
if (!auditBody.innerHTML.includes("Restarted ssh.service") && !auditBody.innerHTML.includes("service.action")) {{
  throw new Error("audit-table-body did not render audit event: " + auditBody.innerHTML);
}}

// 3e. security
let renderedSecurityPorts = null;
let renderedSshSec = null;
const origRenderNetSec = ctx.renderNetworkSecurity;
const origRenderSshSec = ctx.renderSshSecurity;
ctx.renderNetworkSecurity = function(portsPayload, fwPayload) {{
  renderedSecurityPorts = portsPayload;
  if (origRenderNetSec) origRenderNetSec(portsPayload, fwPayload);
}};
ctx.renderSshSecurity = function(sshPayload) {{
  renderedSshSec = sshPayload;
  if (origRenderSshSec) origRenderSshSec(sshPayload);
}};
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "security",
  security: {{
    ports: [{{ proto: "tcp", ip: "0.0.0.0", port: 443, is_public: true, process_name: "nginx" }}],
    summary: {{ total: 1, public: 1, local: 0 }},
    firewall: {{ available: true, enabled: true, rules: [] }},
    ssh_security: {{ active_sessions: [], banned_ips: [], top_offenders: [], failed_attempts_total: 0 }}
  }}
}}));
if (!renderedSecurityPorts || !Array.isArray(renderedSecurityPorts.ports) || renderedSecurityPorts.ports[0].port !== 443) {{
  throw new Error("security module_update did not invoke renderNetworkSecurity with ports");
}}
if (!renderedSshSec) {{
  throw new Error("security module_update did not invoke renderSshSecurity");
}}

// 3f. backups & cron
let renderedBackupsPayload = null;
let renderedCronPayload = null;
const origRenderBackups = ctx.renderBackups;
const origRenderCron = ctx.renderCronJobs;
ctx.renderBackups = function(b) {{
  renderedBackupsPayload = b;
  if (origRenderBackups) origRenderBackups(b);
}};
ctx.renderCronJobs = function(c) {{
  renderedCronPayload = c;
  if (origRenderCron) origRenderCron(c);
}};
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "backups",
  backups: {{ backup_dir: "/var/backups", archives: [{{ filename: "test.tar.gz", size_bytes: 1024 }}], targets: [] }},
  cron_jobs: {{ jobs: [{{ id: 1, name: "Weekly Cleanup", cron_expression: "0 0 * * 0", enabled: true }}] }},
  cron_runs: [{{ id: 1, job_id: 1, job_name: "Weekly Cleanup", status: "success" }}]
}}));
if (!renderedBackupsPayload || renderedBackupsPayload.backup_dir !== "/var/backups") {{
  throw new Error("backups module_update did not invoke renderBackups");
}}
if (!renderedCronPayload || !Array.isArray(renderedCronPayload.jobs) || renderedCronPayload.jobs[0].name !== "Weekly Cleanup") {{
  throw new Error("backups module_update did not invoke renderCronJobs");
}}

// 3g. probes
let renderedProbesPayload = null;
const origRenderProbes = ctx.renderProbes;
ctx.renderProbes = function(p) {{
  renderedProbesPayload = p;
  if (origRenderProbes) origRenderProbes(p);
}};
ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "probes",
  probes: [{{ id: 9, name: "Core Router", type: "ping", target: "10.0.0.1", last_status: "up" }}]
}}));
if (!renderedProbesPayload || !Array.isArray(renderedProbesPayload.probes) || renderedProbesPayload.probes[0].name !== "Core Router") {{
  throw new Error("probes module_update did not invoke renderProbes");
}}

// 3h. docker (containers + stacks) & dropdown preservation
const containersTbody = getEl("docker-containers-tbody");
const stacksTbody = getEl("docker-stacks-tbody");

ctx.handleWebSocketMessage(JSON.stringify({{
  type: "module_update",
  module: "docker",
  docker: {{ installed: true, running: true, containers_running: 1, containers_total: 1, stacks_total: 1, images_total: 1, volumes_total: 1 }},
  containers: [{{ id: "c111", name: "alpha-web", image: "nginx:latest", state: "running", status: "Up 1 hour", ports: "80/tcp", stack: "alpha" }}],
  stacks: [{{ name: "alpha", status: "running", running_count: 1, containers_count: 1, managed: true }}]
}}));

if (!containersTbody.innerHTML.includes("alpha-web")) {{
  throw new Error("docker-containers-tbody did not render alpha-web");
}}
if (!stacksTbody.innerHTML.includes("alpha")) {{
  throw new Error("docker-stacks-tbody did not render alpha stack");
}}

// 4. Verify open dropdown (.actions-dropdown-menu.show) is NOT closed during background live stream ticks
containersTbody.innerHTML = "<tr id='preserved-container-row'><td>OPEN_DROPDOWN_CONTAINER</td></tr>";
containersTbody.setOpenDropdown(true);

stacksTbody.innerHTML = "<tr id='preserved-stack-row'><td>OPEN_DROPDOWN_STACK</td></tr>";
stacksTbody.setOpenDropdown(true);

ctx.renderDockerContainers(null, [
  {{ id: "c222", name: "beta-web", image: "redis:alpine", state: "running", status: "Up 5 mins" }}
]);
ctx.renderDockerStacks([
  {{ name: "beta", status: "running", running_count: 1, containers_count: 1, managed: true }}
]);

if (!containersTbody.innerHTML.includes("OPEN_DROPDOWN_CONTAINER")) {{
  throw new Error("renderDockerContainers overwrote tbody while .actions-dropdown-menu.show was open!");
}}
if (!stacksTbody.innerHTML.includes("OPEN_DROPDOWN_STACK")) {{
  throw new Error("renderDockerStacks overwrote tbody while .actions-dropdown-menu.show was open!");
}}

// Once dropdown closes, rendering updates normally
containersTbody.setOpenDropdown(false);
stacksTbody.setOpenDropdown(false);

ctx.renderDockerContainers(null, [
  {{ id: "c222", name: "beta-web", image: "redis:alpine", state: "running", status: "Up 5 mins" }}
]);
ctx.renderDockerStacks([
  {{ name: "beta", status: "running", running_count: 1, containers_count: 1, managed: true }}
]);

if (!containersTbody.innerHTML.includes("beta-web")) {{
  throw new Error("renderDockerContainers did not update after dropdown closed");
}}
if (!stacksTbody.innerHTML.includes("beta")) {{
  throw new Error("renderDockerStacks did not update after dropdown closed");
}}

console.log("LIVE_STREAMS_NODE_VM_PASSED");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(
            res.returncode,
            0,
            f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}",
        )
        self.assertIn("LIVE_STREAMS_NODE_VM_PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
