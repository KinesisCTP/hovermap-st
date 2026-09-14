# KINESIS Hovermap ST — direct API

This branch provides a small, headless MCP server for routine Hovermap ST
operation. It talks directly to the device HTTP API and exposes six tools to a
locally running LLM application over `stdio`. There is no UI and no ROS runtime
is required. Here, `stdio` means the MCP client launches the server as a local
subprocess and exchanges protocol messages through pipes; the server opens no
MCP network port.

Need a ROS integration instead? Use [`ros1-noetic`](https://github.com/KinesisCTP/hovermap-st/tree/ros1-noetic)
or [`ros2-jazzy`](https://github.com/KinesisCTP/hovermap-st/tree/ros2-jazzy).

## Quick start

Power the Hovermap, connect the Linux workstation to its isolated device
network, and configure the matching client address from the table below. Check
the connection before installing the server:

```bash
git clone --branch direct-api https://github.com/KinesisCTP/hovermap-st.git
cd hovermap-st
./scripts/preflight_network.sh wifi wlan0

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

Configure the MCP client to launch the installed executable. Use the absolute
path to the virtual environment; the server is not an interactive shell
command and normally starts and stops with the MCP client.

```json
{
  "mcpServers": {
    "hovermap": {
      "command": "/absolute/path/to/hovermap-st/.venv/bin/hovermap-mcp",
      "args": ["--profile", "wifi"],
      "env": {
        "HOVERMAP_DOWNLOAD_DIRECTORY": "/absolute/path/to/hovermap_downloads"
      }
    }
  }
}
```

Replace the paths and profile for the workstation in use. Once
`kinesis-hovermap-mcp` is published to the Python package index configured for
your MCP client, the equivalent ephemeral command is:

```json
{
  "command": "uvx",
  "args": [
    "--from",
    "kinesis-hovermap-mcp",
    "hovermap-mcp",
    "--profile",
    "wifi"
  ]
}
```

For an unpublished local checkout, `uvx` can use its absolute path in place of
`kinesis-hovermap-mcp`.

## Network profiles

Configure exactly one Hovermap-facing host interface:

| Profile | Hovermap address | Workstation address | Netmask |
| --- | --- | --- | --- |
| `wifi` | `10.9.0.1` | `10.9.0.99` | `255.255.255.0` |
| `fischer` | `192.168.2.115` | `192.168.2.100` | `255.255.255.0` |
| `usb` | `192.168.3.115` | `192.168.3.100` | `255.255.255.0` |

Run the matching preflight, optionally naming the interface:

```bash
./scripts/preflight_network.sh wifi wlan0
./scripts/preflight_network.sh fischer enp4s0
./scripts/preflight_network.sh usb enx001122334455
```

## Tools

An MCP client discovers exactly these six tools:

| Tool | Input | Result |
| --- | --- | --- |
| `get_status` | none | Prefix, current scan, free bytes, device state, and tri-state `scan_running` |
| `set_scan_prefix` | `prefix` (1–20 ASCII letters or digits) | HTTP acknowledgement; the new state is not independently confirmed |
| `start_scan` | none | Mapping-start HTTP acknowledgement; the state transition is not independently confirmed |
| `stop_scan` | none | Stop HTTP acknowledgement; the state transition is not independently confirmed |
| `list_scans` | none | Validated scan metadata and the ordering rule applied |
| `download_scan` | `scan_name` | Absolute archive path, byte count, ZIP validation, and atomic-publication status |

After a control call, use `get_status` when the project needs to observe the
resulting device state. The server returns expected device, validation, and
filesystem failures as structured tool errors for the calling LLM.

## Startup configuration

Command-line values override environment values, which override defaults.

| CLI option | Environment variable | Default |
| --- | --- | --- |
| `--profile` | `HOVERMAP_PROFILE` | `wifi` |
| `--hovermap-url` | `HOVERMAP_URL` | Selected profile address |
| `--download-directory` | `HOVERMAP_DOWNLOAD_DIRECTORY` | `~/hovermap_downloads` |
| `--request-timeout-seconds` | `HOVERMAP_REQUEST_TIMEOUT_SECONDS` | `5` |
| `--download-timeout-seconds` | `HOVERMAP_DOWNLOAD_TIMEOUT_SECONDS` | `450` |
| `--max-json-bytes` | `HOVERMAP_MAX_JSON_BYTES` | `4194304` |
| `--max-download-bytes` | `HOVERMAP_MAX_DOWNLOAD_BYTES` | `137438953472` |
| `--log-level` | `HOVERMAP_LOG_LEVEL` | `INFO` |

Profiles are `wifi`, `fischer`, and `usb`; log levels are `DEBUG`, `INFO`,
`WARNING`, and `ERROR`. `--hovermap-url` is for a deliberate origin-only
HTTP(S) override. See [`SPEC.md`](SPEC.md) for the exact bounds and result
schemas.

## Operational boundaries

- Keep the unauthenticated device interface on its isolated, trusted network.
- Securely hold or mount the Hovermap before allowing an LLM to start a scan.
- The server itself adds no project-specific confirmation step; the MCP client
  and project prompt determine when the tools may be called.
- Mutating calls run in order, and only one download may be queued or running.
- Downloads stay within the configured root, are never extracted, are streamed
  through a `.part` file, ZIP-checked, and atomically published.
- Do not expose this v1 `stdio` server as a network service.

## Contributor setup

Use Python 3.10 or newer. Python 3.12 on Ubuntu 24.04 is the primary target.

With `uv` 0.12.13 installed outside the project environment:

```bash
uv lock --check
uv sync --locked --extra dev --python python3

uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
uv run --locked python -m build --no-isolation
uv run --locked python scripts/check_stdio_contract.py
```

The committed `uv.lock` fixes the reviewed development and CI resolution. CI
runs the test suite on Python 3.10, 3.12, and 3.14, then installs the built wheel
and its locked dependencies into a clean environment and verifies the real MCP
contract over `stdio`.

Never commit downloaded scans, `.part` files, credentials, or machine-specific
configuration.

## Hardware acceptance

Automated tests do not replace the device check. Although this is the default
branch, complete the following from a clean, ROS-free Python environment on an
isolated Hovermap network before relying on it for production device control:

1. Confirm that the client discovers only the six documented tools.
2. Run status, prefix, start, stop, list, and download calls against the device.
3. Observe requested state changes separately with `get_status`.
4. Confirm the downloaded archive name and byte count, valid ZIP, atomic final
   file, and absence of a leftover `.part` file.
5. Confirm the process opens no unrelated discovery or transport ports.

Hardware acceptance is pending until those steps are recorded against a
connected Hovermap.

KINESIS-authored code is proprietary; public availability does not grant an
open-source license. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
