# Release v1.0.28: API Authentication Security Audit & Backups Reload Lifecycle Fix

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
