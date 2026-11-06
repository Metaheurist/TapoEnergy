# Tapo Energy

Local network energy dashboard with full control of TP-Link Tapo bulbs and P110 smart plugs. Scan your LAN, chart usage, and flip device tiles to power, dim, and recolour lights — no cloud UI required once devices are on your network.

![Energy graph view](docs/graph-view.png)

## Features

- **Discover** Tapo L-series bulbs and P110 / P110M plugs on a configurable subnet
- **Energy KPIs** for today, 7 days, 30 days, and live plug load (W)
- **Charts** — usage by device plus plug/light selection mix
- **Device tables** with live watts and historical kWh
- **Full device control** — on / off / toggle; brightness; colour presets + hue/sat; colour temperature
- **Light Modes** — Control-view effects (police, disco, chaos, heartbeat, lightning, fireplace, alien, SOS) across colour lights, with full state restore
- **Export CSV** of the current scan
- Optional **60s auto-refresh**

![Device control tiles](docs/control-view.png)

![Flipped tile with light controls](docs/control-detail.png)

## Supported devices

| Kind | Models |
|------|--------|
| Lights | L510, L520, L610, L430, L530, L535, L630, L900, L920, L930 |
| Plugs | P110, P115 (energy-monitoring plugs) |

## Setup

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Tapo account credentials and subnet:

```env
TAPO_USERNAME=your_tapo_email@example.com
TAPO_PASSWORD=your_tapo_password_here
SUBNET=192.168.1.0/24
FLASK_HOST=0.0.0.0
FLASK_PORT=8080
```

`.env` is gitignored — keep secrets out of the repo. Use `.env.example` as the template.

Bind `0.0.0.0` so other devices on your LAN can open the dashboard (e.g. `http://<your-pc-ip>:8080`).

## Run

```bash
python app.py
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) locally, or `http://192.168.1.91:8080` from another device on the network.

### LG webOS / Smart TV

Dash (React/Plotly) often fails on TV browsers and only shows the background. Use the lightweight TV UI instead:

- **http://192.168.1.91:8080/tv**
- Opening the root URL from a webOS / Smart TV user-agent auto-redirects to `/tv`
- Large buttons for on/off, brightness presets, and colour swatches (remote-friendly, no modern JS)

1. Click **Refresh scan** to discover devices on the subnet  
2. Select devices (or use All / Plugs / Lights) to drive KPIs and charts  
3. Toggle **Graph → Control** for flip-tile power and light controls  
4. On Control, open **Light Modes** to run party / gag sequences  

### Light Modes (Control view)

1. Run a scan so colour bulbs are cached  
2. Switch to **Control** → **Light Modes**  
3. Pick a mode, toggle **Run mode** on — every colour light from the last scan joins the sequence  
4. Switch modes while running to change the gag without a new snapshot  
5. Toggle off — each light restores the on/off state, colour (or colour temperature), and brightness captured before the first mode started  

| Mode | What it does |
|------|----------------|
| Police | Fast red / blue flash |
| Disco Inferno | Rainbow hue chase across lights |
| Chaos Rave | Random colour per light every blink |
| Heartbeat | Red lub-dub brightness pulse |
| Lightning Storm | Moody blue with random white strikes |
| Fireplace | Warm orange flicker |
| Alien Abduction | Green siren pulse |
| SOS | Morse `... --- ...` in red |

White-only bulbs are skipped (they cannot set hue/saturation).

Optional env knobs:

| Var | Default | Role |
|-----|---------|------|
| `EFFECT_BRIGHTNESS` | `100` | Peak brightness (falls back to `POLICE_BRIGHTNESS`) |
| `EFFECT_MIN_FRAME_S` | `0.10` | Min seconds per animation frame (adaptive; work time counts) |
| `EFFECT_MAX_IN_FLIGHT` | `4` | Max concurrent bulb commands |
| `EFFECT_CMD_TIMEOUT` | `0.8` | Per-command timeout so hung bulbs don’t stall the loop |

## Project layout

| File | Role |
|------|------|
| `app.py` | Flask + Dash UI (graph view + control tiles + light-modes modal) |
| `tapo_scanner.py` | LAN discovery, energy reads, device commands |
| `light_effects.py` | Pluggable light-effect engine with snapshot / restore |
| `police_mode.py` | Thin back-compat shim around `light_effects` |
| `police_lights.py` | Optional single-bulb red/blue flash demo (CLI) |
| `.env.example` | Env template |

## Notes

- Runs on your local network; the machine must reach device IPs on the same LAN/VLAN as the plugs and bulbs.
- First scan can take a minute on a `/24` subnet while devices are discovered and polled.
- `police_lights.py` is a standalone toy script (`POLICE_LIGHT_IP`, `POLICE_BRIGHTNESS`, `POLICE_INTERVAL` optional) — stop with Ctrl+C.
- Dashboard light modes run from the Control UI; only one effect runs at a time.
