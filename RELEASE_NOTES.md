# Release v1.0.37: Smart Compose YAML Snippet Insertion & Ace Editor Modal Sizing Fix

## Overview
Vigil v1.0.37 delivers a **Structure-Aware Compose Snippet Engine** (`applySnippetToComposeYaml` / `insertComposeSnippet`) and fixes **Ace Editor Modal Initialization & Sizing** (`openStackEditor`) in the Docker Compose Stack Editor (`#stack-editor-modal`). Clicking Compose snippet chips (`+ Service`, `+ Port`, `+ Volume`, `+ Env`, `+ Healthcheck`, `+ Network`) in any sequence or repeatedly now intelligently parses top-level Compose sections (`services:`, `networks:`, `volumes:`) and service property blocks—merging lists, auto-incrementing ports, generating unique service and network names, and guaranteeing valid, non-duplicate YAML that never gets silently overwritten by `yaml.safe_load()`.

## Highlights & Enhancements

### 1. Structure-Aware Compose Snippet Engine (`applySnippetToComposeYaml` / `insertComposeSnippet`)
- **Top-Level Section Awareness**: Parses `services:`, `networks:`, and `volumes:` sections so service snippets (`+ Service`) and service property snippets (`+ Port`, `+ Volume`, `+ Env`, `+ Healthcheck`) are always inserted inside the `services:` block, even when a top-level `networks:` section is already present at the bottom of the file.
- **Smart Property Merging & Auto-Incrementing**:
  - **`+ Port`**: Merges additional port mappings directly into an existing `ports:` list on the target service and automatically increments the host port (`"8080:80"`, `"8081:80"`, `"8082:80"`, ...) so repeated clicks never create duplicate keys or conflicting port bindings.
  - **`+ Volume`**: Merges new volume mounts (`./data:/data`, `./data-2:/data-2`, ...) into an existing `volumes:` list on the target service instead of appending duplicate `volumes:` keys.
  - **`+ Env`**: Merges environment variables (`TZ: UTC`, `NODE_ENV: production`, `LOG_LEVEL: info`, ...) into an existing `environment:` mapping or list on the target service.
  - **`+ Healthcheck`**: Replaces or updates an existing `healthcheck:` block on the target service in-place rather than creating duplicate `healthcheck:` keys.
  - **`+ Service` & `+ Network` Auto-Deduplication**: Generates unique service names (`app`, `app-service`, `app-2`, ...) under `services:` and unique network names (`app-net`, `app-net-2`, ...) under top-level `networks:`.

### 2. Ace Editor Modal Initialization & Resize Order (`openStackEditor`)
- **Zero-Dimension Initialization Fix**: Updated `openStackEditor()` to remove the `hidden` class from `#stack-editor-modal` before invoking `initStackCodeEditors()`, and triggers `resize(true)` on value updates so Ace Editor never initializes inside a hidden `0x0` container.

## Reliability & Fixes
- **Sequential Multi-Snippet Round-Trip Regression Test (`tests/test_compose_editor.py`)**: Added end-to-end Node VM + `DockerManager.format_compose_yaml` round-trip verification across 14 sequential and repeated snippet insertions (`service`, `ports`, `volumes`, `env`, `healthcheck`, `networks`, plus repeated clicks after `networks:` is at the bottom of the file).
- **100% Automated Test Pass Rate**: All **309 / 309 automated tests passing (100% PASS)** across unit, integration, contract (`62` routes, `691` DOM IDs), and UI test suites.
