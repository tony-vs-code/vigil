# Release v1.0.38: Safe Vigil Python Package Manager & Updater with SemVer Bounds and Pre-Restart Smoke Testing

## Overview
Vigil v1.0.38 introduces an enterprise-grade **Safe Python Package Manager & Updater** for Vigil's self-hosted Python environment, coupled with **Compatible SemVer Bounds Protection** in `pyproject.toml`. Operators can now inspect installed vs. latest PyPI package versions directly from the web dashboard, simulate dependency resolution with dry-run locks, execute one-click safe upgrades bounded to safe SemVer ranges, and rely on automated pre-restart subprocess smoke testing with instant lockfile rollback to prevent downtime.

## Highlights & Enhancements

### 1. Compatible SemVer Bounds Protection
- **Safe Upgrade Windows**: Replaced rigid `==` pins in `pyproject.toml` with compatible upper bounds (`>=X.Y.Z, <NextMajor`) across core dependencies (`aiohttp`, `discord-py`, `fastapi`, `psutil`, `uvicorn`, `websockets`, `pyyaml`, etc.).
- **Breaking Change Prevention**: Locks dependency upgrades to safe minor and patch releases, preventing accidental breaking major updates while unlocking critical security patches and performance improvements.

### 2. Safe Python Package Manager Backend (`GET/POST /api/packages/python`, `POST /api/packages/python/upgrade`)
- **Package Inspection & PyPI Telemetry**: Discovers all installed packages and compares them with PyPI releases, categorizing dependencies into direct vs. transitive roles.
- **Major Held Detection**: Flags releases that exceed the safe SemVer bound as `major_held`, preventing untested major version jumps from corrupting the runtime.
- **Dry-Run Resolution Simulation**: Supports non-destructive lockfile simulation via `uv lock --dry-run` to preview dependency graph changes before committing.
- **Transactional Upgrades with Smoke Testing & Rollback**: Snapshots `uv.lock`, applies changes via `uv sync`, runs subprocess smoke testing (`import main, api` and system self-test diagnostics) prior to restarting the service, and automatically rolls back (`uv sync --frozen` and lockfile restore) if the smoke test fails.

### 3. Vigil Python Env Subtab (`#subtab-pkg-python-btn`, `#pkg-python-subtab-panel`)
- **Integrated Packages & Updates UI**: Added a dedicated 3rd subtab in the Packages & Updates section of the Vigil dashboard.
- **Summary Ribbon & Search**: Real-time summary metrics for direct packages, transitive dependencies, upgradable packages, and major-held packages, with instant live search and role filtering.
- **One-Click Safe Upgrades & Simulation Modal**: Interactive simulation modal allowing operators to preview changelogs, review dependency impacts, and trigger one-click safe upgrades.

## Reliability & Fixes
- **Cross-Tier Verification Suite (`tests/test_python_package_manager.py`)**: Added 9 comprehensive backend unit/API and frontend Node VM tests validating dependency parsing, PyPI version comparison, dry-run simulations, smoke test verification, rollback logic, and UI element contracts.
- **100% Automated Test Pass Rate**: All **323 / 323 automated tests passing (100% PASS)** across unit, integration, contract (`63` routes, `704` DOM IDs), and UI test suites.
