# Release v1.1.0: Container Registries & Encrypted Credential Management

## Overview
Vigil v1.1.0 introduces **Container Registries & Source Repositories** management under the Containers workspace. Operators can now configure, test, and manage credentials for Docker Hub, GitHub Container Registry (GHCR), Quay.io, Harbor, and custom OCI/Docker v2 registries with AES-256-CBC + HMAC-SHA256 encryption at rest, strict API token masking, live `/v2/` connection testing, and audit trail logging.

## Highlights & Enhancements

### 1. Container Registries & Source Repositories UI
- **Dedicated Registries Sub-View**: Added the **Registries** sidebar sub-menu entry (`#subtab-docker-registries`) and view panel (`#subview-docker-registries`) under **Containers** with live count badge updates (`#subtab-badge-registries`) and `#containers/registries` hash routing.
- **Provider Presets & Custom Endpoints**: One-click preset chips (`#btn-preset-dockerhub`, `#btn-preset-ghcr`, `#btn-preset-quay`) pre-populate Docker Hub (`https://registry-1.docker.io`), GitHub GHCR (`https://ghcr.io`), and Quay.io (`https://quay.io`), alongside support for Harbor and private self-hosted registries.
- **Add/Edit Modal & State Rendering**: Added `#modal-registry` featuring show/hide token visibility toggle (`#btn-toggle-registry-token`), default registry selection (`#registry-form-is-default`), in-modal connection test feedback (`#registry-form-test-feedback`), guided empty state (`#registries-empty-state`), and status-badged table rendering (`#docker-registries-table`).

### 2. Encrypted Credential Storage & Token Masking
- **Encrypt-then-MAC Secret Store**: Persisted registry credentials in the SQLite `container_registries` table with `encrypt_secret` / `decrypt_secret` using AES-256-CBC + HMAC-SHA256, per-record random 16-byte salts and IVs, and PBKDF2-SHA256 master key derivation from `AUTH_SECRET_KEY`.
- **Strict Secret Masking**: API responses never return plaintext tokens, exposing only `token_configured` flags and masked previews (`••••` + trailing characters). Editing a registry safely preserves the existing encrypted token when the input is left unchanged.

### 3. Registry CRUD APIs & Live `/v2/` Connection Testing
- **Authenticated REST Endpoints**: Added full CRUD and test routes on `/api/docker/registries`, `/api/docker/registries/{registry_id}`, `/api/docker/registries/test`, and `/api/docker/registries/{registry_id}/test`, recording `docker.registry.create`, `docker.registry.update`, `docker.registry.delete`, and `docker.registry.test` audit events.
- **Registry `/v2/` Challenge Exchange**: Implemented `test_container_registry_connection` with Basic auth and Docker Registry v2 Bearer `WWW-Authenticate` realm/service/scope token exchange, persisting `verified`/`error` status, `last_tested_at`, and `last_error` results.

## Reliability & Fixes
- **Dedicated Registry Test Suite**: Added `test_container_registries.py` covering AES-256-CBC/HMAC-SHA256 encryption and tamper resistance, SQLite CRUD persistence, API authorization and token masking, `/v2/` Basic/Bearer connection test edge cases, zero duplicate HTML IDs, and Node VM frontend subtab/modal flows.
- **100% QA Quality Gate Pass**: Validated with `node --check static/app.js`, cross-tier contract checks (67 frontend API routes and 745 DOM element IDs), and **454/454** passing tests in `pytest`.
