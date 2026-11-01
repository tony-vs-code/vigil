# Release v1.0.13

## Overview
Fixes RAID resync status classification and telemetry tracking so active array resynchronization is properly tracked with real-time percentage, ETA, and speed instead of being misclassified as degraded or stuck at 0.0%.

## Highlights & Enhancements
- **RAID Resync Status & Progress Tracking**: Correctly parses active resync, recovery, and check states from `/proc/mdstat`, Linux sysfs (`sync_completed`, `sync_speed`), and `mdadm --detail` (`Resync Status : X%`).
- **Real-Time Resync Telemetry**: Progress percentage, transfer rate (MB/s or KB/s), and estimated completion time (ETA) are dynamically calculated and streamed live to the dashboard progress bar and Overview card.
- **Accurate Array Health Classification**: Resynchronization is categorized as an active sync operation (`RESYNCING`) rather than a critical/degraded state (`DEGRADED`), preventing false degraded alarms and red alert badges.
- **Threshold Alert Accuracy**: Threshold evaluation ensures `raid_degraded` notifications only fire on true drive degradation or failure (`degraded`, `failed`), not normal background resyncs or parity checks.
- **Task Manager Memory Telemetry**: Windows Task Manager style physical memory badge and 4-chip allocation grid (In use, Available, Cached, Free).
- **Centralized Alert Dispatch Logs & Drawer Filter**: Moved notification dispatch history into Audit & Logs and added category filtering (All, Alerts, Application) and a Dismiss All action to the notification drawer.

## Reliability & Fixes
- Added unit and cross-tier contract tests covering resync progress parsing, sysfs recovery states, and non-degraded threshold validation.
- Full automated test suite passing cleanly with 158 tests.
