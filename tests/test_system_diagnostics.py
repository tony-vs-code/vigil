import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import api
import main
from test_main import asgi_get


class TestSystemDiagnosticsEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "diagnostics_test.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        # Initialize db and default tables
        main._connect_state_db(self.db_path).close()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_run_system_diagnostics_all_healthy(self):
        result = main.run_system_diagnostics(self.db_path)
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["checks_passed"], 6)
        self.assertEqual(result["checks_total"], 6)
        self.assertGreater(result["duration_ms"], 0.0)
        self.assertTrue(result["timestamp"].endswith("+00:00") or "T" in result["timestamp"])

        checks = result["checks"]
        self.assertIn("database", checks)
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertEqual(checks["database"]["message"], "SQLite integrity verified, 9 tables active")

        self.assertIn("notification_hub", checks)
        self.assertEqual(checks["notification_hub"]["status"], "ok")
        self.assertEqual(checks["notification_hub"]["message"], "6 providers registered with event routing")

        self.assertIn("docker_and_templates", checks)
        self.assertEqual(checks["docker_and_templates"]["status"], "ok")
        self.assertEqual(checks["docker_and_templates"]["message"], "11 templates validated")

        self.assertIn("telemetry_engine", checks)
        self.assertEqual(checks["telemetry_engine"]["status"], "ok")
        self.assertEqual(checks["telemetry_engine"]["message"], "Time-series engine responsive")

        self.assertIn("vigil_scheduler", checks)
        self.assertEqual(checks["vigil_scheduler"]["status"], "ok")
        self.assertEqual(checks["vigil_scheduler"]["message"], "Cron scheduler responsive")

        self.assertIn("api_security", checks)
        self.assertEqual(checks["api_security"]["status"], "ok")
        self.assertEqual(checks["api_security"]["message"], "Session store and security guards active")

    def test_database_check_missing_table_failure(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("DROP TABLE cron_jobs")
        conn.commit()
        conn.close()

        result = main.run_system_diagnostics(self.db_path)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["database"]["status"], "degraded")
        self.assertIn("Missing required tables", result["checks"]["database"]["message"])
        self.assertLess(result["checks_passed"], 6)

    def test_database_check_missing_column_failure(self):
        # Create a database where docker_stacks lacks env_content
        fake_db = Path(self.temp_dir.name) / "bad_col.sqlite3"
        conn = main._connect_state_db(fake_db)
        conn.execute("CREATE TABLE temp_stacks AS SELECT name, compose_yaml, status, created_at, updated_at FROM docker_stacks")
        conn.execute("DROP TABLE docker_stacks")
        conn.execute("ALTER TABLE temp_stacks RENAME TO docker_stacks")
        conn.commit()
        conn.close()

        result = main.run_system_diagnostics(fake_db)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks"]["database"]["status"], "degraded")
        self.assertIn("Missing column 'env_content'", result["checks"]["database"]["message"])

    def test_database_check_integrity_failure(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = ("corrupted root page",)
        mock_conn.execute.return_value = mock_cursor
        with patch("sqlite3.connect", return_value=mock_conn):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["database"]["status"], "degraded")
            self.assertIn("Integrity check failed", result["checks"]["database"]["message"])

    def test_notification_hub_missing_provider_failure(self):
        mock_hub = MagicMock()
        mock_hub.providers = {"discord": MagicMock()}  # Missing telegram, pushover, etc.
        with patch("main.NotificationManager", return_value=mock_hub):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["notification_hub"]["status"], "degraded")
            self.assertIn("Missing providers", result["checks"]["notification_hub"]["message"])

    def test_notification_hub_missing_routing_failure(self):
        mock_hub = MagicMock()
        mock_hub.providers = {
            p: MagicMock(routing=None)
            for p in ["discord", "telegram", "webhook", "pushover", "ntfy", "gotify"]
        }
        with patch("main.NotificationManager", return_value=mock_hub):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["notification_hub"]["status"], "degraded")
            self.assertIn("missing event routing", result["checks"]["notification_hub"]["message"])

    def test_docker_and_templates_invalid_count_failure(self):
        mock_dm = MagicMock()
        mock_dm.get_templates.return_value = [{"name": "Single"}]
        with patch("main.DockerManager", return_value=mock_dm):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["docker_and_templates"]["status"], "degraded")
            self.assertIn("Expected 11 curated templates", result["checks"]["docker_and_templates"]["message"])

    def test_docker_and_templates_yaml_validation_failure(self):
        mock_dm = MagicMock()
        mock_dm.get_templates.return_value = [{"name": f"T{i}", "compose_yaml": "bad"} for i in range(11)]
        mock_dm.validate_compose_yaml.return_value = {"valid": False, "errors": ["Syntax error on line 1"]}
        with patch("main.DockerManager", return_value=mock_dm):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["docker_and_templates"]["status"], "degraded")
            self.assertIn("failed validation", result["checks"]["docker_and_templates"]["message"])

    def test_telemetry_engine_failure(self):
        with patch("main.get_telemetry_history", side_effect=RuntimeError("telemetry disk read error")):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["telemetry_engine"]["status"], "degraded")
            self.assertIn("telemetry disk read error", result["checks"]["telemetry_engine"]["message"])

    def test_vigil_scheduler_validator_failure(self):
        with patch("main.validate_cron_expression", return_value=False):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["vigil_scheduler"]["status"], "degraded")
            self.assertIn("Cron expression validator failed", result["checks"]["vigil_scheduler"]["message"])

    def test_api_security_missing_secret_key_failure(self):
        with patch.object(main, "AUTH_SECRET_KEY", ""), patch.dict(os.environ, {"AUTH_SECRET_KEY": "", "SECRET_KEY": ""}):
            result = main.run_system_diagnostics(self.db_path)
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["checks"]["api_security"]["status"], "degraded")
            self.assertIn("Auth secret key is not configured", result["checks"]["api_security"]["message"])


class TestSystemDiagnosticsEndpoint(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api_diag.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("diagadmin", "securepassword", email="admin@diag.local")
        self.session_token = main.create_session("diagadmin")
        self.auth_cookies = {"session_token": self.session_token}
        self.auth_headers = {"Authorization": f"Bearer {self.session_token}"}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_diagnostics_endpoint_unauthenticated_returns_401(self):
        resp = asgi_get(api.app, "/api/system/diagnostics")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Authentication required", resp.text)

    def test_diagnostics_endpoint_authenticated_returns_200_and_schema(self):
        resp = asgi_get(api.app, "/api/system/diagnostics", cookies=self.auth_cookies)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertIn("status", data)
        self.assertIn("duration_ms", data)
        self.assertIn("timestamp", data)
        self.assertIn("checks_passed", data)
        self.assertIn("checks_total", data)
        self.assertIn("checks", data)

        self.assertEqual(data["checks_total"], 6)
        self.assertEqual(data["checks_passed"], 6)
        self.assertEqual(data["status"], "healthy")

        checks = data["checks"]
        for expected_subsystem in [
            "database",
            "notification_hub",
            "docker_and_templates",
            "telemetry_engine",
            "vigil_scheduler",
            "api_security",
        ]:
            self.assertIn(expected_subsystem, checks)
            self.assertEqual(checks[expected_subsystem]["status"], "ok")
            self.assertTrue(len(checks[expected_subsystem]["message"]) > 0)

    def test_diagnostics_endpoint_authenticated_with_bearer_token(self):
        resp = asgi_get(api.app, "/api/system/diagnostics", headers=self.auth_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "healthy")


class TestSystemDiagnosticsFrontendAndNodeVM(unittest.TestCase):
    """Validation tests for System Diagnostics DOM contract and Node VM simulation."""

    def test_system_diagnostics_dom_contract(self):
        root = Path(__file__).resolve().parent.parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")

        # Top-level and summary elements
        summary_ids = [
            "system-diagnostics-card",
            "btn-run-diagnostics",
            "diagnostics-summary-bar",
            "diagnostics-overall-badge",
            "diagnostics-summary-text",
            "diagnostics-timestamp",
            "diagnostics-grid",
        ]
        for elem_id in summary_ids:
            self.assertIn(f'id="{elem_id}"', html, f"Missing diagnostic summary element #{elem_id} in index.html")

        # Subtab elements
        subtab_ids = [
            "subtab-settings-general",
            "subtab-settings",
            "subtab-settings-diagnostics",
            "subview-settings-general",
            "subview-settings",
        ]
        for elem_id in subtab_ids:
            self.assertIn(f'id="{elem_id}"', html, f"Missing settings subtab element #{elem_id} in index.html")

        # 12 Subsystem diagnostic elements (6 badges + 6 messages) + 6 container items
        subsystems = [
            "database",
            "notification-hub",
            "docker-templates",
            "telemetry-engine",
            "vigil-scheduler",
            "api-security",
        ]
        for sub in subsystems:
            item_id = f"diag-item-{sub}"
            badge_id = f"diag-badge-{sub}"
            msg_id = f"diag-msg-{sub}"
            self.assertIn(f'id="{item_id}"', html, f"Missing diagnostic item #{item_id} in index.html")
            self.assertIn(f'id="{badge_id}"', html, f"Missing diagnostic badge #{badge_id} in index.html")
            self.assertIn(f'id="{msg_id}"', html, f"Missing diagnostic message #{msg_id} in index.html")

    def test_settings_subtab_switching_in_node_vm(self):
        import subprocess

        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  return {{
    id: id,
    style: {{}},
    children: children,
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (child) => {{ children.push(child); return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      return child;
    }},
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
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
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');
let diagnosticsTriggered = false;

const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    addEventListener: () => {{}},
    clearTimeout: () => {{}},
    setTimeout: setTimeout,
  }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  clearInterval: clearInterval,
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  Headers: typeof Headers !== 'undefined' ? Headers : class MockHeaders {{}},
  fetch: () => Promise.resolve({{
    ok: true,
    json: () => Promise.resolve({{
      status: 'healthy',
      checks_passed: 6,
      checks_total: 6,
      duration_ms: 10,
      timestamp: new Date().toISOString(),
      checks: {{}}
    }})
  }})
}});

vm.runInContext(appJs, ctx);

// Override runSystemDiagnostics to record call
ctx.runSystemDiagnostics = function() {{
  diagnosticsTriggered = true;
}};

// 1. Switch to diagnostics subtab
ctx.switchSettingsSubtab('diagnostics');

const btnGen = getEl('subtab-settings-general');
const btnDiag = getEl('subtab-settings');
const btnDiagAlias = getEl('subtab-settings-diagnostics');
const panelGen = getEl('subview-settings-general');
const panelDiag = getEl('subview-settings');

if (btnGen.classList.contains('active')) throw new Error("btnGen should not have active class");
if (btnGen.getAttribute('aria-selected') !== 'false') throw new Error("btnGen aria-selected should be false");
if (!btnDiag.classList.contains('active')) throw new Error("btnDiag should have active class");
if (btnDiag.getAttribute('aria-selected') !== 'true') throw new Error("btnDiag aria-selected should be true");
if (!btnDiagAlias.classList.contains('active')) throw new Error("btnDiagAlias should have active class");
if (btnDiagAlias.getAttribute('aria-selected') !== 'true') throw new Error("btnDiagAlias aria-selected should be true");

if (!panelGen.classList.contains('hidden')) throw new Error("panelGen should have hidden class");
if (panelDiag.classList.contains('hidden')) throw new Error("panelDiag should not have hidden class");
if (!diagnosticsTriggered) throw new Error("switchSettingsSubtab('diagnostics') should trigger runSystemDiagnostics");

// 2. Switch back to general subtab
ctx.switchSettingsSubtab('general');
if (!btnGen.classList.contains('active')) throw new Error("btnGen should have active class");
if (btnGen.getAttribute('aria-selected') !== 'true') throw new Error("btnGen aria-selected should be true");
if (btnDiag.classList.contains('active')) throw new Error("btnDiag should not have active class");
if (btnDiag.getAttribute('aria-selected') !== 'false') throw new Error("btnDiag aria-selected should be false");

if (panelGen.classList.contains('hidden')) throw new Error("panelGen should not have hidden class");
if (!panelDiag.classList.contains('hidden')) throw new Error("panelDiag should have hidden class");

console.log("SUBTAB_SWITCHING_NODE_VM_CHECKS_PASSED");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("SUBTAB_SWITCHING_NODE_VM_CHECKS_PASSED", res.stdout)

    def test_run_system_diagnostics_node_vm(self):
        import subprocess

        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "app.js"

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  return {{
    id: id,
    style: {{}},
    children: children,
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (child) => {{ children.push(child); return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      return child;
    }},
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
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
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync('{str(js_path)}', 'utf-8');

(async () => {{
  let mockApiResponse = {{}};
  let shouldFail = false;

  const ctx = vm.createContext({{
    document: mockDoc,
    window: {{
      addEventListener: () => {{}},
      clearTimeout: () => {{}},
      setTimeout: setTimeout,
      requestAnimationFrame: (cb) => setTimeout(cb, 0),
      cancelAnimationFrame: (id) => clearTimeout(id),
    }},
    requestAnimationFrame: (cb) => setTimeout(cb, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    localStorage: {{ getItem: () => null, setItem: () => {{}} }},
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    WebSocket: class MockWS {{}},
    TextDecoder: TextDecoder,
    TextEncoder: TextEncoder,
    URLSearchParams: URLSearchParams,
    Headers: typeof Headers !== 'undefined' ? Headers : class MockHeaders {{}},
    fetch: async (url) => {{
      if (shouldFail) {{
        return {{
          ok: false,
          status: 500,
          json: async () => ({{ detail: "Internal Server Error in diagnostic engine" }})
        }};
      }}
      return {{
        ok: true,
        status: 200,
        json: async () => mockApiResponse
      }};
    }}
  }});

  vm.runInContext(appJs, ctx);

  // Scenario 1: Healthy (6/6 passing)
  mockApiResponse = {{
    status: "healthy",
    checks_passed: 6,
    checks_total: 6,
    duration_ms: 14.8,
    timestamp: "2026-11-06T22:00:00Z",
    checks: {{
      database: {{ status: "ok", message: "SQLite integrity verified, 9 tables active" }},
      notification_hub: {{ status: "ok", message: "6 providers registered with event routing" }},
      docker_and_templates: {{ status: "ok", message: "11 templates validated" }},
      telemetry_engine: {{ status: "ok", message: "Time-series engine responsive" }},
      vigil_scheduler: {{ status: "ok", message: "Cron scheduler responsive" }},
      api_security: {{ status: "ok", message: "Session store and security guards active" }}
    }}
  }};

  await ctx.runSystemDiagnostics();

  const overallBadge = getEl('diagnostics-overall-badge');
  const summaryText = getEl('diagnostics-summary-text');
  const btnRun = getEl('btn-run-diagnostics');

  if (overallBadge.textContent !== "HEALTHY") throw new Error("Expected HEALTHY overall badge, got: " + overallBadge.textContent);
  if (!overallBadge.classList.contains("status-clean")) throw new Error("overallBadge missing status-clean class");
  if (!summaryText.textContent.includes("6 of 6 checks passed in 14.8 ms")) throw new Error("Unexpected summaryText: " + summaryText.textContent);
  if (btnRun.disabled !== false) throw new Error("btnRun should not be disabled after run");

  const subsystems = [
    ["database", "SQLite integrity verified, 9 tables active"],
    ["notification-hub", "6 providers registered with event routing"],
    ["docker-templates", "11 templates validated"],
    ["telemetry-engine", "Time-series engine responsive"],
    ["vigil-scheduler", "Cron scheduler responsive"],
    ["api-security", "Session store and security guards active"]
  ];

  for (const [sub, expectedMsg] of subsystems) {{
    const badge = getEl(`diag-badge-${{sub}}`);
    const msg = getEl(`diag-msg-${{sub}}`);
    if (badge.textContent !== "OK") throw new Error(`Subsystem ${{sub}} badge should be OK, got: ` + badge.textContent);
    if (!badge.classList.contains("status-clean")) throw new Error(`Subsystem ${{sub}} badge missing status-clean`);
    if (msg.textContent !== expectedMsg) throw new Error(`Subsystem ${{sub}} msg mismatch: ` + msg.textContent);
  }}

  // Scenario 2: Degraded Response (e.g. database failed, 5/6 passed)
  mockApiResponse = {{
    status: "degraded",
    checks_passed: 5,
    checks_total: 6,
    duration_ms: 18.2,
    timestamp: "2026-11-06T22:05:00Z",
    checks: {{
      database: {{ status: "degraded", message: "Missing required tables: ['cron_jobs']" }},
      notification_hub: {{ status: "ok", message: "6 providers registered with event routing" }},
      docker_and_templates: {{ status: "ok", message: "11 templates validated" }},
      telemetry_engine: {{ status: "ok", message: "Time-series engine responsive" }},
      vigil_scheduler: {{ status: "ok", message: "Cron scheduler responsive" }},
      api_security: {{ status: "ok", message: "Session store and security guards active" }}
    }}
  }};

  await ctx.runSystemDiagnostics();

  if (overallBadge.textContent !== "DEGRADED") throw new Error("Expected DEGRADED overall badge, got: " + overallBadge.textContent);
  if (!overallBadge.classList.contains("status-failed")) throw new Error("overallBadge missing status-failed class");
  if (!summaryText.textContent.includes("5 of 6 checks passed")) throw new Error("Unexpected summaryText: " + summaryText.textContent);

  const dbBadge = getEl("diag-badge-database");
  const dbMsg = getEl("diag-msg-database");
  if (dbBadge.textContent !== "FAILED") throw new Error("Expected FAILED db badge, got: " + dbBadge.textContent);
  if (!dbBadge.classList.contains("status-failed")) throw new Error("dbBadge missing status-failed");
  if (!dbMsg.textContent.includes("Missing required tables")) throw new Error("dbMsg missing failure detail: " + dbMsg.textContent);

  // Scenario 3: API Error / 500 failure
  shouldFail = true;
  await ctx.runSystemDiagnostics();

  if (overallBadge.textContent !== "ERROR") throw new Error("Expected ERROR overall badge, got: " + overallBadge.textContent);
  if (!overallBadge.classList.contains("status-failed")) throw new Error("overallBadge missing status-failed on error");
  if (!summaryText.textContent.includes("Self-test failed")) throw new Error("summaryText should indicate error, got: " + summaryText.textContent);
  if (btnRun.disabled !== false) throw new Error("btnRun should be re-enabled on error");

  console.log("ALL_SYSTEM_DIAGNOSTICS_NODE_VM_CHECKS_PASSED");
}})();
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_SYSTEM_DIAGNOSTICS_NODE_VM_CHECKS_PASSED", res.stdout)

