import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import api
import main
from test_main import asgi_get, asgi_post


class TestPythonPackageManagerBackend(unittest.TestCase):
    """Backend tests for Vigil Python Package Manager: SemVer bounds, PyPI checking, safe upgrade, and rollback."""

    def setUp(self):
        self.pm = main.PackageManager()

    def test_parse_pyproject_dependencies_and_semver_bounds(self):
        """Verify parsing pyproject.toml dependencies, constraints, and SemVer upper bound evaluation."""
        deps = self.pm.parse_pyproject_dependencies()
        self.assertIn("aiohttp", deps)
        self.assertIn("fastapi", deps)
        self.assertIn("discord-py", deps)

        aio = deps["aiohttp"]
        self.assertEqual(aio["min_version"], "3.10.5")
        self.assertEqual(aio["upper_bound"], "4.0.0")
        self.assertEqual(aio["constraint"], ">=3.10.5,<4.0.0")

        # Test constraint satisfaction
        self.assertTrue(self.pm._version_satisfies_constraint("3.10.5", ">=3.10.5,<4.0.0"))
        self.assertTrue(self.pm._version_satisfies_constraint("3.11.2", ">=3.10.5,<4.0.0"))
        self.assertFalse(self.pm._version_satisfies_constraint("4.0.0", ">=3.10.5,<4.0.0"))
        self.assertFalse(self.pm._version_satisfies_constraint("4.0.1", ">=3.10.5,<4.0.0"))
        self.assertFalse(self.pm._version_satisfies_constraint("3.10.4", ">=3.10.5,<4.0.0"))

        # Test other operators
        self.assertTrue(self.pm._version_satisfies_constraint("2.4.0", ">=2.4.0,<3.0.0"))
        self.assertFalse(self.pm._version_satisfies_constraint("3.0.0", ">=2.4.0,<3.0.0"))
        self.assertTrue(self.pm._version_satisfies_constraint("1.0.0", "==1.0.0"))
        self.assertFalse(self.pm._version_satisfies_constraint("1.0.1", "==1.0.0"))

    def test_get_python_packages_with_pypi_and_major_held(self):
        """Verify PyPI version checking, update_available, major_held flags, and role separation."""
        def mock_fetch(pkg_name, timeout=4.0):
            if pkg_name == "aiohttp":
                return ["3.10.5", "3.99.0", "4.0.0"]
            elif pkg_name == "fastapi":
                return ["0.112.0", "0.199.0"]
            return ["1.0.0"]

        with patch.object(self.pm, "_fetch_pypi_versions", side_effect=mock_fetch):
            result = self.pm.get_python_packages(check_pypi=True)

        self.assertIn("python_version", result)
        self.assertIn("packages", result)
        self.assertIn("summary", result)
        self.assertTrue(result["uv_available"])

        pkgs = {p["normalized_name"]: p for p in result["packages"]}
        self.assertIn("aiohttp", pkgs)
        aio = pkgs["aiohttp"]
        self.assertEqual(aio["role"], "direct")
        self.assertEqual(aio["latest_version"], "4.0.0")
        self.assertEqual(aio["latest_compatible_version"], "3.99.0")
        self.assertTrue(aio["major_held"])
        self.assertTrue(aio["update_available"])

        # Role separation: direct packages must appear before transitive packages
        seen_transitive = False
        for p in result["packages"]:
            if p["role"] == "transitive":
                seen_transitive = True
            elif p["role"] == "direct":
                self.assertFalse(seen_transitive, f"Direct package {p['name']} found after transitive package")

        self.assertEqual(result["summary"]["total"], len(result["packages"]))
        self.assertGreater(result["summary"]["direct"], 0)
        self.assertGreaterEqual(result["summary"]["major_held"], 1)

    def test_upgrade_python_packages_validation_and_dry_run(self):
        """Verify input validation rejecting malformed names and safe dry-run execution."""
        with self.assertRaises(ValueError):
            self.pm.upgrade_python_packages(packages=["invalid name; rm -rf /"])
        with self.assertRaises(ValueError):
            self.pm.upgrade_python_packages(packages=["--bad-option"])
        with self.assertRaises(ValueError):
            self.pm.upgrade_python_packages(packages=["package$name"])

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Would upgrade aiohttp to 3.11.0\n"
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = self.pm.upgrade_python_packages(packages=["aiohttp"], dry_run=True)
            self.assertTrue(res["success"])
            self.assertTrue(res["dry_run"])
            self.assertFalse(res["rolled_back"])
            self.assertIn("Would upgrade aiohttp", res["output"])
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertIn("lock", cmd)
            self.assertIn("--upgrade-package", cmd)
            self.assertIn("aiohttp", cmd)
            self.assertIn("--dry-run", cmd)

    def test_upgrade_python_packages_success_and_smoke_rollback(self):
        """Verify upgrade success and pre-restart smoke test failure with automatic rollback."""
        # 1. Success path
        mock_run_proc = MagicMock(returncode=0, stdout="Upgrade completed", stderr="")
        with patch("subprocess.run", return_value=mock_run_proc), \
             patch.object(self.pm, "run_python_smoke_test", return_value={"passed": True, "output": "PASS", "error": "", "returncode": 0}):
            res = self.pm.upgrade_python_packages(packages=["aiohttp"], dry_run=False, restart_service=False)
            self.assertTrue(res["success"])
            self.assertFalse(res["dry_run"])
            self.assertFalse(res["rolled_back"])
            self.assertTrue(res["smoke_test"]["passed"])

        # 2. Rollback path when smoke test fails
        with tempfile.TemporaryDirectory() as td:
            fake_lock = Path(td) / "uv.lock"
            fake_lock.write_text("lockfile-content-v1", encoding="utf-8")
            with patch("main.REPO_DIR", Path(td)), \
                 patch("subprocess.run", return_value=mock_run_proc) as mock_subproc, \
                 patch.object(self.pm, "run_python_smoke_test", return_value={"passed": False, "output": "", "error": "ImportError: broken dependency", "returncode": 1}):
                res = self.pm.upgrade_python_packages(packages=["aiohttp"], dry_run=False, restart_service=False)
                self.assertFalse(res["success"])
                self.assertFalse(res["dry_run"])
                self.assertTrue(res["rolled_back"])
                self.assertIn("Pre-restart smoke test failed", res["error"])
                # Ensure original lockfile was restored
                self.assertEqual(fake_lock.read_text(encoding="utf-8"), "lockfile-content-v1")
                # Ensure rollback invoked `uv sync --frozen`
                rollback_calls = [
                    c[0][0] for c in mock_subproc.call_args_list
                    if isinstance(c[0][0], list) and "--frozen" in c[0][0]
                ]
                self.assertTrue(len(rollback_calls) > 0, "uv sync --frozen not invoked during rollback")

    def test_api_python_packages_endpoints_and_auth_guards(self):
        """Verify API auth guard (401) and endpoints behavior (GET, POST /check, POST /upgrade)."""
        # 1. Unauthenticated requests must return 401
        self.assertEqual(asgi_get(api.app, "/api/packages/python").status_code, 401)
        self.assertEqual(asgi_post(api.app, "/api/packages/python/check").status_code, 401)
        self.assertEqual(asgi_post(api.app, "/api/packages/python/upgrade", json_body={"dry_run": True}).status_code, 401)

        # 2. Authenticated requests
        api.app.dependency_overrides[api.require_auth] = lambda: "admin"
        try:
            # GET /api/packages/python
            with patch.object(main.package_manager, "get_python_packages", return_value={"python_version": "3.11", "packages": [], "summary": {}}) as mock_get:
                resp = asgi_get(api.app, "/api/packages/python?check_pypi=false")
                self.assertEqual(resp.status_code, 200)
                mock_get.assert_called_once_with(False)

            # POST /api/packages/python/check
            with patch.object(main.package_manager, "get_python_packages", return_value={"python_version": "3.11", "packages": [], "summary": {}}) as mock_check:
                resp = asgi_post(api.app, "/api/packages/python/check")
                self.assertEqual(resp.status_code, 200)
                mock_check.assert_called_once_with(True)

            # POST /api/packages/python/upgrade invalid name -> 400
            resp = asgi_post(api.app, "/api/packages/python/upgrade", json_body={"packages": ["bad;rm -rf /"]})
            self.assertEqual(resp.status_code, 400)

            # POST /api/packages/python/upgrade valid
            with patch.object(main.package_manager, "upgrade_python_packages", return_value={"success": True, "dry_run": True, "rolled_back": False}):
                resp = asgi_post(api.app, "/api/packages/python/upgrade", json_body={"packages": ["aiohttp"], "dry_run": True})
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.json()["success"])
        finally:
            api.app.dependency_overrides.pop(api.require_auth, None)


class TestPythonPackageManagerDOMContract(unittest.TestCase):
    """Frontend DOM contract tests for Python Package Manager in static/index.html."""

    def setUp(self):
        self.index_html = (Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")

    def test_python_packages_dom_elements_present(self):
        """Verify all required Python package manager DOM elements exist in index.html."""
        required_ids = [
            "subtab-pkg-python-btn",
            "pkg-python-subtab-panel",
            "pkg-python-search",
            "pkg-python-role-filter",
            "pkg-python-restart-toggle",
            "btn-check-pypi-updates",
            "btn-upgrade-all-python",
            "pkg-python-table",
            "pkg-python-tbody",
        ]
        for elem_id in required_ids:
            self.assertIn(f'id="{elem_id}"', self.index_html, f"Missing required element #{elem_id} in index.html")

    def test_python_packages_tab_attributes_and_wiring(self):
        """Verify ARIA and navigation tab attributes for Python subtab panel."""
        self.assertIn('role="tab"', self.index_html)
        self.assertIn('aria-controls="pkg-python-subtab-panel"', self.index_html)
        self.assertIn('aria-labelledby="subtab-pkg-python-btn"', self.index_html)
        self.assertIn('id="pkg-python-table"', self.index_html)
        self.assertIn('id="pkg-python-tbody"', self.index_html)


class TestPythonPackageManagerNodeVM(unittest.TestCase):
    """Node VM execution tests for fetchPythonPackages, renderPythonPackagesTable, and user interactions."""

    def setUp(self):
        self.root = Path(__file__).parent.parent
        self.app_js_path = self.root / "static" / "app.js"

    def test_fetch_and_render_python_packages_in_node_vm(self):
        """Execute fetchPythonPackages and renderPythonPackagesTable in Node VM."""
        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  const listeners = {{}};
  const children = [];
  return {{
    id: id,
    children: children,
    firstChild: null,
    classList: {{
      add: (cls) => classes.add(cls),
      remove: (cls) => classes.delete(cls),
      contains: (cls) => classes.has(cls),
      toggle: (cls) => classes.has(cls) ? classes.delete(cls) : classes.add(cls)
    }},
    setAttribute: (k, v) => {{ attributes[k] = String(v); }},
    getAttribute: (k) => attributes[k] ?? null,
    removeAttribute: (k) => {{ delete attributes[k]; }},
    addEventListener: (evt, cb) => {{ listeners[evt] = cb; }},
    dispatchEvent: (evt) => {{ if (listeners[evt]) listeners[evt](); }},
    appendChild: (child) => {{ children.push(child); }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
    }},
    remove: () => {{}},
    querySelector: () => null,
    querySelectorAll: () => [],
    disabled: false,
    value: '',
    checked: true,
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

// Initialize mock DOM elements
getEl('toast-container');
getEl('subtab-pkg-python-btn');
getEl('pkg-python-subtab-panel');
getEl('pkg-python-search');
getEl('pkg-python-role-filter');
getEl('pkg-python-restart-toggle');
getEl('btn-check-pypi-updates');
getEl('btn-upgrade-all-python');
getEl('pkg-python-table');
getEl('pkg-python-tbody');
getEl('pkg-python-version');
getEl('pkg-python-summary');

const sampleData = {{
  python_version: '3.11.13',
  uv_available: true,
  summary: {{
    total: 3,
    direct: 2,
    transitive: 1,
    updates_available: 1,
    major_held: 1
  }},
  packages: [
    {{
      name: 'aiohttp',
      normalized_name: 'aiohttp',
      role: 'direct',
      constraint: '>=3.10.5,<4.0.0',
      installed_version: '3.10.5',
      latest_version: '4.0.0',
      latest_compatible_version: '3.11.0',
      update_available: true,
      major_held: true,
      summary: 'Async HTTP client/server for asyncio'
    }},
    {{
      name: 'fastapi',
      normalized_name: 'fastapi',
      role: 'direct',
      constraint: '>=0.112.0',
      installed_version: '0.115.0',
      latest_version: '0.115.0',
      latest_compatible_version: '0.115.0',
      update_available: false,
      major_held: false,
      summary: 'FastAPI framework'
    }},
    {{
      name: 'yarl',
      normalized_name: 'yarl',
      role: 'transitive',
      constraint: null,
      installed_version: '1.9.4',
      latest_version: '1.9.4',
      latest_compatible_version: '1.9.4',
      update_available: false,
      major_held: false,
      summary: 'Yet another URL library'
    }}
  ]
}};

const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement(tag),
  querySelectorAll: () => [],
  addEventListener: () => {{}},
  body: createMockElement('body')
}};

class MockHeaders {{
  constructor(init = {{}}) {{
    this.map = {{}};
    if (init) {{
      for (const [k, v] of Object.entries(init)) {{
        this.map[k.toLowerCase()] = v;
      }}
    }}
  }}
  set(k, v) {{ this.map[k.toLowerCase()] = v; }}
  get(k) {{ return this.map[k.toLowerCase()]; }}
  has(k) {{ return k.toLowerCase() in this.map; }}
}}

const appJs = fs.readFileSync('{str(self.app_js_path)}', 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    addEventListener: () => {{}},
    requestAnimationFrame: (cb) => {{ if (typeof cb === 'function') cb(); return 0; }}
  }},
  requestAnimationFrame: (cb) => {{ if (typeof cb === 'function') cb(); return 0; }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: (cb) => {{ if (typeof cb === 'function') cb(); return 0; }},
  clearTimeout: () => {{}},
  setInterval: () => 0,
  clearInterval: () => {{}},
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  Headers: typeof Headers !== 'undefined' ? Headers : MockHeaders,
  fetch: async (url, opts) => ({{
    ok: true,
    status: 200,
    json: async () => sampleData
  }})
}});

vm.runInContext(appJs, ctx);

(async () => {{
  // 1. Initial fetchPythonPackages(false)
  await ctx.fetchPythonPackages(false);

  const tbody = getEl('pkg-python-tbody');
  const tbodyHtml = tbody.innerHTML;

  if (!tbodyHtml.includes('aiohttp')) {{
    throw new Error('aiohttp missing from pkg-python-tbody');
  }}
  if (!tbodyHtml.includes('fastapi')) {{
    throw new Error('fastapi missing from pkg-python-tbody');
  }}
  if (!tbodyHtml.includes('yarl')) {{
    throw new Error('yarl missing from pkg-python-tbody');
  }}

  // Check Direct vs Transitive badges
  if (!tbodyHtml.includes('DIRECT') || !tbodyHtml.includes('TRANSITIVE')) {{
    throw new Error('Role badges missing from pkg-python-tbody');
  }}

  // Check Major Held pill / note
  if (!tbodyHtml.includes('MAJOR HELD') && !tbodyHtml.includes('major_held') && !tbodyHtml.includes('held')) {{
    throw new Error('Major held indicator missing for aiohttp');
  }}

  // 2. Test Filtering by Role: 'direct'
  getEl('pkg-python-role-filter').value = 'direct';
  ctx.renderPythonPackagesTable();
  let directHtml = getEl('pkg-python-tbody').innerHTML;
  if (!directHtml.includes('aiohttp') || !directHtml.includes('fastapi')) {{
    throw new Error('Direct filter failed to keep direct packages');
  }}
  if (directHtml.includes('yarl')) {{
    throw new Error('Direct filter unexpectedly kept transitive package yarl');
  }}

  // 3. Test Filtering by Role: 'updates'
  getEl('pkg-python-role-filter').value = 'updates';
  ctx.renderPythonPackagesTable();
  let updatesHtml = getEl('pkg-python-tbody').innerHTML;
  if (!updatesHtml.includes('aiohttp')) {{
    throw new Error('Updates filter failed to keep aiohttp');
  }}
  if (updatesHtml.includes('fastapi') || updatesHtml.includes('yarl')) {{
    throw new Error('Updates filter kept up-to-date packages');
  }}

  // 4. Test Search Filtering
  getEl('pkg-python-role-filter').value = 'all';
  getEl('pkg-python-search').value = 'fastapi';
  ctx.renderPythonPackagesTable();
  let searchHtml = getEl('pkg-python-tbody').innerHTML;
  if (!searchHtml.includes('fastapi')) {{
    throw new Error('Search failed to find fastapi');
  }}
  if (searchHtml.includes('aiohttp') || searchHtml.includes('yarl')) {{
    throw new Error('Search did not filter out non-matching packages');
  }}

  // 5. Test Empty Filter Result
  getEl('pkg-python-search').value = 'nonexistentpackagexyz';
  ctx.renderPythonPackagesTable();
  let emptyHtml = getEl('pkg-python-tbody').innerHTML;
  if (!emptyHtml.includes('No Python packages found matching filter criteria')) {{
    throw new Error('Empty filter state text missing');
  }}

  console.log('ALL_NODE_PYTHON_PACKAGE_MANAGER_VM_CHECKS_PASSED');
}})();
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_PYTHON_PACKAGE_MANAGER_VM_CHECKS_PASSED", res.stdout)

    def test_python_package_upgrade_actions_in_node_vm(self):
        """Execute simulatePythonPackageUpgrade and upgradePythonPackage in Node VM."""
        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  const listeners = {{}};
  const children = [];
  return {{
    id: id,
    children: children,
    firstChild: null,
    classList: {{
      add: (cls) => classes.add(cls),
      remove: (cls) => classes.delete(cls),
      contains: (cls) => classes.has(cls),
      toggle: (cls) => classes.has(cls) ? classes.delete(cls) : classes.add(cls)
    }},
    setAttribute: (k, v) => {{ attributes[k] = String(v); }},
    getAttribute: (k) => attributes[k] ?? null,
    removeAttribute: (k) => {{ delete attributes[k]; }},
    addEventListener: (evt, cb) => {{ listeners[evt] = cb; }},
    dispatchEvent: (evt) => {{ if (listeners[evt]) listeners[evt](); }},
    appendChild: (child) => {{ children.push(child); }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
    }},
    remove: () => {{}},
    querySelector: () => null,
    querySelectorAll: () => [],
    disabled: false,
    value: '',
    checked: true,
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

// Initialize mock DOM elements
getEl('toast-container');
getEl('pkg-upgrade-modal');
getEl('pkg-upgrade-modal-title');
getEl('pkg-upgrade-modal-subtitle');
getEl('pkg-upgrade-terminal');
getEl('btn-pkg-modal-install');
getEl('pkg-python-restart-toggle');
getEl('pkg-python-tbody');
getEl('pkg-python-search');
getEl('pkg-python-role-filter');
getEl('btn-upgrade-all-python');

const postCalls = [];
const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement(tag),
  querySelectorAll: () => [],
  addEventListener: () => {{}},
  body: createMockElement('body')
}};

class MockHeaders {{
  constructor(init = {{}}) {{
    this.map = {{}};
    if (init) {{
      for (const [k, v] of Object.entries(init)) {{
        this.map[k.toLowerCase()] = v;
      }}
    }}
  }}
  set(k, v) {{ this.map[k.toLowerCase()] = v; }}
  get(k) {{ return this.map[k.toLowerCase()]; }}
  has(k) {{ return k.toLowerCase() in this.map; }}
}}

const appJs = fs.readFileSync('{str(self.app_js_path)}', 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    addEventListener: () => {{}},
    confirm: () => true,
    setTimeout: (cb) => {{ if (typeof cb === 'function') cb(); return 0; }},
    clearTimeout: () => {{}},
    requestAnimationFrame: (cb) => {{ if (typeof cb === 'function') cb(); return 0; }}
  }},
  confirm: () => true,
  requestAnimationFrame: (cb) => {{ if (typeof cb === 'function') cb(); return 0; }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: (cb) => {{ if (typeof cb === 'function') cb(); return 0; }},
  clearTimeout: () => {{}},
  setInterval: () => 0,
  clearInterval: () => {{}},
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  Headers: typeof Headers !== 'undefined' ? Headers : MockHeaders,
  fetch: async (url, opts) => {{
    if (opts && opts.method === 'POST') {{
      postCalls.push({{ url, body: JSON.parse(opts.body) }});
      return {{
        ok: true,
        status: 200,
        json: async () => ({{
          success: true,
          dry_run: opts.body.includes('"dry_run":true'),
          output: "Simulated lockfile resolution OK",
          smoke_test: {{ passed: true }}
        }})
      }};
    }}
    return {{
      ok: true,
      status: 200,
      json: async () => ({{ python_version: '3.11', packages: [] }})
    }};
  }}
}});

vm.runInContext(appJs, ctx);

(async () => {{
  // 1. Test simulatePythonPackageUpgrade (single package)
  postCalls.length = 0;
  await ctx.simulatePythonPackageUpgrade('aiohttp');
  if (postCalls.length !== 1) throw new Error('Expected 1 POST call for simulation');
  if (postCalls[0].url !== '/api/packages/python/upgrade') throw new Error('Wrong simulation URL');
  if (postCalls[0].body.dry_run !== true) throw new Error('dry_run should be true in simulation');
  if (!postCalls[0].body.packages || postCalls[0].body.packages[0] !== 'aiohttp') {{
    throw new Error('Simulation target package mismatch');
  }}

  const modal = getEl('pkg-upgrade-modal');
  if (modal.classList.contains('hidden')) throw new Error('Modal should be visible after simulation');
  const title = getEl('pkg-upgrade-modal-title').textContent;
  if (!title.includes('aiohttp')) throw new Error('Modal title missing package name');

  // 2. Test upgradePythonPackage with restart toggle ON
  postCalls.length = 0;
  getEl('pkg-python-restart-toggle').checked = true;
  await ctx.upgradePythonPackage('aiohttp');
  if (postCalls.length !== 1) throw new Error('Expected 1 POST call for upgrade');
  if (postCalls[0].body.dry_run !== false) throw new Error('dry_run should be false for upgrade');
  if (postCalls[0].body.restart_service !== true) throw new Error('restart_service should be true');

  // 3. Test upgradePythonPackage with restart toggle OFF (bulk upgrade)
  postCalls.length = 0;
  getEl('pkg-python-restart-toggle').checked = false;
  await ctx.upgradePythonPackage(null);
  if (postCalls.length !== 1) throw new Error('Expected 1 POST call for bulk upgrade');
  if (postCalls[0].body.packages !== null) throw new Error('Bulk upgrade packages should be null');
  if (postCalls[0].body.restart_service !== false) throw new Error('restart_service should be false');

  console.log('ALL_NODE_PYTHON_UPGRADE_ACTIONS_VM_CHECKS_PASSED');
}})();
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM execution failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ALL_NODE_PYTHON_UPGRADE_ACTIONS_VM_CHECKS_PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
