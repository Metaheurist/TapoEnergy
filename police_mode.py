"""Background police-light flash across discovered colour bulbs, with state restore."""

from __future__ import annotations

import asyncio
import os
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from tapo import ApiClient

from tapo_scanner import DeviceReading, _client_method_for_model, _creds

# Fast red / blue flash
INTERVAL_S = float(os.getenv("POLICE_INTERVAL", "0.12"))
BRIGHTNESS = int(os.getenv("POLICE_BRIGHTNESS", "100"))
COLORS = ((0, 100), (240, 100))  # red, blue


@dataclass
class LightSnapshot:
    ip: str
    name: str
    model: str
    device_on: bool
    brightness: int | None
    color_temp: int | None
    hue: int | None
    saturation: int | None

    @classmethod
    def from_device(cls, device: DeviceReading) -> LightSnapshot:
        return cls(
            ip=device.ip,
            name=device.name,
            model=device.model,
            device_on=bool(device.device_on),
            brightness=device.brightness,
            color_temp=device.color_temp,
            hue=device.hue,
            saturation=device.saturation,
        )


class PoliceModeController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False
        self._restoring = False
        self._error: str | None = None
        self._snapshots: list[LightSnapshot] = []
        self._message = "Police mode off."

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "restoring": self._restoring,
                "error": self._error,
                "message": self._message,
                "lights": [
                    {"ip": s.ip, "name": s.name, "model": s.model}
                    for s in self._snapshots
                ],
                "count": len(self._snapshots),
            }

    def start(self, devices: list[DeviceReading]) -> dict[str, Any]:
        colour_lights = [
            d for d in devices if d.kind == "bulb" and d.is_color
        ]
        if not colour_lights:
            return {
                "ok": False,
                "error": "No colour lights in the last scan. Refresh scan first.",
                **self.status(),
            }

        with self._lock:
            if self._active or (self._thread and self._thread.is_alive()):
                return {
                    "ok": False,
                    "error": "Police mode is already running.",
                    **self.status(),
                }
            self._snapshots = [LightSnapshot.from_device(d) for d in colour_lights]
            self._error = None
            self._message = (
                f"Police mode on — flashing {len(self._snapshots)} light(s)."
            )
            self._active = True
            self._restoring = False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="police-mode",
                daemon=True,
            )
            self._thread.start()

        return {"ok": True, **self.status()}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            was_active = self._active or (thread is not None and thread.is_alive())
            snapshots = list(self._snapshots)
            if was_active:
                self._message = "Stopping police mode — restoring lights…"
                self._restoring = True
            self._stop.set()
            self._active = False

        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=30)

        with self._lock:
            self._restoring = False
            if not self._error:
                self._message = "Police mode off — previous light states restored."
            self._thread = None

        return {"ok": True, "snapshots": snapshots, **self.status()}

    def _thread_main(self) -> None:
        try:
            if os.name == "nt":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(self._async_main())
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
                self._message = f"Police mode failed: {exc}"
                self._active = False
                self._restoring = False

    async def _async_main(self) -> None:
        username, password = _creds()
        if not username or not password or username.startswith("your_tapo"):
            raise RuntimeError("Set TAPO_USERNAME and TAPO_PASSWORD in your .env file.")

        with self._lock:
            snapshots = deepcopy(self._snapshots)

        client = ApiClient(username, password, timeout_s=20)
        handlers: list[Any] = []
        for snap in snapshots:
            method = _client_method_for_model(snap.model)
            if not method:
                continue
            handler = await getattr(client, method)(snap.ip)
            await handler.on()
            try:
                await handler.set_brightness(BRIGHTNESS)
            except Exception:
                pass
            handlers.append((snap, handler))

        if not handlers:
            raise RuntimeError("Could not connect to any colour lights.")

        idx = 0
        while not self._stop.is_set():
            hue, sat = COLORS[idx % 2]
            await asyncio.gather(
                *[self._set_colour(handler, hue, sat) for _snap, handler in handlers]
            )
            idx += 1
            try:
                await asyncio.wait_for(self._stop_wait(), timeout=INTERVAL_S)
            except asyncio.TimeoutError:
                pass

        with self._lock:
            self._restoring = True
            self._active = False
            self._message = "Restoring previous light states…"

        await asyncio.gather(
            *[self._restore(snap, handler) for snap, handler in handlers],
            return_exceptions=True,
        )

    async def _stop_wait(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.02)
        return None

    @staticmethod
    async def _set_colour(handler: Any, hue: int, sat: int) -> None:
        try:
            await handler.set_hue_saturation(hue, sat)
            await handler.set_brightness(BRIGHTNESS)
        except Exception:
            pass

    @staticmethod
    async def _restore(snap: LightSnapshot, handler: Any) -> None:
        try:
            if not snap.device_on:
                await handler.off()
                return

            await handler.on()
            ct = snap.color_temp
            if ct is not None and int(ct) >= 2500:
                await handler.set_color_temperature(int(ct))
            elif snap.hue is not None and snap.saturation is not None:
                await handler.set_hue_saturation(
                    int(snap.hue),
                    max(1, int(snap.saturation)),
                )
            if snap.brightness is not None:
                await handler.set_brightness(max(1, int(snap.brightness)))
        except Exception:
            pass


police_mode = PoliceModeController()
