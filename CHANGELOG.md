# Changelog

All notable changes, base image updates, and dependency upgrades to **CasaOS Backup Manager Pro** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this file is automatically maintained by our GitHub Actions Bot whenever new base images (`python-slim`, `rclone`) or Python packages are released.

<!-- AUTO-CHANGELOG-ENTRY -->

## [Auto-Update] - 
### Changed
- **Python Package**: Updated `fastapi` from `0.162.4` to `0.162.5`.

## [2026-11-10] - Initial Public Release & CI/CD Automation

### Added
- **Automated Daily Base Updater**: GitHub Actions workflow running daily at `06:00 AM (America/Denver)` to check Docker Hub (`python:3.x-slim`), Rclone releases, and PyPI packages, verify Docker builds, update `CHANGELOG.md`, and publish images to `ghcr.io/boystyle2010/casaos-backup-manager`.
- **3D Transparent App Icon**: Custom 512×512 RGBA `icon.png` integrated at the repository root for CasaOS and Portainer dashboards.
- **Smart SSD Disk Guard**: Per-container maximum size threshold (`max_app_size_gb`) and minimum free SSD space protection (`min_free_ssd_gb`).
- **Admin Session Report Modal**: Detailed post-backup summary highlighting succeeded backups and skipped/oversize containers.
- **1-Tap Cloud Recovery Key (`CBMKEY`)**: Instant export and import of Rclone configuration and application settings.
- **Bilingual Glassmorphic UI**: Full English and Vietnamese localization with dark/light mode support.
