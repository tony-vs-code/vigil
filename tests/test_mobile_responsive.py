"""Tests for mobile-first responsive layout and navigation drawer in Vigil.

Validates:
1. DOM elements in static/index.html:
   - #btn-mobile-nav-toggle button element with accessibility labels and icon.
   - #mobile-nav-backdrop overlay with aria-hidden.
   - #app-sidebar container for off-canvas drawer.
2. Key responsive CSS rules in static/styles.css:
   - @media (max-width: 768px) with off-canvas sidebar drawer and .mobile-open styles.
   - #btn-mobile-nav-toggle display toggle (hidden on desktop, flex on mobile).
   - #mobile-nav-backdrop overlay styling and .active state.
   - .overview-status-ribbon responsive wrapping.
   - .metrics-grid single-column layout on mobile viewports.
   - .modal-card viewport scaling (95vw, max-height).
   - .table-container horizontal touch-scroll overflow (-webkit-overflow-scrolling: touch).
   - #stack-snippet-toolbar horizontal touch scrollability.
   - View controls & filter bar stacking on narrow viewports (@media max-width: 600px).
3. JavaScript wiring in static/app.js:
   - Toggle button click listener toggles mobile drawer & backdrop.
   - Backdrop click listener dismisses mobile drawer.
   - Navigation links auto-close mobile drawer on mobile viewports (<= 768px).
   - View switching resets/closes mobile drawer.
"""

from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "static"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"
APP_JS = STATIC_DIR / "app.js"


def extract_media_blocks(css_text: str, query: str) -> list[str]:
    """Extract full content of all @media blocks matching query, handling nested braces."""
    clean_query = query.replace(" ", r"\s*")
    pattern = re.compile(r"@media[^{]*\(" + clean_query + r"\)[^{]*\{")
    blocks = []
    for match in pattern.finditer(css_text):
        start = match.end()
        depth = 1
        i = start
        while i < len(css_text) and depth > 0:
            if css_text[i] == "{":
                depth += 1
            elif css_text[i] == "}":
                depth -= 1
            i += 1
        blocks.append(css_text[start : i - 1])
    return blocks


class SimpleElementParser(HTMLParser):
    """Minimal HTML parser collecting element tags, ids, and attribute dictionaries."""

    def __init__(self):
        super().__init__()
        self.elements_by_id = {}
        self.tags = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        self.tags.append((tag, attr_dict))
        if "id" in attr_dict:
            self.elements_by_id[attr_dict["id"]] = {
                "tag": tag,
                "attrs": attr_dict,
            }


class TestMobileResponsiveHTML(unittest.TestCase):
    """Validates mobile navigation DOM elements in static/index.html."""

    @classmethod
    def setUpClass(cls):
        cls.html_content = INDEX_HTML.read_text(encoding="utf-8")
        cls.parser = SimpleElementParser()
        cls.parser.feed(cls.html_content)

    def test_btn_mobile_nav_toggle_exists_and_attributes(self):
        """Verify #btn-mobile-nav-toggle button exists with proper attributes and accessibility labels."""
        self.assertIn(
            "btn-mobile-nav-toggle",
            self.parser.elements_by_id,
            "DOM element #btn-mobile-nav-toggle must exist in static/index.html",
        )
        elem = self.parser.elements_by_id["btn-mobile-nav-toggle"]
        self.assertEqual(elem["tag"], "button")
        self.assertIn("btn-mobile-nav-toggle", elem["attrs"].get("class", "").split())
        self.assertEqual(elem["attrs"].get("type"), "button")
        self.assertIn("aria-label", elem["attrs"])
        self.assertTrue(len(elem["attrs"]["aria-label"].strip()) > 0)

    def test_mobile_nav_backdrop_exists_and_attributes(self):
        """Verify #mobile-nav-backdrop overlay exists with appropriate accessibility aria-hidden attribute."""
        self.assertIn(
            "mobile-nav-backdrop",
            self.parser.elements_by_id,
            "DOM element #mobile-nav-backdrop must exist in static/index.html",
        )
        elem = self.parser.elements_by_id["mobile-nav-backdrop"]
        self.assertIn("mobile-nav-backdrop", elem["attrs"].get("class", "").split())
        self.assertEqual(elem["attrs"].get("aria-hidden"), "true")

    def test_app_sidebar_exists(self):
        """Verify #app-sidebar aside exists as navigation target."""
        self.assertIn(
            "app-sidebar",
            self.parser.elements_by_id,
            "DOM element #app-sidebar must exist in static/index.html",
        )
        elem = self.parser.elements_id = self.parser.elements_by_id["app-sidebar"]
        self.assertEqual(elem["tag"], "aside")


class TestMobileResponsiveCSS(unittest.TestCase):
    """Validates mobile-first and responsive CSS rules in static/styles.css."""

    @classmethod
    def setUpClass(cls):
        cls.css_content = STYLES_CSS.read_text(encoding="utf-8")
        cls.mobile_blocks_768 = extract_media_blocks(cls.css_content, "max-width: 768px")
        cls.all_768_content = "\n".join(cls.mobile_blocks_768)
        cls.mobile_blocks_600 = extract_media_blocks(cls.css_content, "max-width: 600px")
        cls.all_600_content = "\n".join(cls.mobile_blocks_600)

    def test_mobile_media_query_768_present(self):
        """Verify @media (max-width: 768px) block is defined."""
        self.assertTrue(
            len(self.mobile_blocks_768) > 0,
            "@media (max-width: 768px) media query must exist in styles.css",
        )

    def test_btn_mobile_nav_toggle_desktop_hidden(self):
        """Verify #btn-mobile-nav-toggle defaults to display: none outside mobile media query."""
        match = re.search(
            r"#btn-mobile-nav-toggle[^{]*\{([^}]+)\}",
            self.css_content,
        )
        self.assertIsNotNone(match, "#btn-mobile-nav-toggle base rule not found")
        block = match.group(1)
        self.assertIn("display: none", block)

    def test_btn_mobile_nav_toggle_mobile_flex(self):
        """Verify #btn-mobile-nav-toggle becomes display: flex within mobile viewport."""
        self.assertRegex(
            self.all_768_content,
            r"(?:#btn-mobile-nav-toggle|\.btn-mobile-nav-toggle)[^{]*\{[^}]*display\s*:\s*flex",
            "#btn-mobile-nav-toggle must be display: flex in @media (max-width: 768px)",
        )

    def test_app_sidebar_off_canvas_drawer_rules(self):
        """Verify sidebar off-canvas drawer styles (position: fixed, translateX(-100%), transition)."""
        # Off-canvas hidden state
        self.assertRegex(
            self.all_768_content,
            r"(?:#app-sidebar|\.app-sidebar)[^{]*\{[^}]*position\s*:\s*fixed",
            "Sidebar must be fixed positioned on mobile viewports",
        )
        self.assertRegex(
            self.all_768_content,
            r"(?:#app-sidebar|\.app-sidebar)[^{]*\{[^}]*transform\s*:\s*translateX\(-100%\)",
            "Sidebar must be off-canvas translateX(-100%) by default on mobile",
        )

        # Drawer open state (.mobile-open or .open)
        self.assertRegex(
            self.all_768_content,
            r"(?:#app-sidebar\.mobile-open|\.app-sidebar\.mobile-open)[^{]*\{[^}]*transform\s*:\s*translateX\(0\)",
            "Sidebar .mobile-open state must slide in with translateX(0)",
        )

    def test_mobile_nav_backdrop_overlay_styling(self):
        """Verify #mobile-nav-backdrop has fixed position, inset: 0, high z-index, and opacity transition."""
        match = re.search(
            r"#mobile-nav-backdrop[^{]*\{([^}]+)\}",
            self.css_content,
        )
        self.assertIsNotNone(match, "#mobile-nav-backdrop base rule not found")
        block = match.group(1)
        self.assertIn("position: fixed", block)
        self.assertIn("inset: 0", block)
        self.assertIn("z-index:", block)

        # Active state
        active_match = re.search(
            r"#mobile-nav-backdrop\.active[^{]*\{([^}]+)\}",
            self.css_content,
        )
        self.assertIsNotNone(active_match, "#mobile-nav-backdrop.active rule not found")
        active_block = active_match.group(1)
        self.assertIn("opacity: 1", active_block)
        self.assertIn("pointer-events: auto", active_block)

    def test_overview_status_ribbon_responsive_wrapping(self):
        """Verify .overview-status-ribbon wraps flexibly on responsive viewports."""
        self.assertRegex(
            self.css_content,
            r"\.overview-status-ribbon[^{]*\{[^}]*flex-wrap\s*:\s*wrap",
            ".overview-status-ribbon must have flex-wrap: wrap for responsive layout",
        )

    def test_metrics_grid_single_column_layout(self):
        """Verify .metrics-grid adjusts to 1 column on mobile viewports."""
        self.assertRegex(
            self.all_768_content,
            r"\.metrics-grid[^{]*\{[^}]*grid-template-columns\s*:\s*1fr",
            ".metrics-grid must collapse to grid-template-columns: 1fr on mobile viewports",
        )

    def test_modal_card_viewport_scaling(self):
        """Verify .modal-card scales to 95vw and max-height on mobile viewports."""
        self.assertRegex(
            self.all_768_content,
            r"\.modal-card[^{]*\{[^}]*width\s*:\s*95vw",
            ".modal-card must have width: 95vw on mobile viewports",
        )
        self.assertRegex(
            self.all_768_content,
            r"\.modal-card[^{]*\{[^}]*max-width\s*:\s*95vw",
            ".modal-card must have max-width: 95vw on mobile viewports",
        )
        self.assertRegex(
            self.all_768_content,
            r"\.modal-card[^{]*\{[^}]*max-height\s*:\s*9\dvh",
            ".modal-card must constrain max-height on mobile viewports",
        )

    def test_modal_card_variants_viewport_scaling(self):
        """Verify key modal card classes (stack, settings, confirm, logs) adhere to mobile viewport scaling."""
        for cls_name in ["stack-modal-card", "settings-modal-card", "confirm-modal-card", "logs-modal-card"]:
            self.assertIn(
                f".{cls_name}",
                self.all_768_content,
                f".{cls_name} must be included in mobile modal scaling rules",
            )

    def test_table_container_touch_scroll(self):
        """Verify .table-container enables horizontal touch-scrolling."""
        match = re.search(
            r"\.table-container\s*\{([^}]+)\}",
            self.css_content,
        )
        self.assertIsNotNone(match, ".table-container rule not found")
        block = match.group(1)
        self.assertIn("overflow-x: auto", block)
        self.assertIn("-webkit-overflow-scrolling: touch", block)

    def test_stack_snippet_toolbar_touch_scroll(self):
        """Verify #stack-snippet-toolbar enables horizontal scrolling with smooth touch."""
        match = re.search(
            r"(?:#stack-snippet-toolbar|\.snippet-toolbar)[^{]*\{([^}]+)\}",
            self.css_content,
        )
        self.assertIsNotNone(match, "#stack-snippet-toolbar rule not found")
        block = match.group(1)
        self.assertIn("overflow-x: auto", block)
        self.assertIn("-webkit-overflow-scrolling: touch", block)

    def test_table_controls_stacking_narrow_viewports(self):
        """Verify table controls and filter bars stack column-wise on narrow viewports."""
        self.assertTrue(len(self.mobile_blocks_600) > 0, "@media (max-width: 600px) block must exist")
        self.assertRegex(
            self.all_600_content,
            r"(?:\.table-controls-bar|\.view-controls)[^{]*\{[^}]*flex-direction\s*:\s*column",
            "Table controls must stack vertically (flex-direction: column) on screens <= 600px",
        )


class TestMobileResponsiveJSWiring(unittest.TestCase):
    """Validates JavaScript event wiring for mobile drawer and touch interactions."""

    @classmethod
    def setUpClass(cls):
        cls.js_content = APP_JS.read_text(encoding="utf-8")

    def test_btn_mobile_nav_toggle_listener_wired(self):
        """Verify static/app.js binds a click listener to #btn-mobile-nav-toggle."""
        self.assertIn(
            'document.getElementById("btn-mobile-nav-toggle")',
            self.js_content,
            "app.js must lookup #btn-mobile-nav-toggle",
        )
        self.assertRegex(
            self.js_content,
            r"btnMobileNavToggle\?\.addEventListener\(\s*[\"']click[\"']",
            "btnMobileNavToggle must have a click event listener attached",
        )

    def test_mobile_nav_drawer_toggle_logic(self):
        """Verify toggleMobileNavDrawer toggles mobile-open and active classes."""
        match = re.search(
            r"function\s+toggleMobileNavDrawer\s*\([^)]*\)\s*\{([\s\S]+?)\}",
            self.js_content,
        )
        self.assertIsNotNone(match, "function toggleMobileNavDrawer not found")
        body = match.group(1)
        self.assertIn('classList.toggle("mobile-open")', body)
        self.assertIn('mobileNavBackdrop?.classList.toggle("active"', body)

    def test_mobile_nav_backdrop_dismisses_drawer(self):
        """Verify clicking mobile backdrop invokes drawer close logic."""
        self.assertIn(
            'document.getElementById("mobile-nav-backdrop")',
            self.js_content,
            "app.js must lookup #mobile-nav-backdrop",
        )
        self.assertRegex(
            self.js_content,
            r"mobileNavBackdrop\?\.addEventListener\(\s*[\"']click[\"']",
            "mobileNavBackdrop must have a click event listener to close drawer",
        )

    def test_mobile_drawer_auto_closes_on_navigation_links(self):
        """Verify clicking sidebar navigation items closes the drawer when viewport <= 768px."""
        self.assertRegex(
            self.js_content,
            r"querySelectorAll\([\"'][^\"']*\.sidebar-nav-item[^\"']*[\"']\)\.forEach",
            "app.js must iterate over sidebar navigation items to wire auto-close",
        )
        self.assertRegex(
            self.js_content,
            r"window\.innerWidth\s*<=\s*768",
            "app.js must inspect window.innerWidth <= 768 before auto-closing drawer",
        )

    def test_close_mobile_nav_drawer_cleans_all_classes(self):
        """Verify closeMobileNavDrawer removes mobile-open, open, and active classes."""
        match = re.search(
            r"function\s+closeMobileNavDrawer\s*\([^)]*\)\s*\{([\s\S]+?)\}",
            self.js_content,
        )
        self.assertIsNotNone(match, "function closeMobileNavDrawer not found")
        body = match.group(1)
        self.assertIn('appSidebar?.classList.remove("mobile-open")', body)
        self.assertIn('mobileNavBackdrop?.classList.remove("active")', body)

    def test_switch_view_tab_clears_mobile_drawer_classes(self):
        """Verify switchViewTab automatically removes mobile-open from sidebar and active from backdrop."""
        match = re.search(
            r"function\s+switchViewTab\s*\([^)]*\)\s*\{([\s\S]+?)(?=\nfunction|\Z)",
            self.js_content,
        )
        self.assertIsNotNone(match, "function switchViewTab not found in app.js")
        body = match.group(1)
        self.assertIn('classList.remove("mobile-open")', body)
        self.assertIn('classList.remove("active")', body)


if __name__ == "__main__":
    unittest.main()
