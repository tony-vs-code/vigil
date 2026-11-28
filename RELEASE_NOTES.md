# Release v1.2.0: Automated Incident Forensics & Registry Auth Enhancements

## Overview
Vigil v1.2.0 introduces **Automated Incident Forensics & Contextual Event Snapshots** alongside hard-won **Docker Registry Authentication Fixes**. Whenever CPU, memory, swap, disk, RAID, or temperature thresholds trigger an alert, Vigil now captures a frozen point-in-time diagnostic snapshot—preserving top contributing processes, system gauges, and hardware context in SQLite, enriching Discord alerts, and rendering an interactive slide-out Forensics Drawer with 1-click Markdown post-mortem export.

## Highlights & Enhancements

### 1. Automated Incident Forensics & Contextual Event Snapshots
- **Point-in-Time Snapshot Capture**: `capture_forensic_snapshot` and `_attach_forensic_snapshot_to_alert` freeze top 10 contributing CPU/RSS processes (`_collect_top_processes`), load averages, CPU/memory/swap metrics (`_collect_forensic_system_metrics`), and event context (disk/RAID/SMART/threshold metadata) at the exact moment an alert triggers.
- **SQLite Persistence & 30-Day Retention**: Added the `alert_forensic_snapshots` table (indexed by `created_at` and `event_type`), linked `notification_history.snapshot_id`, and implemented `prune_forensic_snapshots` with automatic 30-day / 500-record retention.
- **Enriched Discord & Alert Payloads**: Alert embeds automatically include a **Top Contributing Processes** summary (`_format_forensic_processes_field`) so operators immediately see which workloads spiked without SSHing into the host.
- **Forensics Drawer & Post-Mortem Export**: Added authenticated endpoints (`GET /api/notifications/snapshots`, `GET /api/notifications/snapshots/{snapshot_id}`, `GET /api/notifications/{notification_id}/snapshot`) and the slide-out Forensics Drawer (`#drawer-forensic-snapshot`) accessible from Notification History and the Notification Drawer, complete with telemetry gauges, process table (`#snapshot-processes-table`), contextual metadata, and 1-click Markdown post-mortem copy (`copySnapshotSummaryToClipboard`).

### 2. Docker Registry Authentication Fixes
- **Scheme-Free Domain Parsing**: `_parse_registry_url` now handles bare registry domains stored without a URL scheme (such as `ghcr.io` or `quay.io`), ensuring host matching and `config.json` generation work reliably.
- **Multi-Alias Auth Keys & `oauth2` Fallback**: `_get_docker_config_server_aliases` and `_build_registry_auths_payload` populate multi-alias Docker CLI auth keys (e.g., `ghcr.io`, `https://ghcr.io`, `https://index.docker.io/v1/`, `docker.io`, `registry-1.docker.io`) and default empty usernames to `"oauth2"` for token-only credentials.
- **Stack Deployment & Update Check Credential Injection**: Extended `_ephemeral_docker_config_dir` credential injection across Docker Compose stack deployments (`deploy_stack`), stack operations, and remote image update digest checks so private registry images resolve seamlessly.

## Reliability & Fixes
- **Incident Forensics & Registry Test Suites**: Added `test_incident_forensics.py` (`IncidentForensicsEngineTests` covering schema, process/metric capture, retention pruning, evaluator integration, API endpoints, and Node VM drawer/Markdown export) and expanded `test_container_registries.py` for bare-domain parsing, `oauth2` fallback, multi-alias `config.json` generation, and stack/update-check credential injection.
- **100% QA Quality Gate Pass**: Validated with `node --check static/app.js`, cross-tier contract checks (68 frontend API routes and 763 DOM element IDs), and **472/472** passing tests in `pytest`.
