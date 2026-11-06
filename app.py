"""Local Flask + Dash dashboard for Tapo bulbs and P110 plugs."""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, dash_table, dcc, html, no_update
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request

from police_mode import police_mode
from tapo_scanner import DeviceReading, ScanResult, run_device_command, run_scan, update_cached_device

load_dotenv(Path(__file__).resolve().parent / ".env")

# --------------- shared caches ---------------
_cache_lock = threading.Lock()
_cache: dict = {
    "result": ScanResult(error="No scan yet. Press Refresh to discover devices."),
    "scanning": False,
}


def _get_result() -> ScanResult:
    with _cache_lock:
        return _cache["result"]


def _set_result(result: ScanResult) -> None:
    with _cache_lock:
        _cache["result"] = result
        _cache["scanning"] = False


def _is_scanning() -> bool:
    with _cache_lock:
        return bool(_cache["scanning"])


def _start_scan(subnet: str | None = None) -> bool:
    with _cache_lock:
        if _cache["scanning"]:
            return False
        _cache["scanning"] = True

    def worker() -> None:
        try:
            result = run_scan(subnet=subnet or os.getenv("SUBNET", "192.168.1.0/24"))
            _set_result(result)
        except Exception as exc:
            _set_result(ScanResult(error=f"Scan failed: {exc}"))

    threading.Thread(target=worker, daemon=True).start()
    return True


# --------------- Flask + Dash ---------------
server = Flask(__name__, template_folder=str(Path(__file__).resolve().parent / "templates"))

# Smart TV browsers often cannot run the Dash/React bundle — send them to /tv.
_TV_UA_MARKERS = ("Web0S", "WebOS", "webOS", "SmartTV", "Smart TV", "NetCast", "LG Browser")


@server.before_request
def _redirect_smart_tv_to_tv_page():
    if request.path != "/":
        return None
    if request.args.get("full"):
        return None
    ua = request.headers.get("User-Agent", "")
    if any(marker in ua for marker in _TV_UA_MARKERS):
        return redirect("/tv")
    return None


app = Dash(
    __name__,
    server=server,
    external_stylesheets=[dbc.themes.FLATLY, dbc.icons.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title="Tapo Energy",
)

COLORS = {
    "bg": "#0f1419",
    "panel": "#1a2332",
    "panel2": "#243044",
    "text": "#e8eef5",
    "muted": "#8b9bb0",
    "accent": "#2dd4a8",
    "accent2": "#f0b429",
    "bulb": "#5b8def",
    "plug": "#2dd4a8",
    "danger": "#ef6b6b",
    "grid": "#2a3548",
}

def _kpi(label: str, value_id: str, hint: str = "") -> dbc.Card:
    return dbc.Card(
        dbc.CardBody(
            [
                html.Div(label, className="kpi-label"),
                html.Div(id=value_id, className="kpi-value"),
                html.Div(hint, className="kpi-hint") if hint else None,
            ]
        ),
        className="kpi-card",
    )


TABLE_COLUMNS = [
    {"name": "Name", "id": "name"},
    {"name": "Model", "id": "model"},
    {"name": "Live (W)", "id": "live_watts", "type": "numeric", "format": {"specifier": ".1f"}},
    {"name": "Today", "id": "today_kwh", "type": "numeric", "format": {"specifier": ".3f"}},
    {"name": "7d", "id": "past_7d_kwh", "type": "numeric", "format": {"specifier": ".3f"}},
    {"name": "30d", "id": "past_30d_kwh", "type": "numeric", "format": {"specifier": ".3f"}},
]

TABLE_STYLE = {
    "style_table": {
        "width": "100%",
        "minWidth": "100%",
        "height": "320px",
        "overflowY": "auto",
        "overflowX": "auto",
    },
    "style_header": {
        "backgroundColor": COLORS["panel2"],
        "color": COLORS["text"],
        "fontWeight": "600",
        "border": "none",
        "whiteSpace": "nowrap",
    },
    "style_cell": {
        "backgroundColor": COLORS["panel"],
        "color": COLORS["text"],
        "border": f"1px solid {COLORS['grid']}",
        "fontFamily": "IBM Plex Sans, Segoe UI, sans-serif",
        "fontSize": "13px",
        "padding": "10px 12px",
        "minWidth": "72px",
        "maxWidth": "160px",
        "overflow": "hidden",
        "textOverflow": "ellipsis",
        "whiteSpace": "nowrap",
    },
    "style_cell_conditional": [
        {"if": {"column_id": "name"}, "minWidth": "120px", "maxWidth": "180px", "textAlign": "left"},
        {"if": {"column_id": "model"}, "minWidth": "72px", "maxWidth": "88px", "textAlign": "left"},
        {"if": {"column_id": "live_watts"}, "minWidth": "78px", "textAlign": "right"},
        {"if": {"column_id": "today_kwh"}, "minWidth": "72px", "textAlign": "right"},
        {"if": {"column_id": "past_7d_kwh"}, "minWidth": "64px", "textAlign": "right"},
        {"if": {"column_id": "past_30d_kwh"}, "minWidth": "64px", "textAlign": "right"},
    ],
}


def _device_table(table_id: str, extra_conditional: list | None = None) -> dash_table.DataTable:
    conditional = [{"if": {"row_index": "odd"}, "backgroundColor": COLORS["panel2"]}]
    if extra_conditional:
        conditional.extend(extra_conditional)
    return dash_table.DataTable(
        id=table_id,
        columns=TABLE_COLUMNS,
        row_selectable="multi",
        selected_rows=[],
        page_size=10,
        style_as_list_view=True,
        css=[{"selector": ".dash-spreadsheet-container", "rule": "width: 100%;"}],
        style_data_conditional=conditional,
        **TABLE_STYLE,
    )


app.layout = dbc.Container(
    [
        dcc.Store(id="device-options-store"),
        dcc.Store(id="view-mode", data="graph"),
        dcc.Store(id="flipped-tiles", data=[]),
        dcc.Store(id="control-version", data=0),
        dcc.Store(id="scan-stamp", data=0),
        dcc.Interval(id="poll-interval", interval=1500, n_intervals=0),
        dcc.Interval(id="auto-refresh", interval=60_000, n_intervals=0, disabled=True),
        html.Div(
            [
                html.Div(
                    [
                        html.Span("TAPO ENERGY", className="brand"),
                        html.Span("Local network dashboard", className="brand-sub"),
                    ],
                    className="brand-block",
                ),
                html.Div(
                    [
                        html.Div(id="status-badge"),
                        html.Div(
                            [
                                html.Span("Graph", className="view-label"),
                                dbc.Switch(
                                    id="view-toggle",
                                    value=False,
                                    className="view-switch",
                                ),
                                html.Span("Control", className="view-label"),
                            ],
                            className="view-toggle-wrap",
                            title="Switch between energy graphs and device controls",
                        ),
                    ],
                    className="status-wrap",
                ),
            ],
            className="topbar",
        ),
        html.Div(
            [
        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.Div("Controls", className="section-title"),
                                dbc.Label("Subnet", className="ctrl-label"),
                                dbc.Input(
                                    id="subnet-input",
                                    value=os.getenv("SUBNET", "192.168.1.0/24"),
                                    type="text",
                                    className="mb-2",
                                ),
                                dbc.Label("Select devices", className="ctrl-label"),
                                dcc.Dropdown(
                                    id="device-select",
                                    options=[],
                                    value=[],
                                    multi=True,
                                    placeholder="Scan first, then pick devices",
                                    className="mb-2 dash-dropdown",
                                ),
                                html.Div(
                                    [
                                        dbc.Button("All", id="select-all-btn", size="sm", color="secondary", outline=True, className="me-1"),
                                        dbc.Button("None", id="select-none-btn", size="sm", color="secondary", outline=True, className="me-1"),
                                        dbc.Button("Plugs", id="select-plugs-btn", size="sm", color="secondary", outline=True, className="me-1"),
                                        dbc.Button("Lights", id="select-lights-btn", size="sm", color="secondary", outline=True),
                                    ],
                                    className="mb-3",
                                ),
                                dbc.Label("Chart metric", className="ctrl-label"),
                                dcc.Dropdown(
                                    id="metric-select",
                                    options=[
                                        {"label": "Today (kWh)", "value": "today_kwh"},
                                        {"label": "Past 7 days (kWh)", "value": "past_7d_kwh"},
                                        {"label": "Past 30 days (kWh)", "value": "past_30d_kwh"},
                                        {"label": "Live load (W)", "value": "live_watts"},
                                    ],
                                    value="today_kwh",
                                    clearable=False,
                                    className="mb-3 dash-dropdown",
                                ),
                                dbc.Checklist(
                                    id="auto-refresh-toggle",
                                    options=[{"label": " Auto-refresh every 60s", "value": "on"}],
                                    value=[],
                                    className="mb-3",
                                    inputClassName="me-2",
                                ),
                                dbc.Button(
                                    [html.I(className="bi bi-arrow-clockwise me-2"), "Refresh scan"],
                                    id="refresh-btn",
                                    color="success",
                                    className="w-100 mb-2",
                                ),
                                dbc.Button(
                                    [html.I(className="bi bi-download me-2"), "Export CSV"],
                                    id="export-btn",
                                    color="secondary",
                                    outline=True,
                                    className="w-100",
                                ),
                                dcc.Download(id="csv-download"),
                                html.Div(id="scan-message", className="scan-msg mt-3"),
                            ]
                        ),
                        className="panel",
                    ),
                    lg=3,
                    md=12,
                    className="mb-3",
                ),
                dbc.Col(
                    [
                        dbc.Row(
                            [
                                dbc.Col(_kpi("Today", "kpi-today", "selected kWh"), md=3, xs=6, className="mb-3"),
                                dbc.Col(_kpi("7 days", "kpi-7d", "selected kWh"), md=3, xs=6, className="mb-3"),
                                dbc.Col(_kpi("30 days", "kpi-30d", "selected kWh"), md=3, xs=6, className="mb-3"),
                                dbc.Col(_kpi("Live load", "kpi-live", "selected plugs"), md=3, xs=6, className="mb-3"),
                            ]
                        ),
                        dbc.Row(
                            [
                                dbc.Col(
                                    dbc.Card(
                                        dbc.CardBody(
                                            [
                                                html.Div("Usage by selected device", className="section-title"),
                                                dcc.Graph(id="usage-chart", config={"displayModeBar": False}),
                                            ]
                                        ),
                                        className="panel",
                                    ),
                                    lg=8,
                                    className="mb-3",
                                ),
                                dbc.Col(
                                    dbc.Card(
                                        dbc.CardBody(
                                            [
                                                html.Div("Selection mix", className="section-title"),
                                                dcc.Graph(id="mix-chart", config={"displayModeBar": False}),
                                            ]
                                        ),
                                        className="panel",
                                    ),
                                    lg=4,
                                    className="mb-3",
                                ),
                            ]
                        ),
                    ],
                    lg=9,
                    md=12,
                ),
            ]
        ),
        dbc.Row(
            [
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.Div(
                                    [
                                        html.Span("Light bulbs", className="section-title"),
                                        html.Span(id="bulb-count", className="count-pill"),
                                    ],
                                    className="section-head",
                                ),
                                _device_table("bulb-table"),
                            ],
                            className="device-card-body",
                        ),
                        className="panel device-panel h-100",
                    ),
                    lg=6,
                    md=12,
                    className="mb-3 d-flex",
                ),
                dbc.Col(
                    dbc.Card(
                        dbc.CardBody(
                            [
                                html.Div(
                                    [
                                        html.Span("P110 smart plugs", className="section-title"),
                                        html.Span(id="plug-count", className="count-pill"),
                                    ],
                                    className="section-head",
                                ),
                                _device_table(
                                    "plug-table",
                                    extra_conditional=[
                                        {
                                            "if": {
                                                "filter_query": "{live_watts} > 0",
                                                "column_id": "live_watts",
                                            },
                                            "color": COLORS["accent"],
                                            "fontWeight": "700",
                                        },
                                    ],
                                ),
                            ],
                            className="device-card-body",
                        ),
                        className="panel device-panel h-100",
                    ),
                    lg=6,
                    md=12,
                    className="mb-3 d-flex",
                ),
            ],
            className="device-tables-row g-3 align-items-stretch",
        ),
            ],
            id="graph-view",
        ),
        html.Div(
            [
                dbc.Card(
                    dbc.CardBody(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Span("Device controls", className="section-title mb-0"),
                                            html.Span(
                                                "Click a tile to flip it and reveal controls",
                                                className="ctrl-label mb-0",
                                            ),
                                        ],
                                        className="d-flex flex-column gap-1",
                                    ),
                                    html.Div(
                                        [
                                            html.Span(
                                                id="police-active-pill",
                                                className="police-pill d-none",
                                                children="POLICE ON",
                                            ),
                                            dbc.Button(
                                                [
                                                    html.I(className="bi bi-lightning-charge-fill me-2"),
                                                    "Police Lights",
                                                ],
                                                id="police-open-btn",
                                                color="danger",
                                                outline=True,
                                                size="sm",
                                                className="police-btn",
                                            ),
                                        ],
                                        className="d-flex align-items-center gap-2",
                                    ),
                                ],
                                className="section-head",
                                style={"justifyContent": "space-between", "flexWrap": "wrap", "gap": "0.75rem"},
                            ),
                            html.Div(id="control-action-msg", className="scan-msg mb-3"),
                            dbc.Row(id="control-tiles", className="g-3"),
                        ]
                    ),
                    className="panel",
                ),
                dbc.Modal(
                    [
                        dbc.ModalHeader(
                            dbc.ModalTitle("Police Lights"),
                            close_button=True,
                        ),
                        dbc.ModalBody(
                            [
                                html.P(
                                    "Flash every colour light from the last scan red / blue at full brightness. "
                                    "Turning police mode off restores each light’s previous on/off state, colour, and brightness.",
                                    className="mb-3",
                                    style={"color": "var(--muted)", "marginBottom": "0.75rem"},
                                ),
                                html.Div(id="police-modal-status", className="scan-msg mb-3"),
                                html.Div(
                                    [
                                        html.Span("Police mode", className="ctrl-label mb-0"),
                                        dbc.Switch(
                                            id="police-toggle",
                                            value=False,
                                            className="police-switch",
                                        ),
                                    ],
                                    className="d-flex align-items-center justify-content-between",
                                ),
                            ]
                        ),
                        dbc.ModalFooter(
                            dbc.Button("Close", id="police-close-btn", color="secondary", outline=True)
                        ),
                    ],
                    id="police-modal",
                    is_open=False,
                    centered=True,
                    className="police-modal",
                ),
            ],
            id="control-view",
            style={"display": "none"},
        ),
    ],
    fluid=True,
    className="app-shell",
)


app.index_string = """
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg: #0f1419;
                --panel: #1a2332;
                --panel2: #243044;
                --text: #e8eef5;
                --muted: #8b9bb0;
                --accent: #2dd4a8;
                --accent2: #f0b429;
                --grid: #2a3548;
            }
            html, body, * {
                scrollbar-width: none;
                -ms-overflow-style: none;
            }
            *::-webkit-scrollbar {
                width: 0 !important;
                height: 0 !important;
                display: none !important;
            }
            body {
                background:
                    radial-gradient(1200px 600px at 10% -10%, #1c3a36 0%, transparent 55%),
                    radial-gradient(900px 500px at 100% 0%, #2a2a12 0%, transparent 50%),
                    var(--bg) !important;
                color: var(--text);
                font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
                min-height: 100vh;
            }
            .app-shell { padding: 1.25rem 1.5rem 2rem; max-width: 1500px; }
            .topbar {
                display: flex; justify-content: space-between; align-items: flex-end;
                margin-bottom: 1.25rem; gap: 1rem; flex-wrap: wrap;
            }
            .brand {
                font-family: Syne, sans-serif; font-weight: 800; font-size: 2rem;
                letter-spacing: 0.04em; color: var(--text); display: block; line-height: 1;
            }
            .brand-sub { color: var(--muted); font-size: 0.9rem; }
            .panel {
                background: color-mix(in srgb, var(--panel) 92%, transparent);
                border: 1px solid var(--grid) !important;
                border-radius: 14px !important;
                box-shadow: none !important;
                backdrop-filter: blur(8px);
            }
            .device-tables-row > [class*="col"] {
                display: flex;
            }
            .device-panel {
                width: 100%;
                display: flex;
                flex-direction: column;
            }
            .device-panel .card-body,
            .device-card-body {
                display: flex;
                flex-direction: column;
                flex: 1 1 auto;
                width: 100%;
            }
            .device-panel .dash-table-container,
            .device-panel .dash-spreadsheet-container,
            .device-panel .dash-spreadsheet-inner {
                width: 100% !important;
            }
            .device-panel .dash-spreadsheet-container table {
                width: 100% !important;
                table-layout: fixed;
            }
            .kpi-card {
                background: linear-gradient(160deg, var(--panel2), var(--panel));
                border: 1px solid var(--grid) !important;
                border-radius: 14px !important;
                height: 100%;
            }
            .kpi-label { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; }
            .kpi-value { font-family: Syne, sans-serif; font-size: 1.7rem; font-weight: 700; color: var(--accent); margin-top: 0.2rem; }
            .kpi-hint { color: var(--muted); font-size: 0.75rem; margin-top: 0.15rem; }
            .section-title { font-weight: 600; margin-bottom: 0.75rem; color: var(--text); }
            .section-head { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.75rem; }
            .section-head .section-title { margin-bottom: 0; }
            .count-pill {
                background: var(--panel2); color: var(--muted); border: 1px solid var(--grid);
                border-radius: 999px; padding: 0.1rem 0.55rem; font-size: 0.75rem;
            }
            .ctrl-label { color: #d7e0ea !important; font-size: 0.82rem; margin-bottom: 0.25rem; font-weight: 500; }
            .scan-msg { color: var(--muted); font-size: 0.85rem; min-height: 1.2rem; }
            .status-wrap {
                display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
            }
            .status-pill {
                display: inline-flex; align-items: center; gap: 0.4rem;
                padding: 0.35rem 0.75rem; border-radius: 999px;
                border: 1px solid var(--grid); background: var(--panel);
                font-size: 0.8rem; color: var(--muted);
            }
            .view-toggle-wrap {
                display: inline-flex; align-items: center; gap: 0.45rem;
                padding: 0.2rem 0.65rem;
                border-radius: 999px;
                border: 1px solid var(--grid);
                background: var(--panel);
            }
            .view-label { color: var(--muted); font-size: 0.8rem; font-weight: 500; }
            .view-switch {
                margin: 0 !important;
                padding-left: 2.4rem !important;
                min-height: 1.2rem;
            }
            .view-switch .form-check-input {
                width: 2.1rem; height: 1.15rem; cursor: pointer;
                background-color: var(--panel2);
                border-color: var(--grid);
            }
            .view-switch .form-check-input:checked {
                background-color: var(--accent);
                border-color: var(--accent);
            }
            .flip-tile {
                perspective: 1200px;
                height: 430px;
            }
            .rc-slider-rail { background-color: #3a465c !important; }
            .rc-slider-track { background-color: var(--accent) !important; }
            .rc-slider-handle {
                border-color: var(--accent) !important;
                background-color: #e8eef5 !important;
                opacity: 1 !important;
                box-shadow: 0 0 0 2px rgba(45, 212, 168, 0.25);
            }
            .rc-slider-handle:hover,
            .rc-slider-handle:active,
            .rc-slider-handle-dragging {
                border-color: var(--accent) !important;
                box-shadow: 0 0 0 4px rgba(45, 212, 168, 0.28) !important;
            }
            .rc-slider-dot { border-color: #9aabbf !important; background: #1a2332 !important; }
            .rc-slider-dot-active { border-color: var(--accent) !important; }
            .rc-slider-mark-text {
                color: #c8d4e2 !important;
                text-shadow: none !important;
                font-size: 11px !important;
            }
            .rc-slider-mark-text-active { color: #e8eef5 !important; }
            .colour-swatches { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0.35rem 0 0.5rem; }
            .colour-swatch {
                width: 22px; height: 22px; border-radius: 999px; border: 2px solid #e8eef5;
                cursor: pointer; padding: 0;
            }
            .colour-swatch:hover { transform: scale(1.08); }
            .tile-inner {
                position: relative;
                width: 100%;
                height: 100%;
                transition: transform 0.55s cubic-bezier(.2,.8,.2,1);
                transform-style: preserve-3d;
            }
            .tile-inner.is-flipped { transform: rotateY(180deg); }
            .tile-face {
                position: absolute;
                inset: 0;
                backface-visibility: hidden;
                -webkit-backface-visibility: hidden;
                border: 1px solid var(--grid);
                border-radius: 14px;
                background: linear-gradient(165deg, var(--panel2), var(--panel));
                padding: 1rem;
                overflow: auto;
            }
            .tile-front { cursor: pointer; display: flex; flex-direction: column; gap: 0.35rem; }
            .tile-front:hover { border-color: color-mix(in srgb, var(--accent) 55%, var(--grid)); }
            .tile-back { transform: rotateY(180deg); }
            .tile-kind {
                font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted);
            }
            .tile-name { font-family: Syne, sans-serif; font-weight: 700; font-size: 1.15rem; color: var(--text); }
            .tile-model { color: var(--muted); font-size: 0.82rem; }
            .tile-meta { color: #c8d4e2; font-size: 0.78rem; margin-top: auto; }
            .tile-model { color: #c8d4e2 !important; }
            .tile-kind { color: #b7c5d6 !important; }
            .tile-hint { color: #b7c5d6 !important; }
            .tile-power {
                display: inline-flex; align-items: center; gap: 0.35rem;
                width: fit-content; margin-top: 0.35rem;
                padding: 0.2rem 0.55rem; border-radius: 999px;
                font-size: 0.75rem; font-weight: 700; letter-spacing: 0.04em;
                border: 1px solid var(--grid);
            }
            .tile-power.on { color: #06241c; background: var(--accent); border-color: var(--accent); }
            .tile-power.off { color: var(--muted); background: transparent; }
            .tile-hint { color: var(--muted); font-size: 0.75rem; margin-top: 0.4rem; }
            .tile-back-title {
                display: flex; justify-content: space-between; align-items: center;
                gap: 0.5rem; margin-bottom: 0.65rem;
            }
            .tile-back-title strong { color: var(--text); font-size: 0.95rem; }
            .ctrl-row { margin-bottom: 0.55rem; }
            .ctrl-row .ctrl-label { margin-bottom: 0.15rem; }
            .status-dot {
                width: 8px; height: 8px; border-radius: 50%; background: var(--muted);
            }
            .status-dot.ok { background: var(--accent); box-shadow: 0 0 8px var(--accent); }
            .status-dot.busy { background: var(--accent2); box-shadow: 0 0 8px var(--accent2); }
            .status-dot.err { background: #ef6b6b; }
            .dash-dropdown .Select-control,
            .dash-dropdown .Select-menu-outer,
            .dash-dropdown .VirtualizedSelectOption {
                background: var(--panel2) !important; color: var(--text) !important; border-color: var(--grid) !important;
            }
            input.form-control {
                background: var(--panel2) !important; color: var(--text) !important; border-color: var(--grid) !important;
            }
            .form-check-label { color: var(--text); }
            .btn-success {
                background: var(--accent) !important; border-color: var(--accent) !important; color: #06241c !important; font-weight: 600;
            }
            .btn-outline-secondary {
                border-color: var(--grid) !important; color: var(--text) !important;
            }
            .btn-outline-secondary:hover { background: var(--panel2) !important; }
            .police-btn {
                border-color: #ef4444 !important;
                color: #fecaca !important;
                font-weight: 600;
            }
            .police-btn:hover {
                background: color-mix(in srgb, #ef4444 22%, transparent) !important;
                border-color: #f87171 !important;
                color: #fff !important;
            }
            .police-pill {
                display: inline-flex; align-items: center;
                padding: 0.15rem 0.55rem; border-radius: 999px;
                font-size: 0.7rem; font-weight: 800; letter-spacing: 0.08em;
                color: #fff; background: linear-gradient(90deg, #ef4444, #2563eb);
                animation: police-pulse 0.45s steps(2, end) infinite;
            }
            .police-pill.d-none { display: none !important; }
            @keyframes police-pulse {
                0% { filter: brightness(1.15); }
                50% { filter: brightness(0.75); }
            }
            .police-switch .form-check-input:checked {
                background-color: #ef4444;
                border-color: #ef4444;
            }
            .modal-content {
                background: var(--panel) !important;
                color: var(--text) !important;
                border: 1px solid var(--grid) !important;
            }
            .modal-header, .modal-footer {
                border-color: var(--grid) !important;
            }
            .modal-title { font-family: Syne, sans-serif; font-weight: 700; }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
"""


METRIC_LABELS = {
    "today_kwh": "Today (kWh)",
    "past_7d_kwh": "Past 7 days (kWh)",
    "past_30d_kwh": "Past 30 days (kWh)",
    "live_watts": "Live load (W)",
}


def _empty_figure(message: str, height: int = 320) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=10, b=10),
        height=height,
        annotations=[
            dict(
                text=message,
                xref="paper",
                yref="paper",
                x=0.5,
                y=0.5,
                showarrow=False,
                font=dict(color=COLORS["muted"], size=14),
            )
        ],
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


def _selected_devices(result: ScanResult, selected_ips: list[str] | None) -> list[DeviceReading]:
    if not selected_ips:
        return []
    wanted = set(selected_ips)
    return [d for d in result.devices if d.ip in wanted]


def _totals_for(devices: list[DeviceReading]) -> dict[str, float]:
    return {
        "today_kwh": sum(d.today_kwh for d in devices),
        "past_7d_kwh": sum(d.past_7d_kwh for d in devices),
        "past_30d_kwh": sum(d.past_30d_kwh for d in devices),
        "live_watts": sum(d.live_watts for d in devices),
        "bulb_count": float(sum(1 for d in devices if d.kind == "bulb")),
        "plug_count": float(sum(1 for d in devices if d.kind == "plug")),
    }


def _usage_figure(devices: list[DeviceReading], metric: str) -> go.Figure:
    if not devices:
        return _empty_figure("Select one or more devices")

    names = [d.name for d in devices]
    values = [float(getattr(d, metric, 0) or 0) for d in devices]
    colors = [COLORS["bulb"] if d.kind == "bulb" else COLORS["plug"] for d in devices]
    unit = "W" if metric == "live_watts" else "kWh"

    fig = go.Figure(
        data=[
            go.Bar(
                x=names,
                y=values,
                marker_color=colors,
                text=[f"{v:.2f} {unit}" for v in values],
                textposition="outside",
                hovertemplate="%{x}<br>%{y:.3f} " + unit + "<extra></extra>",
            )
        ]
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=20, b=40),
        height=320,
        font=dict(color=COLORS["text"], family="IBM Plex Sans"),
        xaxis=dict(showgrid=False, tickangle=-25),
        yaxis=dict(
            title=METRIC_LABELS.get(metric, metric),
            gridcolor=COLORS["grid"],
            zerolinecolor=COLORS["grid"],
        ),
        bargap=0.35,
    )
    return fig


def _mix_figure(devices: list[DeviceReading]) -> go.Figure:
    totals = _totals_for(devices)
    values = [totals["bulb_count"], totals["plug_count"]]
    if sum(values) == 0:
        return _empty_figure("No selection")

    fig = go.Figure(
        data=[
            go.Pie(
                labels=["Lights", "Plugs"],
                values=values,
                hole=0.62,
                marker=dict(colors=[COLORS["bulb"], COLORS["plug"]]),
                textinfo="label+value",
                hovertemplate="%{label}: %{value}<extra></extra>",
            )
        ]
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=10, b=10),
        height=320,
        showlegend=False,
        font=dict(color=COLORS["text"], family="IBM Plex Sans"),
        annotations=[
            dict(
                text=f"{int(sum(values))}<br>selected",
                x=0.5,
                y=0.5,
                font=dict(size=16, color=COLORS["text"], family="Syne"),
                showarrow=False,
            )
        ],
    )
    return fig


def _device_options(result: ScanResult) -> list[dict]:
    options = []
    for d in result.devices:
        kind = "💡" if d.kind == "bulb" else "🔌"
        options.append({"label": f"{kind} {d.name} ({d.model})", "value": d.ip})
    return options


def _rows_for_ips(devices: list[DeviceReading], selected_ips: list[str] | None) -> list[int]:
    wanted = set(selected_ips or [])
    return [i for i, d in enumerate(devices) if d.ip in wanted]


def _device_tile(device: DeviceReading, flipped: bool) -> dbc.Col:
    ip = device.ip
    kind_label = "Light" if device.kind == "bulb" else "Plug"
    power_class = "tile-power on" if device.device_on else "tile-power off"
    power_text = "ON" if device.device_on else "OFF"

    meta_bits = [device.ip]
    if device.kind == "plug":
        meta_bits.append(f"{device.live_watts:.1f} W")
    elif device.brightness is not None:
        meta_bits.append(f"{device.brightness}%")

    front = html.Div(
        [
            html.Div(kind_label, className="tile-kind"),
            html.Div(device.name, className="tile-name"),
            html.Div(device.model, className="tile-model"),
            html.Div(power_text, className=power_class),
            html.Div(" · ".join(meta_bits), className="tile-meta"),
            html.Div("Click to open controls", className="tile-hint"),
        ],
        id={"type": "tile-flip", "ip": ip},
        n_clicks=0,
        className="tile-face tile-front",
        role="button",
    )

    back_children: list = [
        html.Div(
            [
                html.Strong(device.name),
                dbc.Button(
                    "Done",
                    id={"type": "tile-close", "ip": ip},
                    size="sm",
                    color="secondary",
                    outline=True,
                ),
            ],
            className="tile-back-title",
        ),
        html.Div(
            [
                dbc.Button(
                    "Turn on",
                    id={"type": "ctrl-on", "ip": ip},
                    size="sm",
                    color="success",
                    className="me-1 mb-1",
                ),
                dbc.Button(
                    "Turn off",
                    id={"type": "ctrl-off", "ip": ip},
                    size="sm",
                    color="secondary",
                    outline=True,
                    className="me-1 mb-1",
                ),
                dbc.Button(
                    "Toggle",
                    id={"type": "ctrl-toggle", "ip": ip},
                    size="sm",
                    color="warning",
                    outline=True,
                    className="mb-1",
                ),
            ],
            className="ctrl-row",
        ),
    ]

    if device.kind == "bulb":
        brightness = int(device.brightness or 50)
        back_children.extend(
            [
                html.Div(
                    [
                        html.Div("Brightness", className="ctrl-label"),
                        dcc.Slider(
                            id={"type": "ctrl-brightness", "ip": ip},
                            min=1,
                            max=100,
                            step=1,
                            value=brightness,
                            marks={1: "1", 50: "50", 100: "100"},
                            tooltip={"placement": "bottom"},
                        ),
                    ],
                    className="ctrl-row",
                ),
            ]
        )
        if device.is_color:
            color_temp = int(device.color_temp or 4000)
            if color_temp < 2500:
                color_temp = 4000
            hue = int(device.hue if device.hue is not None else 0)
            sat = int(device.saturation if device.saturation is not None else 100)
            if sat < 1:
                sat = 100
            swatches = [
                ("Red", 0, "#ef4444"),
                ("Orange", 30, "#f97316"),
                ("Yellow", 60, "#eab308"),
                ("Green", 120, "#22c55e"),
                ("Cyan", 180, "#06b6d4"),
                ("Blue", 240, "#3b82f6"),
                ("Purple", 280, "#a855f7"),
                ("Pink", 330, "#ec4899"),
            ]
            back_children.extend(
                [
                    html.Div(
                        [
                            html.Div("Colour", className="ctrl-label"),
                            html.Div(
                                [
                                    html.Button(
                                        "",
                                        id={"type": "ctrl-swatch", "ip": ip, "hue": hue_v},
                                        className="colour-swatch",
                                        style={"backgroundColor": hex_c},
                                        title=name,
                                        n_clicks=0,
                                    )
                                    for name, hue_v, hex_c in swatches
                                ],
                                className="colour-swatches",
                            ),
                            html.Div("Hue", className="ctrl-label"),
                            dcc.Slider(
                                id={"type": "ctrl-hue", "ip": ip},
                                min=0,
                                max=360,
                                step=1,
                                value=hue,
                                marks={0: "R", 120: "G", 240: "B", 360: "R"},
                                tooltip={"placement": "bottom"},
                            ),
                            html.Div("Saturation", className="ctrl-label mt-1"),
                            dcc.Slider(
                                id={"type": "ctrl-sat", "ip": ip},
                                min=1,
                                max=100,
                                step=1,
                                value=sat,
                                marks={1: "1", 50: "50", 100: "100"},
                                tooltip={"placement": "bottom"},
                            ),
                        ],
                        className="ctrl-row",
                    ),
                    html.Div(
                        [
                            html.Div("Colour temperature (K)", className="ctrl-label"),
                            dcc.Slider(
                                id={"type": "ctrl-colortemp", "ip": ip},
                                min=2500,
                                max=6500,
                                step=100,
                                value=color_temp,
                                marks={2500: "2500", 4000: "4000", 6500: "6500"},
                                tooltip={"placement": "bottom"},
                            ),
                        ],
                        className="ctrl-row",
                    ),
                ]
            )
        else:
            # Keep pattern-matching IDs present for non-color bulbs.
            back_children.extend(
                [
                    dcc.Slider(id={"type": "ctrl-hue", "ip": ip}, min=0, max=360, value=0, className="d-none"),
                    dcc.Slider(id={"type": "ctrl-sat", "ip": ip}, min=1, max=100, value=100, className="d-none"),
                    html.Button(id={"type": "ctrl-swatch", "ip": ip, "hue": 0}, n_clicks=0, style={"display": "none"}),
                    dcc.Slider(id={"type": "ctrl-colortemp", "ip": ip}, min=2500, max=6500, value=4000, className="d-none"),
                ]
            )
    else:
        back_children.extend(
            [
                html.Div(
                    f"Live load: {device.live_watts:.1f} W · Today {device.today_kwh:.3f} kWh",
                    className="tile-meta",
                ),
                dcc.Slider(id={"type": "ctrl-brightness", "ip": ip}, min=1, max=100, value=50, className="d-none"),
                dcc.Slider(id={"type": "ctrl-hue", "ip": ip}, min=0, max=360, value=0, className="d-none"),
                dcc.Slider(id={"type": "ctrl-sat", "ip": ip}, min=1, max=100, value=100, className="d-none"),
                html.Button(id={"type": "ctrl-swatch", "ip": ip, "hue": 0}, n_clicks=0, style={"display": "none"}),
                dcc.Slider(id={"type": "ctrl-colortemp", "ip": ip}, min=2500, max=6500, value=4000, className="d-none"),
            ]
        )

    back = html.Div(back_children, className="tile-face tile-back")
    inner_class = "tile-inner is-flipped" if flipped else "tile-inner"

    return dbc.Col(
        html.Div(html.Div([front, back], className=inner_class), className="flip-tile"),
        xl=3,
        lg=4,
        md=6,
        sm=6,
        xs=12,
    )


@app.callback(
    Output("view-mode", "data"),
    Input("view-toggle", "value"),
)
def set_view_mode(is_control: bool):
    return "control" if is_control else "graph"


@app.callback(
    Output("graph-view", "style"),
    Output("control-view", "style"),
    Input("view-mode", "data"),
)
def toggle_views(mode: str):
    if mode == "control":
        return {"display": "none"}, {"display": "block"}
    return {"display": "block"}, {"display": "none"}


@app.callback(
    Output("flipped-tiles", "data"),
    Input({"type": "tile-flip", "ip": ALL}, "n_clicks"),
    Input({"type": "tile-close", "ip": ALL}, "n_clicks"),
    State("flipped-tiles", "data"),
    prevent_initial_call=True,
)
def flip_tiles(front_clicks, close_clicks, flipped):
    from dash import ctx

    if not ctx.triggered_id or not isinstance(ctx.triggered_id, dict):
        return no_update
    if not ctx.triggered or not ctx.triggered[0]["value"]:
        return no_update

    flipped = list(flipped or [])
    ip = ctx.triggered_id.get("ip")
    kind = ctx.triggered_id.get("type")
    if kind == "tile-flip":
        if ip in flipped:
            flipped = [x for x in flipped if x != ip]
        else:
            flipped.append(ip)
        return flipped
    if kind == "tile-close":
        return [x for x in flipped if x != ip]
    return no_update


@app.callback(
    Output("control-tiles", "children"),
    Input("flipped-tiles", "data"),
    Input("control-version", "data"),
    Input("scan-stamp", "data"),
    Input("view-mode", "data"),
)
def render_control_tiles(flipped, _version, _stamp, mode):
    result = _get_result()
    if not result.devices:
        return [
            dbc.Col(
                html.Div("Scan devices first, then switch to Control.", className="scan-msg"),
                width=12,
            )
        ]
    flipped_set = set(flipped or [])
    return [_device_tile(d, d.ip in flipped_set) for d in result.devices]


def _value_for_ip(ids, values, ip):
    for idx, item in enumerate(ids or []):
        if item and item.get("ip") == ip:
            return values[idx]
    return None


@app.callback(
    Output("police-modal", "is_open"),
    Input("police-open-btn", "n_clicks"),
    Input("police-close-btn", "n_clicks"),
    State("police-modal", "is_open"),
    prevent_initial_call=True,
)
def toggle_police_modal(open_clicks, close_clicks, is_open):
    from dash import ctx

    if not ctx.triggered_id:
        return no_update
    if ctx.triggered_id == "police-open-btn":
        return True
    if ctx.triggered_id == "police-close-btn":
        return False
    return not is_open


def _apply_police_snapshots_to_cache(snapshots) -> None:
    """Push restored light state back into the scan cache so tiles match reality."""
    if not snapshots:
        return
    result = _get_result()
    for snap in snapshots:
        device = next((d for d in result.bulbs if d.ip == snap.ip), None)
        if not device:
            continue
        device.device_on = snap.device_on
        device.brightness = snap.brightness
        device.color_temp = snap.color_temp
        device.hue = snap.hue
        device.saturation = snap.saturation
        with _cache_lock:
            _cache["result"] = update_cached_device(_cache["result"], device)


@app.callback(
    Output("police-toggle", "value"),
    Output("police-modal-status", "children"),
    Output("police-active-pill", "className"),
    Output("control-action-msg", "children", allow_duplicate=True),
    Output("control-version", "data", allow_duplicate=True),
    Input("police-toggle", "value"),
    Input("police-open-btn", "n_clicks"),
    Input("poll-interval", "n_intervals"),
    State("police-modal", "is_open"),
    State("control-version", "data"),
    prevent_initial_call=True,
)
def sync_police_mode(toggle_on, _open_clicks, _n, modal_open, version):
    from dash import ctx

    trigger = ctx.triggered_id
    status = police_mode.status()
    active = bool(status.get("active") or status.get("restoring"))

    if trigger == "police-toggle":
        if toggle_on:
            if status.get("active"):
                return True, status.get("message") or "Police mode on.", "police-pill", no_update, no_update
            outcome = police_mode.start(_get_result().devices)
            status = police_mode.status()
            if not outcome.get("ok"):
                msg = outcome.get("error") or status.get("message") or "Could not start police mode."
                return False, msg, "police-pill d-none", msg, version or 0
            msg = status.get("message") or "Police mode on."
            return True, msg, "police-pill", msg, version or 0

        if not status.get("active") and not status.get("restoring"):
            return False, status.get("message") or "Police mode off.", "police-pill d-none", no_update, no_update

        outcome = police_mode.stop()
        _apply_police_snapshots_to_cache(outcome.get("snapshots") or [])
        status = police_mode.status()
        msg = status.get("message") or "Police mode off."
        if status.get("error"):
            msg = status["error"]
        return False, msg, "police-pill d-none", msg, (version or 0) + 1

    pill = "police-pill" if active else "police-pill d-none"
    msg = status.get("error") or status.get("message") or (
        "Police mode on." if active else "Police mode off."
    )

    if trigger == "police-open-btn":
        colour_count = sum(1 for d in _get_result().devices if d.kind == "bulb" and d.is_color)
        if active:
            detail = msg
        elif colour_count:
            detail = f"{colour_count} colour light(s) ready. Toggle police mode to start."
        else:
            detail = "No colour lights yet — run Refresh scan first."
        return active, detail, pill, no_update, no_update

    if trigger == "poll-interval":
        if not (modal_open or active):
            return no_update, no_update, no_update, no_update, no_update
        return active, msg if modal_open else no_update, pill, no_update, no_update

    return active, msg, pill, no_update, no_update


@app.callback(
    Output("control-version", "data"),
    Output("control-action-msg", "children"),
    Input({"type": "ctrl-on", "ip": ALL}, "n_clicks"),
    Input({"type": "ctrl-off", "ip": ALL}, "n_clicks"),
    Input({"type": "ctrl-toggle", "ip": ALL}, "n_clicks"),
    Input({"type": "ctrl-brightness", "ip": ALL}, "value"),
    Input({"type": "ctrl-colortemp", "ip": ALL}, "value"),
    Input({"type": "ctrl-hue", "ip": ALL}, "value"),
    Input({"type": "ctrl-sat", "ip": ALL}, "value"),
    Input({"type": "ctrl-swatch", "ip": ALL, "hue": ALL}, "n_clicks"),
    State({"type": "ctrl-hue", "ip": ALL}, "value"),
    State({"type": "ctrl-hue", "ip": ALL}, "id"),
    State({"type": "ctrl-sat", "ip": ALL}, "value"),
    State({"type": "ctrl-sat", "ip": ALL}, "id"),
    State("control-version", "data"),
    prevent_initial_call=True,
)
def run_controls(
    on_clicks,
    off_clicks,
    toggle_clicks,
    bright_values,
    temp_values,
    hue_input_values,
    sat_input_values,
    swatch_clicks,
    hue_values,
    hue_ids,
    sat_values,
    sat_ids,
    version,
):
    from dash import ctx

    if not ctx.triggered_id or not isinstance(ctx.triggered_id, dict):
        return no_update, no_update
    if not ctx.triggered:
        return no_update, no_update

    triggered_value = ctx.triggered[0]["value"]
    ip = ctx.triggered_id["ip"]
    action_type = ctx.triggered_id["type"]

    # Button clicks: ignore mount / zero clicks. Sliders: ignore None.
    if action_type in {"ctrl-on", "ctrl-off", "ctrl-toggle", "ctrl-swatch"}:
        if not triggered_value:
            return no_update, no_update
    elif triggered_value is None:
        return no_update, no_update

    result = _get_result()
    device = next((d for d in result.devices if d.ip == ip), None)
    if not device:
        return version or 0, f"Device {ip} not found. Rescan."

    try:
        if action_type == "ctrl-on":
            updated = run_device_command(device, "on")
            msg = f"Turned on {updated.name}."
        elif action_type == "ctrl-off":
            updated = run_device_command(device, "off")
            msg = f"Turned off {updated.name}."
        elif action_type == "ctrl-toggle":
            updated = run_device_command(device, "toggle")
            state = "on" if updated.device_on else "off"
            msg = f"Toggled {updated.name} → {state}."
        elif action_type == "ctrl-brightness":
            if device.kind != "bulb":
                return no_update, no_update
            value = int(triggered_value)
            if device.brightness is not None and int(device.brightness) == value:
                return no_update, no_update
            updated = run_device_command(device, "brightness", value)
            msg = f"Set {updated.name} brightness to {value}%."
        elif action_type == "ctrl-colortemp":
            if not device.is_color:
                return no_update, no_update
            value = int(triggered_value)
            if device.color_temp is not None and int(device.color_temp) == value:
                return no_update, no_update
            updated = run_device_command(device, "color_temp", value)
            msg = f"Set {updated.name} colour temp to {value} K."
        elif action_type in {"ctrl-hue", "ctrl-sat"}:
            if not device.is_color:
                return no_update, no_update
            hue = _value_for_ip(hue_ids, hue_values, ip)
            sat = _value_for_ip(sat_ids, sat_values, ip)
            if hue is None or sat is None:
                return version or 0, "Colour sliders not found."
            hue_i, sat_i = int(hue), int(sat)
            if (
                device.hue is not None
                and device.saturation is not None
                and int(device.hue) == hue_i
                and int(device.saturation) == sat_i
            ):
                return no_update, no_update
            updated = run_device_command(device, "hue_sat", (hue_i, sat_i))
            msg = f"Set {updated.name} colour to hue {hue_i} / sat {sat_i}."
        elif action_type == "ctrl-swatch":
            if not device.is_color:
                return no_update, no_update
            hue = int(ctx.triggered_id.get("hue", 0))
            sat = _value_for_ip(sat_ids, sat_values, ip)
            sat = int(sat) if sat is not None else 100
            updated = run_device_command(device, "hue_sat", (hue, max(1, sat)))
            msg = f"Set {updated.name} to swatch colour (hue {hue})."
        else:
            return no_update, no_update

        with _cache_lock:
            _cache["result"] = update_cached_device(_cache["result"], updated)
        return (version or 0) + 1, msg
    except Exception as exc:
        return version or 0, f"Control failed on {device.name}: {exc}"


@app.callback(
    Output("auto-refresh", "disabled"),
    Input("auto-refresh-toggle", "value"),
)
def toggle_auto_refresh(values: list[str]):
    return "on" not in (values or [])


@app.callback(
    Output("device-select", "value", allow_duplicate=True),
    Input("select-all-btn", "n_clicks"),
    Input("select-none-btn", "n_clicks"),
    Input("select-plugs-btn", "n_clicks"),
    Input("select-lights-btn", "n_clicks"),
    State("device-select", "options"),
    prevent_initial_call=True,
)
def quick_select(all_clicks, none_clicks, plugs_clicks, lights_clicks, options):
    from dash import ctx

    if not options:
        return []
    trigger = ctx.triggered_id
    if trigger == "select-all-btn":
        return [o["value"] for o in options]
    if trigger == "select-none-btn":
        return []
    if trigger == "select-plugs-btn":
        return [o["value"] for o in options if str(o["label"]).startswith("🔌")]
    if trigger == "select-lights-btn":
        return [o["value"] for o in options if str(o["label"]).startswith("💡")]
    return no_update


@app.callback(
    Output("scan-message", "children"),
    Output("refresh-btn", "disabled"),
    Input("refresh-btn", "n_clicks"),
    Input("auto-refresh", "n_intervals"),
    State("subnet-input", "value"),
    prevent_initial_call=True,
)
def trigger_scan(n_clicks, n_auto, subnet):
    started = _start_scan(subnet=subnet.strip() if subnet else None)
    if started:
        return "Scanning local network…", True
    return "Scan already running…", True


@app.callback(
    Output("device-select", "options"),
    Output("device-select", "value"),
    Output("kpi-today", "children"),
    Output("kpi-7d", "children"),
    Output("kpi-30d", "children"),
    Output("kpi-live", "children"),
    Output("bulb-table", "data"),
    Output("plug-table", "data"),
    Output("bulb-table", "selected_rows"),
    Output("plug-table", "selected_rows"),
    Output("bulb-count", "children"),
    Output("plug-count", "children"),
    Output("usage-chart", "figure"),
    Output("mix-chart", "figure"),
    Output("status-badge", "children"),
    Output("scan-message", "children", allow_duplicate=True),
    Output("refresh-btn", "disabled", allow_duplicate=True),
    Output("scan-stamp", "data"),
    Input("poll-interval", "n_intervals"),
    Input("device-select", "value"),
    Input("metric-select", "value"),
    prevent_initial_call="initial_duplicate",
)
def refresh_ui(_n, selected_ips, metric):
    from dash import ctx

    result = _get_result()
    scanning = _is_scanning()
    options = _device_options(result)

    # Keep selection in sync with available devices; auto-select all on first scan.
    available = {o["value"] for o in options}
    current = [ip for ip in (selected_ips or []) if ip in available]
    if options and not current and not ctx.triggered_id == "device-select":
        if result.devices and (selected_ips is None or selected_ips == []):
            current = [o["value"] for o in options]

    # If user cleared selection intentionally via device-select, respect empty list.
    if ctx.triggered_id == "device-select":
        current = [ip for ip in (selected_ips or []) if ip in available]

    selected = _selected_devices(result, current)
    totals = _totals_for(selected)

    bulb_data = [d.to_dict() for d in result.bulbs]
    plug_data = [d.to_dict() for d in result.plugs]
    bulb_rows = _rows_for_ips(result.bulbs, current)
    plug_rows = _rows_for_ips(result.plugs, current)

    if scanning:
        status = html.Div(
            [html.Span(className="status-dot busy"), "Scanning network…"],
            className="status-pill",
        )
        msg = "Scanning local network…"
        btn_disabled = True
    elif result.error and not result.devices:
        status = html.Div(
            [html.Span(className="status-dot err"), "Needs attention"],
            className="status-pill",
        )
        msg = result.error
        btn_disabled = False
    elif result.devices:
        when = datetime.fromtimestamp(result.scanned_at).strftime("%H:%M:%S") if result.scanned_at else "—"
        status = html.Div(
            [
                html.Span(className="status-dot ok"),
                f"{len(result.devices)} devices · {len(current)} selected · scan {when}",
            ],
            className="status-pill",
        )
        msg = result.error or f"Last scan finished at {when}."
        btn_disabled = False
    else:
        status = html.Div(
            [html.Span(className="status-dot"), "Idle — press Refresh"],
            className="status-pill",
        )
        msg = result.error or "Press Refresh scan to discover Tapo lights and plugs."
        btn_disabled = False

    return (
        options,
        current,
        f"{totals['today_kwh']:.3f}",
        f"{totals['past_7d_kwh']:.3f}",
        f"{totals['past_30d_kwh']:.3f}",
        f"{totals['live_watts']:.1f} W",
        bulb_data,
        plug_data,
        bulb_rows,
        plug_rows,
        str(len(result.bulbs)),
        str(len(result.plugs)),
        _usage_figure(selected, metric or "today_kwh"),
        _mix_figure(selected),
        status,
        msg,
        btn_disabled,
        result.scanned_at or 0,
    )


@app.callback(
    Output("device-select", "value", allow_duplicate=True),
    Input("bulb-table", "selected_rows"),
    Input("plug-table", "selected_rows"),
    State("bulb-table", "data"),
    State("plug-table", "data"),
    State("device-select", "value"),
    prevent_initial_call=True,
)
def tables_to_device_select(bulb_rows, plug_rows, bulb_data, plug_data, current):
    from dash import ctx

    # Avoid feedback loops from refresh_ui writing selected_rows.
    if ctx.triggered_id not in {"bulb-table", "plug-table"}:
        return no_update

    selected = set(current or [])
    bulb_data = bulb_data or []
    plug_data = plug_data or []

    bulb_ips = {row["ip"] for row in bulb_data}
    plug_ips = {row["ip"] for row in plug_data}

    if ctx.triggered_id == "bulb-table":
        selected -= bulb_ips
        for idx in bulb_rows or []:
            if 0 <= idx < len(bulb_data):
                selected.add(bulb_data[idx]["ip"])
    elif ctx.triggered_id == "plug-table":
        selected -= plug_ips
        for idx in plug_rows or []:
            if 0 <= idx < len(plug_data):
                selected.add(plug_data[idx]["ip"])

    return sorted(selected)


@app.callback(
    Output("csv-download", "data"),
    Input("export-btn", "n_clicks"),
    State("device-select", "value"),
    prevent_initial_call=True,
)
def export_csv(n_clicks, selected_ips):
    result = _get_result()
    devices = _selected_devices(result, selected_ips) or result.devices
    if not devices:
        return no_update

    rows = ["name,kind,model,ip,today_kwh,past_7d_kwh,past_30d_kwh,live_watts"]
    for d in devices:
        rows.append(
            f"{d.name},{d.kind},{d.model},{d.ip},"
            f"{d.today_kwh:.6f},{d.past_7d_kwh:.6f},{d.past_30d_kwh:.6f},{d.live_watts:.6f}"
        )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return dict(content="\n".join(rows) + "\n", filename=f"tapo_energy_{stamp}.csv")


@server.get("/tv")
def tv_page():
    """Lightweight ES5 UI for LG webOS / Smart TV browsers."""
    return render_template("tv.html")


@server.get("/api/devices")
def api_devices():
    payload = _get_result().to_dict()
    payload["scanning"] = _is_scanning()
    return jsonify(payload)


@server.get("/api/health")
def api_health():
    return jsonify({"ok": True, "scanning": _is_scanning()})


@server.post("/api/scan")
def api_scan():
    data = request.get_json(silent=True) or {}
    subnet = data.get("subnet") or os.getenv("SUBNET", "192.168.1.0/24")
    started = _start_scan(subnet)
    return jsonify({"ok": True, "started": started, "scanning": True, "subnet": subnet})


@server.post("/api/control")
def api_control():
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    action = (data.get("action") or "").strip()
    value = data.get("value")
    if not ip or not action:
        return jsonify({"ok": False, "error": "ip and action are required"}), 400

    result = _get_result()
    device = next((d for d in result.devices if d.ip == ip), None)
    if not device:
        return jsonify({"ok": False, "error": f"Device {ip} not found. Scan first."}), 404

    if action == "hue_sat" and isinstance(value, list) and len(value) >= 2:
        value = (int(value[0]), int(value[1]))

    try:
        updated = run_device_command(device, action, value)
        with _cache_lock:
            _cache["result"] = update_cached_device(_cache["result"], updated)
        if action == "on":
            msg = f"Turned on {updated.name}."
        elif action == "off":
            msg = f"Turned off {updated.name}."
        elif action == "toggle":
            msg = f"Toggled {updated.name} → {'on' if updated.device_on else 'off'}."
        elif action == "brightness":
            msg = f"Set {updated.name} brightness to {int(value)}%."
        elif action == "color_temp":
            msg = f"Set {updated.name} colour temp to {int(value)} K."
        elif action == "hue_sat":
            msg = f"Set {updated.name} colour."
        else:
            msg = f"Updated {updated.name}."
        return jsonify({"ok": True, "message": msg, "device": updated.to_dict()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "8050"))
    print(f"Tapo Energy dashboard -> http://{host}:{port}")
    print(f"TV / webOS view     -> http://{host}:{port}/tv")
    app.run(host=host, port=port, debug=False)
