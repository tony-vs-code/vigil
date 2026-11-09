# Release v1.0.36: Ace Code Editor & Canonical YAML / .env Formatter

## Overview
Vigil v1.0.36 upgrades the Docker Compose Stack Editor modal (`#stack-editor-modal`) with an IDE-grade **Ace Code Editor** integration (`v1.36.2`, One Dark theme) and a **Canonical YAML & `.env` Formatter** engine (`POST /api/docker/stacks/format`). Operators can now author, validate, format, and deploy multi-container Docker Compose stacks and `.env` configurations with full `yaml` and `sh` syntax highlighting, live line/column cursor telemetry, 1-click canonical key ordering, inline Ace gutter error diagnostics, and quick Compose snippet insertion chips—with seamless graceful fallback to enhanced `<textarea>` editors when offline.

## Highlights & Enhancements

### 1. Ace Code Editor Integration (`#stack-yaml-ace-editor`, `#stack-env-ace-editor`)
- **Full Syntax Highlighting & IDE Ergonomics**: Integrated Ace Editor (`v1.36.2`, One Dark theme) for both `docker-compose.yml` (`ace/mode/yaml`) and `.env` (`ace/mode/sh`) panes with 2-space soft tabs, visible indentation guides, code folding, active line highlighting, and line numbers.
- **Live Cursor & Character Status Bar (`#stack-editor-statusbar`)**: Real-time status bar tracking cursor position (`Ln X, Col Y`), total line count, character count, and tab mode (`Spaces: 2`).
- **Offline Graceful Fallback**: Automatically detects if the CDN-hosted Ace library is unreachable and falls back transparently to synchronized `<textarea>` editors (`#stack-yaml-editor`, `#stack-env-editor`) with keyboard shortcut support (`Tab` / `Shift+Tab`, smart auto-indent on `Enter` after `:`, `Ctrl+/` comment toggling).

### 2. Canonical YAML & `.env` Formatter (`POST /api/docker/stacks/format`)
- **1-Click Format YAML (`#stack-format-btn`)**: Backed by `DockerManager.format_compose_yaml`, normalizes leading tabs to 2 spaces, strips trailing whitespace, validates YAML syntax and top-level mapping structure, and canonically orders top-level Compose keys (`version`, `name`, `services`, `networks`, `volumes`, `configs`, `secrets`, `x-*`) as well as per-service keys (`image`, `build`, `container_name`, `restart`, `ports`, `environment`, `env_file`, `volumes`, `networks`, `devices`, `depends_on`, `healthcheck`, `command`, `entrypoint`, `user`, `working_dir`, `labels`, `logging`, `deploy`). Accessible via button click or `Shift+Alt+F` / `Ctrl+Shift+F`.
- **1-Click Format `.env` (`#stack-env-format-btn`)**: Backed by `DockerManager.format_env_content`, normalizes `KEY=value` definitions, strips extraneous whitespace around `=`, preserves `#` comments, and collapses excessive blank lines.

### 3. Quick Compose Snippet Bar & Inline Gutter Diagnostics
- **1-Click Compose Snippets (`#stack-snippet-toolbar`)**: Added quick-insert snippet chips (`+ Service`, `+ Ports`, `+ Volumes`, `+ Env`, `+ Healthcheck`, `+ Network`) that insert properly indented YAML blocks directly at the cursor position.
- **Exact Line & Column Error Diagnostics**: `DockerManager.validate_compose_yaml` now extracts exact 1-indexed `error_line` and `error_col` coordinates from `yaml.YAMLError.problem_mark`, rendering inline error annotations directly in the Ace Editor gutter alongside detailed validation banner alerts (`#stack-validation-banner`).

## Reliability & Fixes
- **Compose Editor Regression Suite (`tests/test_compose_editor.py`)**: Added 9 comprehensive backend and frontend Node VM tests covering YAML formatting, canonical key ordering, tab conversion, exact error line/column reporting, `.env` normalization, API endpoint responses, and Ace Editor DOM/JS contracts.
- **100% Automated Test Pass Rate**: All **308 / 308 automated tests passing (100% PASS)** across unit, integration, contract (`62` routes, `691` DOM IDs), and UI test suites.
