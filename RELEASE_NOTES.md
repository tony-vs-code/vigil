# Release v1.0.42: Visual Progress Chart Discord Notifications & Embed Enhancements

## Overview
Vigil v1.0.42 introduces an upgraded, professional Discord notification layout. Moving away from cluttered monospace text tables, raw box-drawing dumps, and emojis, Vigil now sends sleek, high-DPI graphical progress bar charts showing live system resource utilization with explicit capacity totals, paired with clean multi-column status cards, author headers, and thumbnails.

## Highlights & Enhancements

### 1. Dynamic Visual Telemetry Progress Bar Chart
- **Real-Time Graphical Resource Meter**: Renders high-resolution horizontal progress bar charts for **CPU**, **Memory**, **Data Pool**, and **Root Disk**.
- **Human-Readable Capacity Labels**: Each bar displays exact used and total amounts directly on the axis (e.g., `CPU Usage (24.5%)`, `Memory (42.1% - 6.7/16 GB)`, `Data Pool (52.5% - 8.4/16 TB)`, `Root Disk (16.8% - 42/250 GB)`).
- **Dynamic Threshold Color Mapping**: Automatically colors bars based on safe limits:
  - `< 75%`: `#22c55e` (Vibrant Green)
  - `75% – 89.9%`: `#f59e0b` (Amber Warning)
  - `≥ 90%`: `#ef4444` (Critical Red)
- **Dark-Theme Discord Integration**: Charts are generated with `#1e1f22` background, smooth rounded bar corners, and muted gray gridlines matching Discord's native dark interface.

### 2. Clean Professional Bot Embed Architecture
- **Zero Codeblocks & Zero Emojis**: Removed noisy backticks and emoji icons in favor of clean, typography-driven field cards.
- **Author Bar & Thumbnail**: Added `author` header (`Vigil System Monitor — {hostname}`) and top-right `thumbnail` (Vigil shield icon) for instant visual recognition.
- **Structured Subsystem Cards**:
  - `Host Environment` (inline): OS, Kernel, Uptime, and Load Averages.
  - `Storage Infrastructure` (inline): RAID status, array level, pool capacity, and root capacity.
  - `Services & Monitoring`: Docker containers (running/stopped/updates available) and Sentinel probe health with average response latency.

### 3. Streamlined RAID Diagnostics & Embed Engine
- **Cleaner RAID Alerts**: Eliminated legacy ASCII box-drawing text dumps in favor of clean diagnostic cards accompanied by storage progress charts.
- **Engine Upgrades**: Enhanced `dict_to_discord_embed` to natively support `thumbnail` and `image` across all alert types (periodic reports, RAID degradation alerts, thermal warnings, and test dispatches).

## Reliability & Fixes
- **Dedicated Embed Styling Test Suite**: Added `tests/test_discord_embed_styling.py` with 19 comprehensive tests validating QuickChart URL generation, threshold color selection, capacity label formatting, embed image/thumbnail bindings, and zero-codeblock constraints.
- **100% Quality Gate Pass**: Full automated test suite passes cleanly with **385 passed, 0 failed** across all backend routes, unit tests, and cross-tier UI contracts (63 API routes, 718 DOM elements).
