# Release v1.0.29: In-Browser Docker Container Web Terminal

## Overview
Vigil v1.0.29 introduces an interactive, in-browser Docker Container Web Terminal, allowing system administrators to open zero-latency interactive shell sessions directly into running containers without SSH or external CLI tools. Powered by a raw duplex WebSocket streaming engine to the Docker daemon socket, an integrated ANSI rendering engine, dynamic PTY window resizing, zero-trust token authentication, and tamper-evident audit logging, this release delivers robust container management and terminal control right from the dashboard.

## Highlights & Enhancements

### 1. In-Browser Docker Container Web Terminal
- **1-Click Shell Access**: Launch interactive terminal sessions directly into any running Docker container from the Docker dashboard table using a dedicated **Terminal** action button.
- **Adaptive Shell Selection**: Intelligently attempts `/bin/bash` with graceful fallback to `/bin/sh` for lightweight, alpine, or minimal container images.
- **Dedicated Modal UI**: Clean, distraction-free modal terminal dialog equipped with container metadata badge, connection status indicator, shell selector, clear screen, and disconnect actions.

### 2. Real-Time WebSocket PTY Streaming Engine
- **Raw Duplex Streaming**: Fast, bidirectional streaming over `/api/docker/containers/{id}/terminal` bridging the web client directly to the Docker daemon UNIX socket exec stream.
- **Non-blocking Concurrency**: Built with Python `asyncio` and `websockets` for high-performance duplex forwarding between WebSocket frames and raw socket channels.

### 3. ANSI Rendering Engine & Complete Keyboard Controls
- **Rich Terminal Emulation**: Full support for standard 16-color ANSI palettes, 256-color palettes, text styling (bold, italic, underline, dim, inverted), cursor positioning, and line erase sequences.
- **Full Interactive Keyboard Control**: Native handling of shell control combinations (`Ctrl+C`, `Ctrl+D`, `Ctrl+Z`, `Ctrl+L`), Tab auto-completion, directional arrow keys, history recall, backspace, carriage returns, and clipboard text paste.

### 4. Interactive Window Resizing & Responsive Geometry
- **Dynamic Dimension Calculation**: Automatically calculates available character columns and rows based on client viewport and font dimensions.
- **Synchronized PTY Dimensions**: Dispatches live resize events (`{"type": "resize", "cols": N, "rows": N}`) that dynamically reconfigure the container's pseudo-terminal via the Docker exec resize API (`/exec/{id}/resize`).

### 5. Zero-Trust Security & Comprehensive Auditing
- **Strict Handshake Authentication**: Enforces token authentication prior to WebSocket upgrade; unauthenticated connection attempts are rejected immediately with RFC 6455 policy violation code `1008`.
- **Audit Logging**: Successful terminal connections log structured audit records (`docker.container.terminal_opened`) capturing container ID, authenticated user, remote IP, and timestamp.

## Reliability & Fixes

### Cross-Tier Quality & Automated Test Coverage
- **Automated Quality Gate**: Cross-tier verification gate (`validate_requirements.sh`) passed with 0 errors, 100% DOM contract compliance, and clean JavaScript syntax.
- **Comprehensive Test Suite**: 243/243 unit, integration, frontend VM, and security tests passing with 0 failures.
