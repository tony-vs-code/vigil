# Release v1.0.39: Portainer-Style Environment Variable Manager & Safe Package Upgrades

## Overview
Vigil v1.0.39 delivers a comprehensive **Portainer-Style Environment Variable Manager** for the Docker Compose Stack Editor and upgrades Vigil's core Python dependencies within safe SemVer bounds in `uv.lock`. Operators can now seamlessly toggle between a dynamic Key-Value visual grid and the advanced raw Ace Editor, import `.env` files with 1-click parsing, export or copy environment variables, mask sensitive secrets, and rely on cross-tier bidirectional synchronization between visual and raw modes.

## Highlights & Enhancements

### 1. Portainer-Style Environment Variable Manager (`#stack-env-panel`)
- **Dual-Mode Visual Grid & Raw Ace Editor**: Integrated mode switcher (`#stack-env-mode-visual-btn` / `#stack-env-mode-raw-btn`) allowing operators to edit environment variables via structured key-value inputs or direct raw text with instantaneous bidirectional synchronization.
- **Dynamic Row Management & Search**: Add new key-value pairs with `#stack-env-add-row-btn`, remove rows with per-row delete buttons, and quickly filter large `.env` sets using the instant search filter input (`#stack-env-search-input`).
- **Secret Masking Toggle**: Easily mask or reveal sensitive credentials using the per-row secret toggle eye button (`👁️`), switching between password-masked and clear-text views.
- **One-Click Import, Export & Copy**:
  - **Import `.env` (`#stack-env-import-file-btn` / `#stack-env-file-input`)**: Instantly upload `.env` files via client-side `FileReader` and populate both visual grid and raw editor.
  - **Export / Download `.env` (`#stack-env-export-btn`)**: Download current environment variables as a standard `.env` file.
  - **Copy to Clipboard (`#stack-env-copy-btn`)**: 1-click clipboard copy with immediate visual badge confirmation.

### 2. Backend `.env` Parsing & Serialization Endpoints
- **`POST /api/docker/stacks/env/parse`**: Robust parsing of raw `.env` content into structured key-value arrays via `DockerManager.parse_env_key_values`, safely handling quoted values, comments, and empty lines.
- **`POST /api/docker/stacks/env/serialize`**: High-fidelity serialization of structured key-value pairs into standardized `.env` strings via `DockerManager.serialize_env_key_values`.

### 3. Vigil Core Python Dependency Upgrades
- **Safe SemVer Upgrades in `uv.lock`**: Applied compatible upgrades to core dependencies within their declared SemVer bounds, including:
  - `aiohttp` 3.10.5 → 3.14.5
  - `discord-py` 2.4.0 → 2.7.1
  - `fastapi` 0.112.0 → 0.162.2
  - `multidict` 6.0.5 → 6.9.1
  - `yarl` 1.9.6 → 1.25.1
  - `websockets` 12.0 → 15.0.1
  - `pydantic` 2.8.2 → 2.14.3
- **Protected Stability**: Preserves rock-solid runtime stability with zero breaking API changes while leveraging up-to-date security patches and performance improvements.

## Reliability & Fixes
- **Cross-Tier Verification Suite**: Added comprehensive backend unit and Node VM contract tests in `tests/test_compose_editor.py` and `test_main.py` validating `.env` parsing, serialization edge cases, UI grid synchronization, search filtering, import/export helpers, and DOM contracts.
- **100% Automated Test Pass Rate**: Full automated test suite passes cleanly with **327 passed, 0 failed** across all unit, integration, and UI contract tests (`check_contracts.py` — 63 routes, 716 DOM IDs; `node --check static/app.js`).
