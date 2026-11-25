# Modernization

- [x] Audit upstream and the local fork.
- [x] Record the upstream test baseline and replace legacy test dependencies.
- [x] Prepare HACS packaging and release validation.
- [x] Investigate GM OAuth and the mobile redirect.
- [x] Implement token-only authentication and native reauthentication.
- [x] Default to read-only and make location optional.
- [x] Expose available EV metrics and explicit freshness.
- [x] Coordinate requests, token refresh and legacy migration.
- [x] Run mocked tests, Hassfest and package checks locally.
- [ ] Run the hosted HACS job and verify installation from a published tag.
- [ ] Validate interactive sign-in, refresh and Silverado EV telemetry with a live account.

## Upstream audit

The local origin is `babgvant/hass-mychevrolet`, forked from `Dvalin21/hass-mychevrolet`. GitHub's issues API returned no open issues on November 25, 2026. The upstream repository contains a native async integration and mocked tests, with no HACS metadata or release automation. It vendors a modified pyOnStar client. PyOnStar's README states MIT, but its public repository has no LICENSE file. The integration upstream has no published license grant. OnStarJS carries MIT notices for Ruben Medina and BigThunderSR. Preserve attribution and obtain confirmation from the relevant rights holders before publicly distributing the inherited integration code. [Integration upstream](https://github.com/Dvalin21/hass-mychevrolet) · [pyOnStar](https://github.com/leboff/pyonstar) · [OnStarJS](https://github.com/BigThunderSR/OnStarJS)

The legacy configuration stores password, TOTP seed and optional PIN. The client uses a shared token directory, attempts automated login on refresh failure, uses inconsistent redirect URIs across authorization and exchange, and logs raw responses. Its diagnostics and location polling can suppress a successful endpoint when the other fails and can advance freshness on cached values. Its rate-limit bypass rotates the device ID. The existing domain, VIN-based entity unique IDs and device identifiers are worth keeping.

## GM OAuth and EV API evidence

GM's public OpenID discovery document identifies its tenant GUID issuer and policy-specific signing keys. An anonymous S256 authorization request using `https://my.home-assistant.io/redirect/oauth` returned `AADB2C90006`. The same probe using `msauth.com.gm.mychevrolet://auth` returned HTTP 200 with GM's sign-in form. The integration uses browser sign-in with manual handoff of the registered mobile redirect and offers local Microsoft refresh-token import. The referenced Firefox add-on logs authorization redirect details. The API bearer produced by GM's separate token exchange is treated as opaque. Issuer, audience and signing keys are verified for the Microsoft identity and access tokens used to establish account identity. [GM discovery metadata](https://custlogin.gm.com/gmb2cprod.onmicrosoft.com/b2c_1a_seamless_mobile_signuporsignin/v2.0/.well-known/openid-configuration) · [Firefox implementation](https://github.com/metheos/onstar_firefox/blob/main/background.js)

OnStarJS implements v3 alongside older APIs. onstar2mqtt publishes cached and live EV metrics separately and its sample identifies a vehicle-side timestamp, SOC, target, charging mode, estimated power, charging-location flags and discharge settings. The sample omits verified measured electrical power and current. [OnStarJS](https://github.com/BigThunderSR/OnStarJS) · [EV metrics sample](https://github.com/BigThunderSR/onstar2mqtt/blob/main/test/command-sample-getEVChargingMetrics.json)

The implementation prefers cached v3 vehicle health and timestamped EV metrics. It retains legacy diagnostics for vehicles with advertised support when the newer health endpoint is unavailable. The current fixtures support a charger category and a kWh reading whose battery-capacity interpretation is ambiguous. Measured current, measured power and the units of the estimated power field need live or documented evidence before they can be exposed as electrical measurements. [v3 vehicle-health sample](https://github.com/BigThunderSR/onstar2mqtt/blob/main/test/diagnostic-sample-v3-ev-1.json)

## Baseline and release checks

The unchanged legacy client tests produced eight passes on Windows and one failure from an unconditional POSIX file-mode assertion. The old integration tests reference the previous custom-component fixture package. The modernized suite uses the official `pytest-homeassistant` package. Local Windows tests use a compatibility shim for Linux-only imports and the asyncio self-pipe. The GitHub workflow runs the suite on Linux, including the POSIX token-file permissions assertion. HACS installation and update selection require a GitHub ref with the release files. No release has been published from this workspace.
