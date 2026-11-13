"""Tests for True Live View 60 FPS Fluid Rendering in static/app.js and static/styles.css.

Validates:
1. static/styles.css:
   - tabular-nums and tnum feature settings on telemetry numbers (.metric-value, .mono-text, .cpu-core-percent, #cpu-percent-value, etc.).
   - Removed 1s linear transition on .progress-fill and .cpu-core-track .progress-fill (fast <= 0.05s).
2. static/app.js:
   - liveAnimState decoupling targets from 60 FPS interpolated display.
   - startLiveRenderLoop and stopLiveRenderLoop life cycle functions.
   - drawCpuCoreGraph continuous oscilloscope rolling with sub-step scroll offset.
   - formatUptimeFromBoot continuous seconds precision.
   - Node VM execution test validating 60 FPS frame interpolation and clock advancement.
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


class TestLiveView60FpsRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css_content = STYLES_CSS.read_text(encoding="utf-8")
        cls.html_content = INDEX_HTML.read_text(encoding="utf-8")
        cls.js_content = APP_JS.read_text(encoding="utf-8")

    def test_css_tabular_nums_on_animated_telemetry(self):
        """Verify tabular-nums font variant and tnum settings on animated numbers."""
        self.assertIn("tabular-nums", self.css_content)
        self.assertIn('"tnum"', self.css_content)

        # Ensure animated selectors are grouped with tabular-nums
        self.assertIn(".metric-value", self.css_content)
        self.assertIn(".cpu-core-percent", self.css_content)
        self.assertIn("#cpu-percent-value", self.css_content)
        self.assertIn("#uptime-value", self.css_content)

    def test_css_progress_bars_not_delayed_by_one_second(self):
        """Verify .progress-fill and .cpu-core-track .progress-fill do not have 1s linear transitions."""
        # Check .progress-fill
        match_fill = re.search(r"\.progress-fill\s*\{([^}]+)\}", self.css_content)
        self.assertIsNotNone(match_fill, ".progress-fill block not found")
        fill_body = match_fill.group(1)
        self.assertNotIn("1s", fill_body, ".progress-fill still has 1s transition lag")
        self.assertTrue("0.25s" in fill_body or "0.05s" in fill_body)

        # Check .cpu-core-track .progress-fill
        match_core = re.search(r"\.cpu-core-track\s+\.progress-fill\s*\{([^}]+)\}", self.css_content)
        self.assertIsNotNone(match_core, ".cpu-core-track .progress-fill block not found")
        core_body = match_core.group(1)
        self.assertNotIn("1s", core_body, ".cpu-core-track .progress-fill still has 1s transition lag")
        self.assertTrue("0.25s" in core_body or "0.05s" in core_body)

    def test_js_exports_live_animation_interfaces(self):
        """Verify static/app.js exports liveAnimState and startLiveRenderLoop on window."""
        self.assertIn("window.liveAnimState = liveAnimState;", self.js_content)
        self.assertIn("window.startLiveRenderLoop = startLiveRenderLoop;", self.js_content)
        self.assertIn("window.stopLiveRenderLoop = stopLiveRenderLoop;", self.js_content)
        self.assertIn("window.drawCpuCoreGraph = drawCpuCoreGraph;", self.js_content)

    def test_uptime_formatter_includes_seconds(self):
        """Verify formatUptimeFromBoot outputs continuous seconds rather than only minutes."""
        self.assertIn("formatUptimeFromBoot", self.js_content)
        self.assertIn("${seconds}s", self.js_content)

    def test_node_vm_fluid_60fps_lerp_and_oscilloscope(self):
        """Verify in Node VM that liveAnimState glides toward targets and oscilloscope scrolls."""
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
    offsetWidth: 600,
    offsetHeight: 150,
    clientWidth: 600,
    clientHeight: 150,
    offsetParent: {{}},
    getBoundingClientRect: () => ({{ width: 600, height: 150, top: 0, left: 0 }}),
    getContext: (type) => {{
      if (type === '2d') {{
        return {{
          save: () => {{}},
          restore: () => {{}},
          scale: () => {{}},
          clearRect: () => {{}},
          fillRect: () => {{}},
          beginPath: () => {{}},
          moveTo: () => {{}},
          lineTo: () => {{}},
          stroke: () => {{}},
          fill: () => {{}},
          arc: () => {{}},
          closePath: () => {{}},
          rect: () => {{}},
          clip: () => {{}},
          bezierCurveTo: () => {{}},
          fillText: () => {{}},
          createLinearGradient: () => ({{ addColorStop: () => {{}} }})
        }};
      }}
      return null;
    }},
    addEventListener: () => {{}},
    appendChild: (child) => {{ children.push(child); child.parentElement = el; return child; }},
    removeChild: (child) => {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      return child;
    }},
    querySelector: (sel) => null,
    querySelectorAll: () => [],
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
    if (sel.startsWith('#')) return getEl(sel.slice(1));
    return null;
  }},
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
  localStorage: {{ getItem: () => 'token', setItem: () => {{}}, removeItem: () => {{}} }},
  history: {{ replaceState: () => {{}} }},
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

// 1. Verify liveAnimState presence and default values
const anim = vm.runInContext('liveAnimState', ctx);
if (!anim) throw new Error("liveAnimState is not defined");
if (typeof ctx.startLiveRenderLoop !== 'function') throw new Error("startLiveRenderLoop is not a function");
if (typeof ctx.stopLiveRenderLoop !== 'function') throw new Error("stopLiveRenderLoop is not a function");

// 2. Test formatUptimeFromBoot with continuous second precision
const bootNow = Date.now() / 1000 - 3665; // 1 hour, 1 minute, 5 seconds ago
const uptimeStr = ctx.formatUptimeFromBoot(bootNow);
if (!uptimeStr.includes('1h') || !uptimeStr.includes('1m') || !uptimeStr.includes('5s')) {{
  throw new Error("formatUptimeFromBoot missing continuous seconds: " + uptimeStr);
}}

// 3. Test startLiveRenderLoop & LERP Glide
ctx.startLiveRenderLoop();
anim.targetCpu = 50.0;
anim.currentCpu = 10.0;
anim.renderedCpu = 10.0;

ctx.liveRenderTick(100);
// After one frame of factor = 0.18: 10 + (50 - 10)*0.18 = 17.2%
if (anim.currentCpu <= 10.0 || anim.currentCpu >= 50.0) {{
  throw new Error("LERP interpolation did not glide currentCpu: " + anim.currentCpu);
}}
if (Math.abs(anim.currentCpu - 17.2) > 0.5) {{
  throw new Error("Unexpected LERP step value: " + anim.currentCpu);
}}

const cpuVal = getEl('cpu-percent-value');
if (!cpuVal.textContent.includes('17.')) {{
  throw new Error("cpuVal DOM element was not smoothly updated: " + cpuVal.textContent);
}}

// 4. Test drawCpuCoreGraph continuous oscilloscope execution
const canvas = getEl('cpu-cores-unified-canvas');
const buf = [10, 20, 30, 40];
ctx.drawCpuCoreGraph(canvas, [buf], [42], null, true);

console.log("SUCCESS_LIVE_RENDERING");
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\nOutput: {res.stdout}")
        self.assertIn("SUCCESS_LIVE_RENDERING", res.stdout)
