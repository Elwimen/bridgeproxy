#!/usr/bin/env python3
"""
Reverse proxy with TUI — request log and per-device bandwidth.
"""

import asyncio
import argparse
import json
import socket
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from aiohttp import web, ClientSession, WSMsgType, TCPConnector
from multidict import CIMultiDict

from textual.app import App, ComposeResult
from textual.widgets import DataTable, RichLog, Header, Footer, Input, Button, Label, Switch
from textual.containers import Vertical, Horizontal
from textual.reactive import reactive


# ── Shared state ──────────────────────────────────────────────────────────────

class DeviceStats:
    def __init__(self):
        self.total_down = 0
        self.total_up = 0
        self.request_count = 0
        self.last_seen = time.monotonic()
        self._samples: deque = deque()  # (monotonic, bytes) for speed

    def record_down(self, n: int):
        now = time.monotonic()
        self.total_down += n
        self.last_seen = now
        self._samples.append((now, n))
        self._prune(now)

    def record_up(self, n: int):
        self.total_up += n
        self.request_count += 1
        self.last_seen = time.monotonic()

    def _prune(self, now: float, window: float = 3.0):
        cutoff = now - window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def speed_down(self) -> float:
        now = time.monotonic()
        self._prune(now)
        if not self._samples:
            return 0.0
        elapsed = now - self._samples[0][0]
        return sum(b for _, b in self._samples) / max(elapsed, 0.5)


class State:
    def __init__(self):
        self.devices: dict[str, DeviceStats] = defaultdict(DeviceStats)
        self.log_queue: asyncio.Queue = asyncio.Queue()

    def device(self, ip: str) -> DeviceStats:
        return self.devices[ip]

    def log(self, method: str, path: str, ip: str, status: int):
        self.log_queue.put_nowait({
            "time": datetime.now().strftime("%H:%M:%S"),
            "method": method,
            "path": path,
            "ip": ip,
            "status": status,
        })


# ── Proxy logic ───────────────────────────────────────────────────────────────

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})


def build_request_headers(req: web.Request) -> CIMultiDict:
    headers = CIMultiDict()
    for k, v in req.headers.items():
        if k.lower() in HOP_BY_HOP or k.lower() == "host":
            continue
        headers.add(k, v)
    headers["X-Forwarded-For"] = req.remote
    headers["X-Real-IP"] = req.remote
    headers["X-Forwarded-Proto"] = "http"
    return headers


def build_response_headers(upstream_headers, upstream_url: str, proxy_base: str) -> CIMultiDict:
    headers = CIMultiDict()
    for k, v in upstream_headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        if k.lower() == "location":
            v = v.replace(upstream_url, proxy_base, 1)
        headers.add(k, v)
    return headers


async def handle_websocket(req: web.Request, upstream: str, state: State) -> web.WebSocketResponse:
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(req)

    url = (upstream + req.path_qs).replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    ip = req.remote

    async with req.app["session"].ws_connect(url, headers=build_request_headers(req)) as ws_up:
        async def fwd(src, dst, track: bool):
            async for msg in src:
                if msg.type == WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                    if track:
                        state.device(ip).record_down(len(msg.data.encode()))
                elif msg.type == WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                    if track:
                        state.device(ip).record_down(len(msg.data))
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break

        await asyncio.gather(fwd(ws_client, ws_up, False), fwd(ws_up, ws_client, True))

    state.log("WS", req.path, ip, 101)
    return ws_client


async def handle_http(req: web.Request, upstream: str, proxy_base: str, state: State) -> web.StreamResponse:
    ip = req.remote
    body = await req.read()
    if body:
        state.device(ip).record_up(len(body))

    async with req.app["session"].request(
        method=req.method,
        url=upstream + req.path_qs,
        headers=build_request_headers(req),
        data=body or None,
        allow_redirects=False,
    ) as up:
        resp = web.StreamResponse(
            status=up.status,
            headers=build_response_headers(up.headers, upstream, proxy_base),
        )
        await resp.prepare(req)
        async for chunk in up.content.iter_chunked(65536):
            await resp.write(chunk)
            state.device(ip).record_down(len(chunk))
        await resp.write_eof()
        state.log(req.method, req.path, ip, up.status)

    return resp


async def handler(req: web.Request):
    upstream = req.app["upstream"]
    proxy_base = req.app["proxy_base"]
    state = req.app["state"]

    if req.headers.get("upgrade", "").lower() == "websocket":
        return await handle_websocket(req, upstream, state)
    return await handle_http(req, upstream, proxy_base, state)


# ── TUI ───────────────────────────────────────────────────────────────────────

STATUS_COLOR = {2: "green", 3: "yellow", 4: "red", 5: "bold red"}


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class ProxyTUI(App):
    CSS = """
    DataTable {
        height: 12;
        border: solid $accent;
        margin-bottom: 1;
    }
    RichLog {
        border: solid $accent;
    }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, state: State, runner_factory, title: str):
        super().__init__()
        self._title = title
        self.state = state
        self._runner_factory = runner_factory
        self._server_task = None
        self._log_task = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield DataTable(id="devices")
        yield RichLog(id="log", highlight=True, markup=True, wrap=False, max_lines=1000)
        yield Footer()

    def on_mount(self):
        self.title = self._title
        table = self.query_one("#devices", DataTable)
        table.cursor_type = "none"
        table.add_columns("Device IP", "↓ Total", "↓ Speed", "↑ Total", "Requests")

        self._server_task = asyncio.create_task(self._run_server())
        self._log_task = asyncio.create_task(self._drain_logs())
        self.set_interval(1.0, self._refresh_devices)

    async def _run_server(self):
        self._runner = await self._runner_factory()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self._runner.cleanup()

    async def _drain_logs(self):
        log = self.query_one("#log", RichLog)
        while True:
            e = await self.state.log_queue.get()
            color = STATUS_COLOR.get(e["status"] // 100, "white")
            log.write(
                f"[dim]{e['time']}[/] [{color}]{e['status']}[/] "
                f"[bold]{e['method']:6}[/] [cyan]{e['ip']}[/] {e['path']}"
            )

    def _refresh_devices(self):
        table = self.query_one("#devices", DataTable)
        table.clear()
        now = time.monotonic()
        for ip, s in sorted(self.state.devices.items()):
            idle = now - s.last_seen > 30
            speed = s.speed_down
            speed_str = f"{fmt_bytes(speed)}/s" if speed > 1 else "—"

            def cell(text: str) -> str:
                return f"[dim]{text}[/dim]" if idle else text

            table.add_row(
                cell(ip),
                cell(fmt_bytes(s.total_down)),
                cell(speed_str),
                cell(fmt_bytes(s.total_up)),
                cell(str(s.request_count)),
            )

    def action_quit(self):
        for task in (self._server_task, self._log_task):
            if task:
                task.cancel()
        self.exit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_lan_ip() -> str:
    """Return the LAN IP this machine uses for outbound traffic."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    with CONFIG_FILE.open() as f:
        return json.load(f)


def save_config(cfg: dict):
    with CONFIG_FILE.open("w") as f:
        json.dump(cfg, f, indent=4)


# ── Setup TUI (first run) ─────────────────────────────────────────────────────

class SetupApp(App):
    CSS = """
    Screen { align: center middle; }
    #box {
        width: 60;
        height: auto;
        border: double $accent;
        padding: 1 2;
    }
    Label { margin-top: 1; }
    Input { margin-bottom: 1; }
    #silent-row { margin-top: 1; margin-bottom: 1; height: 3; }
    #silent-label { width: 1fr; content-align: left middle; }
    Button { width: 100%; margin-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Reverse Proxy — First Run Setup", id="title")
            yield Label("Upstream URL")
            yield Input(id="upstream", placeholder="http://host:port")
            yield Label("Listen host")
            yield Input(id="host", placeholder="0.0.0.0")
            yield Label("Port")
            yield Input(id="port", placeholder="8080")
            with Horizontal(id="silent-row"):
                yield Label("Silent mode (no TUI)", id="silent-label")
                yield Switch(id="silent")
            yield Button("Save & Start", variant="primary", id="save")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id != "save":
            return
        try:
            port = int(self.query_one("#port", Input).value)
        except ValueError:
            self.query_one("#port", Input).focus()
            return
        self.exit({
            "upstream": self.query_one("#upstream", Input).value.strip(),
            "host":     self.query_one("#host", Input).value.strip(),
            "port":     port,
            "silent":   self.query_one("#silent", Switch).value,
        })


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Reverse proxy with TUI")
    parser.add_argument("--upstream", default=None)
    parser.add_argument("--host",     default=None)
    parser.add_argument("--port",     type=int, default=None)
    parser.add_argument("--silent",   action="store_true", default=False)
    parser.add_argument("--config",   action="store_true",
                        help="Save the resolved settings (config.json + CLI args) back to config.json")
    cli = parser.parse_args()

    cfg = load_config()

    # No config.json and no meaningful CLI args → interactive first-run setup
    if cfg is None and not any([cli.upstream, cli.host, cli.port]):
        cfg = SetupApp().run()
        if cfg is None:
            return  # user closed setup without saving
        save_config(cfg)
    elif cfg is None:
        cfg = {}

    # CLI args override config
    upstream = cli.upstream or cfg["upstream"]
    host     = cli.host     or cfg["host"]
    port     = cli.port     or cfg["port"]
    silent   = cli.silent   or cfg["silent"]

    # --config: persist the resolved values back to config.json
    if cli.config:
        save_config({"upstream": upstream, "host": host, "port": port, "silent": silent})

    lan_ip = get_lan_ip()
    proxy_base = f"http://{lan_ip}:{port}"
    state = State()

    aiohttp_app = web.Application()
    aiohttp_app["upstream"] = upstream.rstrip("/")
    aiohttp_app["proxy_base"] = proxy_base
    aiohttp_app["state"] = state

    async def on_startup(app):
        app["session"] = ClientSession(connector=TCPConnector(ssl=False), auto_decompress=False)

    async def on_cleanup(app):
        await app["session"].close()

    aiohttp_app.on_startup.append(on_startup)
    aiohttp_app.on_cleanup.append(on_cleanup)
    aiohttp_app.router.add_route("*", "/{path_info:.*}", handler)

    async def runner_factory():
        runner = web.AppRunner(aiohttp_app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        return runner

    if silent:
        print(f"Proxy listening on  http://{lan_ip}:{port}")
        web.run_app(aiohttp_app, host=host, port=port, print=None)
    else:
        ProxyTUI(
            state=state,
            runner_factory=runner_factory,
            title=f"Proxy  {upstream} → http://{lan_ip}:{port}",
        ).run()


if __name__ == "__main__":
    main()
