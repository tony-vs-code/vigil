# Release v1.0.24: SSH Intrusion Detection, Docker Stack Backups & Unified Cron Scheduler

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
