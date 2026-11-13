# Release v1.0.46: Unified Live State Engine and Continuous Hardware Streaming

## Overview
Vigil v1.0.46 introduces a unified reactive live state architecture (`LiveStateHub` backend service and `window.VigilLiveStore` frontend state engine) delivering continuous 1.0-second hardware telemetry streaming with zero background polling overhead, fluid linear meter transitions, and instant tab switching.

## Highlights & Enhancements

### 1. Unified Reactive Live State Engine (`LiveStateHub` & `window.VigilLiveStore`)
- **Backend Hub Coordination**: `LiveStateHub` centralizes real-time event broadcasting and high-frequency telemetry streaming across Server-Sent Events (SSE) connections.
- **Global Reactive Live Store**: Introduced `window.VigilLiveStore` featuring a pub/sub subscription model (`subscribe(slice, callback)` and `getState()`) with immediate replay upon subscription and wildcard (`*`) listener support for full state tracking across the entire dashboard.

### 2. Dedicated 1.0-Second Live Telemetry Worker
- **Non-Blocking Telemetry Sampling**: Implemented `live_telemetry_stream_worker` executing sub-millisecond (`<0.2ms`) `psutil` sampling ticks every 1.0s.
- **Client-Aware Efficiency**: The worker runs actively when subscribers are connected and remains completely idle when zero SSE clients are open, eliminating unnecessary server overhead.
- **Comprehensive Hardware Metrics**: Streams real-time aggregate CPU, per-core CPU breakdown, memory utilization, swap usage, and Linux 1/5/15-minute load averages.

### 3. Elimination of Background Polling for Metrics
- **Zero Redundant REST Requests**: The periodic `refreshDashboard()` interval timer is paused whenever the live stream is connected; all telemetry arrives continuously over the SSE channel.
- **Flicker-Free Live Operation**: Eliminates UI redraw flickers and redundant HTTP GET roundtrips.
- **Resilient Fallback**: If SSE disconnects or encounters network interruptions, standard interval polling automatically resumes as a transparent safety net until reconnection.

### 4. Continuous Rolling Waveforms & Linear Meter Gliding
- **Oscilloscope Waveforms**: 1.0s telemetry ticks directly append to `cpuCoresHistoryBuffers` and `memoryHistoryBuffer`, driving real-time rolling canvas graphs rendered smoothly at 60 FPS via `requestAnimationFrame()`.
- **Continuous Linear Gliding**: Tuned progress bar meters with `.progress-fill` transitions using `1s linear` easing, allowing gauges and utilization bars to breathe and glide continuously with actual hardware fluctuations.

### 5. Instant Zero-Latency Tab Switching
- **Instant State Hydration**: Switching between dashboard views (System, Docker, Storage, Packages, Probes, Logs, Settings) instantly hydrates UI components directly from `window.VigilLiveStore.getState()`.
- **Zero Loading Spinners**: Operators experience instant view rendering without awaiting REST fetch roundtrips.

## Reliability & Fixes
- **100% Contract Integrity**: Maintained full backward compatibility across all 63 REST API endpoints and 718 client DOM element contracts.
- **Comprehensive Test Coverage**: Expanded test suite with dedicated unit tests in `tests/test_sse_stream.py` verifying non-blocking worker execution, schema compliance, and subscriber lifecycle management.
- **QA Gate Verification**: Automated cross-tier quality test suite signed off with **412/412 tests passing** and 0 failures.
