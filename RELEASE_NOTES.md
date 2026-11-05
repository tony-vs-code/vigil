# Release v1.0.25: Configurable Backup Storage, Pure Host Directory Backups & Notification Cron Consolidation

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
