# Release v1.0.35: Sub-Second Live Data Streams & Zero-Clutter Reactive UI

## Overview
Vigil v1.0.35 delivers a truly reactive, sub-second live telemetry architecture and a zero-clutter interface. Core host metrics now stream continuously at **500ms (2 Hz)** over WebSockets with smart in-memory TTL caching to eliminate subprocess overhead, while active dashboard views and container logs multiplex automatically over the single `/api/ws` connection. Coupled with an instant event-driven broadcast bus and a streamlined, button-free live UI, operators see every system state change immediately without manual refreshes or polling intervals.

## Highlights & Enhancements

### 1. Sub-Second 500ms (2 Hz) Core Telemetry Streaming
- **High-Frequency WebSocket Stream**: Upgraded the `/api/ws` telemetry loop to push live CPU utilization, per-core usage, RAM, swap, network I/O rates, disk I/O throughput, and thermal sensors every **500ms (2 Hz)**.
- **Zero-Subprocess Fast-Tick Caching**: Added intelligent in-memory TTL caches (`_SERVICES_CACHE`, `_NETPLAN_CACHE`) for heavy subprocess calls (`systemctl`, `netplan`), ensuring sub-second telemetry ticks remain lightweight and non-blocking.

### 2. Active View Multiplexing Over `/api/ws`
- **Unified WebSocket Channel**: Replaced manual refresh loops and separate polling timers with dynamic view subscriptions (`{"action": "subscribe", "view": "<tab>"}`) over `/api/ws`.
- **Automatic Live View Updates**: Active dashboard tabs (`processes`, `services`, `security`, `backups`, `probes`, `users`, `audit`, `containers`, and `stacks`) automatically receive pushed state snapshots while viewed.

### 3. Real-Time Container Log Streaming
- **WebSocket Log Subscriptions**: Added `subscribe_logs` and `unsubscribe_logs` WebSocket actions over `/api/ws` to stream live container log output continuously, replacing legacy 3-second HTTP polling timers.

### 4. Instant Event-Driven Broadcast Bus
- **Immediate Cross-Client State Synchronization**: Introduced `_broadcast_module()` to broadcast instant state payloads across all connected WebSocket clients the moment a mutation occurs—covering Docker container/stack actions, systemd service operations, cron job runs, backup completions, probe executions, and notification dispatches.

### 5. Zero-Clutter Reactive UI
- **Streamlined Control Surface**: Hid manual refresh buttons, refresh interval dropdown selectors, and redundant live stream indicator badges via CSS while preserving full DOM backward compatibility and preserving open dropdown state across high-frequency live ticks.

## Reliability & Fixes
- **100% Automated Test Pass Rate**: Added `tests/test_live_streams.py` with **298 / 298 automated tests passing (100% PASS)** across unit, integration, WebSocket streaming, and UI contract suites.
- **Full DOM & Contract Compatibility**: Maintained all structural DOM selectors and interaction handlers for complete backward compatibility with existing test suites.
