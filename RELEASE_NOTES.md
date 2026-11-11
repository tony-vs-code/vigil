# Release v1.0.44: Server-Sent Events Live Streaming & Reactive DOM Patching

## Overview
Vigil v1.0.44 introduces **Server-Sent Events (SSE) Live Data Streaming** and **Reactive In-Place DOM Patching** across the observability dashboard. Real-time telemetry, Docker lifecycle events, RAID status changes, and outbound notification deliveries are pushed instantly over persistent SSE connections without WebSocket reverse-proxy configuration hurdles. The UI now applies fine-grained DOM patches without layout redraws or flicker, while adaptive polling automatically throttles periodic REST requests during active streaming to minimize CPU and network overhead.

## Highlights & Enhancements

### 1. Persistent Server-Sent Events (SSE) Streaming
- **Dedicated Streaming Endpoints**: Introduced `/api/events` and `/api/stream/events` serving continuous, standards-compliant `text/event-stream` feeds for telemetry ticks, container lifecycle events, RAID health updates, and system notifications.
- **Reverse Proxy Friendly**: SSE operates over standard HTTP/HTTPS channels, bypassing common reverse proxy (Nginx, Caddy, Traefik, Cloudflare) WebSocket upgrade timeouts, sticky session issues, and connection drops.
- **Client Live Stream Indicator**: Added header indicator (`#live-stream-indicator` and `#live-stream-text`) displaying real-time stream status with animated pulse dots (`Live Stream Active` vs `Polling Fallback`).

### 2. Reactive In-Place DOM Patching
- **Zero-Flicker Telemetry Updates**: CPU, Memory, Disk gauges, per-core meters, and load average badges update smoothly in-place without wiping element trees or causing scroll jumps.
- **Granular Container & Host Targeting**: Targeted element mutations update container status pills and host metrics without re-rendering tables or disturbing operator focus.
- **Seamless State Reconciliation**: Retains DOM state, input focus, expanded cards, and modal states during live background telemetry updates.

### 3. Adaptive Polling Throttling & Fail-Safe Fallbacks
- **Bandwidth & CPU Conservation**: Active SSE streaming automatically throttles periodic polling intervals from 5s/10s down to a relaxed 60s watchdog cycle, significantly reducing server CPU cycles and network payload volume.
- **Instant Automatic Fallback**: Seamlessly resumes standard 5s/10s interval polling if the SSE connection drops, encounters network errors, or runs in environments lacking native `EventSource` support (such as headless Node VM test harnesses).
- **Graceful Reconnection**: Automatic exponential backoff and reconnection logic ensures immediate stream resumption when network connectivity is restored.

## Reliability & Fixes
- **Full Backward Compatibility**: Preserved complete compatibility across all 63 REST API routes, existing WebSocket endpoints, and test harnesses.
- **Comprehensive Cross-Tier Verification**: 100% QA pass with **404 tests passing** and 0 failures across backend telemetry, streaming endpoints, SSE lifecycle handlers, and Node VM headless DOM contract tests.
