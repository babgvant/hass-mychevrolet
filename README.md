# myChevrolet for Home Assistant

[![Add myChevrolet to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=babgvant&repository=hass-mychevrolet&category=integration)

myChevrolet connects Home Assistant directly to GM's unofficial OnStar APIs. It focuses on vehicle telemetry for energy-management automations, including EV state of charge, charge target and plug state. It runs inside Home Assistant using Python. The integration targets US accounts on GM's North American backend. Coverage varies by model, subscription and API response. The 2026 Silverado EV Trail Boss Max Range still needs live validation.

## Install and update with HACS

Use Home Assistant **2026.11.0 or later** and, for development, **Python 3.14.2 or later**. In HACS, open **Custom repositories** from the menu, add `https://github.com/babgvant/hass-mychevrolet` as an **Integration**, download **myChevrolet**, and restart Home Assistant. In **Settings → Devices & services**, add **myChevrolet**. HACS can use the default branch or published tagged releases. Use HACS **Redownload** to select another published release, then restart. The application automatically resolves the integration's Python dependencies.

The release workflow builds a manual install ZIP and opens a draft for review. HACS installs the tree under `custom_components/mychevrolet` from the selected Git ref, including the vendored client. The HACS catalog is optional for a custom repository. [HACS integration publishing requirements](https://www.hacs.xyz/docs/publish/integration/)

## Sign in to GM

1. In the setup flow, choose **Sign in with GM in your browser**. On a desktop browser, open Developer Tools, select **Network**, and enable **Preserve log** or **Persist Logs**.
2. Open the GM sign-in link while the Network panel is recording. Enter your username, password and any MFA response on GM's site.
3. After GM redirects to its mobile app, inspect the final redirect response from `custlogin.gm.com`. Copy its **Location** header starting with `msauth.com.gm.mychevrolet://auth?` into the setup form. Keep the whole URL, including both state and code. The in-progress authorization expires after ten minutes. Close the Network panel when finished and avoid saving a HAR.
4. Select the vehicles to add.

GM rejected the standard Home Assistant callback with `AADB2C90006` and serves its sign-in page for the registered mobile callback. PKCE binds the manually handed-off redirect to your setup flow. If your browser cannot expose the mobile redirect, choose **Import GM identity tokens** and paste the refresh token or the contents of a locally obtained `microsoft_tokens.json` issued to the same GM client. Home Assistant submits the refresh token directly to GM and validates the fresh Microsoft response. The reference Firefox tool can obtain a local token set. Review browser tools before using them and clear their redirect logs and downloaded token files afterward. [Reference Firefox tool](https://github.com/metheos/onstar_firefox)

Home Assistant prompts for reauthentication when GM revokes or stops refreshing the session. Open the notification in **Repairs**, then sign in to the same account and keep the original entry. Refresh lifetime and policy are controlled by GM. A fixed lifetime has not been verified.

## Read-only and optional controls

Read-only is the default, requires no OnStar PIN, and adds no remote control entities. The update button and `mychevrolet.refresh_data` only request telemetry. Location is off by default. Enable it under **Configure** to add a GPS tracker for vehicles with the location endpoint. The optional controls setting enables only supported actions advertised as non-privileged. PIN-gated or ambiguous entitlements stay disabled. Door state is unknown unless independently available. These Home Assistant settings do not narrow the scopes of GM's tokens.

## Telemetry and power semantics

| Reported when supported | Meaning |
| --- | --- |
| Battery level, target charge level, electric range, reported battery energy | State of charge and charge limit in percent, range in km, and GM's reported `kwh` value. Its interpretation as usable pack capacity is unverified. |
| Plug state, charge state, plug voltage, charger power level | Vehicle-reported status, GM diagnostic voltage and an unscaled charger category such as `level_2`. |
| Charge completion, time remaining and charge mode | GM's completion estimate. Time remaining is calculated from that timestamp. |
| Charge location and discharge settings | Reported location flags and discharge settings where GM returns these fields. |
| Odometer, tire pressures and trip economy | Exposed from the covered telemetry endpoints. Trip consumption rate is in kWh/100 km. |
| GPS, last successful telemetry read, telemetry age, health | Optional location, receipt time, newest observed sample age and endpoint health. |

A cached EV response may be older than the current poll. Each sample-based entity includes `sample_at` and `timestamp_source`. The latter is `vehicle` when GM supplies a sample timestamp or `onstar_response` when only receipt time is available. A recent receipt does not prove recent vehicle activity. Missing fields retain their prior sample and original timestamp. Aged samples become unavailable after the greater of 60 minutes and two configured intervals, as evaluated on coordinator updates. Health can show `reauth_required`, `rate_limited`, `degraded`, `stale` or `ok`. The age sensor refers to the newest sample across available telemetry. Automations using SOC or plug status should check that entity's availability and `sample_at` before acting. Use Emporia for measured EVSE electrical consumption. The integration has no verified mapping for measured charging power, charging current, or total last-trip energy.

Home Assistant polls every 30 minutes by default. Configure 15–1440 minutes to suit the vehicle and subscription. Repeated refresh requests coalesce and respect the 15-minute minimum and endpoint backoff. Temporary failures and sleeping or unreachable vehicles increase the delay. GM's `Retry-After` is honored when supplied. The integration prefers cached vehicle health and EV metrics. Legacy diagnostics and optional location can contact the vehicle. Charging status is sampled and can lag.

## Migrate, roll back and troubleshoot

1. Back up the Home Assistant configuration, including its registries, before replacing the upstream files.
2. Install this fork over the same `custom_components/mychevrolet` directory and restart. Keep the integration entry. The migration preserves its VIN-based entity unique IDs and device identifiers, removes older authentication fields and requests reauthentication. New configurations support selecting multiple VINs per account. Migrated VIN entries remain individual entries and share a session once linked to the same GM identity.
3. If you need to roll back to an upstream pre-migration build, restore the pre-migration Home Assistant backup and its matching integration files. Downgrading just the code cannot restore credentials or reverse the config-entry migration.

For `reauth_required`, complete GM sign-in again. For `rate_limited`, allow the cooldown to expire. For unavailable telemetry, check the entity's sample time and the integration's endpoint health. A 403 on an individual telemetry endpoint can mean the vehicle or subscription does not grant access. Download integration diagnostics for a report containing endpoint status and sample counts. Screenshots of your devices, raw network captures, debug logs from your browser, and Home Assistant backups can contain private data.

## Security and development

Account-scoped refresh tokens are written atomically with private permissions where supported. TLS certificate verification remains enabled. The integration verifies Microsoft identity tokens against GM's published signing keys, issuer and client audience. GM's exchanged service bearer is used with GM APIs over TLS. The integration derives account identity from the verified Microsoft claims. Config entries contain vehicle selections and an account identifier. Home Assistant backups can contain reusable refresh tokens, and local token storage is plaintext. Secure access to the Home Assistant configuration and backups. Removing the last entry for an account removes its token file. Browser sign-in and MFA remain GM's responsibility.

Use a Linux development environment with Python 3.14 and run:

```sh
python -m venv .venv
. .venv/bin/activate
pip install pytest-homeassistant==2026.11.3 ruff
ruff check .
ruff format --check .
python -m pytest
python scripts/package_release.py 0.2.0
```

Use sanitized fixtures and mock GM network requests. See the [modernization audit and checklist](docs/modernization.md) and [changelog](CHANGELOG.md). The vendored client derives from Bryan Leboff's pyOnStar and the OnStarJS project by Ruben Medina and contributors. The upstream integration was created by Dvalin21. Included OnStarJS license notices are under `custom_components/mychevrolet/onyonstar`. Public release preparation remains subject to resolving the upstream integration's missing license grant. [Original integration](https://github.com/Dvalin21/hass-mychevrolet) · [pyOnStar](https://github.com/leboff/pyonstar) · [OnStarJS](https://github.com/BigThunderSR/OnStarJS)
