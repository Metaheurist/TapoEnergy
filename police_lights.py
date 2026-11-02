"""Flash Master bedroom L535B red/blue like police lights until stopped (Ctrl+C)."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from dotenv import load_dotenv
from tapo import ApiClient

load_dotenv(Path(__file__).resolve().parent / ".env")

# Master bedroom from last scan
DEVICE_IP = os.getenv("POLICE_LIGHT_IP", "192.168.1.81")
BRIGHTNESS = int(os.getenv("POLICE_BRIGHTNESS", "100"))
INTERVAL_S = float(os.getenv("POLICE_INTERVAL", "0.35"))

_stop = False


def _request_stop(*_args) -> None:
    global _stop
    _stop = True
    print("\nStopping police lights…")


async def main() -> None:
    username = os.getenv("TAPO_USERNAME", "")
    password = os.getenv("TAPO_PASSWORD", "")
    if not username or not password:
        raise SystemExit("Set TAPO_USERNAME / TAPO_PASSWORD in .env")

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    print(f"Connecting to Master bedroom at {DEVICE_IP}…", flush=True)
    client = ApiClient(username, password, timeout_s=20)
    light = await client.l535(DEVICE_IP)
    info = await light.get_device_info()
    print(f"Connected to '{info.nickname}' ({info.model})", flush=True)

    await light.on()
    await light.set_brightness(BRIGHTNESS)
    print(f"Police lights running on {DEVICE_IP} — tell me to stop, or Ctrl+C.", flush=True)

    # Classic police flash: saturated red / blue
    colors = ((0, 100), (240, 100))
    idx = 0
    while not _stop:
        hue, sat = colors[idx % 2]
        try:
            await light.set_hue_saturation(hue, sat)
            await light.set_brightness(BRIGHTNESS)
        except Exception as exc:
            print(f"Command failed ({exc}); retrying…")
            await asyncio.sleep(1)
            continue
        idx += 1
        await asyncio.sleep(INTERVAL_S)

    # Leave a calm white-ish state when stopping
    try:
        await light.set_color_temperature(4000)
        await light.set_brightness(40)
    except Exception:
        pass
    print("Stopped.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _request_stop()
