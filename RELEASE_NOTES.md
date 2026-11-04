# Release v1.0.22: Vigil Sentinel, Hardware Health Cockpit, Firewall Manager & Telemetry Refinements

## Overview
Vigil v1.0.22 introduces comprehensive service uptime and SSL monitoring via Vigil Sentinel, physical NVMe/SATA disk S.M.A.R.T. and thermal telemetry in the Hardware Health Cockpit, active network port scanning and UFW firewall management, real non-interactive package installation, and Docker volume pruning, alongside continuous storage live streaming and UI chart rendering fixes.

## Highlights & Enhancements

### Added
- **Vigil Sentinel (Service Uptime & Health Probes)**: HTTP/S, TCP Port, and ICMP Ping monitors with 24-hour heartbeat bars, latency tracking, SSL certificate expiration monitoring, and Notification Center alerting.
- **Hardware Health Cockpit (S.M.A.R.T. & Thermal Telemetry)**: Physical NVMe/SATA disk discovery, `smartctl` JSON health attributes, drive temperatures, wear levels, power-on hours, `#smart-modal` attribute drilldown, and Linux `sysfs` CPU/chassis thermal & fan sensors.
- **Network Security, Open Ports & UFW Firewall Manager**: Active listening TCP/UDP socket scanner correlated with PIDs, users, and Docker containers, `PUBLIC` vs `LOCAL` exposure classification, and UFW firewall rule management (`ALLOW`, `DENY`, `REJECT`, `LIMIT`, delete).
- **Real Package Upgrade Installation**: Non-interactive `apt-get` package installation (`Install All Updates`, per-package `Install`, and modal `Install Now`) alongside dry-run simulation.
- **Docker Volume Pruning**: Added `Prune Unused Volumes` (`POST /api/docker/volumes/prune`) and enhanced volume removal in Container Management.

### Changed
- **Storage Live Stream & RAID Metadata**: Removed the manual refresh interval selector and "5s Rebuild Boost" badge in favor of continuous live streaming, and populated RAID `UUID`, `Layout / Chunk`, and `Consistency Policy` from `sysfs`.

## Reliability & Fixes

### Fixed
- **Overview CPU Processor Cores Utilization Chart `0%` Visibility**: Fixed `.cpu-unified-chart-container` fixed-height CSS clipping (`height: auto`) that cut off the bottom of `#cpu-cores-unified-canvas` below `25%`, and added the `0%` Y-axis label and baseline padding in `drawCpuCoreGraph`.
- **Packages Navbar Badge**: Removed the duplicate `#nav-tab-packages-badge` so only a single update count badge renders on the Packages navigation tab.
