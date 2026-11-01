# Release v1.0.17: Streamlined Overview Layout, Physical Network Priority & UI Refinements

## Overview
This release optimizes dashboard responsiveness, telemetry clarity, and layout efficiency across the system. It brings full-width rolling CPU time-series charts, chunked memory telemetry, accurate Debian/Netplan physical network adapter detection, and cleaned page stat ribbons.

## Highlights & Enhancements
- **Full-Width CPU Cores Chart**: The rolling processor core time-series chart now spans the full width at the top of the Overview view, followed linearly by the overall CPU telemetry summary and per-core utilization drilldown cards.
- **Chunked Memory Telemetry**: Memory metrics are now displayed in four dedicated visual chunk cards (In Use, Available, Cached, and Free) with distinct colored accents for immediate status recognition.
- **Debian & Netplan Physical Network Adapter Detection**: Network telemetry now prioritizes host physical hardware adapters discovered via Netplan YAML definitions (`/etc/netplan`, `/run/netplan`, `/lib/netplan`) and Linux sysfs links (`/sys/class/net/*/device`), filtering out Docker container bridges and virtual interfaces from primary reporting and host counters.
- **Streamlined Page Stat Ribbons**: Removed run-on paragraph descriptions and rigid dividers from top stat ribbons across Services, Processes, Packages, Users, Groups, and Audit views in favor of clean, responsive status pill cards that eliminate text clipping and horizontal overflow.
- **Accurate Alerts Badge & Drawer Cleanup**: The topbar notification badge now reflects actual active undismissed alerts (`status in ('degraded', 'critical', 'failed', 'warning')`), hiding when 0, and the redundant counter inside the drawer header has been removed.
- **Preloaded Groups Subtab Count**: Groups data is preloaded upon authenticated session initialization and user tab navigation, instantly displaying the accurate group count badge.
- **Topbar Action Streamlining**: Removed the redundant topbar Vigil Logs button in favor of the dedicated sidebar action.

## Reliability & Verification
- **Cross-Tier Contract Audit**: Audited all 468 DOM element IDs and 37 frontend API routes.
- **Test Suite**: 159 passing automated unit, integration, and UI contract tests.
