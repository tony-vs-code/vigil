"""Tests for UI styling fixes and Alert Dispatch status enhancements.

Validates:
1. static/styles.css:
   - .btn-danger and .btn-danger-outline defined with expected properties.
   - Top-level .search-box and .search-box input styling defined.
   - .modal-card default max-width: 540px and #modal-add-user .modal-card max-width: 520px.
   - .status-badge.status-info defined with appropriate palette.
2. static/index.html:
   - #notification-history-table column headers updated to 5 columns including <th>Trigger / Source</th>.
   - All expected DOM IDs (>= 718 unique IDs) remain intact.
3. static/app.js:
   - renderNotificationHistory renders CRON: NOTIFICATIONS.SUMMARY for scheduled_report.
   - renderNotificationHistory renders THRESHOLD: <METRIC> for alert_* / threshold triggers.
   - renderNotificationHistory renders SENTINEL PROBE for sentinel_* triggers.
   - renderNotificationHistory does not render UNKNOWN for fallback/unknown status.
"""

from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"
APP_JS = STATIC_DIR / "app.js"


class TestUIStylingFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css_content = STYLES_CSS.read_text(encoding="utf-8")
        cls.html_content = INDEX_HTML.read_text(encoding="utf-8")
        cls.js_content = APP_JS.read_text(encoding="utf-8")

    def test_btn_danger_classes_defined(self):
        """Verify .btn-danger and .btn-danger-outline classes are properly defined in styles.css."""
        self.assertIn(".btn-danger {", self.css_content)
        self.assertIn(".btn-danger-outline {", self.css_content)

        # Verify .btn-danger has red-themed styling
        btn_danger_match = re.search(r"\.btn-danger\s*\{([^}]+)\}", self.css_content)
        self.assertIsNotNone(btn_danger_match, ".btn-danger rule block not found")
        danger_body = btn_danger_match.group(1)
        self.assertIn("#dc2626", danger_body)
        self.assertIn("#b91c1c", danger_body)

        # Verify .btn-danger-outline styling
        btn_outline_match = re.search(
            r"\.btn-danger-outline\s*\{([^}]+)\}", self.css_content
        )
        self.assertIsNotNone(
            btn_outline_match, ".btn-danger-outline rule block not found"
        )
        outline_body = btn_outline_match.group(1)
        self.assertIn("#f87171", outline_body)
        self.assertIn("rgba(239, 68, 68", outline_body)

        # Verify hover pseudo-classes
        self.assertIn(".btn-danger:hover", self.css_content)
        self.assertIn(".btn-danger-outline:hover", self.css_content)

    def test_top_level_search_box_styling(self):
        """Verify top-level .search-box and .search-box input styling are defined."""
        # Check rule grouping with top-level .search-box
        self.assertTrue(
            re.search(
                r"\.search-box\s*,\s*\.table-controls-bar\s+\.search-box\s*\{",
                self.css_content,
            )
            or re.search(r"\.search-box\s*\{", self.css_content),
            "Top-level .search-box styling missing",
        )

        self.assertTrue(
            re.search(
                r"\.search-box\s+input\s*,\s*\.table-controls-bar\s+\.search-box\s+input\s*\{",
                self.css_content,
            )
            or re.search(r"\.search-box\s+input\s*\{", self.css_content),
            "Top-level .search-box input styling missing",
        )

        # Focus-within styling
        self.assertTrue(
            re.search(
                r"\.search-box:focus-within",
                self.css_content,
            ),
            ".search-box:focus-within rule missing",
        )

    def test_modal_card_max_widths(self):
        """Verify .modal-card default max-width: 540px and user modal max-width: 520px."""
        # Find default .modal-card definition
        modal_card_match = re.search(r"\.modal-card\s*\{([^}]+)\}", self.css_content)
        self.assertIsNotNone(modal_card_match, ".modal-card rule block not found")
        modal_card_body = modal_card_match.group(1)
        self.assertIn("max-width: 540px", modal_card_body)

        # Find user/group modal override
        add_user_modal_match = re.search(
            r"#modal-add-user\s+\.modal-card[^{]*\{([^}]+)\}", self.css_content
        )
        self.assertIsNotNone(
            add_user_modal_match, "#modal-add-user .modal-card rule block not found"
        )
        add_user_body = add_user_modal_match.group(1)
        self.assertIn("max-width: 520px", add_user_body)

    def test_status_badge_info_defined(self):
        """Verify .status-badge.status-info class is defined with info palette."""
        info_badge_match = re.search(
            r"\.status-badge\.status-info[^{]*\{([^}]+)\}", self.css_content
        )
        self.assertIsNotNone(
            info_badge_match, ".status-badge.status-info rule block not found"
        )
        body = info_badge_match.group(1)
        self.assertIn("#38bdf8", body)
        self.assertIn("rgba(14, 165, 233", body)

    def test_notification_history_table_header(self):
        """Verify #notification-history-table header has 5 columns including 'Trigger / Source' and not 'RAID Status'."""
        # Find notification-history-table section
        table_match = re.search(
            r'<table[^>]*id=["\']notification-history-table["\'][^>]*>(.*?)</table>',
            self.html_content,
            re.DOTALL,
        )
        self.assertIsNotNone(
            table_match, "#notification-history-table not found in index.html"
        )
        table_html = table_match.group(1)
        self.assertIn("<th>Trigger / Source</th>", table_html)
        self.assertIn("<th>Channel</th>", table_html)
        self.assertIn("<th>Subject / Summary</th>", table_html)
        self.assertIn("<th>Delivery Outcome</th>", table_html)
        self.assertNotIn("<th>RAID Status</th>", table_html)

    def test_index_html_dom_id_count_and_integrity(self):
        """Verify all DOM IDs in static/index.html are intact (>= 718 unique IDs)."""
        dom_ids = re.findall(r'id=["\']([^"\']+)["\']', self.html_content)
        unique_ids = set(dom_ids)
        self.assertGreaterEqual(
            len(unique_ids),
            718,
            f"Expected at least 718 unique DOM IDs, found {len(unique_ids)}",
        )

        # Essential IDs check
        critical_ids = [
            "notification-history-table",
            "notification-history-tbody",
            "modal-add-user",
            "modal-edit-user",
            "modal-add-group",
            "modal-group-member",
            "process-kill-modal",
            "audit-alerts-subtab-panel",
            "history-card",
        ]
        for cid in critical_ids:
            self.assertIn(
                cid,
                unique_ids,
                f"Critical DOM ID '{cid}' missing from static/index.html",
            )

    def test_app_js_render_notification_history_status_labels(self):
        """Verify renderNotificationHistory correctly maps triggers/statuses and doesn't default to UNKNOWN."""
        # Locate the renderNotificationHistory function in app.js
        func_match = re.search(
            r"function\s+renderNotificationHistory\s*\([^)]*\)\s*\{(.*?)\n\}",
            self.js_content,
            re.DOTALL,
        )
        self.assertIsNotNone(
            func_match, "renderNotificationHistory function not found in app.js"
        )
        func_body = func_match.group(1)

        # Check scheduled_report / cron maps to CRON: NOTIFICATIONS.SUMMARY
        self.assertIn("scheduled_report", func_body)
        self.assertIn("CRON: NOTIFICATIONS.SUMMARY", func_body)
        self.assertIn("trigger-badge trigger-cron", func_body)

        # Check threshold / alert_* mapping
        self.assertIn('startsWith("alert_")', func_body)
        self.assertIn("trigger-badge trigger-threshold", func_body)
        self.assertIn("THRESHOLD:", func_body)

        # Check sentinel_* probe alert mapping
        self.assertIn('startsWith("sentinel_")', func_body)
        self.assertIn("SENTINEL PROBE", func_body)

        # Check fallback text is DISPATCHED, not UNKNOWN
        self.assertIn('"DISPATCHED"', func_body)
        self.assertNotIn('statusBadge.textContent = "UNKNOWN"', func_body)

    def test_render_notification_history_node_vm(self):
        """Execute renderNotificationHistory in Node VM to verify 5 cells per row, badges, and click triggers."""
        import subprocess

        node_script = f"""
const fs = require('fs');
const vm = require('vm');

function createMockElement(tagName = 'div') {{
  const classes = new Set();
  const attributes = {{}};
  const children = [];
  const listeners = {{}};
  return {{
    tagName: tagName.toUpperCase(),
    style: {{}},
    children: children,
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: function(child) {{ children.push(child); return child; }},
    removeChild: function(child) {{
      const idx = children.indexOf(child);
      if (idx !== -1) children.splice(idx, 1);
      return child;
    }},
    addEventListener: function(event, handler) {{
      listeners[event] = listeners[event] || [];
      listeners[event].push(handler);
    }},
    dispatchEvent: function(event) {{
      const list = listeners[event.type || event] || [];
      list.forEach(fn => fn(event));
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
    setAttribute: function(name, val) {{ attributes[name] = String(val); }},
    getAttribute: function(name) {{ return attributes[name] !== undefined ? attributes[name] : null; }},
    textContent: '',
    get innerHTML() {{ return ''; }},
    set innerHTML(val) {{
      if (val === '') children.length = 0;
    }},
  }};
}}

const tbody = createMockElement('tbody');
tbody.id = 'notification-history-tbody';
const countBadge = createMockElement('span');
countBadge.id = 'history-count-badge';

const dom = {{
  'notification-history-tbody': tbody,
  'history-count-badge': countBadge,
}};

const historyMock = {{ pushState: () => {{}}, replaceState: () => {{}} }};
const sandbox = {{
  TextDecoder: TextDecoder,
  TextEncoder: TextEncoder,
  history: historyMock,
  window: {{
    location: {{ hash: '' }},
    history: historyMock,
  }},
  document: {{
    getElementById: (id) => dom[id] || null,
    createElement: (tag) => createMockElement(tag),
    addEventListener: () => {{}},
  }},
  VALID_RAID_STATES: new Set(['clean', 'active', 'degraded', 'recovering', 'resyncing']),
  formatTimestampLocal: (ts) => 'Formatted: ' + ts,
  renderDrawerNotificationList: () => {{}},
  console: console,
}};

vm.createContext(sandbox);

// Read app.js code
const code = fs.readFileSync('{APP_JS}', 'utf8');
vm.runInContext(code, sandbox);

let targetTab = null;
sandbox.switchViewTab = function(t) {{
  targetTab = t;
}};

// 1. Test empty state
sandbox.renderNotificationHistory([]);
if (tbody.children.length !== 1) throw new Error('Expected 1 empty row');
const emptyTd = tbody.children[0].children[0];
if (emptyTd.colSpan !== 5) throw new Error('Empty row colSpan must be 5, got ' + emptyTd.colSpan);

// 2. Test entries
const testPayload = [
  {{
    sent_at: '2026-11-09T12:00:00Z',
    sent_at_formatted: 'Nov 9, 2026 12:00 UTC',
    trigger: 'cron:notifications.summary',
    channel: 'discord',
    title: 'System Health Digest',
    details: 'Sent via discord',
    outcome: 'delivered',
    status: 'scheduled_report',
    weekly: true
  }},
  {{
    sent_at: '2026-11-09T12:05:00Z',
    trigger: 'threshold:cpu',
    channel: 'telegram',
    title: 'Threshold Alert: CPU',
    details: 'CPU metric breached configured threshold',
    outcome: 'failed',
    status: 'alert_cpu'
  }},
  {{
    sent_at: '2026-11-09T12:10:00Z',
    trigger: 'sentinel_probe',
    channel: 'pushover',
    outcome: 'dispatched'
  }},
  {{
    sent_at: '2026-11-09T12:15:00Z',
    trigger: 'manual_test',
    status: 'test',
    outcome: 'delivered'
  }},
  {{
    sent_at: '2026-11-09T12:20:00Z',
    trigger: 'raid_monitor',
    status: 'degraded',
    outcome: 'delivered'
  }}
];

sandbox.renderNotificationHistory(testPayload);
if (tbody.children.length !== 5) throw new Error('Expected 5 table rows, got ' + tbody.children.length);

// Verify row 0 (cron)
const r0 = tbody.children[0];
if (r0.children.length !== 5) throw new Error('Row 0 must have 5 cells, got ' + r0.children.length);
if (r0.children[0].textContent !== 'Nov 9, 2026 12:00 UTC') throw new Error('Row 0 time mismatch');

const cronBadge = r0.children[1].children[0];
if (!cronBadge.classList.contains('trigger-cron')) throw new Error('Row 0 trigger badge not trigger-cron');
if (cronBadge.textContent !== 'CRON: NOTIFICATIONS.SUMMARY') throw new Error('Row 0 trigger text mismatch: ' + cronBadge.textContent);

// Verify click on cron badge
cronBadge.dispatchEvent({{ type: 'click' }});
if (targetTab !== 'backups') throw new Error('Clicking cron badge did not call switchViewTab("backups"): ' + targetTab);

// Verify channel cell
const chanBadge0 = r0.children[2].children[0];
if (!chanBadge0.classList.contains('channel-discord')) throw new Error('Row 0 channel badge not channel-discord');
if (chanBadge0.textContent !== 'DISCORD') throw new Error('Row 0 channel text mismatch');

// Verify summary cell
const title0 = r0.children[3].children[0];
const details0 = r0.children[3].children[1];
if (title0.textContent !== 'System Health Digest') throw new Error('Row 0 title mismatch');
if (details0.textContent !== 'Sent via discord') throw new Error('Row 0 details mismatch');

// Verify outcome cell
const outcome0 = r0.children[4].children[0];
if (!outcome0.classList.contains('status-ok') || outcome0.textContent !== '✓ Delivered') {{
  throw new Error('Row 0 outcome mismatch');
}}

// Verify row 1 (threshold & failed)
const r1 = tbody.children[1];
const threshBadge = r1.children[1].children[0];
if (!threshBadge.classList.contains('trigger-threshold')) throw new Error('Row 1 not trigger-threshold');
if (threshBadge.textContent !== 'THRESHOLD: CPU') throw new Error('Row 1 threshold text mismatch: ' + threshBadge.textContent);

const outcome1 = r1.children[4].children[0];
if (!outcome1.classList.contains('status-danger') || outcome1.textContent !== '✗ Failed') {{
  throw new Error('Row 1 outcome mismatch');
}}

// Verify row 2 (sentinel)
const r2 = tbody.children[2];
const sentinelBadge = r2.children[1].children[0];
if (!sentinelBadge.classList.contains('trigger-sentinel') || sentinelBadge.textContent !== 'SENTINEL PROBE') {{
  throw new Error('Row 2 sentinel badge mismatch');
}}

// Verify row 3 (manual test)
const r3 = tbody.children[3];
const testBadge = r3.children[1].children[0];
if (!testBadge.classList.contains('trigger-test') || testBadge.textContent !== 'MANUAL TEST') {{
  throw new Error('Row 3 test badge mismatch');
}}

// Verify row 4 (raid monitor)
const r4 = tbody.children[4];
const raidBadge = r4.children[1].children[0];
if (!raidBadge.classList.contains('trigger-raid') || raidBadge.textContent !== 'RAID MONITOR') {{
  throw new Error('Row 4 raid badge mismatch');
}}

console.log('OK');
"""

        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}\n{res.stdout}")
        self.assertIn("OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
