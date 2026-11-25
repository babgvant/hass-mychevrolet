# Changelog

## 0.2.0

- Add HACS custom repository metadata, release packaging, and CI checks.
- Replace credential automation with browser PKCE and local token import, account-scoped storage, rotation, and native reauthentication.
- Default to telemetry-only operation and make GPS and advertised non-privileged controls optional.
- Support vehicle discovery and selection for multiple GM vehicles. Preserve VIN-based entity and device identities during migration.
- Read cached EV charging telemetry and track individual sample freshness, endpoint failures, and throttling.
- Remove device-ID rotation and the legacy password, TOTP and shared token caches.
