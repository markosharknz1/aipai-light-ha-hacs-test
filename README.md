# AIPAI Aquarium Light — HACS install test

A **public test copy** of the AIPAI Aquarium Light Home Assistant integration,
published solely to validate the HACS custom-repository install path (standard
HACS cannot install from private repositories).

The canonical project — including the schedule designer, dashboards,
provisioning tools, protocol documentation and tests — lives elsewhere and is
currently private. This repo contains only the integration itself.

## Install (HACS)

1. HACS → ⋮ (top right) → **Custom repositories**.
2. Repository: `https://github.com/markosharknz1/aipai-light-ha-hacs-test`,
   Type: **Integration** → **Add**.
3. Find **AIPAI Aquarium Light** in HACS → **Download** → **restart** Home Assistant.
4. Settings → Devices & Services → **Add Integration** → search **AIPAI** →
   enter the light's serial number.

## What it does

Controls AIPAI / Doseen WiFi aquarium lights (sold as A8-SE8, A8-PRO, A7 and
related models — clones of the EcoTech Radion XR30 G5), which otherwise only
work through a poor vendor app with no API. Each light appears as:

- a **Power** switch and a **Control mode** select (manual vs scheduled),
- a 0–100 % **number** control per spectral channel (auto-detected per model),
- a **Temperature** sensor and a diagnostic **Schedule** sensor,
- services for the clock, day schedule, and the native moonlight timer, plus a
  `generate_dashboard` action.

The light's clock is kept correct automatically (DST-aware), so its
sunrise/sunset schedule stays accurate without the vendor app.

## Notes

- Only the **A8-SE8** is verified against real hardware. Other models and all
  non-light devices are transcribed from the vendor app and **unverified**.
- The integration connects to the vendor's cloud MQTT broker using the
  credentials that ship, identical, inside every copy of the vendor app — it is
  not a per-user secret. Anyone knowing a light's serial can control it; that is
  the vendor's design, not something introduced here.

## Licence

MIT — see [LICENSE](LICENSE). Not affiliated with or endorsed by the
manufacturer.
