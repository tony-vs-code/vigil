# Release v1.0.15: Streamlined Notification Drawer & Decoupled Navigation

## Overview
This release resolves UX redundancies and modal layer clipping by decoupling sidebar navigation from the slide-over notification drawer and streamlining the slide-over notification drawer workflow.

## Highlights & Enhancements
- **Decoupled Sidebar Navigation**: Clicking "Notifications" in the left sidebar exclusively navigates to the Notification Center page view (`#view-notifications`), eliminating unintentional drawer opening.
- **Dedicated Topbar Drawer Trigger**: The topbar notification bell (`#btn-topbar-notifications`) remains the dedicated action for launching the slide-over drawer.
- **Streamlined Notification Drawer**: Removed redundant quick action buttons (`Channels`, `Schedule`, `Thresholds`, `Send Test`) and status strips that previously launched modals underneath the drawer/backdrop layer.
- **Quick-Access Navigation Links**: Added header "Manage" and pinned footer "Notification Center Settings & Channels" buttons inside the drawer that smoothly dismiss the slide-over and navigate straight to the Notification Center.

## Reliability & Verification
- **DOM & API Contract Integrity**: Audited all 470 DOM element IDs and 37 frontend API routes, ensuring zero broken element bindings.
- **Test Suite**: 158 of 158 tests passing across unit, integration, and UI contract suites.
