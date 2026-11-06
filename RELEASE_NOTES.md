# Release v1.0.33: Multi-Channel Alert Hub

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
