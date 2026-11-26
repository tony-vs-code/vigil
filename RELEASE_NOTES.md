# Release v1.0.50: Sidebar Sub-Menu Navigation & Container Table Layout Polish

## Overview
Vigil v1.0.50 streamlines dashboard navigation and container table ergonomics. In-page horizontal tab bars across multi-section views have been consolidated into collapsible sidebar sub-menus with live count badges and mobile drawer support, container action dropdowns now render with viewport-fixed positioning to eliminate clipping, and the Docker containers table has been tightened into a cleaner 6-column layout.

## Highlights & Enhancements

### 1. Collapsible Sidebar Sub-Menu Navigation
- **Unified Multi-View Sub-Menus**: Replaced in-page horizontal tab bars on **Containers** (`#view-containers`), **Packages** (`#view-packages`), **Users & Groups** (`#view-users`), and **Audit Log** (`#view-audit`) with integrated collapsible sidebar sub-menus (`#sidebar-submenu-containers`, `#sidebar-submenu-packages`, `#sidebar-submenu-users`, `#sidebar-submenu-audit`).
- **Live Badges & Chevron Indicators**: Preserved real-time item and update badge pills alongside animated chevron indicators (`.sidebar-submenu-chevron`) and accessible `role="tablist"` / `aria-expanded` attributes.
- **Collapsed & Mobile Drawer Awareness**: Synchronizes active parent and sub-tab states via `syncSidebarSubmenus` and `toggleSidebarSubmenuGroup`, cleanly hides sub-menus when the desktop sidebar is collapsed (`.app-layout.sidebar-collapsed`), and supports intuitive parent-vs-submenu navigation inside the mobile drawer.

### 2. Viewport-Fixed Container Actions Dropdown
- **Clip-Free Fixed Positioning**: Added `positionActionsDropdownMenu` and `closeAllActionsDropdowns` so `.actions-dropdown-menu` renders in viewport-fixed coordinates at `z-index: 1050`, preventing clipping by table overflow scrollbars or sibling elements.
- **Smart Boundary Handling**: Automatically flips upward when near the bottom edge of the viewport, clamps horizontally within viewport margins, and dismisses cleanly on scroll, resize, `Escape`, outside click, or menu action selection.

### 3. Streamlined 6-Column Container Table Layout
- **Merged Primary Container Cell**: Consolidated `#docker-containers-table` into a 6-column layout (`Container`, `Image`, `State`, `Ports`, `Stack`, `Actions`), pairing container names and truncated short IDs cleanly inside `.container-cell-primary` and `.container-cell-id`.
- **Balanced Column Widths**: Added disciplined `max-width` and ellipsis truncation rules across container metadata columns so action controls remain accessible without horizontal crowding.

## Reliability & Fixes
- **Sidebar & Layout Test Suite**: Added `SidebarSubmenuAndContainerLayoutTests` in `test_main.py` verifying zero duplicate HTML IDs, sidebar sub-menu DOM contracts, collapsed/mobile navigation flows, viewport-fixed dropdown flipping/closing, and 6-column container table rendering in Node VM.
- **Cron Lifecycle Test Fix**: Updated `test_fastapi_cron_endpoints_auth_and_lifecycle` in `test_main.py` to mock `DOCKER_MANAGER.prune_volumes` cleanly during scheduled maintenance execution tests.
- **100% QA Quality Gate Pass**: Validated with `node --check static/app.js`, cross-tier contract checks (64 frontend API routes and 722 DOM element IDs), and **440/440** passing tests in `pytest`.
