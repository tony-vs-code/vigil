# Release v1.1.1: Registry-Authenticated Docker Image Pulls & Registry Visibility

## Overview
Vigil v1.1.1 connects the Container Registries secret store directly to Docker image operations. Operators can now pull private images using configured registry credentials—either automatically matched by image host or explicitly selected from the Images toolbar—while viewing resolved registry origin and authentication pills directly in the Docker Images table.

## Highlights & Enhancements

### 1. Registry-Authenticated Image Pulling
- **Automatic Host Matching & Default Fallback**: `DockerManager.pull_image` now resolves matching credentials from configured registries via `extract_registry_host_from_image` and `resolve_registry_for_image` (supporting Docker Hub aliases, GHCR, Quay.io, Harbor, and custom ports/hosts), falling back to the default configured registry when appropriate.
- **Ephemeral Isolated Docker Config**: Authenticated pulls materialize an ephemeral `0700` temporary directory containing a `0600` `config.json` passed via `docker --config <dir> pull <image>` and clean it up in a `finally` block—preventing credentials from ever persisting in host Docker config files.
- **Pull API & Audit Trail**: `POST /api/docker/images/pull` accepts an optional `registry_id` override (returning `404` for unknown IDs), returns `authenticated` and `registry_name` metadata, and records `docker.image.pull` audit events without exposing secrets.

### 2. Pull Registry Selector & Images Table Registry Column
- **Pull Registry Selector (`#docker-image-pull-registry`)**: Added a credential selector beside the image pull input populated with configured registries (showing `(Default)` labels) while defaulting to `Registry: Auto (Recommended)` and displaying authenticated pull badges (`.pull-auth-badge`) and toasts on completion.
- **Images Table `Registry` Column**: Expanded `#docker-images-table` with a `Registry` column rendering `.registry-pill` origin badges (`Local`, `Docker Hub`, or matched registry name) and `.registry-pill.authenticated` status indicators when encrypted credentials are configured for that source.

## Reliability & Fixes
- **Authenticated Pull Test Coverage**: Added `RegistryAuthenticatedPullTests` in `test_container_registries.py` covering image host extraction, registry matching/fallback logic, ephemeral `0700`/`0600` `config.json` lifecycle & exception-safe cleanup, `POST /api/docker/images/pull` API responses/audit logs, and Node VM tests for `#docker-image-pull-registry` and the 7-column Images table.
- **100% QA Quality Gate Pass**: Validated with `node --check static/app.js`, cross-tier contract checks (67 frontend API routes and 746 DOM element IDs), and **461/461** passing tests in `pytest`.
