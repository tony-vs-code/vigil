import asyncio
import collections
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import psutil

import api
import main
from test_main import asgi_get


def _make_fake_proc(
    pid: int,
    name: str,
    *,
    user: str = "root",
    cpu: float = 0.0,
    mem: float = 0.0,
    rss_mb: float = 10.0,
    cmdline: list[str] | None = None,
    raise_exc: Exception | None = None,
):
    if raise_exc is not None:
        proc = MagicMock()
        type(proc).info = property(lambda self: (_ for _ in ()).throw(raise_exc))
        proc.as_dict.side_effect = raise_exc
        return proc

    mem_info = MagicMock()
    mem_info.rss = int(rss_mb * 1024 * 1024)
    proc = MagicMock()
    proc.info = {
        "pid": pid,
        "name": name,
        "username": user,
        "cpu_percent": cpu,
        "memory_percent": mem,
        "memory_info": mem_info,
        "cmdline": cmdline if cmdline is not None else [name],
    }
    return proc


class IncidentForensicsEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_forensics.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.init_db()

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_schema_creates_snapshots_table_indexes_and_history_column(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            snap_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(alert_forensic_snapshots)")
            }
            hist_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(notification_history)")
            }
            indexes = {
                row[1]
                for row in conn.execute(
                    "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='alert_forensic_snapshots'"
                )
            }
        finally:
            conn.close()

        self.assertTrue(
            {
                "id",
                "notification_id",
                "event_type",
                "severity",
                "metric_value",
                "system_metrics_json",
                "top_processes_json",
                "context_json",
                "created_at",
            }.issubset(snap_cols)
        )
        self.assertIn("snapshot_id", hist_cols)
        self.assertIn("idx_snapshots_created", indexes)
        self.assertIn("idx_snapshots_event", indexes)

    def test_capture_snapshot_cpu_and_ram_sorting_and_safe_process_errors(self):
        procs = [
            _make_fake_proc(101, "idle_proc", cpu=1.0, mem=2.0, rss_mb=32),
            _make_fake_proc(
                102,
                "ffmpeg",
                user="media",
                cpu=82.1,
                mem=15.0,
                rss_mb=512,
                cmdline=["/usr/bin/ffmpeg", "-i", "input.mkv"],
            ),
            _make_fake_proc(
                103,
                "java",
                user="app",
                cpu=12.4,
                mem=64.5,
                rss_mb=4096,
                cmdline=["java", "-Xmx4g", "-jar", "svc.jar"],
            ),
            _make_fake_proc(104, "dead_proc", raise_exc=psutil.NoSuchProcess(104)),
            _make_fake_proc(105, "locked_proc", raise_exc=psutil.AccessDenied(105)),
            _make_fake_proc(106, "zombie_proc", raise_exc=psutil.ZombieProcess(106)),
        ]
        # Add 10 more low-usage procs to test top-10 cap
        for i in range(10):
            procs.append(_make_fake_proc(200 + i, f"worker-{i}", cpu=0.5 + i * 0.1, mem=0.2))

        mock_vm = MagicMock(
            total=16 * 1024**3, available=4 * 1024**3, used=12 * 1024**3, percent=75.0
        )
        mock_swap = MagicMock(total=4 * 1024**3, used=512 * 1024**2, percent=12.5)

        def fake_cpu_percent(interval=None, percpu=False):
            return [88.0, 92.0] if percpu else 90.0

        with (
            patch("psutil.process_iter", return_value=procs),
            patch("psutil.cpu_percent", side_effect=fake_cpu_percent),
            patch("psutil.virtual_memory", return_value=mock_vm),
            patch("psutil.swap_memory", return_value=mock_swap),
            patch("os.getloadavg", return_value=(4.2, 3.8, 3.1), create=True),
        ):
            cpu_snap = main.capture_forensic_snapshot(
                "cpu", "CRITICAL", "90.0%", context={"threshold": 80.0}
            )
            ram_snap = main.capture_forensic_snapshot(
                "memory", "WARNING", "75.0%", context={"threshold": 70.0}
            )

        # CPU snapshot sorted by cpu_percent descending and capped at 10
        self.assertEqual(len(cpu_snap["top_processes"]), 10)
        self.assertEqual(cpu_snap["top_processes"][0]["name"], "ffmpeg")
        self.assertEqual(cpu_snap["top_processes"][0]["pid"], 102)
        self.assertEqual(cpu_snap["top_processes"][0]["cpu_percent"], 82.1)
        self.assertEqual(cpu_snap["top_processes"][0]["user"], "media")
        self.assertEqual(cpu_snap["top_processes"][0]["memory_mb"], 512.0)
        self.assertEqual(
            cpu_snap["top_processes"][0]["cmdline"], "/usr/bin/ffmpeg -i input.mkv"
        )
        self.assertEqual(cpu_snap["top_processes"][1]["name"], "java")

        # Memory snapshot sorted by memory_percent descending
        self.assertEqual(len(ram_snap["top_processes"]), 10)
        self.assertEqual(ram_snap["top_processes"][0]["name"], "java")
        self.assertEqual(ram_snap["top_processes"][0]["memory_percent"], 64.5)
        self.assertEqual(ram_snap["top_processes"][1]["name"], "ffmpeg")

        # System metrics captured cleanly
        self.assertEqual(cpu_snap["system_metrics"]["load_avg"], [4.2, 3.8, 3.1])
        self.assertEqual(cpu_snap["system_metrics"]["cpu_percent"], 90.0)
        self.assertEqual(cpu_snap["system_metrics"]["cpu_cores"], [88.0, 92.0])
        self.assertEqual(cpu_snap["system_metrics"]["memory"]["percent"], 75.0)
        self.assertEqual(cpu_snap["system_metrics"]["swap"]["percent"], 12.5)
        self.assertEqual(cpu_snap["context"], {"threshold": 80.0})

    def test_capture_snapshot_disk_and_raid_context(self):
        with patch("psutil.process_iter", return_value=[]):
            disk_snap = main.capture_forensic_snapshot(
                "disk",
                "WARNING",
                "91.5%",
                context={"path": "/mnt/media", "percent": 91.5},
            )
            raid_snap = main.capture_forensic_snapshot(
                "raid",
                "CRITICAL",
                "DEGRADED",
                context={"status": "degraded", "failed_devices": 1},
            )

        self.assertEqual(disk_snap["event_type"], "disk")
        self.assertEqual(disk_snap["context"]["path"], "/mnt/media")
        self.assertEqual(raid_snap["event_type"], "raid")
        self.assertEqual(raid_snap["context"]["status"], "degraded")

    def test_auto_pruning_by_age_and_max_records(self):
        now = time.time()
        with patch("psutil.process_iter", return_value=[]):
            # Insert an old snapshot (45 days ago) and 5 recent ones
            old_snap = main.capture_forensic_snapshot(
                "cpu", "WARNING", "81%", created_at=now - (45 * 86400.0)
            )
            recent_ids = []
            for i in range(5):
                s = main.capture_forensic_snapshot(
                    "cpu", "WARNING", f"{82 + i}%", created_at=now + i
                )
                recent_ids.append(s["id"])

        # Auto-pruning inside capture_forensic_snapshot already evicted old_snap
        after_auto = [s["id"] for s in main.list_forensic_snapshots(limit=20)]
        self.assertNotIn(old_snap["id"], after_auto)
        self.assertEqual(len(after_auto), 5)

        # Explicit max_records=3 prunes the remaining 2 oldest recent records
        deleted = main.prune_forensic_snapshots(max_records=3, max_age_days=30)
        self.assertEqual(deleted, 2)

        remaining_ids = [s["id"] for s in main.list_forensic_snapshots(limit=20)]
        self.assertEqual(remaining_ids, list(reversed(recent_ids[-3:])))

    def test_threshold_alerts_generate_snapshots_enrich_embeds_and_link_history(self):
        t0 = time.time()
        main.update_notification_center_config(
            {
                "channels": {
                    "discord": {"enabled": True, "bot_token": "tok", "channel_id": "123"}
                },
                "thresholds": {
                    "enabled": True,
                    "cpu_percent": 80.0,
                    "ram_percent": 80.0,
                    "disk_percent": 80.0,
                    "raid_degraded": True,
                    "smart_failure": True,
                    "cooldown_minutes": 60,
                },
            }
        )

        procs = [
            _make_fake_proc(1234, "ffmpeg", cpu=82.1, mem=10.0, rss_mb=256),
            _make_fake_proc(5678, "python3", cpu=12.4, mem=55.0, rss_mb=2048),
            _make_fake_proc(9012, "node", cpu=4.2, mem=8.0, rss_mb=128),
        ]
        mock_vm = MagicMock(percent=88.0, used=8800, total=10000, available=1200)

        def fake_cpu_percent(interval=None, percpu=False):
            return [91.0, 93.0] if percpu else 92.0

        with (
            patch("psutil.process_iter", return_value=procs),
            patch("psutil.virtual_memory", return_value=mock_vm),
            patch("psutil.cpu_percent", side_effect=fake_cpu_percent),
            patch.object(
                main,
                "get_disk_usage_statvfs",
                return_value={"percent": 85.0, "used": 850, "total": 1000},
            ),
            patch.object(
                main, "check_raid_status", return_value=("degraded", "/dev/sdb failed")
            ),
            patch.object(
                main,
                "get_all_disks_smart_metrics",
                return_value=[
                    {
                        "device": "/dev/sda",
                        "model": "WD Red",
                        "serial_number": "WD123",
                        "passed": False,
                        "temperature_c": 40,
                    }
                ],
            ),
            patch.object(main, "send_message", new=AsyncMock(return_value=True)),
        ):
            alerts = asyncio.run(main.evaluate_threshold_alerts(now=t0))

        metrics = {a["metric"] for a in alerts}
        self.assertIn("ram", metrics)
        self.assertIn("cpu", metrics)
        self.assertIn("raid_degraded", metrics)
        self.assertIn("smart_fail_sda", metrics)

        for alert in alerts:
            self.assertTrue(alert.get("snapshot_id"))
            field_names = [f["name"] for f in alert["embed"]["fields"]]
            self.assertIn("Top Contributing Processes", field_names)
            proc_field = next(
                f
                for f in alert["embed"]["fields"]
                if f["name"] == "Top Contributing Processes"
            )
            if alert["metric"] == "cpu":
                self.assertIn("1. `ffmpeg` [PID 1234] — 82.1% CPU", proc_field["value"])
            if alert["metric"] == "ram":
                self.assertIn("1. `python3` [PID 5678] — 55.0% RAM", proc_field["value"])

        snapshots = main.list_forensic_snapshots(limit=20)
        self.assertGreaterEqual(len(snapshots), 5)
        for snap in snapshots:
            self.assertIsNotNone(snap["notification_id"])
            linked = main.get_forensic_snapshot_by_notification_id(
                snap["notification_id"]
            )
            self.assertIsNotNone(linked)
            self.assertEqual(linked["id"], snap["id"])


class IncidentForensicsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_api_forensics.sqlite3"
        self.db_patcher = patch.object(main, "RAID_STATE_DB", self.db_path)
        self.db_patcher.start()
        main.create_user("admin", "adminpass", email="admin@example.com")
        self.session_token = main.create_session("admin")
        self.auth_cookies = {"session_token": self.session_token}

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def test_snapshot_endpoints_auth_list_filter_get_by_id_and_notification_id(self):
        # 401 when unauthenticated
        self.assertEqual(
            asgi_get(api.app, "/api/notifications/snapshots").status_code, 401
        )
        self.assertEqual(
            asgi_get(api.app, "/api/notifications/snapshots/abc").status_code, 401
        )
        self.assertEqual(
            asgi_get(api.app, "/api/notifications/1/snapshot").status_code, 401
        )

        with patch(
            "psutil.process_iter",
            return_value=[_make_fake_proc(42, "stress-ng", cpu=99.0, mem=5.0)],
        ):
            snap_cpu = main.capture_forensic_snapshot(
                "cpu", "CRITICAL", "99.0%", context={"threshold": 85.0}
            )
            snap_disk = main.capture_forensic_snapshot(
                "disk", "WARNING", "91.0%", context={"path": "/"}
            )

        main.record_notification(
            "alert_cpu",
            1700000100.0,
            weekly=False,
            title="High CPU",
            details="CPU at 99%",
            snapshot_id=snap_cpu["id"],
        )
        main.record_notification(
            "test",
            1700000200.0,
            weekly=False,
        )

        # List all
        list_resp = asgi_get(
            api.app, "/api/notifications/snapshots", cookies=self.auth_cookies
        )
        self.assertEqual(list_resp.status_code, 200)
        snaps = list_resp.json()["snapshots"]
        self.assertEqual(len(snaps), 2)

        # Filter by event_type
        cpu_only = asgi_get(
            api.app,
            "/api/notifications/snapshots?event_type=cpu&limit=10",
            cookies=self.auth_cookies,
        )
        self.assertEqual(cpu_only.status_code, 200)
        self.assertEqual(len(cpu_only.json()["snapshots"]), 1)
        self.assertEqual(cpu_only.json()["snapshots"][0]["id"], snap_cpu["id"])

        # Get by snapshot_id
        get_snap = asgi_get(
            api.app,
            f"/api/notifications/snapshots/{snap_cpu['id']}",
            cookies=self.auth_cookies,
        )
        self.assertEqual(get_snap.status_code, 200)
        snap_body = get_snap.json()["snapshot"]
        self.assertEqual(snap_body["id"], snap_cpu["id"])
        self.assertEqual(snap_body["top_processes"][0]["name"], "stress-ng")
        self.assertEqual(snap_body["context"], {"threshold": 85.0})
        self.assertIn("memory", snap_body["system_metrics"])

        # 404 on unknown snapshot_id
        self.assertEqual(
            asgi_get(
                api.app,
                "/api/notifications/snapshots/nonexistent",
                cookies=self.auth_cookies,
            ).status_code,
            404,
        )

        # History includes id and snapshot_id
        hist_resp = asgi_get(
            api.app, "/api/history?limit=10", cookies=self.auth_cookies
        )
        self.assertEqual(hist_resp.status_code, 200)
        history_items = hist_resp.json()["history"]
        cpu_hist = next(h for h in history_items if h["status"] == "alert_cpu")
        self.assertEqual(cpu_hist["snapshot_id"], snap_cpu["id"])
        self.assertIsNotNone(cpu_hist["id"])

        # Get by notification_id
        by_notif = asgi_get(
            api.app,
            f"/api/notifications/{cpu_hist['id']}/snapshot",
            cookies=self.auth_cookies,
        )
        self.assertEqual(by_notif.status_code, 200)
        self.assertEqual(by_notif.json()["snapshot"]["id"], snap_cpu["id"])

        # 404 when notification has no snapshot or doesn't exist
        test_hist = next(h for h in history_items if h["status"] == "test")
        self.assertEqual(
            asgi_get(
                api.app,
                f"/api/notifications/{test_hist['id']}/snapshot",
                cookies=self.auth_cookies,
            ).status_code,
            404,
        )
        self.assertEqual(
            asgi_get(
                api.app, "/api/notifications/999999/snapshot", cookies=self.auth_cookies
            ).status_code,
            404,
        )


class IncidentForensicsFrontendContractsTests(unittest.TestCase):
    def test_zero_duplicate_html_ids_and_forensic_drawer_dom_css_js_contracts(self):
        root = Path(__file__).parent
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "static" / "styles.css").read_text(encoding="utf-8")

        all_ids = re.findall(r'\bid=["\']([^"\']+)["\']', html)
        duplicates = {k: v for k, v in collections.Counter(all_ids).items() if v > 1}
        self.assertEqual(
            duplicates,
            {},
            f"Duplicate HTML IDs found in static/index.html: {duplicates}",
        )

        required_dom_ids = [
            "forensic-snapshot-backdrop",
            "drawer-forensic-snapshot",
            "snapshot-drawer-title",
            "snapshot-severity-badge",
            "snapshot-event-type",
            "snapshot-metric-value",
            "snapshot-timestamp",
            "btn-close-snapshot-drawer",
            "snapshot-loading-state",
            "snapshot-error-state",
            "snapshot-content-body",
            "snapshot-load-avg",
            "snapshot-cpu-gauge",
            "snapshot-memory-gauge",
            "snapshot-swap-gauge",
            "snapshot-processes-table",
            "snapshot-processes-tbody",
            "snapshot-context-card",
            "snapshot-context-body",
            "btn-copy-snapshot-summary",
            "btn-dismiss-snapshot",
        ]
        for dom_id in required_dom_ids:
            self.assertIn(
                f'id="{dom_id}"',
                html,
                f"Missing #{dom_id} in static/index.html",
            )

        for css_sel in [
            ".btn-snapshot",
            ".forensic-snapshot-backdrop",
            ".forensic-snapshot-drawer",
            ".forensic-gauge-fill",
            ".forensic-context-card",
        ]:
            self.assertIn(css_sel, css, f"Missing CSS selector {css_sel} in static/styles.css")

        for fn_sig in [
            "function formatSnapshotTitle(",
            "function updateSnapshotGauge(",
            "function renderSnapshotSystemMetrics(",
            "function renderSnapshotProcesses(",
            "function renderSnapshotContext(",
            "function renderForensicSnapshotDetails(",
            "async function openForensicSnapshotDrawer(",
            "function closeForensicSnapshotDrawer(",
            "async function copySnapshotSummaryToClipboard(",
        ]:
            self.assertIn(fn_sig, js, f"Missing JS function '{fn_sig}' in static/app.js")

    def test_forensic_drawer_rendering_copy_and_history_button_in_node_vm(self):
        node_script = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/app.js', 'utf8');

function extractFunc(source, name) {
  let idx = source.indexOf('async function ' + name + '(');
  if (idx === -1) idx = source.indexOf('function ' + name + '(');
  if (idx === -1) throw new Error('Function not found: ' + name);
  let depth = 0, started = false, i = idx;
  while (i < source.length) {
    if (source[i] === '{') { depth++; started = true; }
    else if (source[i] === '}') {
      depth--;
      if (started && depth === 0) return source.slice(idx, i + 1);
    }
    i++;
  }
  throw new Error('Unbalanced braces in ' + name);
}

function makeClassList(initial = []) {
  const set = new Set(initial);
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    contains: (c) => set.has(c),
  };
}

function makeGaugeBox(id) {
  const pctEl = { textContent: '' };
  const subEl = { textContent: '' };
  const fillEl = { style: { width: '0%' }, classList: makeClassList(['forensic-gauge-fill']) };
  return {
    id,
    classList: makeClassList(),
    querySelector(sel) {
      if (sel === '.forensic-gauge-pct') return pctEl;
      if (sel === '.forensic-gauge-sub') return subEl;
      if (sel === '.forensic-gauge-fill') return fillEl;
      return null;
    },
    _pctEl: pctEl,
    _subEl: subEl,
    _fillEl: fillEl,
  };
}

const elements = {
  'snapshot-cpu-gauge': makeGaugeBox('snapshot-cpu-gauge'),
  'snapshot-memory-gauge': makeGaugeBox('snapshot-memory-gauge'),
  'snapshot-swap-gauge': makeGaugeBox('snapshot-swap-gauge'),
};

function getEl(id) {
  if (!elements[id]) {
    let _innerHTML = '';
    const el = {
      id,
      textContent: '',
      className: '',
      attrs: {},
      style: {},
      children: [],
      offsetWidth: 0,
      classList: makeClassList(['hidden']),
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k]; },
      appendChild(child) { this.children.push(child); },
    };
    Object.defineProperty(el, 'innerHTML', {
      get() { return _innerHTML; },
      set(val) {
        _innerHTML = String(val);
        if (_innerHTML === '') el.children = [];
      },
    });
    elements[id] = el;
  }
  return elements[id];
}

function createEl(tag) {
  const node = {
    tagName: tag.toUpperCase(),
    type: '',
    className: '',
    textContent: '',
    innerHTML: '',
    attrs: {},
    children: [],
    listeners: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(child) { this.children.push(child); },
    addEventListener(evt, cb) { this.listeners[evt] = cb; },
  };
  return node;
}

const fetchUrls = [];
const copiedPayloads = [];
const toasts = [];
let nextResponse = null;

const context = vm.createContext({
  console,
  Array,
  Object,
  String,
  Number,
  Boolean,
  Math,
  JSON,
  encodeURIComponent,
  setTimeout: () => {},
  activeForensicSnapshot: null,
  cachedNotificationHistorySig: '',
  VALID_RAID_STATES: new Set(['clean', 'degraded', 'rebuilding']),
  setTextIfChanged: (el, txt) => { if (el) el.textContent = String(txt); },
  renderDrawerNotificationList: () => {},
  switchViewTab: () => {},
  document: {
    getElementById: getEl,
    createElement: createEl,
  },
  navigator: {
    clipboard: {
      writeText: async (text) => { copiedPayloads.push(text); },
    },
  },
  escapeHtml: (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  formatTimestampLocal: () => '2026-11-28 12:00:00',
  formatBytes: (b) => `${(Number(b) / (1024 * 1024 * 1024)).toFixed(1)} GB`,
  showToast: (t) => toasts.push(t),
  apiFetch: async (url) => {
    fetchUrls.push(url);
    return nextResponse;
  },
});

for (const fnName of [
  'formatSnapshotTitle',
  'updateSnapshotGauge',
  'renderSnapshotSystemMetrics',
  'renderSnapshotProcesses',
  'formatContextKeyLabel',
  'renderSnapshotContext',
  'renderForensicSnapshotDetails',
  'openForensicSnapshotDrawer',
  'closeForensicSnapshotDrawer',
  'copySnapshotSummaryToClipboard',
  'renderNotificationHistory',
]) {
  vm.runInContext(extractFunc(code, fnName), context);
}

const sampleSnapshot = {
  id: 'snap-uuid-123',
  event_type: 'cpu',
  severity: 'CRITICAL',
  metric_value: '94.2%',
  created_at: 1700000000,
  created_at_formatted: '2026-11-28 12:00:00 UTC',
  system_metrics: {
    load_avg: [5.12, 4.44, 3.21],
    cpu_percent: 94.2,
    cpu_cores: [92.0, 96.4],
    memory: { total: 16 * 1024 ** 3, used: 12 * 1024 ** 3, percent: 75.0 },
    swap: { total: 4 * 1024 ** 3, used: 512 * 1024 ** 2, percent: 12.5 },
  },
  top_processes: [
    { pid: 102, name: 'ffmpeg', user: 'media', cpu_percent: 82.1, memory_mb: 512.0, memory_percent: 3.1, cmdline: '/usr/bin/ffmpeg <test>' },
  ],
  context: {
    threshold: 80,
    affected_service: 'transcoder',
  },
};

(async () => {
  // 1. Open drawer and fetch snapshot
  nextResponse = { ok: true, status: 200, json: async () => ({ snapshot: sampleSnapshot }) };
  await context.openForensicSnapshotDrawer('snap-uuid-123');
  if (fetchUrls[0] !== '/api/notifications/snapshots/snap-uuid-123') {
    throw new Error('Unexpected snapshot endpoint: ' + fetchUrls[0]);
  }
  const drawer = getEl('drawer-forensic-snapshot');
  if (!drawer.classList.contains('open') || drawer.attrs['aria-hidden'] !== 'false') {
    throw new Error('Drawer did not open cleanly');
  }
  if (!getEl('snapshot-loading-state').classList.contains('hidden')) {
    throw new Error('Loading state should be hidden after render');
  }
  if (getEl('snapshot-content-body').classList.contains('hidden')) {
    throw new Error('Content body should be visible');
  }
  if (getEl('snapshot-load-avg').textContent !== '5.12 / 4.44 / 3.21') {
    throw new Error('Load average mismatch: ' + getEl('snapshot-load-avg').textContent);
  }
  const cpuGauge = getEl('snapshot-cpu-gauge');
  if (cpuGauge._pctEl.textContent !== '94.2%' || !cpuGauge._fillEl.classList.contains('gauge-crit')) {
    throw new Error('CPU gauge not updated with critical class');
  }
  const memGauge = getEl('snapshot-memory-gauge');
  if (memGauge._pctEl.textContent !== '75.0%' || !memGauge._fillEl.classList.contains('gauge-warn')) {
    throw new Error('Memory gauge not updated with warn class');
  }
  const procHtml = getEl('snapshot-processes-tbody').innerHTML;
  if (!procHtml.includes('ffmpeg') || !procHtml.includes('&lt;test&gt;')) {
    throw new Error('Processes table missing escaped process row: ' + procHtml);
  }
  const ctxHtml = getEl('snapshot-context-body').innerHTML;
  if (!ctxHtml.includes('Threshold') || !ctxHtml.includes('transcoder')) {
    throw new Error('Context card missing diagnostic details: ' + ctxHtml);
  }

  // 2. Copy markdown summary to clipboard
  await context.copySnapshotSummaryToClipboard();
  if (copiedPayloads.length !== 1) throw new Error('Expected 1 clipboard copy');
  const md = copiedPayloads[0];
  if (!md.includes('## High CPU Utilization Incident') || !md.includes('snap-uuid-123') || !md.includes('**ffmpeg**') || !md.includes('transcoder')) {
    throw new Error('Copied markdown summary incomplete: ' + md);
  }
  if (toasts[0].title !== 'Post-Mortem Summary Copied') {
    throw new Error('Missing copy toast');
  }

  // 3. Error fallback when snapshot not found
  nextResponse = { ok: false, status: 404, json: async () => ({ detail: 'Forensic snapshot not found' }) };
  await context.openForensicSnapshotDrawer('missing-id');
  if (getEl('snapshot-error-state').classList.contains('hidden') || getEl('snapshot-error-state').textContent !== 'Forensic snapshot not found') {
    throw new Error('Error state did not display 404 detail');
  }

  // 4. Close drawer
  context.closeForensicSnapshotDrawer();
  if (drawer.classList.contains('open') || drawer.attrs['aria-hidden'] !== 'true') {
    throw new Error('Drawer did not close');
  }

  // 5. Notification history renders .btn-snapshot only when snapshot_id is present
  const histTbody = getEl('notification-history-tbody');
  context.renderNotificationHistory([
    { id: 1, sent_at: 1700000000, status: 'alert_cpu', trigger: 'High CPU', channel: 'discord', details: '94%', outcome: 'delivered', snapshot_id: 'snap-uuid-123' },
    { id: 2, sent_at: 1700000010, status: 'test', trigger: 'Test', channel: 'discord', details: 'ok', outcome: 'delivered', snapshot_id: '' },
  ]);
  if (histTbody.children.length !== 2) throw new Error('Expected 2 history rows');
  const row1Outcome = histTbody.children[0].children[4];
  const row2Outcome = histTbody.children[1].children[4];
  if (!row1Outcome.children.some((c) => (c.className || '').includes('btn-snapshot') && c.attrs['data-snapshot-id'] === 'snap-uuid-123')) {
    throw new Error('Row 1 missing .btn-snapshot button');
  }
  if (row2Outcome.children.some((c) => (c.className || '').includes('btn-snapshot'))) {
    throw new Error('Row 2 should not render .btn-snapshot without snapshot_id');
  }

  console.log('FORENSICS_VM_PASS');
})().catch((e) => { console.error(e); process.exit(1); });
"""
        proc = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(
            proc.returncode,
            0,
            f"Node VM forensics test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}",
        )
        self.assertIn("FORENSICS_VM_PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
