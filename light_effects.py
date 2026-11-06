"""Pluggable light-effect sequences across colour bulbs, with state restore.

Designed to stay light on the LAN and never block the Dash/Flask request thread:
a single persistent worker owns all device I/O; start/stop only enqueue commands.
"""

from __future__ import annotations

import asyncio
import math
import os
import queue
import random
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tapo import ApiClient

from tapo_scanner import DeviceReading, _client_method_for_model, _creds

BRIGHTNESS = int(os.getenv("EFFECT_BRIGHTNESS", os.getenv("POLICE_BRIGHTNESS", "100")))
CMD_TIMEOUT_S = float(os.getenv("EFFECT_CMD_TIMEOUT", "1.0"))
MAX_IN_FLIGHT = int(os.getenv("EFFECT_MAX_IN_FLIGHT", "3"))


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


HandlerPair = tuple[LightSnapshot, Any]
EffectRunner = Callable[
    ["LightEffectController", list[HandlerPair], int],
    Awaitable[None],
]


@dataclass(frozen=True)
class EffectSpec:
    id: str
    label: str
    description: str
    runner: EffectRunner


class LightEffectController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._active = False
        self._restoring = False
        self._error: str | None = None
        self._snapshots: list[LightSnapshot] = []
        self._mode_id = "police"
        self._job_id = 0
        self._message = "Light modes off."
        self._sem: asyncio.Semaphore | None = None
        self._worker = threading.Thread(
            target=self._thread_main,
            name="light-effects",
            daemon=True,
        )
        self._worker.start()

    # ---- public API (non-blocking) ----

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = bool(self._active and not self._restoring)
            spec = EFFECT_BY_ID.get(self._mode_id)
            return {
                "active": active,
                "restoring": self._restoring,
                "error": self._error,
                "message": self._message,
                "mode": self._mode_id if active else None,
                "mode_label": (spec.label if spec and active else None),
                "lights": [
                    {"ip": s.ip, "name": s.name, "model": s.model}
                    for s in self._snapshots
                ],
                "count": len(self._snapshots),
            }

    def start(self, devices: list[DeviceReading], mode_id: str = "police") -> dict[str, Any]:
        if mode_id not in EFFECT_BY_ID:
            return {
                "ok": False,
                "error": f"Unknown light mode: {mode_id}",
                **self.status(),
            }

        colour_lights = [d for d in devices if d.kind == "bulb" and d.is_color]
        if not colour_lights:
            return {
                "ok": False,
                "error": "No colour lights in the last scan. Refresh scan first.",
                **self.status(),
            }

        spec = EFFECT_BY_ID[mode_id]
        with self._lock:
            if self._restoring:
                return {
                    "ok": False,
                    "error": "Still restoring previous light states — wait a moment.",
                    **self.status(),
                }
            if self._active and self._mode_id == mode_id:
                return {"ok": True, **self.status()}

            if not self._snapshots:
                self._snapshots = [LightSnapshot.from_device(d) for d in colour_lights]

            self._mode_id = mode_id
            self._job_id += 1
            job_id = self._job_id
            self._error = None
            self._active = True
            self._restoring = False
            self._message = f"{spec.label} on — {len(self._snapshots)} light(s)."

        # Interrupt current effect (if any) and enqueue the new mode.
        self._cancel.set()
        self._q.put(("run", mode_id, job_id))
        return {"ok": True, **self.status()}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            snapshots = list(self._snapshots)
            was_active = self._active or self._restoring
            if was_active:
                self._message = "Stopping light mode — restoring lights…"
                self._restoring = True
            self._active = False
            self._job_id += 1

        self._cancel.set()
        self._q.put(("stop",))
        return {"ok": True, "snapshots": snapshots, **self.status()}

    # ---- helpers used by effect runners ----

    def current_mode(self) -> str:
        with self._lock:
            return self._mode_id

    def running_mode(self, mode_id: str, gen: int | None = None) -> bool:
        with self._lock:
            if self._cancel.is_set() or not self._active or self._mode_id != mode_id:
                return False
            if gen is not None and self._job_id != gen:
                return False
            return True

    async def sleep(self, seconds: float, mode_id: str | None = None, gen: int | None = None) -> bool:
        end = asyncio.get_running_loop().time() + max(0.0, seconds)
        while True:
            if not self.running_mode(mode_id or self.current_mode(), gen):
                return False
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(0.05, remaining))

    async def _await_cmd(self, coro: Awaitable[Any]) -> None:
        sem = self._sem
        try:
            if sem is not None:
                async with sem:
                    await asyncio.wait_for(coro, timeout=CMD_TIMEOUT_S)
            else:
                await asyncio.wait_for(coro, timeout=CMD_TIMEOUT_S)
        except Exception:
            pass

    async def set_hsb(
        self,
        handler: Any,
        *,
        hue: int | None = None,
        sat: int | None = None,
        brightness: int | None = None,
    ) -> None:
        # Prefer a single hue/sat call; only touch brightness when asked.
        if hue is not None and sat is not None:
            await self._await_cmd(
                handler.set_hue_saturation(int(hue) % 360, max(1, min(100, int(sat))))
            )
        if brightness is not None:
            await self._await_cmd(
                handler.set_brightness(max(1, min(100, int(brightness))))
            )

    async def set_temp(self, handler: Any, kelvin: int, brightness: int | None = None) -> None:
        await self._await_cmd(handler.set_color_temperature(int(kelvin)))
        if brightness is not None:
            await self._await_cmd(handler.set_brightness(max(1, min(100, int(brightness)))))

    async def apply_all(
        self,
        handlers: list[HandlerPair],
        *,
        mode_id: str,
        gen: int,
        hue: int | None = None,
        sat: int | None = None,
        brightness: int | None = None,
        kelvin: int | None = None,
    ) -> bool:
        if not self.running_mode(mode_id, gen):
            return False

        async def one(handler: Any) -> None:
            if kelvin is not None:
                await self.set_temp(handler, kelvin, brightness)
            else:
                await self.set_hsb(handler, hue=hue, sat=sat, brightness=brightness)

        await asyncio.gather(*[one(h) for _s, h in handlers])
        return self.running_mode(mode_id, gen)

    # ---- persistent worker ----

    def _thread_main(self) -> None:
        try:
            if os.name == "nt":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(self._async_worker())
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
                self._message = f"Light mode failed: {exc}"
                self._active = False
                self._restoring = False

    def _drain_latest(self) -> Any:
        """Block for one command, then keep only the newest if several piled up."""
        cmd = self._q.get()
        while True:
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                return cmd

    async def _async_worker(self) -> None:
        self._sem = asyncio.Semaphore(MAX_IN_FLIGHT)
        handlers: list[HandlerPair] = []
        client: ApiClient | None = None

        while True:
            cmd = await asyncio.to_thread(self._drain_latest)

            if cmd[0] == "run":
                mode_id, job_id = cmd[1], cmd[2]
                with self._lock:
                    current_job = self._job_id
                    snapshots = deepcopy(self._snapshots)
                if job_id != current_job:
                    continue

                self._cancel.clear()
                try:
                    client, handlers = await self._connect(client, snapshots)
                    if not handlers:
                        raise RuntimeError("Could not connect to any colour lights.")
                    spec = EFFECT_BY_ID.get(mode_id)
                    if spec and self.running_mode(mode_id, job_id):
                        await spec.runner(self, handlers, job_id)
                except Exception as exc:
                    with self._lock:
                        self._error = str(exc)
                        self._message = f"Light mode failed: {exc}"
                        self._active = False

                # If a newer run/stop is already queued, let that command decide restore.
                if not self._q.empty():
                    continue
                with self._lock:
                    still_this_job = self._job_id == job_id
                    should_restore = still_this_job and not self._active
                if should_restore and handlers:
                    await self._restore_all(handlers)
                    handlers = []
                    client = None

            elif cmd[0] == "stop":
                self._cancel.set()
                if handlers:
                    await self._restore_all(handlers)
                handlers = []
                client = None
                with self._lock:
                    self._active = False
                    self._restoring = False
                    self._snapshots = []
                    self._mode_id = "police"
                    if not self._error:
                        self._message = "Light modes off — previous light states restored."

    async def _connect(
        self,
        client: ApiClient | None,
        snapshots: list[LightSnapshot],
    ) -> tuple[ApiClient, list[HandlerPair]]:
        username, password = _creds()
        if not username or not password or username.startswith("your_tapo"):
            raise RuntimeError("Set TAPO_USERNAME and TAPO_PASSWORD in your .env file.")

        if client is None:
            client = ApiClient(username, password, timeout_s=8)

        handlers: list[HandlerPair] = []
        for snap in snapshots:
            if self._cancel.is_set():
                break
            method = _client_method_for_model(snap.model)
            if not method:
                continue
            try:
                handler = await asyncio.wait_for(
                    getattr(client, method)(snap.ip),
                    timeout=CMD_TIMEOUT_S * 2,
                )
                await self._await_cmd(handler.on())
                await self._await_cmd(handler.set_brightness(BRIGHTNESS))
                handlers.append((snap, handler))
            except Exception:
                continue
        return client, handlers

    async def _restore_all(self, handlers: list[HandlerPair]) -> None:
        with self._lock:
            self._restoring = True
            self._active = False
            self._message = "Restoring previous light states…"
        await asyncio.gather(
            *[self._restore(snap, handler) for snap, handler in handlers],
            return_exceptions=True,
        )
        with self._lock:
            self._restoring = False

    async def _restore(self, snap: LightSnapshot, handler: Any) -> None:
        try:
            if not snap.device_on:
                await self._await_cmd(handler.off())
                return
            await self._await_cmd(handler.on())
            ct = snap.color_temp
            if ct is not None and int(ct) >= 2500:
                await self._await_cmd(handler.set_color_temperature(int(ct)))
            elif snap.hue is not None and snap.saturation is not None:
                await self._await_cmd(
                    handler.set_hue_saturation(
                        int(snap.hue),
                        max(1, int(snap.saturation)),
                    )
                )
            if snap.brightness is not None:
                await self._await_cmd(handler.set_brightness(max(1, int(snap.brightness))))
        except Exception:
            pass


# --------------- effect runners (hue-first, slower frames) ---------------


async def _run_police(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    colors = ((0, 100), (240, 100))
    idx = 0
    while ctrl.running_mode("police", gen):
        hue, sat = colors[idx % 2]
        # Hue only — brightness already set on connect.
        if not await ctrl.apply_all(handlers, mode_id="police", gen=gen, hue=hue, sat=sat):
            return
        idx += 1
        if not await ctrl.sleep(0.28, "police", gen):
            return


async def _run_disco(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    step = 0
    while ctrl.running_mode("disco", gen):
        n = max(1, len(handlers))
        await asyncio.gather(
            *[
                ctrl.set_hsb(h, hue=(step * 14 + i * (360 // n)) % 360, sat=100)
                for i, (_s, h) in enumerate(handlers)
            ]
        )
        if not ctrl.running_mode("disco", gen):
            return
        step += 1
        if not await ctrl.sleep(0.25, "disco", gen):
            return


async def _run_chaos(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    while ctrl.running_mode("chaos", gen):
        await asyncio.gather(
            *[
                ctrl.set_hsb(h, hue=random.randint(0, 359), sat=random.randint(80, 100))
                for _s, h in handlers
            ]
        )
        if not ctrl.running_mode("chaos", gen):
            return
        if not await ctrl.sleep(0.22, "chaos", gen):
            return


async def _run_heartbeat(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    pattern = [(100, 0.18), (22, 0.14), (85, 0.18), (12, 0.7)]
    while ctrl.running_mode("heartbeat", gen):
        for bri, pause in pattern:
            if not await ctrl.apply_all(
                handlers, mode_id="heartbeat", gen=gen, hue=0, sat=100, brightness=bri
            ):
                return
            if not await ctrl.sleep(pause, "heartbeat", gen):
                return


async def _run_lightning(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    while ctrl.running_mode("lightning", gen):
        if not await ctrl.apply_all(
            handlers, mode_id="lightning", gen=gen, hue=230, sat=55, brightness=10
        ):
            return
        if not await ctrl.sleep(random.uniform(0.9, 2.6), "lightning", gen):
            return

        flashes = random.randint(1, 2)
        for _ in range(flashes):
            if not await ctrl.apply_all(
                handlers,
                mode_id="lightning",
                gen=gen,
                hue=210,
                sat=30,
                brightness=BRIGHTNESS,
            ):
                return
            if not await ctrl.sleep(random.uniform(0.08, 0.16), "lightning", gen):
                return
            if not await ctrl.apply_all(
                handlers, mode_id="lightning", gen=gen, hue=230, sat=55, brightness=6
            ):
                return
            if not await ctrl.sleep(random.uniform(0.06, 0.2), "lightning", gen):
                return


async def _run_fireplace(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    while ctrl.running_mode("fireplace", gen):
        await asyncio.gather(
            *[
                ctrl.set_hsb(
                    h,
                    hue=random.choice((18, 25, 32, 38)),
                    sat=random.randint(88, 100),
                    brightness=random.randint(40, 78),
                )
                for _s, h in handlers
            ]
        )
        if not ctrl.running_mode("fireplace", gen):
            return
        if not await ctrl.sleep(0.32, "fireplace", gen):
            return


async def _run_alien(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    t = 0.0
    while ctrl.running_mode("alien", gen):
        bri = int(55 + 35 * math.sin(t))
        hue = 120 + int(12 * math.sin(t * 0.7))
        if not await ctrl.apply_all(
            handlers, mode_id="alien", gen=gen, hue=hue, sat=100, brightness=bri
        ):
            return
        t += 0.4
        if not await ctrl.sleep(0.22, "alien", gen):
            return


async def _run_sos(
    ctrl: LightEffectController, handlers: list[HandlerPair], gen: int
) -> None:
    unit = 0.18
    letters = [[1, 1, 1], [3, 3, 3], [1, 1, 1]]
    while ctrl.running_mode("sos", gen):
        for li, letter in enumerate(letters):
            for si, symbol in enumerate(letter):
                if not await ctrl.apply_all(
                    handlers, mode_id="sos", gen=gen, brightness=BRIGHTNESS, hue=0, sat=100
                ):
                    return
                if not await ctrl.sleep(unit * symbol, "sos", gen):
                    return
                if not await ctrl.apply_all(
                    handlers, mode_id="sos", gen=gen, brightness=5, hue=0, sat=100
                ):
                    return
                if si < len(letter) - 1 and not await ctrl.sleep(unit, "sos", gen):
                    return
            if li < len(letters) - 1 and not await ctrl.sleep(unit * 3, "sos", gen):
                return
        if not await ctrl.sleep(unit * 7, "sos", gen):
            return


EFFECTS: list[EffectSpec] = [
    EffectSpec("police", "Police", "Classic red / blue flash — someone called it in.", _run_police),
    EffectSpec("disco", "Disco Inferno", "Rainbow chase across every colour light. Mirror ball not included.", _run_disco),
    EffectSpec("chaos", "Chaos Rave", "Each light picks a random colour every blink. No notes, just vibes.", _run_chaos),
    EffectSpec("heartbeat", "Heartbeat", "Dramatic red lub-dub. Peak soap-opera energy.", _run_heartbeat),
    EffectSpec("lightning", "Lightning Storm", "Moody blue, then random white strikes. Flinch optional.", _run_lightning),
    EffectSpec("fireplace", "Fireplace", "Warm orange flicker. Cozy, until you remember it’s LEDs.", _run_fireplace),
    EffectSpec("alien", "Alien Abduction", "Green siren pulse. Prepare for probing (of your dimmer).", _run_alien),
    EffectSpec("sos", "SOS", "Morse …---… in red. Extremely calm, definitely fine.", _run_sos),
]

EFFECT_BY_ID: dict[str, EffectSpec] = {e.id: e for e in EFFECTS}

light_effects = LightEffectController()
