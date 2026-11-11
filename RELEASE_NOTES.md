# Release v1.0.43: Real-Time Package Installation Output Streaming

## Overview
Vigil v1.0.43 introduces **Real-Time Terminal Streaming** for both host system (`apt-get`) and Python virtual environment (`uv`) package installations and upgrades. Operators no longer need to wait on opaque spinners during long-running package operations. Subprocess output lines stream immediately into the Package Manager UI terminal modal with auto-scrolling, live ANSI/VT100 stripping, automated pre-restart health checks, automated lockfile rollbacks, and instant cache invalidation upon completion.

## Highlights & Enhancements

### 1. Real-Time Terminal Streaming (`/stream`)
- **Direct Subprocess Chunk Streaming**: Added streaming endpoints `/api/packages/upgrade/stream` and `/api/python/packages/upgrade/stream` using HTTP chunked transfer (`Transfer-Encoding: chunked` / `text/plain`).
- **Live Terminal Output Modal**: Real-time terminal (`#pkg-upgrade-terminal`) in the frontend renders live logs as chunks arrive from `apt-get` and `uv lock` / `uv sync` / diagnostics smoke tests.
- **Auto-Scrolling Log Stream**: Terminal log viewer automatically scrolls down and stays pinned to the bottom during live subprocess execution.

### 2. Resilience, Safety & Automated Rollback
- **Pre-Restart Automated Smoke Test Verification**: Automatically validates system diagnostics before triggering any service reloads following package upgrades.
- **Automated Lockfile Rollback**: If a Python package upgrade fails post-sync smoke testing or package resolution, the previous `uv.lock` is automatically restored.
- **Backward-Compatible Non-Streaming Fallback**: Preserved existing JSON endpoints (`/api/packages/upgrade` and `/api/python/packages/upgrade`) with full support for Node VM test environments and external API automation.
- **Dynamic Feedback & Cache Refresh**: Live toast notifications alert operators upon operation success or failure, accompanied by immediate background cache refreshes to reflect new package versions.

## Reliability & Fixes
- **Comprehensive Test Suite**: Full cross-tier QA verification passing cleanly with **387 tests passed, 0 failed** across all backend routes, unit tests, and cross-tier UI contracts (63 API routes, 718 DOM elements).
- **Node VM Contract Compatibility**: Verified client-side streaming and fallback rendering inside headless Node VM test harnesses.
