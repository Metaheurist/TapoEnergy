"""Scan the local network for Tapo L-series bulbs and P110 plugs."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from tapo import ApiClient, DiscoveryResult
from tapo.requests import EnergyDataInterval


@dataclass
class DeviceReading:
    ip: str
    name: str
    model: str
    kind: str  # "bulb" | "plug"
    today_kwh: float = 0.0
    past_7d_kwh: float = 0.0
    past_30d_kwh: float = 0.0
    live_watts: float = 0.0
    device_on: bool = False
    brightness: int | None = None
    color_temp: int | None = None
    hue: int | None = None
    saturation: int | None = None
    is_color: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_MODEL_CLIENT_METHOD = {
    "L510": "l510",
    "L520": "l520",
    "L610": "l610",
    "L430": "l430",
    "L530": "l530",
    "L535": "l535",
    "L630": "l630",
    "L900": "l900",
    "L920": "l920",
    "L930": "l930",
    "P110": "p110",
    "P115": "p115",
}


def _client_method_for_model(model: str) -> str | None:
    model_u = (model or "").upper()
    for prefix, method in _MODEL_CLIENT_METHOD.items():
        if model_u.startswith(prefix):
            return method
    if "P110" in model_u:
        return "p110"
    return None


async def _connect_device(client: ApiClient, device: DeviceReading) -> Any:
    method = _client_method_for_model(device.model)
    if not method:
        raise ValueError(f"Unsupported model: {device.model}")
    return await getattr(client, method)(device.ip)


@dataclass
class HistoryPoint:
    ts: str  # ISO local/UTC timestamp
    kwh: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeviceHistory:
    ip: str
    name: str
    kind: str
    points: list[HistoryPoint] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "name": self.name,
            "kind": self.kind,
            "points": [p.to_dict() for p in self.points],
            "error": self.error,
        }


@dataclass
class HistoryResult:
    series: list[DeviceHistory] = field(default_factory=list)
    days: int = 7
    fetched_at: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "series": [s.to_dict() for s in self.series],
            "days": self.days,
            "fetched_at": self.fetched_at,
            "error": self.error,
        }


@dataclass
class ScanResult:
    bulbs: list[DeviceReading] = field(default_factory=list)
    plugs: list[DeviceReading] = field(default_factory=list)
    scanned_at: float = 0.0
    subnet: str = ""
    error: str | None = None

    @property
    def devices(self) -> list[DeviceReading]:
        return [*self.bulbs, *self.plugs]

    @property
    def totals(self) -> dict[str, float]:
        devices = self.devices
        return {
            "today_kwh": sum(d.today_kwh for d in devices),
            "past_7d_kwh": sum(d.past_7d_kwh for d in devices),
            "past_30d_kwh": sum(d.past_30d_kwh for d in devices),
            "live_watts": sum(d.live_watts for d in devices),
            "device_count": float(len(devices)),
            "bulb_count": float(len(self.bulbs)),
            "plug_count": float(len(self.plugs)),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "bulbs": [d.to_dict() for d in self.bulbs],
            "plugs": [d.to_dict() for d in self.plugs],
            "devices": [d.to_dict() for d in self.devices],
            "totals": self.totals,
            "scanned_at": self.scanned_at,
            "subnet": self.subnet,
            "error": self.error,
        }


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except Exception:
        pass


def _creds() -> tuple[str, str]:
    _load_env()
    return os.getenv("TAPO_USERNAME", ""), os.getenv("TAPO_PASSWORD", "")


def _broadcast_for_subnet(subnet: str) -> str:
    """Turn 192.168.1.0/24 into the subnet broadcast, or pass through a host/broadcast IP."""
    text = subnet.strip()
    if "/" in text:
        return str(ipaddress.ip_network(text, strict=False).broadcast_address)
    return text


def _wh_to_kwh(value: float | int | None) -> float:
    return float(value or 0) / 1000.0


def _mw_to_w(value: float | int | None) -> float:
    return float(value or 0) / 1000.0


def _quarter_start(day: date) -> date:
    month = ((day.month - 1) // 3) * 3 + 1
    return date(day.year, month, 1)


async def _read_bulb(handler: Any, info: Any, ip: str, *, is_color: bool = False) -> DeviceReading:
    usage = await handler.get_device_usage()
    power = usage.power_usage
    return DeviceReading(
        ip=ip,
        name=getattr(info, "nickname", None) or ip,
        model=getattr(info, "model", "") or "",
        kind="bulb",
        today_kwh=_wh_to_kwh(power.today),
        past_7d_kwh=_wh_to_kwh(power.past7),
        past_30d_kwh=_wh_to_kwh(power.past30),
        live_watts=0.0,
        device_on=bool(getattr(info, "device_on", False)),
        brightness=getattr(info, "brightness", None),
        color_temp=getattr(info, "color_temp", None),
        hue=getattr(info, "hue", None),
        saturation=getattr(info, "saturation", None),
        is_color=is_color,
    )


async def _read_plug(handler: Any, info: Any, ip: str) -> DeviceReading:
    energy = await handler.get_energy_usage()
    usage = await handler.get_device_usage()
    power = usage.power_usage
    return DeviceReading(
        ip=ip,
        name=getattr(info, "nickname", None) or ip,
        model=getattr(info, "model", "") or "",
        kind="plug",
        today_kwh=_wh_to_kwh(getattr(energy, "today_energy", None) or power.today),
        past_7d_kwh=_wh_to_kwh(power.past7),
        past_30d_kwh=_wh_to_kwh(power.past30),
        live_watts=_mw_to_w(energy.current_power),
        device_on=bool(getattr(info, "device_on", False)),
        is_color=False,
    )


async def scan_network(
    username: str,
    password: str,
    subnet: str,
    timeout_s: int = 12,
) -> ScanResult:
    result = ScanResult(subnet=subnet, scanned_at=time.time())

    if not username or not password or username.startswith("your_tapo"):
        result.error = "Set TAPO_USERNAME and TAPO_PASSWORD in your .env file."
        return result

    try:
        client = ApiClient(username, password, timeout_s=20)
        target = _broadcast_for_subnet(subnet)
        bulbs: list[DeviceReading] = []
        plugs: list[DeviceReading] = []

        async for maybe in await client.discover_devices(target, timeout_s=timeout_s):
            try:
                discovered = maybe.get()
            except Exception:
                continue

            try:
                if isinstance(
                    discovered,
                    (
                        DiscoveryResult.Light,
                        DiscoveryResult.ColorLight,
                        DiscoveryResult.RgbLightStrip,
                        DiscoveryResult.RgbicLightStrip,
                    ),
                ):
                    ip, _port = discovered.handler.address
                    is_color = isinstance(
                        discovered,
                        (
                            DiscoveryResult.ColorLight,
                            DiscoveryResult.RgbLightStrip,
                            DiscoveryResult.RgbicLightStrip,
                        ),
                    )
                    bulbs.append(
                        await _read_bulb(
                            discovered.handler,
                            discovered.device_info,
                            ip,
                            is_color=is_color,
                        )
                    )
                elif isinstance(discovered, DiscoveryResult.PlugEnergyMonitoring):
                    ip, _port = discovered.handler.address
                    plugs.append(await _read_plug(discovered.handler, discovered.device_info, ip))
            except Exception:
                continue

        result.bulbs = sorted(bulbs, key=lambda d: d.name.lower())
        result.plugs = sorted(plugs, key=lambda d: d.name.lower())

        if not result.devices:
            result.error = (
                f"No compatible Tapo lights/plugs found via {target}. "
                "Check .env credentials and that SUBNET matches your LAN "
                "(e.g. 192.168.1.0/24 or 192.168.1.255)."
            )
    except Exception as exc:
        result.error = f"Scan failed: {exc}"

    result.scanned_at = time.time()
    return result


async def _fetch_one_plug_history(
    client: ApiClient,
    device: DeviceReading,
    days: int,
) -> DeviceHistory:
    history = DeviceHistory(ip=device.ip, name=device.name, kind="plug")
    try:
        plug = await client.p110(device.ip)
        end = date.today()
        start = end - timedelta(days=max(days, 1) - 1)

        if days <= 8:
            data = await plug.get_energy_data(EnergyDataInterval.Hourly, start, end)
        else:
            data = await plug.get_energy_data(
                EnergyDataInterval.Daily,
                _quarter_start(end),
                end,
            )

        points: list[HistoryPoint] = []
        cutoff = datetime.combine(start, datetime.min.time())
        for entry in data.entries:
            energy = entry.energy
            if energy is None:
                continue
            ts = entry.start_date_time
            # Daily mode returns a full quarter — keep only the requested window.
            ts_naive = ts.replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts
            if ts_naive < cutoff:
                continue
            points.append(
                HistoryPoint(
                    ts=ts.isoformat(),
                    kwh=_wh_to_kwh(energy),
                )
            )
        history.points = points
    except Exception as exc:
        history.error = str(exc)
    return history


async def _fetch_one_bulb_summary(
    device: DeviceReading,
    days: int,
) -> DeviceHistory:
    """Bulbs expose only aggregate windows — project them onto a simple timeline."""
    history = DeviceHistory(ip=device.ip, name=device.name, kind="bulb")
    today = date.today()
    points: list[HistoryPoint] = []

    if days <= 1:
        points.append(
            HistoryPoint(ts=datetime.combine(today, datetime.min.time()).isoformat(), kwh=device.today_kwh)
        )
    elif days <= 7:
        # Evenly distribute past-7d across the lookback (best available estimate).
        per_day = device.past_7d_kwh / 7.0 if device.past_7d_kwh else 0.0
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            value = device.today_kwh if offset == 0 and device.today_kwh else per_day
            points.append(
                HistoryPoint(ts=datetime.combine(day, datetime.min.time()).isoformat(), kwh=value)
            )
    else:
        per_day = device.past_30d_kwh / 30.0 if device.past_30d_kwh else 0.0
        for offset in range(min(days, 30) - 1, -1, -1):
            day = today - timedelta(days=offset)
            value = device.today_kwh if offset == 0 and device.today_kwh else per_day
            points.append(
                HistoryPoint(ts=datetime.combine(day, datetime.min.time()).isoformat(), kwh=value)
            )

    history.points = points
    return history


async def fetch_history(
    devices: list[DeviceReading],
    days: int,
    username: str | None = None,
    password: str | None = None,
) -> HistoryResult:
    days = max(1, min(int(days), 30))
    result = HistoryResult(days=days, fetched_at=time.time())

    username = username or _creds()[0]
    password = password or _creds()[1]
    if not username or not password or username.startswith("your_tapo"):
        result.error = "Set TAPO_USERNAME and TAPO_PASSWORD in your .env file."
        return result

    if not devices:
        result.error = "Select at least one device."
        return result

    try:
        client = ApiClient(username, password, timeout_s=20)
        tasks = []
        for device in devices:
            if device.kind == "plug":
                tasks.append(_fetch_one_plug_history(client, device, days))
            else:
                tasks.append(_fetch_one_bulb_summary(device, days))
        series = await asyncio.gather(*tasks)
        result.series = list(series)
        if all(s.error and not s.points for s in result.series):
            result.error = "Could not load history for the selected devices."
    except Exception as exc:
        result.error = f"History fetch failed: {exc}"

    result.fetched_at = time.time()
    return result


def run_scan(
    username: str | None = None,
    password: str | None = None,
    subnet: str | None = None,
) -> ScanResult:
    username = username or _creds()[0]
    password = password or _creds()[1]
    subnet = subnet or os.getenv("SUBNET", "192.168.1.0/24")

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    return asyncio.run(scan_network(username, password, subnet))


def run_history(
    devices: list[DeviceReading],
    days: int,
) -> HistoryResult:
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(fetch_history(devices, days))


async def apply_device_command(
    device: DeviceReading,
    action: str,
    value: Any = None,
    username: str | None = None,
    password: str | None = None,
) -> DeviceReading:
    """Run a control action and return refreshed device state fields."""
    username = username or _creds()[0]
    password = password or _creds()[1]
    client = ApiClient(username, password, timeout_s=20)
    handler = await _connect_device(client, device)

    if action == "on":
        await handler.on()
    elif action == "off":
        await handler.off()
    elif action == "toggle":
        info = await handler.get_device_info()
        if bool(getattr(info, "device_on", False)):
            await handler.off()
        else:
            await handler.on()
    elif action == "brightness":
        await handler.set_brightness(int(value))
    elif action == "color_temp":
        await handler.set_color_temperature(int(value))
    elif action == "hue_sat":
        hue, sat = value
        await handler.set_hue_saturation(int(hue), int(sat))
    else:
        raise ValueError(f"Unknown action: {action}")

    info = await handler.get_device_info()
    device.device_on = bool(getattr(info, "device_on", device.device_on))
    if hasattr(info, "brightness"):
        device.brightness = getattr(info, "brightness", device.brightness)
    if hasattr(info, "color_temp"):
        device.color_temp = getattr(info, "color_temp", device.color_temp)
    if hasattr(info, "hue"):
        device.hue = getattr(info, "hue", device.hue)
    if hasattr(info, "saturation"):
        device.saturation = getattr(info, "saturation", device.saturation)

    if device.kind == "plug":
        try:
            energy = await handler.get_energy_usage()
            device.live_watts = _mw_to_w(energy.current_power)
        except Exception:
            pass

    return device


def run_device_command(device: DeviceReading, action: str, value: Any = None) -> DeviceReading:
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(apply_device_command(device, action, value))


def update_cached_device(result: ScanResult, updated: DeviceReading) -> ScanResult:
    target = result.bulbs if updated.kind == "bulb" else result.plugs
    for i, existing in enumerate(target):
        if existing.ip == updated.ip:
            target[i] = updated
            break
    return result
