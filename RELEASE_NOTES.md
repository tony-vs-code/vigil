# Release v1.0.32: Visual Docker Compose Editor & 1-Click Homelab App Catalog

## Overview
Vigil v1.0.32 introduces a complete **Visual Docker Compose Editor & 1-Click Homelab App Catalog**, enabling operators to explore, customize, deploy, and manage self-hosted applications effortlessly. Featuring 11 production-ready homelab application templates, instant 1-click deployment, pre-deploy customization with real-time environment variable templating, a dual-tab compose and `.env` editor, live YAML validation with syntax diagnostics, and service/port pill detection, managing multi-container stacks in Vigil is faster, safer, and more intuitive than ever.

## Highlights & Enhancements

### 1. Curated Homelab App Catalog (11 Templates)
- **Production-Ready Blueprints**: Includes 11 popular homelab services: Pi-hole (DNS & Ad-blocking), Uptime Kuma (Monitoring), Nginx Proxy Manager (Reverse Proxy), Vaultwarden (Password Management), Jellyfin (Media Server), Home Assistant (Smart Home), Portainer CE (Docker UI), Dozzle (Real-time Container Logs), Redis (In-Memory Datastore), PostgreSQL (Relational Database), and Glances (System Observability).
- **Category Filter & Instant Search**: Filter catalog apps by category tags (`All`, `Monitoring`, `Networking`, `Security`, `Media`, `Database`, `Automation`, `Management`) or perform instant multi-attribute text search matching application names, descriptions, and categories.
- **Card-Based App Discovery**: Each application card features iconography, category badge, quick port references, volume hints, and immediate call-to-actions.

### 2. 1-Click Launch & Customize Workflows
- **1-Click Launch**: Instantly scaffold and deploy default application configurations with automatic port assignments and data directory generation.
- **Customize & Deploy Modal**: Interactive configuration modal prompting for stack name, exposed host port mapping, host volume storage paths, and environment variable defaults prior to spinning up containers.
- **Pre-populated Stack Editor**: Effortlessly seed the Docker Compose Stack Editor directly from any catalog template with one click.

### 3. Dual-Tab Stack Editor & Multi-File Persistence
- **Dual-Tab Architecture**: Seamlessly switch between `docker-compose.yml` (YAML Editor) and `.env Variables` (Environment Configuration) without losing unsaved changes.
- **Multi-File Storage Synchronization**: Synchronizes both Compose specification and environment variables to disk filesystem (`/opt/stacks/<name>/.env` and `docker-compose.yml`) as well as Vigil SQLite storage for persistent management.

### 4. Live YAML Validation & Diagnostic Feedback Pills
- **Real-Time Validation Endpoint**: Asynchronous `/api/docker/compose/validate` endpoint verifies Docker Compose syntax and structure using PyYAML.
- **Interactive Service & Port Pills**: Detects defined service names and parsed port forward mappings, displaying live visual pills beneath the editor for immediate feedback.
- **Actionable Diagnostic Alerts**: Highlights YAML syntax errors, indentation faults, and missing service directives inline with clear warning/success state pills.

## Reliability & Fixes
- **Comprehensive Test Suite**: 253/253 automated tests passing (100% PASS), covering backend API endpoints, SQLite/filesystem persistence, YAML validation logic, and frontend DOM/VM contracts.
- **Node VM Contract Validation**: Full DOM event routing, tab switching, search/filtering, and modal parameter injection verified in automated JavaScript test harnesses.
- **Backward Compatibility**: Fully compatible with existing standalone containers and previously deployed compose stacks.
