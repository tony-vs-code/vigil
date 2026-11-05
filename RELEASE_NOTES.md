# Release v1.0.26: Historical Telemetry Time-Machine (1h / 6h / 24h / 7d Charts)

## Overview
Vigil v1.0.26 introduces the **Historical Telemetry Time-Machine**, giving host operators deep operational visibility into past system performance. Operating alongside real-time live telemetry, the Time-Machine features an embedded SQLite time-series persistence engine, adaptive bucketed aggregation, and a responsive High-DPI multi-series chart with interactive tooltips and summary metrics across 1-hour, 6-hour, 24-hour, and 7-day observation windows.

## Highlights & Enhancements

### 1. SQLite Time-Series Persistence & 7-Day Auto-Pruning
- **Dedicated Telemetry Storage**: Persists periodic host resource snapshots into a indexed SQLite table (`telemetry_samples`) capturing CPU load, memory utilization, swap usage, disk capacity, temperature, and network RX/TX transfer rates.
- **Background Sampling Engine**: Automatic background evaluation captures system metrics every 15 seconds without blocking API requests or degrading host performance.
- **Automated Retention Pruning**: Enforces a rolling 7-day data retention policy (`prune_telemetry_samples`), automatically cleaning up expired records to maintain a lean database footprint.

### 2. Bucketed Aggregation Engine & Telemetry History API
- **Dynamic Downsampling**: Added `GET /api/telemetry/history?range={1h|6h|24h|7d}&points={10-300}` delivering time-bucketed averages and interpolated points tailored for canvas rendering.
- **Statistical Summaries**: Computes comprehensive min, max, and average resource utilization metrics for each window, providing operators with immediate high-level capacity insights.
- **Manual Sample Ingestion Endpoint**: Added `POST /api/telemetry/sample` enabling on-demand sample capture or external metric ingestion.

### 3. Interactive Overview Time-Machine Canvas & Selector Toolbar
- **Modern Selector Toolbar**: Introduced intuitive pill controls (`Live`, `1h`, `6h`, `24h`, `7d`) in the dashboard Overview tab, allowing instant switching between real-time ticks and historical windows.
- **High-DPI Multi-Series Canvas Chart**: Custom retina-ready canvas renderer graphing CPU (%), Memory (%), and Network Transfer Rates (KB/s) with clean grid lines and formatted time axes.
- **Interactive Hover Tooltips & Summary Stats**: Real-time crosshair and hover tooltips showing precise timestamped metrics at any cursor position, backed by min/avg/max summary indicator chips.

## Reliability & Fixes

### Quality & Contract Verification
- **Cross-Tier Contract Integrity**: Verified 58 frontend API route endpoints and 607 DOM element bindings with zero mismatches or unhandled exceptions.
- **Comprehensive Automated Test Suite**: Expanded test suite to 229 tests (`229 passed, 0 failures`) covering time-series schema creation, retention pruning, bucket downsampling, summary statistics, API authentication, and UI DOM bindings.
