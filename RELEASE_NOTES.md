# Release v1.0.27: Cron Job Edit Mode, RAID Discord Status Embeds, and Notification Badges

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
