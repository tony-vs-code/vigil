# Release v1.0.41: Outbound Notification Delivery Log & UI Styling Fixes

## Overview
Vigil v1.0.41 introduces an enhanced **Outbound Notification Delivery Log** and resolves key UI styling and layout constraints across the host observability suite. System administrators now have complete visibility into outbound alert dispatches—including trigger origins, target notification channels, summary payloads, and delivery outcomes—alongside smart deep links from scheduled notifications directly into the Cron Scheduler. In addition, critical UI components (destructive action buttons, socket/process search bars, and user management modals) have been polished for clean visual hierarchy across all display sizes.

## Highlights & Enhancements

### 1. Outbound Notification Delivery Log
- **Comprehensive 5-Column Telemetry Table**: Replaced the legacy 3-column table with a modern delivery log featuring `Timestamp`, `Trigger / Source`, `Channel`, `Subject / Summary`, and `Delivery Outcome`.
- **Smart Trigger Badges & Deep Linking**:
  - `CRON: NOTIFICATIONS.SUMMARY`: Rendered as an interactive badge linking operators directly to the Cron Scheduler tab (`switchTab('cron')`).
  - Contextual trigger badges for `THRESHOLD: CPU`, `SENTINEL PROBE`, `MANUAL TEST`, and `RAID MONITOR`.
- **Channel & Outcome Indicators**: Distinct visual badges for notification channels (Discord, Webhooks, Telegram, Slack, Email) and status outcomes (`✓ Delivered` in green, `✗ Failed` in red).
- **Resilient Fallbacks**: Clean message summarization with expandable details and backward-compatible inference for legacy notification records.

### 2. UI Styling & Visual Refinements
- **Destructive Button Styling**: Restored top-level `.btn-danger` and `.btn-danger-outline` CSS definitions, fixing unstyled "Prune Unused Volumes" (Docker Volumes) and "Ban IP" (Security & Firewall) buttons.
- **Unified Dark-Theme Search Boxes**: Added top-level `.search-box` and `.search-box input` rules to ensure listening sockets (`#sec-ports-filter`), process explorer, and user filter inputs match Vigil's dark slate aesthetic with glowing focus states.
- **Modal Viewport Bounding**: Applied desktop max-width constraints to `.modal-card` (`max-width: 540px`) and `#modal-add-user` (`max-width: 520px`), preventing full-screen stretching of user and group creation modals on wide monitors.

### 3. Backend SQLite Telemetry Migration
- **Schema Migration**: Auto-migrates `notification_history` table to include `channel`, `trigger`, `title`, `details`, and `outcome` columns.
- **Backward-Compatible Queries & Fallback Inference**: Automatically infers trigger origins and delivery outcomes for legacy rows lacking discrete telemetry columns.

## Reliability & Fixes
- **Dedicated UI & Styling Test Suite**: Added `tests/test_ui_styling_fixes.py` validating button classes, search box styling, modal card width rules, DOM integrity, and Node VM notification table rendering.
- **100% QA Quality Gate Pass**: Full automated test suite passes cleanly with **365 passed, 0 failed** across all backend routes, unit tests, and cross-tier UI contracts (63 API routes, 718 DOM elements).
