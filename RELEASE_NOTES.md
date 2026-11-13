# Release v1.0.49: Windows Task Manager Performance Dashboard

## Overview
Vigil v1.0.49 transforms the real-time system monitoring experience into a high-performance Windows Task Manager-style dashboard. This release introduces the iconic 60-second rolling grid canvas with dark grid styling and cyan gradient fills, dynamic Update Speed control (`High (0.5s) | Normal (1s) | Low (2s) | Paused`) via topbar controls and backend streaming API, zero DOM thrashing with calm meter easing transitions, tabular numeral stabilization across all metrics, an Overall CPU vs Logical Processors multi-core toggle, and subtle pastel heatmap shading across the Task Manager Process Explorer.

## Highlights & Enhancements

### 1. Iconic Task Manager 60-Second Rolling Grid Canvas
- **Dark Grid Palette & Cyan Gradient**: Engineered a high-contrast canvas with `#1a2634` grid divisions, `#0d131a` background, and smooth cyan `#00d2ff` gradient fill with subtle glow.
- **60-Point Rolling Buffer**: Continuous 60-sample historical ring buffer scrolling smoothly across time, providing instantaneous visualization of historical compute pressure and load spikes.

### 2. Task Manager Update Speed Selector & Backend Stream Interval
- **Topbar Speed Selector**: Added intuitive speed options: **High (0.5s)**, **Normal (1s)**, **Low (2s)**, and **Paused (0s)** directly in the dashboard header.
- **Dynamic Stream Synchronization**: Connected via `/api/stream/interval` endpoint to dynamically adjust backend SSE telemetry intervals in real time without dropping active WebSocket or SSE sessions.

### 3. Zero DOM Thrashing & Calm Meter Transitions
- **Eliminated 60fps DOM Thrashing**: Removed hyperactive `requestAnimationFrame` DOM mutation loops, allowing telemetry updates to apply directly on data receipt.
- **Calm Easing Transitions**: Gauges and progress fills smoothly glide with `0.4s cubic-bezier(0.25, 0.1, 0.25, 1)` easing, allowing meters to glide calmly and settle gracefully before subsequent ticks.

### 4. Tabular Numerals Across All Telemetry
- **Digit Jitter Elimination**: Enforced `font-variant-numeric: tabular-nums` across all aggregate CPU, multi-core logical processor percentages, RAM, swap, and disk metrics.
- **Visual Stability**: Monospaced digit widths eliminate horizontal micro-shakes and layout oscillations as values fluctuate rapidly.

### 5. Overall CPU vs Logical Processors Multi-Core View Toggle
- **Multi-Core Visualization Modes**: Toggle seamlessly between single aggregate CPU chart and per-core logical processor oscilloscope charts.
- **State Persistence**: Remembers view preference in browser `localStorage`, automatically preserving operator display choices across reloads.

### 6. Process Explorer Heatmap Shading
- **Subtle Pastel Heatmaps**: Dynamically shades CPU% and Memory% cells in the Process Explorer table with graduated pastel heatmap tints (`rgba(0, 210, 255, alpha)` and `rgba(255, 180, 0, alpha)`).
- **Instant Hotspot Identification**: Allows operators to immediately spot resource-intensive processes and runaway background tasks at a glance.

## Reliability & Fixes
- **100% Contract & Backward Compatibility**: Fully preserves all 64 REST API endpoints and 722 client DOM element contracts.
- **Comprehensive Test Suite**: 437 unit and cross-tier tests passing cleanly in `tests/test_live_streams.py`, `tests/test_sse_stream.py`, `tests/test_ui_task_manager_dashboard.py`, and across the entire suite.
- **Cross-Tier QA Gate Verification**: Automated requirements validation passed with 0 failures, 0 syntax errors, and 100% contract compliance.
