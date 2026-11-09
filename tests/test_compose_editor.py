from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import api
import main
from test_main import asgi_post


class TestComposeCanonicalFormatterBackend(unittest.TestCase):
    """Backend unit & API integration tests for canonical Compose YAML and .env formatting."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_compose_editor.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("editoradmin", "editorpass123", email="editor@test.local")
        self.session_token = main.create_session("editoradmin")
        self.auth_cookies = {"session_token": self.session_token}
        self.dm = main.DockerManager()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_format_compose_yaml_converts_tabs_and_strips_trailing_whitespace(self):
        raw_yaml = (
            "services:   \r\n"
            "\tweb:  \r\n"
            "\t\timage: nginx:alpine   \r\n"
            "\t\tports:\t  \r\n"
            "\t\t\t- '8080:80'   \r\n"
        )
        res = self.dm.format_compose_yaml(raw_yaml)
        self.assertTrue(res.get("valid"), f"Expected valid formatted YAML, got: {res}")
        formatted = res.get("formatted_yaml", "")
        self.assertNotIn("\t", formatted)
        self.assertNotIn("\r", formatted)
        for line in formatted.splitlines():
            self.assertEqual(line, line.rstrip(), f"Line should have no trailing whitespace: {line!r}")

    def test_format_compose_yaml_canonical_top_level_and_service_key_order(self):
        unformatted = """
x-logging:
  driver: json-file
secrets:
  db_secret:
    file: ./secret.txt
configs:
  app_cfg:
    file: ./app.conf
volumes:
  db_data: {}
networks:
  backend:
    driver: bridge
services:
  api:
    custom_key: custom_val
    deploy:
      replicas: 2
    logging:
      driver: json-file
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost"]
    command: ["npm", "start"]
    depends_on:
      - db
    networks:
      - backend
    volumes:
      - db_data:/var/lib/data
    environment:
      - NODE_ENV=production
    ports:
      - "3000:3000"
    restart: unless-stopped
    container_name: api_svc
    build: ./api
    image: node:20-alpine
name: my-stack
version: '3.9'
"""
        res = self.dm.format_compose_yaml(unformatted)
        self.assertTrue(res.get("valid"), f"Expected valid YAML, got: {res}")
        formatted = res["formatted_yaml"]
        parsed = yaml.safe_load(formatted)

        # Verify top-level key ordering
        top_keys = list(parsed.keys())
        expected_top_order = [
            "version",
            "name",
            "services",
            "networks",
            "volumes",
            "configs",
            "secrets",
            "x-logging",
        ]
        self.assertEqual(top_keys, expected_top_order)

        # Verify service-level key ordering
        svc_keys = list(parsed["services"]["api"].keys())
        expected_svc_order = [
            "image",
            "build",
            "container_name",
            "restart",
            "ports",
            "environment",
            "volumes",
            "networks",
            "depends_on",
            "command",
            "healthcheck",
            "logging",
            "deploy",
            "custom_key",
        ]
        self.assertEqual(svc_keys, expected_svc_order)

    def test_format_env_content_canonical_normalization(self):
        raw_env = (
            "\r\n"
            "  # Database Configuration   \r\n"
            "  POSTGRES_USER = admin   \r\n"
            "POSTGRES_PASSWORD = supersecret  \r\n"
            "\r\n"
            "\r\n"
            "\r\n"
            "# App Port  \r\n"
            "  APP_PORT = 8080   \r\n"
            "\r\n"
            "\r\n"
        )
        formatted_env = self.dm.format_env_content(raw_env)
        expected_env = (
            "  # Database Configuration\n"
            "POSTGRES_USER=admin\n"
            "POSTGRES_PASSWORD=supersecret\n"
            "\n"
            "# App Port\n"
            "APP_PORT=8080\n"
        )
        self.assertEqual(formatted_env, expected_env)
        self.assertEqual(self.dm.format_env_content(""), "")
        self.assertEqual(self.dm.format_env_content("   \n\t  "), "")

    def test_format_compose_yaml_broken_syntax_returns_1_indexed_line_and_col(self):
        broken_yaml = "services:\n  web:\n    image: nginx:alpine\n    ports: [unclosed_bracket\n"
        res = self.dm.format_compose_yaml(broken_yaml)
        self.assertFalse(res.get("valid"))
        self.assertTrue(len(res.get("errors", [])) > 0)
        self.assertIsInstance(res.get("error_line"), int)
        self.assertIsInstance(res.get("error_col"), int)
        self.assertGreaterEqual(res["error_line"], 1)
        self.assertGreaterEqual(res["error_col"], 1)

    def test_api_format_endpoint_auth_and_payload_modes(self):
        # 1. Unauthenticated request returns 401
        unauth_resp = asgi_post(
            api.app,
            "/api/docker/stacks/format",
            json_body={"compose_yaml": "services:\n  app:\n    image: nginx\n"},
        )
        self.assertEqual(unauth_resp.status_code, 401)

        # 2. Full Compose + .env formatting
        full_resp = asgi_post(
            api.app,
            "/api/docker/stacks/format",
            cookies=self.auth_cookies,
            json_body={
                "compose_yaml": "services:\n\tapp:\n\t\tports:\n\t\t\t- '8080:80'\n\t\timage: redis:alpine\n",
                "env_content": "  REDIS_HOST = localhost  \n\n\n  REDIS_PORT = 6379  \n",
            },
        )
        self.assertEqual(full_resp.status_code, 200)
        full_data = full_resp.json()
        self.assertTrue(full_data["valid"])
        self.assertLess(
            full_data["formatted_yaml"].index("image:"),
            full_data["formatted_yaml"].index("ports:"),
        )
        self.assertEqual(
            full_data["formatted_env"],
            "REDIS_HOST=localhost\n\nREDIS_PORT=6379\n",
        )
        self.assertEqual(full_data["services"], ["app"])
        self.assertEqual(full_data["ports"], ["8080:80"])

        # 3. .env-only formatting when compose_yaml is empty
        env_only_resp = asgi_post(
            api.app,
            "/api/docker/stacks/format",
            cookies=self.auth_cookies,
            json_body={
                "compose_yaml": "   ",
                "env_content": "  API_KEY = abcdef123  \r\n",
            },
        )
        self.assertEqual(env_only_resp.status_code, 200)
        env_only_data = env_only_resp.json()
        self.assertTrue(env_only_data["valid"])
        self.assertEqual(env_only_data["formatted_yaml"], "")
        self.assertEqual(env_only_data["formatted_env"], "API_KEY=abcdef123\n")


class TestComposeEditorFrontendContractsAndNodeVM(unittest.TestCase):
    """Frontend DOM, CSS, and Node VM execution tests for Ace Code Editor & Compose Formatter."""

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.index_html = (self.root / "static" / "index.html").read_text(encoding="utf-8")
        self.styles_css = (self.root / "static" / "styles.css").read_text(encoding="utf-8")
        self.app_js_path = self.root / "static" / "app.js"

    def test_index_html_dom_contract(self):
        # Ace CDN script tags
        for script_name in [
            "ace.min.js",
            "mode-yaml.min.js",
            "mode-sh.min.js",
            "theme-one_dark.min.js",
        ]:
            self.assertIn(script_name, self.index_html, f"Missing {script_name} script tag in index.html")

        # Required DOM IDs
        required_ids = [
            "stack-format-btn",
            "stack-env-format-btn",
            "stack-snippet-toolbar",
            "stack-yaml-ace-editor",
            "stack-env-ace-editor",
            "stack-editor-statusbar",
            "stack-editor-cursor-pos",
            "stack-editor-indent-info",
            "stack-editor-mode-info",
        ]
        for dom_id in required_ids:
            self.assertIn(f'id="{dom_id}"', self.index_html, f"Missing #{dom_id} in index.html")

        # All 6 snippet buttons
        for snippet_type in ["service", "ports", "volumes", "environment", "healthcheck", "network"]:
            self.assertIn(
                f'data-snippet="{snippet_type}"',
                self.index_html,
                f"Missing snippet button data-snippet=\"{snippet_type}\" in index.html",
            )

    def test_styles_css_contract(self):
        for selector in [
            ".ace-editor-container",
            ".snippet-toolbar",
            ".snippet-chip-btn",
            ".editor-statusbar",
        ]:
            self.assertIn(selector, self.styles_css, f"Missing CSS selector {selector} in styles.css")

    def test_ace_editor_initialization_and_bidirectional_sync_in_node_vm(self):
        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  const listeners = {{}};
  return {{
    id: id,
    style: {{}},
    children: [],
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (c) => c,
    removeChild: (c) => c,
    addEventListener: (evt, cb) => {{ listeners[evt] = cb; }},
    dispatchEvent: (evt) => {{ if (listeners[evt.type]) listeners[evt.type](evt); }},
    focus: () => {{}},
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
    value: '',
    disabled: false,
    checked: true,
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) elements[id] = createMockElement(id);
  return elements[id];
}}

const aceInstances = {{}};
function createMockAceInstance(id) {{
  let val = '';
  let theme = '';
  let mode = '';
  let options = {{}};
  let annotations = [];
  let cursor = {{ row: 0, column: 0 }};
  const sessionListeners = {{}};
  const selectionListeners = {{}};
  const commands = [];

  const instance = {{
    id,
    setTheme: (t) => {{ theme = t; }},
    getTheme: () => theme,
    setOptions: (opts) => {{ Object.assign(options, opts); }},
    getOptions: () => options,
    getValue: () => val,
    setValue: (v) => {{
      val = String(v ?? '');
      if (sessionListeners.change) sessionListeners.change();
    }},
    resize: () => {{}},
    focus: () => {{}},
    getCursorPosition: () => cursor,
    setCursorPosition: (r, c) => {{
      cursor = {{ row: r, column: c }};
      if (selectionListeners.changeCursor) selectionListeners.changeCursor();
    }},
    session: {{
      setMode: (m) => {{ mode = m; }},
      getMode: () => mode,
      on: (evt, cb) => {{ sessionListeners[evt] = cb; }},
      setAnnotations: (ann) => {{ annotations = ann; }},
      getAnnotations: () => annotations,
    }},
    selection: {{
      on: (evt, cb) => {{ selectionListeners[evt] = cb; }},
    }},
    commands: {{
      addCommand: (cmd) => {{ commands.push(cmd); }},
      getCommands: () => commands,
    }}
  }};
  aceInstances[id] = instance;
  return instance;
}}

const mockWindow = {{
  addEventListener: () => {{}},
  clearTimeout: clearTimeout,
  setTimeout: setTimeout,
  ace: {{
    edit: (id) => createMockAceInstance(id),
  }},
}};

const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement(tag),
  querySelectorAll: () => [],
  addEventListener: () => {{}},
}};

const appJs = fs.readFileSync({str(self.app_js_path)!r}, 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: mockWindow,
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: () => 0,
  clearInterval: () => {{}},
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  Headers: typeof Headers !== 'undefined' ? Headers : class MockHeaders {{}},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }}),
}});

vm.runInContext(appJs, ctx);

// Pre-populate textareas before init
getEl('stack-yaml-input').value = 'services:\\n  app:\\n    image: nginx\\n';
getEl('stack-env-input').value = 'PORT=8080\\n';

const initialized = ctx.initStackCodeEditors();
if (!initialized) throw new Error('Expected initStackCodeEditors() to return true');

const yamlAce = aceInstances['stack-yaml-ace-editor'];
const envAce = aceInstances['stack-env-ace-editor'];
if (!yamlAce || !envAce) throw new Error('Expected both Ace instances to be created');

if (yamlAce.getTheme() !== 'ace/theme/one_dark') throw new Error('Wrong YAML Ace theme: ' + yamlAce.getTheme());
if (yamlAce.session.getMode() !== 'ace/mode/yaml') throw new Error('Wrong YAML Ace mode: ' + yamlAce.session.getMode());
if (envAce.getTheme() !== 'ace/theme/one_dark') throw new Error('Wrong ENV Ace theme: ' + envAce.getTheme());
if (envAce.session.getMode() !== 'ace/mode/sh') throw new Error('Wrong ENV Ace mode: ' + envAce.session.getMode());

const opts = yamlAce.getOptions();
if (opts.tabSize !== 2 || opts.useSoftTabs !== true || opts.displayIndentGuides !== true) {{
  throw new Error('Unexpected Ace options: ' + JSON.stringify(opts));
}}

if (!getEl('stack-yaml-editor-wrapper').classList.contains('ace-active')) {{
  throw new Error('Expected #stack-yaml-editor-wrapper to have ace-active class');
}}
if (!getEl('stack-env-editor-wrapper').classList.contains('ace-active')) {{
  throw new Error('Expected #stack-env-editor-wrapper to have ace-active class');
}}

// Verify bidirectional sync: Ace change -> textarea.value
yamlAce.setValue('services:\\n  db:\\n    image: postgres:16\\n');
if (getEl('stack-yaml-input').value !== 'services:\\n  db:\\n    image: postgres:16\\n') {{
  throw new Error('Ace -> textarea sync failed for YAML');
}}

envAce.setValue('POSTGRES_DB=vigil\\n');
if (getEl('stack-env-input').value !== 'POSTGRES_DB=vigil\\n') {{
  throw new Error('Ace -> textarea sync failed for ENV');
}}

// Verify textarea direct mutation -> getStackYamlValue() / getStackEnvValue() syncs Ace
getEl('stack-yaml-input').value = 'services:\\n  redis:\\n    image: redis:alpine\\n';
if (ctx.getStackYamlValue() !== 'services:\\n  redis:\\n    image: redis:alpine\\n') {{
  throw new Error('Textarea -> getStackYamlValue() sync failed');
}}
if (yamlAce.getValue() !== 'services:\\n  redis:\\n    image: redis:alpine\\n') {{
  throw new Error('Textarea -> Ace sync failed');
}}

// Verify cursor statusbar update
yamlAce.setCursorPosition(4, 2);
if (getEl('stack-editor-cursor-pos').textContent !== 'Ln 5, Col 3') {{
  throw new Error('Expected statusbar Ln 5, Col 3, got: ' + getEl('stack-editor-cursor-pos').textContent);
}}

console.log('ACE_INIT_AND_SYNC_PASSED');
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("ACE_INIT_AND_SYNC_PASSED", res.stdout)

    def test_format_stack_yaml_and_snippets_and_keydown_in_node_vm(self):
        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  return {{
    id: id,
    style: {{}},
    children: [],
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (c) => c,
    removeChild: (c) => c,
    addEventListener: () => {{}},
    focus: () => {{}},
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
    value: '',
    selectionStart: 0,
    selectionEnd: 0,
    disabled: false,
    checked: true,
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) elements[id] = createMockElement(id);
  return elements[id];
}}

const aceInstances = {{}};
function createMockAceInstance(id) {{
  let val = '';
  let annotations = [];
  const sessionListeners = {{}};
  const instance = {{
    id,
    setTheme: () => {{}},
    setOptions: () => {{}},
    getValue: () => val,
    setValue: (v) => {{
      val = String(v ?? '');
      if (sessionListeners.change) sessionListeners.change();
    }},
    resize: () => {{}},
    focus: () => {{}},
    getCursorPosition: () => ({{ row: 0, column: 0 }}),
    session: {{
      setMode: () => {{}},
      on: (evt, cb) => {{ sessionListeners[evt] = cb; }},
      setAnnotations: (ann) => {{ annotations = ann; }},
      getAnnotations: () => annotations,
    }},
    selection: {{ on: () => {{}} }},
    commands: {{ addCommand: () => {{}} }}
  }};
  aceInstances[id] = instance;
  return instance;
}}

let lastFetchUrl = null;
let lastFetchBody = null;
let mockFormatResponse = {{
  valid: true,
  formatted_yaml: 'services:\\n  web:\\n    image: nginx:alpine\\n    ports:\\n      - 8080:80\\n',
  formatted_env: 'PORT=8080\\n',
  services: ['web'],
  ports: ['8080:80'],
  volumes: [],
  warnings: []
}};

const mockWindow = {{
  addEventListener: () => {{}},
  clearTimeout: clearTimeout,
  setTimeout: setTimeout,
  ace: {{ edit: (id) => createMockAceInstance(id) }},
}};

const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement(tag),
  querySelectorAll: () => [],
  addEventListener: () => {{}},
}};

const appJs = fs.readFileSync({str(self.app_js_path)!r}, 'utf-8');

(async () => {{
  const ctx = vm.createContext({{
    document: mockDoc,
    window: mockWindow,
    localStorage: {{ getItem: () => null, setItem: () => {{}} }},
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {{}},
    WebSocket: class MockWS {{}},
    TextDecoder: TextDecoder,
    TextEncoder: TextEncoder,
    URLSearchParams: URLSearchParams,
    Headers: typeof Headers !== 'undefined' ? Headers : class MockHeaders {{}},
    fetch: async (url, opts) => {{
      lastFetchUrl = url;
      lastFetchBody = opts && opts.body ? JSON.parse(opts.body) : null;
      return {{
        ok: true,
        status: 200,
        json: async () => mockFormatResponse,
      }};
    }},
  }});

  vm.runInContext(appJs, ctx);
  ctx.initStackCodeEditors();

  // 1. Test formatStackYaml() on valid YAML
  ctx.setStackYamlValue('services:\\n\\tweb:\\n\\t\\tports:\\n\\t\\t\\t- 8080:80\\n\\t\\timage: nginx:alpine\\n');
  ctx.setStackEnvValue('  PORT = 8080  \\n');
  await ctx.formatStackYaml();

  if (lastFetchUrl !== '/api/docker/stacks/format') {{
    throw new Error('Expected POST /api/docker/stacks/format, got: ' + lastFetchUrl);
  }}
  if (getEl('stack-yaml-input').value !== mockFormatResponse.formatted_yaml) {{
    throw new Error('YAML input was not updated with formatted_yaml');
  }}
  if (getEl('stack-env-input').value !== mockFormatResponse.formatted_env) {{
    throw new Error('ENV input was not updated with formatted_env');
  }}
  const statusHtml = getEl('stack-validation-status').innerHTML;
  if (!statusHtml.includes('Formatted &amp; Valid Compose') && !statusHtml.includes('Formatted & Valid Compose')) {{
    throw new Error('Expected Formatted & Valid Compose badge in statusBox, got: ' + statusHtml);
  }}
  if (!statusHtml.includes('web') || !statusHtml.includes('8080:80')) {{
    throw new Error('Expected service and port pills in statusBox');
  }}

  // 2. Test formatStackYaml() on invalid YAML sets Ace gutter error annotation (0-indexed row = error_line - 1)
  mockFormatResponse = {{
    valid: false,
    errors: ['Invalid YAML syntax on line 4'],
    error_line: 4,
    error_col: 7,
  }};
  await ctx.formatStackYaml();

  const annotations = aceInstances['stack-yaml-ace-editor'].session.getAnnotations();
  if (!annotations || annotations.length !== 1) {{
    throw new Error('Expected 1 Ace error annotation, got: ' + JSON.stringify(annotations));
  }}
  if (annotations[0].row !== 3 || annotations[0].column !== 6 || annotations[0].type !== 'error') {{
    throw new Error('Expected row 3, column 6, type error annotation, got: ' + JSON.stringify(annotations[0]));
  }}
  if (!getEl('stack-validation-status').innerHTML.includes('Line 4, Col 7')) {{
    throw new Error('Expected Line 4, Col 7 in validation status box');
  }}

  // 3. Test insertComposeSnippet() for all 6 snippet types
  const snippetTypes = ['service', 'ports', 'volumes', 'environment', 'healthcheck', 'network'];
  for (const sType of snippetTypes) {{
    ctx.setStackYamlValue('');
    mockFormatResponse = {{
      valid: true,
      formatted_yaml: '',
      formatted_env: '',
      services: ['app'],
      ports: [],
      volumes: [],
      warnings: []
    }};
    ctx.insertComposeSnippet(sType);
    const inserted = getEl('stack-yaml-input').value;
    if (!inserted || !inserted.includes('services:')) {{
      throw new Error(`Snippet ${{sType}} did not produce valid services root: ` + inserted);
    }}
  }}

  // 4. Test handleComposeTextareaKeydown() keyboard ergonomics
  const ta = createMockElement('test-ta');
  ta.value = 'services:';
  ta.selectionStart = ta.selectionEnd = 9;

  // Enter after ':' -> auto-indents by 2 spaces
  ctx.handleComposeTextareaKeydown({{
    target: ta,
    key: 'Enter',
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    preventDefault: () => {{}},
  }});
  if (ta.value !== 'services:\\n  ') {{
    throw new Error('Enter auto-indent after colon failed: ' + JSON.stringify(ta.value));
  }}

  // Tab -> inserts 2 spaces
  ctx.handleComposeTextareaKeydown({{
    target: ta,
    key: 'Tab',
    shiftKey: false,
    preventDefault: () => {{}},
  }});
  if (ta.value !== 'services:\\n    ') {{
    throw new Error('Tab 2-space indent failed: ' + JSON.stringify(ta.value));
  }}

  // Shift+Tab -> unindents 2 spaces on current line
  ctx.handleComposeTextareaKeydown({{
    target: ta,
    key: 'Tab',
    shiftKey: true,
    preventDefault: () => {{}},
  }});
  if (ta.value !== 'services:\\n  ') {{
    throw new Error('Shift+Tab unindent failed: ' + JSON.stringify(ta.value));
  }}

  // Ctrl+/ -> toggles '# ' comment on current line
  ta.value = '  image: nginx:alpine';
  ta.selectionStart = ta.selectionEnd = 5;
  ctx.handleComposeTextareaKeydown({{
    target: ta,
    key: '/',
    ctrlKey: true,
    metaKey: false,
    preventDefault: () => {{}},
  }});
  if (ta.value !== '  # image: nginx:alpine') {{
    throw new Error('Ctrl+/ comment failed: ' + JSON.stringify(ta.value));
  }}

  // Ctrl+/ again -> uncomments line
  ctx.handleComposeTextareaKeydown({{
    target: ta,
    key: '/',
    ctrlKey: true,
    metaKey: false,
    preventDefault: () => {{}},
  }});
  if (ta.value !== '  image: nginx:alpine') {{
    throw new Error('Ctrl+/ uncomment failed: ' + JSON.stringify(ta.value));
  }}

  console.log('FORMAT_SNIPPETS_AND_KEYDOWN_PASSED');
}})();
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node VM failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}")
        self.assertIn("FORMAT_SNIPPETS_AND_KEYDOWN_PASSED", res.stdout)

    def test_sequential_and_repeated_snippet_insertions_roundtrip_with_backend_formatter(self):
        """Verify repeated and sequential snippet clicks round-trip cleanly with DockerManager.format_compose_yaml
        and openStackEditor() unhides #stack-editor-modal before initializing Ace editors."""
        import json

        dm = main.DockerManager()

        node_driver = f"""
const fs = require('fs');
const vm = require('vm');
const readline = require('readline');

function createMockElement(id) {{
  const classes = new Set();
  const attributes = {{}};
  return {{
    id: id,
    style: {{}},
    children: [],
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (c) => c,
    removeChild: (c) => c,
    addEventListener: () => {{}},
    focus: () => {{}},
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
    value: '',
    disabled: false,
    checked: true,
    innerHTML: '',
    textContent: ''
  }};
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) elements[id] = createMockElement(id);
  return elements[id];
}}

// Start with modal hidden to verify openStackEditor() unhides it before initStackCodeEditors()
getEl('stack-editor-modal').classList.add('hidden');
let modalWasHiddenDuringAceInit = null;

function createMockAceInstance(id) {{
  if (modalWasHiddenDuringAceInit === null) {{
    modalWasHiddenDuringAceInit = getEl('stack-editor-modal').classList.contains('hidden');
  }}
  let val = '';
  const sessionListeners = {{}};
  return {{
    id,
    setTheme: () => {{}},
    setOptions: () => {{}},
    getValue: () => val,
    setValue: (v) => {{
      val = String(v ?? '');
      if (sessionListeners.change) sessionListeners.change();
    }},
    resize: () => {{}},
    focus: () => {{}},
    getCursorPosition: () => ({{ row: 0, column: 0 }}),
    session: {{
      setMode: () => {{}},
      on: (evt, cb) => {{ sessionListeners[evt] = cb; }},
      setAnnotations: () => {{}},
    }},
    selection: {{ on: () => {{}} }},
    commands: {{ addCommand: () => {{}} }}
  }};
}}

const ctx = vm.createContext({{
  document: {{
    getElementById: getEl,
    createElement: (tag) => createMockElement(tag),
    querySelectorAll: () => [],
    addEventListener: () => {{}},
  }},
  window: {{
    addEventListener: () => {{}},
    clearTimeout: clearTimeout,
    setTimeout: setTimeout,
    ace: {{ edit: (id) => createMockAceInstance(id) }},
  }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: () => 0,
  clearInterval: () => {{}},
  WebSocket: class MockWS {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  URLSearchParams: URLSearchParams,
  Headers: typeof Headers !== 'undefined' ? Headers : class MockHeaders {{}},
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{ valid: true }}) }}),
}});

const appJs = fs.readFileSync({str(self.app_js_path)!r}, 'utf-8');
vm.runInContext(appJs, ctx);

const rl = readline.createInterface({{ input: process.stdin, output: process.stdout, terminal: false }});
rl.on('line', async (line) => {{
  if (!line.trim()) return;
  const req = JSON.parse(line);
  if (req.cmd === 'openStackEditor') {{
    await ctx.openStackEditor();
    process.stdout.write(JSON.stringify({{
      modalWasHiddenDuringAceInit,
      modalHiddenNow: getEl('stack-editor-modal').classList.contains('hidden'),
    }}) + '\\n');
  }} else if (req.cmd === 'applySnippet') {{
    const out = ctx.applySnippetToComposeYaml(req.yaml, req.snippetType);
    process.stdout.write(JSON.stringify({{ yaml: out }}) + '\\n');
  }}
}});
"""
        proc = subprocess.Popen(
            ["node", "-e", node_driver],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            def send_cmd(payload: dict) -> dict:
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    err = proc.stderr.read() if proc.stderr else ""
                    self.fail(f"Node VM exited unexpectedly. STDERR:\n{err}")
                return json.loads(line)

            # Verify openStackEditor() unhides #stack-editor-modal BEFORE initStackCodeEditors()
            open_res = send_cmd({"cmd": "openStackEditor"})
            self.assertFalse(
                open_res["modalWasHiddenDuringAceInit"],
                "Expected #stack-editor-modal to be unhidden before initStackCodeEditors() calls ace.edit()",
            )
            self.assertFalse(open_res["modalHiddenNow"])

            def click_snippet_and_format(current_yaml: str, snippet_type: str) -> tuple[str, dict, dict]:
                step = send_cmd({"cmd": "applySnippet", "yaml": current_yaml, "snippetType": snippet_type})
                raw_updated = step["yaml"]
                fmt_res = dm.format_compose_yaml(raw_updated)
                self.assertTrue(
                    fmt_res.get("valid"),
                    f"Snippet {snippet_type!r} produced invalid YAML:\n{raw_updated}\nErrors: {fmt_res.get('errors')}",
                )
                formatted_yaml = fmt_res["formatted_yaml"]
                parsed_doc = yaml.safe_load(formatted_yaml)
                return formatted_yaml, fmt_res, parsed_doc

            yaml_text = ""

            # 1. Click + Service -> creates `app` with 8080:80
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "service")
            self.assertEqual(fmt["services"], ["app"])
            self.assertEqual(fmt["ports"], ["8080:80"])

            # 2. Click + Port on service that already has ports: -> adds 8081:80 without overwriting 8080:80
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "ports")
            self.assertEqual(fmt["ports"], ["8080:80", "8081:80"])
            self.assertEqual(parsed["services"]["app"]["ports"], ["8080:80", "8081:80"])

            # 3. Click + Port again -> adds 8082:80
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "ports")
            self.assertEqual(fmt["ports"], ["8080:80", "8081:80", "8082:80"])

            # 4. Click + Volume twice -> adds ./data:/data and ./config:/config
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "volumes")
            self.assertEqual(parsed["services"]["app"]["volumes"], ["./data:/data"])
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "volumes")
            self.assertEqual(parsed["services"]["app"]["volumes"], ["./data:/data", "./config:/config"])

            # 5. Click + Env twice -> adds TZ=UTC, NODE_ENV=production, and PUID=1000
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "environment")
            self.assertEqual(parsed["services"]["app"]["environment"], ["TZ=UTC", "NODE_ENV=production"])
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "environment")
            self.assertEqual(
                parsed["services"]["app"]["environment"],
                ["TZ=UTC", "NODE_ENV=production", "PUID=1000"],
            )

            # 6. Click + Network twice -> creates top-level networks: (app-net, app-net-2) and attaches to service
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "network")
            self.assertEqual(list(parsed["networks"].keys()), ["app-net"])
            self.assertEqual(parsed["services"]["app"]["networks"], ["app-net"])

            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "network")
            self.assertEqual(list(parsed["networks"].keys()), ["app-net", "app-net-2"])
            self.assertEqual(parsed["services"]["app"]["networks"], ["app-net", "app-net-2"])
            # Confirm top-level networks: is at the bottom of the file
            self.assertLess(yaml_text.index("services:"), yaml_text.index("networks:"))

            # 7. Click + Service again AFTER networks: is at the bottom -> inserts app-service inside services:
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "service")
            self.assertEqual(fmt["services"], ["app", "app-service"])
            self.assertEqual(list(parsed["networks"].keys()), ["app-net", "app-net-2"])
            self.assertNotIn("app-service", parsed["networks"])
            self.assertEqual(parsed["services"]["app-service"]["ports"], ["8083:80"])

            # 8. Click + Port, + Volume, + Env, + Healthcheck after networks: is at the bottom -> targets app-service
            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "ports")
            self.assertEqual(parsed["services"]["app-service"]["ports"], ["8083:80", "8084:80"])

            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "volumes")
            self.assertEqual(parsed["services"]["app-service"]["volumes"], ["./data:/data"])

            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "environment")
            self.assertEqual(
                parsed["services"]["app-service"]["environment"],
                ["TZ=UTC", "NODE_ENV=production"],
            )

            yaml_text, fmt, parsed = click_snippet_and_format(yaml_text, "healthcheck")
            self.assertIn("healthcheck", parsed["services"]["app-service"])
            self.assertEqual(
                parsed["services"]["app-service"]["healthcheck"]["test"],
                ["CMD", "curl", "-f", "http://localhost"],
            )
            self.assertEqual(fmt["services"], ["app", "app-service"])
            self.assertEqual(list(parsed["networks"].keys()), ["app-net", "app-net-2"])
        finally:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    unittest.main()
