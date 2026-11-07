# Release v1.0.34: Comprehensive Feature Invariant & System Self-Test Diagnostic Suite

## Overview
Vigil v1.0.34 introduces a comprehensive **Feature Invariant & System Self-Test Diagnostic Suite**, giving operators instant visibility and automated confidence into core subsystem health and feature invariants. With a sub-100ms non-destructive internal diagnostics engine, an authenticated diagnostic API endpoint, a responsive Settings UI self-test cockpit with real-time subsystem breakdown, and 29 modular invariant test suites, Vigil continuously verifies critical subsystems without disrupting production operations.

## Highlights & Enhancements

### 1. High-Performance Non-Destructive Diagnostics Engine
- **Sub-100ms Internal Diagnostics**: The asynchronous `run_system_diagnostics()` engine evaluates host subsystem health, schema invariants, and internal subsystems in under 100 milliseconds without mutative side-effects or service disruption.
- **SQLite Database Integrity Verification**: Executes internal `PRAGMA integrity_check` alongside active schema verification across all 9 critical tables (`system_metrics`, `raid_events`, `docker_containers`, `cron_tasks`, `cron_logs`, `audit_events`, `notification_providers`, `alert_history`, `sentinel_probes`).
- **Alert Hub Invariant Verification**: Validates initialization and configuration integrity across all 6 alert providers (Discord, Telegram, Pushover, Ntfy, Gotify, Webhook) and verifies event routing rules.
- **Docker App Catalog Blueprint Verification**: Audits all 11 curated homelab app templates ensuring compose definitions, required ports, category taxonomies, and environment variable schema integrity.
- **Subsystem Health Probing**: Assesses CPU, memory, and disk telemetry probes, Vigil cron scheduler thread health, and API security guard invariants (API token authorization and path sanitation).

### 2. Authenticated Diagnostics API Endpoint
- **RESTful Diagnostics Endpoint**: `GET /api/system/diagnostics` provides authenticated access to real-time subsystem diagnostic reports.
- **Structured Health Payload**: Returns overall operational status (`healthy` / `degraded`), microsecond-precision execution latency, ISO-8601 timestamps, and granular per-subsystem checks (`database`, `alert_hub`, `app_catalog`, `telemetry`, `cron_scheduler`, `security_guards`).
- **Actionable Diagnostic Metrics**: Subsystem objects report individual status, execution latency, and rich diagnostic metadata (table counts, provider states, catalog templates, memory metrics).

### 3. Interactive Settings UI Self-Test Card
- **1-Click Self-Test Execution**: Interactive "Run Diagnostics" button in the Settings view (`#btn-run-diagnostics`) with loading spinners and immediate visual responsiveness.
- **Dynamic Status Badges & Latency Counter**: Visual status indicators (`SYSTEM HEALTHY` / `DEGRADED`) and execution duration badges (`#diag-overall-badge`, `#diag-duration-badge`).
- **Subsystem Breakdown Cockpit**: Real-time rendering of all 6 diagnostic subsystems (`#diag-subsystems-list`) with status pills, execution latency, and detail summaries.
- **Raw Diagnostic Payload Inspection**: Collapsible raw JSON viewer (`#diag-raw-json`) for rapid copy-paste and homelab debugging.

### 4. Modular Invariant & Regression Test Suites
- **Dedicated Diagnostic Tests**: New `tests/test_system_diagnostics.py` testing database checks, table validation, alert provider verification, catalog validation, and REST API contracts.
- **Feature Invariant Test Suite**: New `tests/test_feature_invariants.py` enforcing immutable guarantees for core homelab features, schema backward compatibility, and security boundaries.

## Reliability & Fixes
- **100% Test Pass Rate**: 292 / 292 automated tests passing cleanly across unit, integration, and UI contract suites.
- **Zero-Downtime Safe Checks**: All diagnostic routines employ read-only queries and non-blocking in-memory state inspections.
- **Comprehensive UI Contract Testing**: Full DOM validation of diagnostic cards, buttons, status pills, and list renders.
