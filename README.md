# AIPAI Aquarium Light for Home Assistant

An unofficial Home Assistant custom integration for AIPAI-branded WiFi aquarium
LED controllers (sold under names including A8-SE8, A8SE Blue/Max, A8 PRO
Blue/Max, and A7 III — clones of the EcoTech XR30 G5). Built by
reverse-engineering the official AIPAI Android app, since that app is unreliable
and has no official local or cloud API.

**Not affiliated with, endorsed by, or supported by the manufacturer or the
AIPAI app developers.** Use at your own risk — see [Security](#security) below.

> This is the public install repo. Development happens in a separate private
> repository; only the integration itself is published here.

## Features

- One HA device per light, with a `number` control (0–100 %) per spectral
  channel — count and labels auto-detected from the light's model. (Channels are
  `number`, not `light`, so a fixture never floods the "Lights" summary.)
- A master **Power** switch and a **Control mode** select (Manual vs Scheduled).
- **The Reef Light card** (`type: custom:aipai-reef-card`) — the main UI, served
  and auto-registered by the integration (no token, no manual resource):
  - **Schedule designer** — the day is a set of **time points**, each carrying a
    level for *every* channel (white-heavy mornings, blue-heavy evenings). Drag
    points to re-time, tap to add; sunrise/sunset are implicit.
  - **Preview, then save** — edits show on the lights immediately; nothing is
    permanent until **Save settings**, and **Discard** reverts.
  - **Three blank preset slots** — tapping one *views* it; Apply/Edit/Delete are
    explicit buttons. **Import / Export** shareable, label-keyed configs.
  - **Moonlight** timer.
- **Automatic, DST-aware clock sync** (on connect + every 6 h) — the schedule
  stays on the right local time without ever opening the vendor app.
- Add as many lights as you own, each as its own config entry.

## Install

**Via HACS** (custom repository):

1. HACS → ⋮ (top right) → **Custom repositories**.
2. Repository: `https://github.com/markosharknz1/ha-aipai-light`,
   Type: **Integration** → **Add**.
3. Find **AIPAI Aquarium Light**, **Download**, then **restart** Home Assistant.

**Or manually:** copy `custom_components/aipai_light` into your
`config/custom_components/`, restart, then **Settings → Devices & Services → Add
Integration → "AIPAI Aquarium Light"**.

Then add each light: enter its serial (from the AIPAI app's device list, or the
unit itself) and keep the default **Light (verified)** type.

### Add the card

Any dashboard → **Edit** → **＋ Add card** → search **AIPAI Reef Light**. Or add
it manually:

```yaml
type: custom:aipai-reef-card
name: Display tank          # optional
# serials: ["12345678"]     # optional; omit = every light HA knows about
```

Editing the card previews **live on the real lights** and stays until you press
**Save settings** or **Discard** — it's not a sandbox.

## Supported hardware

Channel layouts for the whole A7/A8 family are applied automatically from the
model each light reports (A8-SE8, A8-PRO5/6, A8-SEB/PROB, A8-SE/S, A8-HP, A8-X,
A7-S, A7-P, A7-P4, A46-P, …). Unknown models fall back to a channel count derived
from the light's own state. **Only the A8-SE8 has been verified on real
hardware;** other mappings are transcribed from the app — please report back.

The config flow can also add **experimental, unverified** support for the
non-light AIPAI devices (pumps, doser, skimmer, ATO, chiller, …); only "Light"
is hardware-verified.

## Security

The vendor's cloud broker uses **one shared, hardcoded username and password
across every copy of the app** — not tied to your account or device. In effect,
knowing a light's **serial number** is enough to read or control it from
anywhere. This integration uses the same shared credentials the app does,
because there's no alternative — it's a manufacturer design decision, not
introduced here. Practical implication: **don't publish your device serials.**

## License

MIT — see [LICENSE](LICENSE). Provided as-is with no warranty; you are
responsible for how you use it with your own hardware.
