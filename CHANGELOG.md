# Changelog

All notable changes to the SystemAlerts project are documented in this file.

## [v1.0.34] - 2026-11-06
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

## [v1.0.33] - 2026-11-06
## Overview
Vigil v1.0.33 transforms the notification engine into a comprehensive **Multi-Channel Alert Hub**, adding native support for **Pushover**, **Ntfy**, and **Gotify** alongside existing Discord, Telegram, and Generic Webhook channels. System operators now have complete granular control over notification routing per channel (RAID degradation alerts, weekly system summaries, cron execution failures, and cron successes), robust credential protection with secret masking and preservation, dynamic real-time channel status badge pills, and direct channel-specific test dispatches.

## Highlights & Enhancements

### 1. Unified 6-Channel Alert Ecosystem
- **Expanded Service Integrations**: Native notification delivery support across 6 distinct notification platforms: Discord, Telegram, Pushover, Ntfy, Gotify, and Generic HTTP Webhooks.
- **Pushover Integration**: Supports user keys, application API tokens, message priority levels, and custom audio alert tones.
- **Ntfy Integration**: Supports self-hosted and cloud ntfy servers, custom topics, priorities, tags, and authorization tokens.
- **Gotify Integration**: Seamless alerting via self-hosted Gotify instances with app tokens and priority mapping.
- **Generic Webhook**: Configurable HTTP POST webhook destination with customizable JSON payload templates and custom headers.

### 2. Granular Per-Channel Notification Routing
- **Fine-Grained Event Matrix**: Configure exactly which alerts dispatch to each channel:
  - **RAID Alerts**: Immediate notifications for degraded arrays, disk failures, and resync events.
  - **Weekly Summaries**: Consolidated weekly system status and storage health reports.
  - **Cron Failures**: Instant critical failure notifications when scheduled cron jobs fail.
  - **Cron Successes**: Optional execution confirmations for completed tasks.
- **Independent Channels**: Each integration operates independently, allowing critical alerts to page on-call phones via Pushover while weekly digests route to Discord or Telegram.

### 3. Real-Time Channel Badges & Secret Preservation
- **Live Status Badges**: Visual status indicators (`ACTIVE`, `DISABLED`, `NOT CONFIGURED`) for all 6 channels dynamically computed from channel state and route selections.
- **Secure Secret Masking & Preservation**: Passwords, bot tokens, user keys, and webhook URLs are masked on the frontend (`••••••••`) and safely preserved on backend configuration updates unless explicitly overwritten.

### 4. Channel-Specific Test Dispatching
- **Targeted Diagnostic Testing**: Quick test dispatch dropdown (`#notif-test-channel-select`) enabling operators to verify delivery to specific channels (`All Channels`, `Discord`, `Telegram`, `Pushover`, `Ntfy`, `Gotify`, or `Webhook`) on demand.
- **Actionable Diagnostic Feedback**: Real-time modal and inline alerts report delivery status and precise error diagnostics when channel tokens or endpoints are misconfigured.

## Reliability & Fixes
- **Comprehensive Quality Assurance**: 263 / 263 automated tests passing (100% PASS), verifying async notification dispatches, secret masking, routing matrix rules, and SQLite persistence.
- **DOM & Node VM UI Contract Auditing**: Full verification of frontend event handlers, test dispatch payloads, configuration modals, and status badges in automated Node VM test suites.
- **Zero-Downtime Migration**: Seamless backward compatibility with existing Discord and Telegram configurations.

## [v1.0.32] - 2026-11-06
## Overview
Vigil v1.0.32 introduces a complete **Visual Docker Compose Editor & 1-Click Homelab App Catalog**, enabling operators to explore, customize, deploy, and manage self-hosted applications effortlessly. Featuring 11 production-ready homelab application templates, instant 1-click deployment, pre-deploy customization with real-time environment variable templating, a dual-tab compose and `.env` editor, live YAML validation with syntax diagnostics, and service/port pill detection, managing multi-container stacks in Vigil is faster, safer, and more intuitive than ever.

## Highlights & Enhancements

### 1. Curated Homelab App Catalog (11 Templates)
- **Production-Ready Blueprints**: Includes 11 popular homelab services: Pi-hole (DNS & Ad-blocking), Uptime Kuma (Monitoring), Nginx Proxy Manager (Reverse Proxy), Vaultwarden (Password Management), Jellyfin (Media Server), Home Assistant (Smart Home), Portainer CE (Docker UI), Dozzle (Real-time Container Logs), Redis (In-Memory Datastore), PostgreSQL (Relational Database), and Glances (System Observability).
- **Category Filter & Instant Search**: Filter catalog apps by category tags (`All`, `Monitoring`, `Networking`, `Security`, `Media`, `Database`, `Automation`, `Management`) or perform instant multi-attribute text search matching application names, descriptions, and categories.
- **Card-Based App Discovery**: Each application card features iconography, category badge, quick port references, volume hints, and immediate call-to-actions.

### 2. 1-Click Launch & Customize Workflows
- **1-Click Launch**: Instantly scaffold and deploy default application configurations with automatic port assignments and data directory generation.
- **Customize & Deploy Modal**: Interactive configuration modal prompting for stack name, exposed host port mapping, host volume storage paths, and environment variable defaults prior to spinning up containers.
- **Pre-populated Stack Editor**: Effortlessly seed the Docker Compose Stack Editor directly from any catalog template with one click.

### 3. Dual-Tab Stack Editor & Multi-File Persistence
- **Dual-Tab Architecture**: Seamlessly switch between `docker-compose.yml` (YAML Editor) and `.env Variables` (Environment Configuration) without losing unsaved changes.
- **Multi-File Storage Synchronization**: Synchronizes both Compose specification and environment variables to disk filesystem (`/opt/stacks/<name>/.env` and `docker-compose.yml`) as well as Vigil SQLite storage for persistent management.

### 4. Live YAML Validation & Diagnostic Feedback Pills
- **Real-Time Validation Endpoint**: Asynchronous `/api/docker/compose/validate` endpoint verifies Docker Compose syntax and structure using PyYAML.
- **Interactive Service & Port Pills**: Detects defined service names and parsed port forward mappings, displaying live visual pills beneath the editor for immediate feedback.
- **Actionable Diagnostic Alerts**: Highlights YAML syntax errors, indentation faults, and missing service directives inline with clear warning/success state pills.

## Reliability & Fixes
- **Comprehensive Test Suite**: 253/253 automated tests passing (100% PASS), covering backend API endpoints, SQLite/filesystem persistence, YAML validation logic, and frontend DOM/VM contracts.
- **Node VM Contract Validation**: Full DOM event routing, tab switching, search/filtering, and modal parameter injection verified in automated JavaScript test harnesses.
- **Backward Compatibility**: Fully compatible with existing standalone containers and previously deployed compose stacks.

## [v1.0.31] - 2026-11-06
## Overview
Vigil v1.0.31 streamlines the Actions columns for Docker Containers and Docker Stacks across the dashboard. By introducing prominent Primary Quick Actions (`Restart`/`Start`, `Logs`, `Terminal`, `Edit`) paired with an elegant, responsive "⋯" More dropdown for secondary and destructive operations (`Stop`, `Pause`, `Pull`, `Down`, `Remove`, `Delete`), container and stack management is significantly cleaner, prevents horizontal table bloating, and eliminates accidental destructive clicks.

## Highlights & Enhancements

### 1. Streamlined Docker Containers Actions
- **Primary Quick Actions**: High-frequency workflows remain 1-click accessible. Running containers show prominent `Restart`, `Logs`, and `Terminal` buttons; stopped containers provide instant `Start` and `Logs`; paused containers feature an immediate `Unpause` action.
- **"⋯" More Dropdown Menu**: Secondary lifecycle actions (`Stop`, `Pause`, `Terminal` for stopped containers, and `Remove`) are consolidated inside a sleek popover dropdown menu.
- **Accidental Deletion Protection**: Destructive operations like container removal (`Remove`) feature distinctive red danger styling (`actions-dropdown-item danger`) with confirmation dialogs.

### 2. Streamlined Docker Compose Stacks Actions
- **Primary Quick Actions**: Active stacks feature direct `Restart` and `Edit` buttons, while stopped stacks display immediate `Start` and `Edit` actions. External/unmanaged compose stacks retain their protective `Read-Only (External)` safety badge.
- **Unified Stack Dropdown**: Lifecycle and teardown operations (`Stop`, `Pull`, `Down`, `Delete`) are consolidated within the stack's "⋯" More dropdown menu.
- **Contextual Destruction Styling**: Teardown (`Down`) and stack file deletion (`Delete`) operations are cleanly separated by a divider and styled with danger hover highlights.

### 3. Accessible UX & Dismissal Lifecycle
- **Click-Outside & Keyboard Dismissal**: Opening any dropdown registers automatic click-outside backdrop dismissal and Escape key listeners, closing all active action popovers instantly.
- **Multi-Menu Mutual Exclusion**: Activating any dropdown automatically closes any other open dropdown menus across the dashboard.
- **Dynamic Positioning & Elevated Contrast**: High-contrast, dark-mode elevated card styling (`box-shadow`, subtle borders, and hover micro-transitions) ensures consistent readability across compact viewport widths.

## Reliability & Fixes
- **Comprehensive Test Suite**: All 245 unit, integration, and frontend Node VM tests passed with 100% success.
- **Node VM & DOM Contract Verification**: Verified complete HTML generation, DOM event contracts, and CSS selector validity across container states (`running`, `stopped`, `paused`) and stack configurations (`running`, `stopped`, `external`).

## [v1.0.30] - 2026-11-06
## Overview
Vigil v1.0.30 addresses terminal character encoding and ANSI control sequence rendering defects in the In-Browser Docker Container Web Terminal. By setting standardized modern terminal environment variables on the backend, implementing stateful multi-byte UTF-8 stream decoding on the frontend, gracefully filtering unsupported control characters and C1 codes, and implementing DEC private mode support (cursor visibility, alternate screen buffers, bracketed paste), interactive sessions now render clean, crisp typography, Box Drawing characters, and emojis with zero visual corruption or replacement glyphs (`[?]`).

## Highlights & Enhancements

### 1. Robust Multi-Byte UTF-8 Streaming Decoder
- **Stateful Text Decoding**: Integrated a persistent `TextDecoder("utf-8", { stream: true })` instance across WebSocket binary message frames. Multi-byte sequences split across TCP or WebSocket frame boundaries are now properly assembled without emitting replacement characters (`` / `[?]`).
- **Control Character & C1 Code Sanitization**: Safely strips non-printable control characters, DEL (`0x7f`), and C1 control codes (`0x80-0x9f`) that could otherwise corrupt terminal buffer layouts.

### 2. Standardized Terminal & UTF-8 Locale Environment
- **Default Docker Exec Environment**: Configured `TERM=xterm-256color`, `COLORTERM=truecolor`, and `LANG=C.UTF-8` as default execution environment variables in Docker daemon exec sessions.
- **Enhanced Locale Negotiation**: Ensures guest interactive programs (e.g. `htop`, `mc`, `zsh`, `tmux`) immediately detect UTF-8 and 24-bit TrueColor capability rather than degrading to ASCII or ISO-8859 fallback mode.

### 3. Extended DEC Private Mode Sequences & Buffer Controls
- **Cursor Visibility Controls**: Added support for DEC private mode cursor show/hide sequences (`?25h` and `?25l`), dynamically synchronizing cursor rendering in the terminal DOM.
- **Alternate Screen Buffer Handling**: Added alternate screen buffer transitions (`?1049h` / `?1049l`), allowing full-screen interactive programs like `nano` and `vi` to open in isolated buffers and cleanly restore the primary terminal screen upon exit.
- **Bracketed Paste & Cursor Mode Compatibility**: Gracefully recognizes and ignores bracketed paste (`?2004h/l`) and application cursor key (`?1h/l`) mode toggles without leaking escape sequences to screen text.

### 4. Monospace Typography & Grid Alignment Refinements
- **Font Stack & Metric Consistency**: Updated `static/styles.css` with a dedicated modern monospace font stack (`ui-monospace`, `SFMono-Regular`, `Menlo`, `Monaco`, `Consolas`, `monospace`), explicit character line-height, and subpixel antialiasing to guarantee strict column alignment across all platforms.

## Reliability & Fixes
- **Cross-Tier Validation Gate**: 100% compliance across all 58 frontend API routes and 623 DOM element contracts with 0 errors.
- **Comprehensive Test Suite**: All 243 unit, integration, and frontend Node VM tests passed successfully.

## [v1.0.29] - 2026-11-06
## Overview
Vigil v1.0.29 introduces an interactive, in-browser Docker Container Web Terminal, allowing system administrators to open zero-latency interactive shell sessions directly into running containers without SSH or external CLI tools. Powered by a raw duplex WebSocket streaming engine to the Docker daemon socket, an integrated ANSI rendering engine, dynamic PTY window resizing, zero-trust token authentication, and tamper-evident audit logging, this release delivers robust container management and terminal control right from the dashboard.

## Highlights & Enhancements

### 1. In-Browser Docker Container Web Terminal
- **1-Click Shell Access**: Launch interactive terminal sessions directly into any running Docker container from the Docker dashboard table using a dedicated **Terminal** action button.
- **Adaptive Shell Selection**: Intelligently attempts `/bin/bash` with graceful fallback to `/bin/sh` for lightweight, alpine, or minimal container images.
- **Dedicated Modal UI**: Clean, distraction-free modal terminal dialog equipped with container metadata badge, connection status indicator, shell selector, clear screen, and disconnect actions.

### 2. Real-Time WebSocket PTY Streaming Engine
- **Raw Duplex Streaming**: Fast, bidirectional streaming over `/api/docker/containers/{id}/terminal` bridging the web client directly to the Docker daemon UNIX socket exec stream.
- **Non-blocking Concurrency**: Built with Python `asyncio` and `websockets` for high-performance duplex forwarding between WebSocket frames and raw socket channels.

### 3. ANSI Rendering Engine & Complete Keyboard Controls
- **Rich Terminal Emulation**: Full support for standard 16-color ANSI palettes, 256-color palettes, text styling (bold, italic, underline, dim, inverted), cursor positioning, and line erase sequences.
- **Full Interactive Keyboard Control**: Native handling of shell control combinations (`Ctrl+C`, `Ctrl+D`, `Ctrl+Z`, `Ctrl+L`), Tab auto-completion, directional arrow keys, history recall, backspace, carriage returns, and clipboard text paste.

### 4. Interactive Window Resizing & Responsive Geometry
- **Dynamic Dimension Calculation**: Automatically calculates available character columns and rows based on client viewport and font dimensions.
- **Synchronized PTY Dimensions**: Dispatches live resize events (`{"type": "resize", "cols": N, "rows": N}`) that dynamically reconfigure the container's pseudo-terminal via the Docker exec resize API (`/exec/{id}/resize`).

### 5. Zero-Trust Security & Comprehensive Auditing
- **Strict Handshake Authentication**: Enforces token authentication prior to WebSocket upgrade; unauthenticated connection attempts are rejected immediately with RFC 6455 policy violation code `1008`.
- **Audit Logging**: Successful terminal connections log structured audit records (`docker.container.terminal_opened`) capturing container ID, authenticated user, remote IP, and timestamp.

## Reliability & Fixes

### Cross-Tier Quality & Automated Test Coverage
- **Automated Quality Gate**: Cross-tier verification gate (`validate_requirements.sh`) passed with 0 errors, 100% DOM contract compliance, and clean JavaScript syntax.
- **Comprehensive Test Suite**: 243/243 unit, integration, frontend VM, and security tests passing with 0 failures.

## [v1.0.28] - 2026-11-05
## Overview
Vigil v1.0.28 brings critical security hardening and frontend reliability enhancements. This release completes an exhaustive authentication audit across all 100+ backend API endpoints, establishes RFC 6455 WebSocket security policies, resolves a browser reload race condition in the Backups & Cron scheduler lifecycle, and introduces network request deduplication for optimal tab switching performance.

## Highlights & Enhancements

### 1. Exhaustive API Authentication Security Audit & Hardening
- **Zero-Trust Route Verification**: Audited and secured all 100+ API endpoints in `api.py`. Every non-whitelisted endpoint (system control, process management, file inspection, firewall/ufw manipulation, probe management, and backup operations) strictly enforces authentication returning `401 Unauthorized` for missing or invalid tokens.
- **WebSocket Policy Violation Enforcement**: WebSocket endpoints (`/ws/live` and `/ws/terminal`) enforce token verification upon connection handshake, immediately rejecting unauthenticated connections with RFC 6455 policy violation code `1008` (`Policy Violation`).
- **Strict Automated Auth Test Assertions**: Comprehensive security test suite (`ApiEndpointAuthenticationSecurityTests`) inspects all registered FastAPI routes programmatically to ensure no route is inadvertently exposed without authentication dependencies.

### 2. Backups & Cron Reload Lifecycle Resolution
- **Reliable Direct Page Reloads**: Resolved a frontend initialization race condition where performing a hard refresh or navigation directly to `/#backups` could skip initial data fetching before session authentication resolved.
- **Guaranteed Post-Auth Resolution**: Hooked `fetchBackupsAndCron()` into `onAuthenticatedSuccess()` to guarantee that backup archives, scheduled cron jobs, and backup storage targets are reliably fetched immediately upon successful session authentication.

### 3. In-Flight Network Request Deduplication
- **Tab Switching Optimization**: Added an in-flight network request deduplication guard (`isBackupsAndCronRefreshing`) in `static/app.js`.
- **Eliminated Redundant Requests**: Prevents concurrent, duplicate background network requests when operators rapidly switch tabs or trigger manual refreshes while a backup/cron fetch cycle is already underway.

## Reliability & Fixes

### Cross-Tier Quality & Contract Verification
- **Automated Quality Gate**: Cross-tier verification gate (`validate_requirements.sh`) passed with 0 errors, full HTML DOM contract alignment, and clean JavaScript syntax.
- **Comprehensive Test Suite**: 232/232 unit, integration, and security tests passing with 100% contract compliance and 0 failures.

## [v1.0.27] - 2026-11-05
## Overview
Vigil v1.0.27 introduces significant enhancements to the unified Cron Scheduler and Discord automated reporting pipeline. Scheduled storage and health tasks now broadcast rich Discord status reports for routine healthy operations, jobs feature full interactive edit workflows in the dashboard, and job notification routing is surfaced with clear visual badge indicators.

## Highlights & Enhancements

### 1. Rich RAID Status Embeds for Scheduled Storage Checks
- **Automated Health Dispatch**: `storage.smart_check` scheduled tasks now format and dispatch full RAID Discord embeds (including array state, component device telemetry, disk health metrics, and formatted `duf` disk space breakdown) on every execution.
- **Healthy State Visibility**: Routine checks on healthy `clean` or `active` arrays now reliably deliver affirmative status reports to Discord instead of silently terminating on non-degraded checks.

### 2. Default Success Notifications for Summary Tasks
- **Intelligent Defaults**: Summary and observability tasks (`storage.smart_check` and `notifications.summary`) now default to `notify_on_success = true`.
- **Operator Confidence**: Routine status tasks immediately confirm operational health across notification channels without requiring manual configuration tweaks.

### 3. Interactive Cron Job Edit Mode
- **1-Click Form Pre-population**: Added an **Edit** action to each job in the Cron table that seamlessly loads the job parameters, task type, schedule timing, notification routing, and configuration payload into the creation form.
- **Visual Editing Context**: An active `Editing: <Name>` banner highlights the job being updated, toggling the form submission button to **Update Scheduled Job** and providing a **Cancel Edit** button to discard changes and reset the form.

### 4. Notification Routing Badges
- **Visual Notification Policies**: Introduced clear badge indicators in the Cron table displaying notification behavior at a glance:
  - `🔔 Success + Failure` (Success & Failure notifications enabled)
  - `🔔 Failure Only` (Failure notifications enabled)
  - `🔕 Muted` (No automated notifications)

## Reliability & Fixes

### Cross-Tier Quality & Contract Verification
- **DOM & API Alignment**: Full contract alignment across 58 API routes and 611 DOM elements.
- **Test Suite Pass**: 230/230 tests passed with 0 failures, validating cron job update APIs, Discord embed dispatches, task defaults, and responsive UI interactions.

## [v1.0.26] - 2026-11-05
## Overview
Vigil v1.0.26 introduces the **Historical Telemetry Time-Machine**, giving host operators deep operational visibility into past system performance. Operating alongside real-time live telemetry, the Time-Machine features an embedded SQLite time-series persistence engine, adaptive bucketed aggregation, and a responsive High-DPI multi-series chart with interactive tooltips and summary metrics across 1-hour, 6-hour, 24-hour, and 7-day observation windows.

## Highlights & Enhancements

### 1. SQLite Time-Series Persistence & 7-Day Auto-Pruning
- **Dedicated Telemetry Storage**: Persists periodic host resource snapshots into a indexed SQLite table (`telemetry_samples`) capturing CPU load, memory utilization, swap usage, disk capacity, temperature, and network RX/TX transfer rates.
- **Background Sampling Engine**: Automatic background evaluation captures system metrics every 15 seconds without blocking API requests or degrading host performance.
- **Automated Retention Pruning**: Enforces a rolling 7-day data retention policy (`prune_telemetry_samples`), automatically cleaning up expired records to maintain a lean database footprint.

### 2. Bucketed Aggregation Engine & Telemetry History API
- **Dynamic Downsampling**: Added `GET /api/telemetry/history?range={1h|6h|24h|7d}&points={10-300}` delivering time-bucketed averages and interpolated points tailored for canvas rendering.
- **Statistical Summaries**: Computes comprehensive min, max, and average resource utilization metrics for each window, providing operators with immediate high-level capacity insights.
- **Manual Sample Ingestion Endpoint**: Added `POST /api/telemetry/sample` enabling on-demand sample capture or external metric ingestion.

### 3. Interactive Overview Time-Machine Canvas & Selector Toolbar
- **Modern Selector Toolbar**: Introduced intuitive pill controls (`Live`, `1h`, `6h`, `24h`, `7d`) in the dashboard Overview tab, allowing instant switching between real-time ticks and historical windows.
- **High-DPI Multi-Series Canvas Chart**: Custom retina-ready canvas renderer graphing CPU (%), Memory (%), and Network Transfer Rates (KB/s) with clean grid lines and formatted time axes.
- **Interactive Hover Tooltips & Summary Stats**: Real-time crosshair and hover tooltips showing precise timestamped metrics at any cursor position, backed by min/avg/max summary indicator chips.

## Reliability & Fixes

### Quality & Contract Verification
- **Cross-Tier Contract Integrity**: Verified 58 frontend API route endpoints and 607 DOM element bindings with zero mismatches or unhandled exceptions.
- **Comprehensive Automated Test Suite**: Expanded test suite to 229 tests (`229 passed, 0 failures`) covering time-series schema creation, retention pruning, bucket downsampling, summary statistics, API authentication, and UI DOM bindings.

## [v1.0.25] - 2026-11-04
## Overview
Vigil v1.0.25 streamlines system data protection and schedule management by introducing a **Configurable Backup Archive Storage Directory**, simplifying the backup architecture to **Pure Host Directory Backups**, and **Consolidating Notification Crons** directly into the unified Cron Scheduler.

## Highlights & Enhancements

### 1. Configurable Backup Archive Storage Directory
- **Custom Archive Destination**: Users can now configure custom host filesystem storage locations (e.g., dedicated storage pools or external mount paths) for backup tarballs directly from the Backups & Cron settings (`POST /api/backups/config`).
- **Persistent App Settings**: The configured directory path is persisted in SQLite `app_settings` under `backup_storage_dir` and resolves dynamically at runtime with fallback to `~/.vigil/backups`.
- **Safety Validation Guards**: Enforces strict path security, rejecting dangerous destinations such as root (`/`), `/etc`, `/usr`, `/proc`, `/sys`, and virtual kernel filesystems.
- **Modern Dashboard Controls**: Added `#backup-dir-input` and `#backup-dir-save-btn` inline controls with feedback indicators in the Backups & Cron view.

### 2. Streamlined Pure Directory Backups
- **Focused Host Directory Operations**: Replaced stack-specific container discovery and intrusive container pause/stop logic with clean, high-performance host directory backups (e.g., `~/appdata` or `~/docker/plex`).
- **Archive Integrity & Pruning**: Full support for `.tar.gz` archive creation, SHA-256 integrity checksumming, customizable exclusion glob patterns, and automated retention pruning.
- **Cron Task Harmonization**: Replaced `backup.stack` cron task types with `backup.directory` across the job creation UI, schema, and scheduler engine.

### 3. Consolidated Notification Crons in Backups & Cron
- **Unified Scheduling Architecture**: Centralized all periodic report schedules (`notifications.summary`) inside the Backups & Cron scheduler engine, eliminating duplicate schedule implementations.
- **Direct Navigation Workflows**: Notification Center schedule management buttons now link directly to the Backups & Cron scheduler view, providing seamless cross-tab coordination.

## Reliability & Fixes

### Quality & Contract Verification
- **Cross-Tier Contract Integrity**: Verified 57 frontend API route contracts and 589 DOM element bindings across the dashboard with 0 mismatches or broken selectors.
- **Comprehensive Automated Test Suite**: 221 out of 221 tests passed cleanly (`221 passed, 0 failures`) in pytest, verifying backup configuration endpoints, directory path safety validation, cron scheduler execution, and frontend DOM bindings.

## [v1.0.24] - 2026-11-04
## Overview
Vigil v1.0.24 delivers a major expansion to host observability and infrastructure resilience, introducing three flagship capabilities: real-time **SSH Intrusion Detection & IP Ban Management** with Fail2ban/UFW correlation, **Docker Stack Folder & Host Directory Backups** with checksummed archive retention and 1-click restores, and a **Unified Vigil Cron Scheduler** for automated recurrent maintenance, backup jobs, and system tasks.

## Highlights & Enhancements

### 1. SSH Intrusion Detection, Active Sessions & IP Ban Manager
- **Automated SSH Log & Intrusion Scanner**: Scans `/var/log/auth.log` and `journalctl` (`sshd`/`ssh`) for brute-force attacks, invalid usernames, and pre-auth disconnects, grouping top offending IPs, incident counts, targeted usernames, and timestamps (`GET /api/network/ssh-security`).
- **Fail2ban & UFW Ban Correlation**: Correlates active Fail2ban sshd jails and UFW firewall deny/reject rules into a unified security table with 1-click Ban/Unban actions (`POST /api/network/ssh-security/ban`).
- **Self-Lockout Safeguards & Audit Logging**: Prevents accidental administrator lockouts by rejecting loopback and active admin IP addresses, and records all ban/unban mutations to the tamper-evident audit log.
- **Active Sessions & Login History**: Real-time table of active SSH/TTY terminal sessions (`who`/`psutil.users()`) displaying user, TTY, remote IP, PID, and login timestamp, alongside recent login history (`last`).

### 2. Docker Stack Folder & Host Directory Backups
- **Automated Stack & Directory Discovery**: Automatically discovers Docker Compose stack folders (locating `docker-compose.yml` / `compose.yaml`) and custom host directories with fast metadata inspection.
- **Compressed Archive Creation**: Generates compressed `.tar.gz` snapshots with SHA-256 integrity verification, customizable exclusion patterns (e.g. `.git`, cache directories, temporary files), and automatic retention policies.
- **Safe Execution & Container Pause Support**: Optional container shutdown or pause during backup creation to ensure transactional data consistency before automatic restart.
- **One-Click Restore & Repository Management**: Dedicated backup repository browser with direct archive download, deletion, storage usage telemetry, and safe 1-click restore workflows.

### 3. Unified Vigil Cron Scheduler
- **Integrated Scheduling Engine**: Full-featured cron daemon running directly within the Vigil runtime supporting standard 5-part cron syntax expressions with human-readable interval evaluations and next-run calculations.
- **Built-in Task Types**: First-class support for automated backups (`backup`), Docker container/image cleanup (`docker_prune`), Debian package cache refreshes (`apt_update`), and arbitrary shell command executions.
- **Interactive Control & Run History**: Instant job enable/disable toggling (`POST /api/cron/jobs/{id}/toggle`), manual on-demand execution (`POST /api/cron/jobs/{id}/run`), and persistent execution history logging with exit codes, durations, and stdout/stderr output capture.
- **Automated Alerting Integrations**: Optional notification triggers upon job failure or success dispatched via Vigil's Notification Center.

## Reliability & Fixes

### Quality, Security & Contract Verification
- **Cross-Tier Contract Integrity**: Validated all 56 frontend API route contracts and 595 DOM element bindings across the dashboard with zero broken selectors or route mismatches.
- **Comprehensive Automated Test Suite**: Expanded unit, integration, and UI execution tests in `test_main.py` with 221 passing tests (`221/221` passed, 0 failures), covering backup creation, exclusion filters, retention pruning, cron expression parsing, and SSH security actions.
- **Strict Path Traversal Protection**: Implemented strict filesystem path resolution and safety validations preventing directory traversal or unauthorized directory access across backup creation and restoration endpoints.

## [v1.0.23] - 2026-11-04
## Overview
Vigil v1.0.23 streamlines the primary dashboard navigation by organizing all eleven operational views into four balanced, cohesive sections, eliminating single-item section headers and improving workflow discoverability.

## Highlights & Enhancements

### Changed
- **Reorganized 4-Pillar Sidebar Navigation**: Streamlined the sidebar navigation menu into four balanced operational sections (**Monitoring & Health**: Overview, Storage, Uptime & Probes; **Workloads & Runtime**: Containers, Services, Processes; **System & Security**: Packages, Network & Security, Users & Groups; **Alerts & Auditing**: Notifications, Audit Log), eliminating single-item section headers.

## Reliability & Fixes

### Quality & Testing
- **Sidebar Navigation Hierarchy Contract Verification**: Updated `test_sidebar_navigation_hierarchy_and_view_switching` in `test_main.py` to assert the 4-pillar section ordering and button groupings across all 204 automated unit, integration, and UI/API contract tests (`204/204` passing, 0 contract discrepancies).

## [v1.0.22] - 2026-11-04
## Overview
Vigil v1.0.22 introduces comprehensive service uptime and SSL monitoring via Vigil Sentinel, physical NVMe/SATA disk S.M.A.R.T. and thermal telemetry in the Hardware Health Cockpit, active network port scanning and UFW firewall management, real non-interactive package installation, and Docker volume pruning, alongside continuous storage live streaming and UI chart rendering fixes.

## Highlights & Enhancements

### Added
- **Vigil Sentinel (Service Uptime & Health Probes)**: HTTP/S, TCP Port, and ICMP Ping monitors with 24-hour heartbeat bars, latency tracking, SSL certificate expiration monitoring, and Notification Center alerting.
- **Hardware Health Cockpit (S.M.A.R.T. & Thermal Telemetry)**: Physical NVMe/SATA disk discovery, `smartctl` JSON health attributes, drive temperatures, wear levels, power-on hours, `#smart-modal` attribute drilldown, and Linux `sysfs` CPU/chassis thermal & fan sensors.
- **Network Security, Open Ports & UFW Firewall Manager**: Active listening TCP/UDP socket scanner correlated with PIDs, users, and Docker containers, `PUBLIC` vs `LOCAL` exposure classification, and UFW firewall rule management (`ALLOW`, `DENY`, `REJECT`, `LIMIT`, delete).
- **Real Package Upgrade Installation**: Non-interactive `apt-get` package installation (`Install All Updates`, per-package `Install`, and modal `Install Now`) alongside dry-run simulation.
- **Docker Volume Pruning**: Added `Prune Unused Volumes` (`POST /api/docker/volumes/prune`) and enhanced volume removal in Container Management.

### Changed
- **Storage Live Stream & RAID Metadata**: Removed the manual refresh interval selector and "5s Rebuild Boost" badge in favor of continuous live streaming, and populated RAID `UUID`, `Layout / Chunk`, and `Consistency Policy` from `sysfs`.

## Reliability & Fixes

### Fixed
- **Overview CPU Processor Cores Utilization Chart `0%` Visibility**: Fixed `.cpu-unified-chart-container` fixed-height CSS clipping (`height: auto`) that cut off the bottom of `#cpu-cores-unified-canvas` below `25%`, and added the `0%` Y-axis label and baseline padding in `drawCpuCoreGraph`.
- **Packages Navbar Badge**: Removed the duplicate `#nav-tab-packages-badge` so only a single update count badge renders on the Packages navigation tab.

## [v1.0.17] - 2026-11-01
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

## [v1.0.15] - 2026-11-01
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

## [v1.0.13] - 2026-11-01
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

## [v1.0.0] - 2026-10-31
## Overview
Vigil v1.0.0 marks a landmark milestone, evolving Vigil into a unified, zero-overhead **Linux Host Operations & Observability Suite**. This major release introduces full systemd service management, an interactive process explorer, Linux user and group administration, a security and activity audit log system, and an optimized, balanced telemetry dashboard layout.

## Highlights & Enhancements

### 1. Systemctl Services Management
- **Live Daemon Observability**: Real-time status monitoring for systemd service units with active/sub state tracking, instant failed unit alerts, and summary counters.
- **Interactive Lifecycle Controls**: Direct `start`, `stop`, `restart`, `enable`, and `disable` actions with input sanitization and safety guards protecting core system daemons (`systemd`, `dbus`, `vigil`).
- **Integrated Journald Log Viewer**: Built-in modal to stream and inspect recent journalctl service logs directly from the browser.

### 2. Interactive Process Explorer
- **Live Process Telemetry**: Sortable task list detailing PID, Process Name, User, CPU %, RAM %, Thread count, and Status.
- **Visual Utilization Meters**: Mini inline progress bars for per-process CPU and Memory utilization.
- **Dynamic Sorting & Filtering**: Real-time search query filtering and sorting by CPU %, Memory %, PID, or Name.
- **Safe Process Termination**: Secure modal controls to send `SIGTERM` or `SIGKILL` signals, enforced by a safety blacklist guarding PID 1, kernel threads, and the Vigil process itself.

### 3. Linux User & Group Management
- **Host Account Categorization**: Clear distinction between **Human / Regular Users** (`UID >= 1000`) and **System Daemons** (`UID < 1000`), detailing display names, home directories, login shells, and group memberships.
- **Group Administration**: Group explorer displaying GIDs, membership lists, and member management.
- **Account Lifecycle Actions**: Modals to create, modify, and delete host users and groups (`useradd`, `usermod`, `userdel`, `groupadd`, `groupdel`, `gpasswd`).
- **System Protection Locks**: Safeguards preventing deletion of `root`, `sudo`, `wheel`, `shadow`, or `docker` accounts.

### 4. Security & Activity Audit Log
- **Persistent SQLite Event Log**: Dedicated `audit_logs` store capturing operational history, configuration changes, and security events.
- **Authentication Security Tracking**: Automated logging of failed login attempts (with client IP, timestamp, and failure reason), successful logins, user logouts, and initial admin setup.
- **Configuration & Operations Diffs**: Records application settings changes, notification threshold adjustments, systemctl service commands, and process termination signals.
- **Audit Explorer UI**: Filterable timeline by severity (`INFO`, `WARNING`, `SECURITY`), event type, or keyword search with expandable JSON metadata details.

### 5. System Metrics Layout Optimization
- **Enhanced Health Status Ribbon**: Prominent top overview ribbon monitoring **Storage**, **Containers**, **Alerts**, and **Services** with direct navigation links.
- **Balanced Telemetry Grid**: Re-sorted into clean multi-column rows:
  - **Row 1**: CPU Telemetry (overall load, load averages 1m/5m/15m, core count, per-core drilldown) & Memory Telemetry (vector SVG rolling trend chart, breakdown chips: Used, Cached, Free).
  - **Row 2**: Storage & Filesystems table (Root `/` and all mounted storage volumes with capacity gauges) & Host Platform Details (uptime, OS distribution, kernel version, boot time).

### 6. UI Polish & Navigation
- **Collapsed Navbar Fix**: Strict vertical icon stacking for user avatar and logout buttons within `.user-session-box`, ensuring zero border overflow in collapsed sidebar mode.

## [v0.9.7] - 2026-10-30
## Overview
Vigil v0.9.7 introduces a visual overhaul for Discord notifications—featuring high-contrast unicode telemetry gauges, multi-column inline grids, dynamic status banners, and host metadata footers. This release also debuts the zero-friction automated installer (`install.sh`), automatic systemd service deployment (`vigil.service`), and the global `vigil` command-line helper tool.

## Highlights & Enhancements

### 1. Modernized Discord Notification Embeds
- **Unicode Telemetry Gauges**: Real-time progress bars (`▰▰▰▰▰▱▱▱▱▱`) and humanized byte scaling for CPU, RAM, and storage mounts.
- **Dynamic Status Banners**: Top-level alert banner reflecting array and system health (`🟢 All Systems Operational`, `🟡 RAID Array REBUILDING`, `🔴 Attention: RAID Array DEGRADED`).
- **Multi-Column Inline Grids**: Structured parallel field cards displaying CPU load with core counts, memory usage with exact GB metrics, and uptime with load averages.
- **Host Context & Native Timestamps**: Author header with host badge (`Vigil System Monitor [<hostname>]`), system footer (`Vigil v0.9.7 • <OS/Kernel> • <hostname>`), and native Discord ISO timestamps.
- **Color-Coded Severity Palette**: Test/Healthy (Emerald `0x2ECC71`), Warnings/Elevated (Amber `0xF59E0B`), and Critical alerts (Coral Red `0xEF4444`).
- **Modernized RAID Embeds**: Replaced raw text blocks with structured status cards, device breakdown metrics, and drive health indicators.

### 2. Zero-Friction Automated Installer (`install.sh`)
- **Single-Line Quickstart**:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/tony-vs-code/vigil/main/install.sh | sudo bash
  ```
- **Universal Linux Support**: Automated package detection and prerequisite installation across Debian/Ubuntu (`apt`), Fedora/RHEL (`dnf`/`yum`), and Arch (`pacman`).
- **Astral `uv` Virtual Environment**: High-speed, reproducible dependency isolation with automatic fallback to standard Python `venv`.
- **Automated Systemd Deployment**: Configures, enables, and starts `/etc/systemd/system/vigil.service` with automatic restarts and secure environment file handling.

### 3. Global `vigil` CLI Management Tool
- Symlinks `/usr/local/bin/vigil` for instant server management from any shell:
  - `vigil status`: Service health and uptime
  - `vigil logs`: Live streaming journald service logs
  - `sudo vigil restart`: Immediate daemon reload
  - `sudo vigil update`: Automated pull, dependency sync, and service restart
  - Application subcommands: `vigil create-user`, `vigil test-notification`, `vigil status`

### 4. Documentation & Repository Cleanup
- Updated `README.md` with the 1-line installer and `vigil` CLI commands.
- Removed legacy `systemd/raidcheck*.service` files and replaced them with the unified `vigil.service`.

## [v0.9.5] - 2026-10-30
## Overview
Vigil v0.9.5 delivers a major layout and observability upgrade featuring decondensed system telemetry, a collapsible side navigation bar, CPU per-core drilldowns, memory SVG trend visualization, storage filesystem mounts, real-time alert data, and streamlined notification delivery channel management.

## Highlights & Enhancements
- **Brand Identity**: Complete evolution to **Vigil: Lightweight Linux Host Operations & Observability Suite** across documentation, interfaces, and metadata.
- **Decondensed System Metrics Layout**: Re-spaced overview metrics into a spacious 2-column desktop layout, giving CPU and memory telemetry ample breathing room.
- **Health Status Ribbon**: Replaced redundant bottom status summary cards with a sleek, low-profile status ribbon on System Metrics with live array, container, and alert status indicators.
- **Collapsible Side Navigation**: Shrinks smoothly to a 68px icon-only mini bar with tooltips and `localStorage` state persistence.
- **CPU & Memory Telemetry Drilldowns**: Interactive per-core CPU load grid, logical core count badges, and rolling SVG memory usage trend charts with detailed Used/Cached/Free memory chips.
- **Storage Volumes & Filesystems**: Dedicated storage breakdown card in RAID view tracking mount capacities, available space, and filesystem types.
- **Stack Status Clean-up**: Stripped container count strings from Compose stack statuses for cleaner state visualization.
- **Unified Notification Management**: Combined automated report cadence and topic selection into one unified card, and expanded the Notification Audit History table to full width.
- **Delivery Channels Persistence**: Renamed channel save control to **"Save Delivery Channels"**, saving Discord, Telegram, and Generic Webhook settings simultaneously.
- **Live Test Notifications**: Test alerts compile and transmit real host metrics, storage capacity, and RAID status.

## Reliability & Fixes
- **Single Feedback Banner**: Resolved duplicate and triplicate flash message banners by strictly targeting a single DOM feedback banner with a 5-second auto-clear timer.
- **Static Asset Cache Busting**: Automated asset version stamping (`?v=0.9.5`) to eliminate stale cached scripts in client browsers.
- **Test Suite Verification**: All 101 unit and integration test assertions pass with 100% contract compliance.

## [v0.9.1] - 2026-10-30
## Overview
Accurate Docker container update detection, instant update schedule loading, and automated update frequency persistence.

## Highlights & Enhancements
- Remote Registry Digest Resolution: Multi-stage remote image manifest inspection via buildx imagetools inspect --raw and Docker manifest inspect matching host architecture.
- Container Update Accuracy: Accurately compares remote manifest digest against local image RepoDigests, replacing the hardcoded false flag so container updates are reliably discovered and flagged.
- Decoupled Update Schedule Loading: Decoupled update schedule and settings fetching from registry network scans, eliminating UI lag and enabling instantaneous tab switching.
- Concurrent Update Checking & Caching: Added 120-second TTL caching and a 5-worker thread pool for concurrent remote registry checks with manual refresh bypass.

## Reliability & Fixes
- Container Update Action Guard: The "Update" button now only appears on containers with pullable image updates, showing a clean "Current" badge on up-to-date containers.
- "Update All" Toggle: The "Update All" button automatically disables when no container updates are available, preventing unnecessary pull jobs.
- Auto-Check Persistence: Added immediate auto-save on select change for Docker image check frequency with persistent SQLite storage.
- Comprehensive Test Coverage: All 80 unit and integration tests passing.

## [v0.9.0] - 2026-10-30
## Overview
Direct Linux sysfs and Docker Unix socket streaming, interactive update modal, in-app changelog, and clean SVG icons.

## Highlights & Enhancements
- Direct POSIX statvfs implementation replacing duf and df subprocess calls with microsecond-level disk capacity metrics.
- Direct Linux Sysfs RAID inspection reading /sys/block/md* for zero-fork kernel status and real-time resync/rebuild progress.
- Docker Engine Unix domain socket client (/var/run/docker.sock) in DockerManager for zero-subprocess container inspections, stats, volumes, and networks.
- Real-time Docker container lifecycle event listener (/events) broadcasting instant WebSocket updates on container failure or status change.
- Redesigned update experience featuring an unobtrusive header notice button and an interactive Software Update modal.
- In-app Changelog viewer with an authenticated GET /api/changelog endpoint and automatic CHANGELOG.md maintenance on every release.
- Professional typographic styling and crisp SVG vector stroke icons across all components, eliminating emojis entirely.

## Reliability & Fixes
- Added authenticated GET /api/docker/updates/config and auto-save on select change for Docker image auto-check frequency.
- Restricted the container Update action button to only appear on containers with pullable image updates.
- Added comprehensive unit and integration tests covering sysfs RAID parsing, statvfs metrics, Docker socket requests, and changelog endpoints.
- Self-testing workflow enforcement across all subagents.
- Automated Pull Request creation and branch management via GitHub CLI.

## [v0.8.1] - 2026-10-30
### Highlights & Enhancements
- Docker refresh latency optimizations and background stats caching.
- Enhanced real-time container metrics.

## [v0.8.0] - 2026-10-30
### Highlights & Enhancements
- Rebrand UI to SystemAlerts Server Manager.
- Consolidated system and container monitoring views.

## [v0.7.1] - 2026-10-29
### Highlights & Enhancements
- Real-time WebSocket live streaming and badges for Docker containers and subtabs.

## [v0.7.0] - 2026-10-29
### Highlights & Enhancements
- Container stack and status filtering.
- Volume inspection modal with disk usage breakdowns.
- Automated update scheduling and account timezone support.
- External stack protections.

## [v0.6.0] - 2026-10-28
### Highlights & Enhancements
- Docker Container and Compose Stack management with UI and automated tests.

## [v0.5.2] - 2026-10-28
### Reliability & Fixes
- Test suite hermeticity improvements for CI environments.

## [v0.5.1] - 2026-10-28
### Highlights & Enhancements
- Discord test notifications in web mode via REST API fallback.
- Discord setup moved into Notifications tab with asset cache-busting.

## [v0.5.0] - 2026-10-28
### Highlights & Enhancements
- Dedicated Notifications & Logs dashboard.
- Discord toggle in settings and on-demand test notifications.

## [v0.4.0] - 2026-10-28
### Highlights & Enhancements
- Multi-rate per-module refresh controls.
- Adaptive RAID WebSocket streaming during resync or recovery.

## [v0.3.0] - 2026-10-28
### Highlights & Enhancements
- WebSocket real-time modular dashboard streaming (/api/ws).

## [v0.2.3] - 2026-10-28
### Reliability & Fixes
- Resilient release updater when uv is outside systemd PATH.

## [v0.2.2] - 2026-10-28
### Reliability & Fixes
- Release self-updater handling for dirty lockfile checkouts.

## [v0.2.1] - 2026-10-28
### Reliability & Fixes
- Release packaging and version alignment.

## [v0.2.0] - 2026-10-28
### Highlights & Enhancements
- SQLite authentication, web settings modal, and dynamic md RAID device detection.
- Release-tag self-updater.
