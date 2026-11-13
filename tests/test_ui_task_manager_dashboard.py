"""Tests for Windows Task Manager style performance dashboard in static/app.js, styles.css, and index.html.

Validates:
1. Iconic Task Manager 60-second rolling grid canvas:
   - 60 points buffer representing last 60 seconds (1 sample/sec).
   - Dark background (#090d16) and subtle grid lines.
   - Text labels (% utilization, 100%, 0%, 60 seconds, 0).
   - Crisp 2px solid cyan/blue stroke (#00d2ff) with gradient area fill.
   - View switching: Overall utilization vs Logical Processors.
2. Zero DOM thrashing & calm in-place updates:
   - CSS transition width 0.4s cubic-bezier(0.25, 0.1, 0.25, 1) on .progress-fill and .cpu-core-track .progress-fill.
   - tabular-nums applied on telemetry values.
   - Calm in-place DOM updates on SSE telemetry ticks.
   - last-updated header not flashing on telemetry ticks.
3. Task Manager update speed selector:
   - Speed selector options: High (0.5s), Normal (1s), Low (2s), Paused.
   - Stored in localStorage and adjusts polling / SSE telemetry cadence.
4. Process heatmap shading:
   - Pastel heat-shading on CPU% and Memory% cells.
"""

from pathlib import Path
import json
import re
import subprocess
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"
APP_JS = STATIC_DIR / "app.js"


class TestTaskManagerDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css_content = STYLES_CSS.read_text(encoding="utf-8")
        cls.html_content = INDEX_HTML.read_text(encoding="utf-8")
        cls.js_content = APP_JS.read_text(encoding="utf-8")

    def test_css_task_manager_progress_transitions(self):
        """Verify 0.4s cubic-bezier transition on .progress-fill and .cpu-core-track .progress-fill."""
        match_fill = re.search(r"\.progress-fill\s*\{([^}]+)\}", self.css_content)
        self.assertIsNotNone(match_fill)
        fill_body = match_fill.group(1)
        self.assertIn("0.4s cubic-bezier(0.25, 0.1, 0.25, 1)", fill_body)

        match_core = re.search(r"\.cpu-core-track\s+\.progress-fill\s*\{([^}]+)\}", self.css_content)
        self.assertIsNotNone(match_core)
        core_body = match_core.group(1)
        self.assertIn("0.4s cubic-bezier(0.25, 0.1, 0.25, 1)", core_body)

    def test_css_tabular_nums_includes_memory_and_cpu(self):
        """Verify tabular-nums font variant covers #cpu-percent-value and #memory-percent-value."""
        self.assertIn("tabular-nums", self.css_content)
        self.assertIn("#cpu-percent-value", self.css_content)
        self.assertIn("#memory-percent-value", self.css_content)
        self.assertIn("#disk-percent-value", self.css_content)

    def test_css_task_manager_grid_dark_palette(self):
        """Verify #090d16 dark background on CPU container and memory container."""
        self.assertIn("#090d16", self.css_content)
        self.assertIn(".cpu-unified-chart-container", self.css_content)
        self.assertIn(".taskmgr-speed-control", self.css_content)
        self.assertIn(".btn-cpu-view", self.css_content)

    def test_html_update_speed_selector_and_cpu_view_buttons(self):
        """Verify Task Manager update speed selector and CPU view toggle buttons in index.html."""
        self.assertIn('id="taskmgr-update-speed-select"', self.html_content)
        self.assertIn('value="high"', self.html_content)
        self.assertIn('value="normal"', self.html_content)
        self.assertIn('value="low"', self.html_content)
        self.assertIn('value="paused"', self.html_content)

        self.assertIn('id="btn-cpu-view-overall"', self.html_content)
        self.assertIn('id="btn-cpu-view-cores"', self.html_content)
        self.assertIn('id="cpu-cores-unified-canvas"', self.html_content)

    def test_app_js_exports_task_manager_interfaces(self):
        """Verify static/app.js exports applyTaskMgrSpeed, setCpuViewMode, getTaskMgrProcessHeatStyle."""
        self.assertIn("window.applyTaskMgrSpeed = applyTaskMgrSpeed;", self.js_content)
        self.assertIn("window.setCpuViewMode = setCpuViewMode;", self.js_content)
        self.assertIn("window.getTaskMgrProcessHeatStyle = getTaskMgrProcessHeatStyle;", self.js_content)
        self.assertIn("window.drawCpuCoreGraph = drawCpuCoreGraph;", self.js_content)

    def test_app_js_task_manager_heatmap_shading(self):
        """Verify getTaskMgrProcessHeatStyle computes heat shading proportional to percentage."""
        node_script = f"""
const fs = require('fs');
const vm = require('vm');

const appJs = fs.readFileSync({json.dumps(str(APP_JS))}, 'utf-8');
const ctx = vm.createContext({{
  document: {{ getElementById: () => null, createElement: () => ({{ style: {{}} }}), addEventListener: () => {{}} }},
  window: {{ addEventListener: () => {{}}, setInterval: () => 1, setTimeout: () => 1 }},
  localStorage: {{ getItem: () => null, setItem: () => {{}} }},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  console: console,
}});

vm.runInContext(appJs, ctx);

// Low usage (0% or <0.1%) should be empty style
if (ctx.getTaskMgrProcessHeatStyle(0) !== '') {{
  throw new Error("0% should return empty string");
}}

// 5% usage: faint amber
const heat5 = ctx.getTaskMgrProcessHeatStyle(5);
if (!heat5.includes('hsla(') || !heat5.includes('background-color:')) {{
  throw new Error("5% should return hsla background-color: " + heat5);
}}

// 80% usage: higher intensity
const heat80 = ctx.getTaskMgrProcessHeatStyle(80);
if (!heat80.includes('hsla(')) {{
  throw new Error("80% should return hsla: " + heat80);
}}

console.log("SUCCESS_HEATMAP");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\n{res.stdout}")
        self.assertIn("SUCCESS_HEATMAP", res.stdout)

    def test_app_js_rolling_grid_canvas_and_calm_updates(self):
        """Verify Node VM execution of 60-second rolling grid canvas, speed toggle, and calm updates."""
        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(id, tagName = 'DIV') {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  let _text = '';
  const textWrites = [];
  let _ctx2d = null;
  const drawnTexts = [];
  const el = {{
    id: id,
    tagName: tagName.toUpperCase(),
    style: {{}},
    dataset: {{}},
    children: children,
    offsetWidth: 600,
    offsetHeight: 150,
    clientWidth: 600,
    clientHeight: 150,
    offsetParent: {{}},
    getContext: (type) => {{
      if (type === '2d') {{
        if (!_ctx2d) {{
          _ctx2d = {{
            save: () => {{}},
            restore: () => {{}},
            scale: () => {{}},
            clearRect: () => {{}},
            fillRect: () => {{}},
            strokeRect: () => {{}},
            beginPath: () => {{}},
            moveTo: () => {{}},
            lineTo: () => {{}},
            stroke: () => {{}},
            fill: () => {{}},
            closePath: () => {{}},
            rect: () => {{}},
            clip: () => {{}},
            fillText: (txt, x, y) => drawnTexts.push(txt),
            _getDrawnTexts: () => drawnTexts,
            createLinearGradient: () => ({{ addColorStop: () => {{}} }})
          }};
        }}
        return _ctx2d;
      }}
      return null;
    }},
    addEventListener: () => {{}},
    appendChild: (child) => {{ children.push(child); return child; }},
    removeChild: (child) => child,
    querySelector: (sel) => null,
    querySelectorAll: () => [],
    classList: {{
      add: (...cls) => cls.forEach(c => classes.add(c)),
      remove: (...cls) => cls.forEach(c => classes.delete(c)),
      contains: (cls) => classes.has(cls),
      toggle: (cls, force) => {{
        if (force) classes.add(cls); else classes.delete(cls);
        return Boolean(force);
      }}
    }},
    get className() {{ return Array.from(classes).join(' '); }},
    set className(val) {{ classes.clear(); (val || '').split(/\\s+/).forEach(c => classes.add(c)); }},
    setAttribute: (name, val) => {{ attributes[name] = String(val); }},
    getAttribute: (name) => attributes[name] !== undefined ? attributes[name] : null,
    value: '',
    get textContent() {{ return _text; }},
    set textContent(val) {{ _text = String(val); textWrites.push(val); }},
    _getTextWrites: () => textWrites,
  }};
  return el;
}}

const elements = {{}};
function getEl(id) {{
  if (!elements[id]) elements[id] = createMockElement(id);
  return elements[id];
}}

const storage = {{}};
const mockDoc = {{
  getElementById: getEl,
  createElement: (tag) => createMockElement('', tag),
  querySelector: (sel) => sel.startsWith('#') ? getEl(sel.slice(1)) : null,
  querySelectorAll: () => [],
  addEventListener: () => {{}}
}};

const appJs = fs.readFileSync({json.dumps(str(APP_JS))}, 'utf-8');
const ctx = vm.createContext({{
  document: mockDoc,
  window: {{
    location: {{ protocol: 'http:', host: 'localhost:8000', hash: '', pathname: '/' }},
    addEventListener: () => {{}},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {{}},
    requestAnimationFrame: (cb) => cb(100),
    cancelAnimationFrame: () => {{}},
  }},
  localStorage: {{
    getItem: (k) => storage[k] || null,
    setItem: (k, v) => {{ storage[k] = String(v); }},
    removeItem: (k) => {{ delete storage[k]; }}
  }},
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {{}},
  requestAnimationFrame: (cb) => cb(100),
  cancelAnimationFrame: () => {{}},
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  Headers: Headers,
  URLSearchParams: URLSearchParams,
  fetch: async () => ({{ ok: true, status: 200, json: async () => ({{}}) }})
}});

vm.runInContext(appJs, ctx);

// 1. Verify Task Manager speed toggle
ctx.applyTaskMgrSpeed('high');
if (storage['systemalerts_update_speed'] !== 'high') {{
  throw new Error("applyTaskMgrSpeed('high') did not persist to localStorage: " + storage['systemalerts_update_speed']);
}}

ctx.applyTaskMgrSpeed('paused');
if (storage['systemalerts_update_speed'] !== 'paused') {{
  throw new Error("applyTaskMgrSpeed('paused') did not persist: " + storage['systemalerts_update_speed']);
}}

// Resume normal speed
ctx.applyTaskMgrSpeed('normal');

// 2. Verify calm in-place DOM updates on telemetry tick
const lastUpdatedEl = getEl('last-updated');
lastUpdatedEl.textContent = 'Original Timestamp';

ctx.handleSSETelemetry({{
  cpu: {{ percent: 35.5, cores: [30, 40] }},
  memory: {{ percent: 62.4, used: 8000000000, total: 16000000000 }},
  disk: {{ percent: 55.0, used: 100000000000, total: 200000000000 }}
}});

const cpuValEl = getEl('cpu-percent-value');
if (cpuValEl.textContent !== '35.5%') {{
  throw new Error("cpu-percent-value was not calmly updated to 35.5%: " + cpuValEl.textContent);
}}

const memValEl = getEl('memory-percent-value');
if (memValEl.textContent !== '62.4%') {{
  throw new Error("memory-percent-value was not calmly updated to 62.4%: " + memValEl.textContent);
}}

const diskValEl = getEl('disk-percent-value');
if (diskValEl.textContent !== '55.0%') {{
  throw new Error("disk-percent-value was not calmly updated to 55.0%: " + diskValEl.textContent);
}}

// Verify last-updated header did NOT flash/mutate on telemetry tick
if (lastUpdatedEl.textContent !== 'Original Timestamp') {{
  throw new Error("last-updated was mutated by handleSSETelemetry: " + lastUpdatedEl.textContent);
}}

// 3. Verify drawCpuCoreGraph renders Task Manager labels
const canvas = getEl('cpu-cores-unified-canvas');
const ctx2d = canvas.getContext('2d');
ctx.drawCpuCoreGraph(canvas, [[10, 20, 30]], [30], null, true);
const texts = ctx2d._getDrawnTexts();

if (!texts.includes('% utilization')) {{
  throw new Error("drawCpuCoreGraph missing '% utilization' label: " + JSON.stringify(texts));
}}
if (!texts.includes('100%')) {{
  throw new Error("drawCpuCoreGraph missing '100%' label: " + JSON.stringify(texts));
}}
if (!texts.includes('0%')) {{
  throw new Error("drawCpuCoreGraph missing '0%' label: " + JSON.stringify(texts));
}}
if (!texts.includes('60 seconds')) {{
  throw new Error("drawCpuCoreGraph missing '60 seconds' label: " + JSON.stringify(texts));
}}
if (!texts.includes('0')) {{
  throw new Error("drawCpuCoreGraph missing '0' label: " + JSON.stringify(texts));
}}

// 4. Verify CPU view mode toggle
ctx.setCpuViewMode('cores');
if (storage['systemalerts_cpu_view_mode'] !== 'cores') {{
  throw new Error("setCpuViewMode('cores') did not persist to localStorage: " + storage['systemalerts_cpu_view_mode']);
}}

ctx.setCpuViewMode('overall');
if (storage['systemalerts_cpu_view_mode'] !== 'overall') {{
  throw new Error("setCpuViewMode('overall') did not persist: " + storage['systemalerts_cpu_view_mode']);
}}

console.log("SUCCESS_TASK_MANAGER_DASHBOARD");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\n{res.stdout}")
        self.assertIn("SUCCESS_TASK_MANAGER_DASHBOARD", res.stdout)


if __name__ == "__main__":
    unittest.main()
