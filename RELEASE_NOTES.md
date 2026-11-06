# Release v1.0.31: Streamlined Docker Actions & Quick Dropdown Menus

## Overview
Vigil v1.0.31 streamlines the Actions columns for Docker Containers and Docker Stacks across the dashboard. By introducing prominent Primary Quick Actions (`Restart`/`Start`, `Logs`, `Terminal`, `Edit`) paired with an elegant, responsive "⋯" More dropdown for secondary and destructive operations (`Stop`, `Pause`, `Pull`, `Down`, `Remove`, `Delete`), container and stack management is significantly cleaner, prevents horizontal table bloating, and eliminates accidental destructive clicks.

## Highlights & Enhancements

### 1. Streamlined Docker Containers Actions
- **Primary Quick Actions**: High-frequency workflows remain 1-click accessible. Running containers show prominent `Restart`, `Logs`, and `Terminal` buttons; stopped containers provide instant `Start` and `Logs`; paused containers feature an immediate `Unpause` action.
- **"⋯" More Dropdown Menu**: Secondary lifecycle actions (`Stop`, `Pause`, `Terminal` for stopped containers, and `Remove`) are consolidated inside a sleek popover dropdown menu.
- **Accidental Deletion Protection**: Destructive operations like container removal (`Remove`) feature distinctive red danger styling (`actions-dropdown-item danger`) with confirmation dialogs.

### 2. Streamlined Docker Compose Stacks Actions
- **Primary Quick Actions**: Active stacks feature direct `Restart` and `Edit` buttons, while stopped stacks display immediate `Start` and `Edit` actions. External/unmanaged compose stacks retain their protective `Read-Only (External)` safety badge.
- **Unified Stack Dropdown**: Lifecycle and teardown operations (`Stop`, `Pull`, `Down`, `Delete`) are consolidated within the stack's "⋯" More dropdown menu.
- **Contextual Destruction Styling**: Teardown (`Down`) and stack file deletion (`Delete`) operations are cleanly separated by a divider and styled with danger hover highlights.

### 3. Accessible UX & Dismissal Lifecycle
- **Click-Outside & Keyboard Dismissal**: Opening any dropdown registers automatic click-outside backdrop dismissal and Escape key listeners, closing all active action popovers instantly.
- **Multi-Menu Mutual Exclusion**: Activating any dropdown automatically closes any other open dropdown menus across the dashboard.
- **Dynamic Positioning & Elevated Contrast**: High-contrast, dark-mode elevated card styling (`box-shadow`, subtle borders, and hover micro-transitions) ensures consistent readability across compact viewport widths.

## Reliability & Fixes
- **Comprehensive Test Suite**: All 245 unit, integration, and frontend Node VM tests passed with 100% success.
- **Node VM & DOM Contract Verification**: Verified complete HTML generation, DOM event contracts, and CSS selector validity across container states (`running`, `stopped`, `paused`) and stack configurations (`running`, `stopped`, `external`).
