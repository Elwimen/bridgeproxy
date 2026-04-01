# bridgeproxy

A lightweight HTTP/WebSocket reverse proxy with a terminal UI. Useful any time you need to bridge a server that is only reachable from one machine (via VPN, Tailscale, a private network, or any other means) to devices that cannot reach it directly.

## Features

- HTTP and WebSocket proxying
- TUI with per-device bandwidth (download speed, totals, request count)
- Scrolling request log, color-coded by status
- First-run setup wizard if no config is present
- `--silent` mode — no TUI, just prints the LAN address and runs

## Requirements

```
pip install -r requirements.txt
```

## Setup

Copy the template and edit it:

```bash
cp config.template.json config.json
```

Or just run the script with no config and fill in the setup wizard:

```bash
python proxy.py
```

## Usage

```bash
# Run with TUI
python proxy.py

# Run silently (prints LAN IP and exits to background-friendly mode)
python proxy.py --silent

# Override upstream for this run only
python proxy.py --upstream http://myserver:8080

# Override and save back to config.json
python proxy.py --upstream http://myserver:8080 --config
```

## Config

`config.json` (gitignored — copy from `config.template.json`):

```json
{
    "upstream": "http://hostname:port",
    "host": "0.0.0.0",
    "port": 8080,
    "silent": false
}
```

CLI args always override `config.json`. Use `--config` to persist CLI args back to the file.
