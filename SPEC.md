# Hovermap Direct API and MCP Server Specification

## Status

- Target branch: `direct-api`
- Product stage: implementation and automated acceptance complete; connected-device hardware acceptance pending
- Intended users: KINESIS operators and locally running LLM applications

## Value proposition

Provide a small, ROS-free interface for routine Hovermap ST operation. A local
MCP-capable LLM can discover a fixed set of tools and use them as directed by
its project prompt, without installing ROS or understanding the device's HTTP
endpoints.

The current alternatives require ROS-facing packages or manual API calls. The
direct server should make these three workflows straightforward:

1. Inspect device readiness and storage.
2. Set a scan prefix and start or stop a Mapping mission.
3. List and download completed scans.

## Why MCP and an LLM

The conversational advantage comes from allowing an operator or project agent
to incorporate Hovermap actions into a broader local workflow. The LLM supplies
intent and orchestration; the MCP server supplies discoverable schemas and the
ability to call the real device API.

Project prompts and MCP clients decide when tools should be called. The server
does not impose project-specific confirmation, approval, or arming rules. It
does enforce universal transport, validation, concurrency, and filesystem
boundaries.

## User experience

This is a headless MCP server. It has no widget, dashboard, web page, resource,
or prompt surface.

1. An MCP client launches `hovermap-mcp` locally.
2. The client discovers exactly six tools.
3. The LLM calls tools as needed and receives concise text plus structured,
   JSON-compatible results.
4. The server exits cleanly on standard-input EOF or client disconnect and
   handles `SIGINT` and `SIGTERM` without corrupting the protocol stream.

MCP protocol messages use standard input and standard output. All application
logs go to standard error so they cannot corrupt the protocol stream.

## Product context

- Device interface: the Hovermap HTTP API.
- MCP transport: local `stdio` only in v1.
- Runtime location: the Linux workstation connected to the Hovermap network.
- Device authentication: none; operation assumes the existing isolated,
  trusted Hovermap network.
- MCP authentication: none for local `stdio`; access is governed by the local
  operating-system account and MCP client configuration.
- Implementation language: Python, reusing the existing hardened,
  ROS-independent HTTP client.
- Supported runtime: Python 3.10 or newer, with Python 3.12 on Ubuntu 24.04 as
  the primary deployment target.
- MCP SDK: the official Python package `mcp>=2.2,<3`; the initial lock resolves
  the reviewed 2.2 release line.
- No ROS, Mule, catkin, colcon, ROS message, or ROS master dependency is
  permitted at runtime or in tests.

## MCP tools

The server exposes exactly the following tools. The target device URL,
download directory, timeouts, and response limits are startup configuration,
not tool arguments.

### `get_status`

Input: none.

Returns:

- `scan_prefix`: configured device prefix.
- `current_scan_name`: current scan directory/name reported by the device.
- `free_space_bytes`: available device storage in bytes.
- `scan_running`: locally derived `true`, `false`, or `null`.
- `state`: unmodified device state string.
- `retrieved_at_utc`: server observation time.

Both `state` and `scan_running` are returned because the device has no dedicated
running flag. The derived value is `true` only for the known `Mapping` state,
`false` for `Stopped`, `Disabled`, `Booting`, `USB Error`, `USB Mounted`,
`Transferring Data`, or `Processes Stopped`, and `null` for every unknown or
future state. Unknown states must never be treated as running implicitly.

### `set_scan_prefix`

Input:

- `prefix`: 1-20 ASCII alphanumeric characters.

Returns the requested prefix, `acknowledgement: "http_2xx"`, and
`state_confirmed: false`.

### `start_scan`

Input: none.

Requests Mapping mission type `1`. Returns the requested mission type,
`acknowledgement: "http_2xx"`, and `state_confirmed: false`.

### `stop_scan`

Input: none.

Returns `acknowledgement: "http_2xx"` and `state_confirmed: false`.

### `list_scans`

Input: none.

Returns:

- `count`.
- `ordering`: `numeric_time_desc`, `iso8601_time_desc`, or `device_order`.
- `scans`: objects containing `name`, `size_bytes`, the original device
  `timestamp`, and nullable `parsed_time_utc` for ISO-8601 input only.
- `warnings`: any non-fatal interpretation warnings.

Numeric device time units are undocumented. Scans may be sorted descending only
when all timestamps are homogeneous finite numeric values, or when all are
parseable ISO-8601 values. Mixed or unparseable formats preserve device order
and produce a warning. Numeric values are never labelled as Unix time.

For identical behavior on every supported Python version, ISO classification
uses the extended calendar datetime form `YYYY-MM-DDTHH:MM:SS`, with an optional
fraction and optional `Z`, `z`, or `±HH:MM` offset. An absent offset is treated as
UTC. Other ISO-8601 forms, surrounding whitespace, and firmware-local text remain
unparseable and therefore preserve device order.

### `download_scan`

Input:

- `scan_name`: `<1-20 ASCII alphanumeric characters>_<digits>`, with a total
  length no greater than 64 characters.

Returns the scan name, absolute local archive path, byte size,
`zip_validated: true`, and `published_atomically: true`. It never returns the
archive contents through MCP.

The destination is the configured download root. A valid completed download
may atomically replace an existing archive with the same device-derived name.
A failed download never replaces the completed archive and may leave a `.part`
file for diagnosis if streaming had begun.

## Device endpoint contract

The implementation calls only these inherited device endpoints:

| Tool | Method | Path | Fixed/query parameters |
| --- | --- | --- | --- |
| `get_status` | GET | `/status` | none |
| `set_scan_prefix` | GET | `/setprefix` | `prefix=<validated-prefix>` |
| `start_scan` | GET | `/startsystem` | `mission_type=1` |
| `stop_scan` | GET | `/stopsystem` | none |
| `list_scans` | GET | `/files` | none |
| `download_scan` | GET | `/downloadscan` | `scanname=<validated-name>` |

Endpoint paths and methods are not configurable through MCP.

## Acknowledgement semantics

The Hovermap control endpoints do not provide a correlated state-transition
acknowledgement. Success from `set_scan_prefix`, `start_scan`, or `stop_scan`
means only that the fixed endpoint returned HTTP 2xx. Tool descriptions and
results must not claim that the requested transition completed. A caller may
use `get_status` afterward when it needs observed state.

Download success is stronger: the complete local archive has passed declared
length checks when available, ZIP structure and CRC validation, a content-file
flush before atomic replacement, and atomic publication. It does not promise
power-loss durability for the containing-directory entry and does not validate
the semantic contents of the scan archive.

ZIP validation reads every entry, permits at most 100,000 entries, and limits
the cumulative uncompressed bytes to `--max-download-bytes`. Directory entries
must not claim data or a nonzero CRC.

## Startup configuration

The console entry point is `hovermap-mcp`. It supports the following exact
command-line options and environment variables. Command-line values take
precedence over environment values, which take precedence over defaults.

| CLI option | Environment variable | Default | Validation |
| --- | --- | --- | --- |
| `--profile` | `HOVERMAP_PROFILE` | `wifi` | `wifi`, `fischer`, or `usb` |
| `--hovermap-url` | `HOVERMAP_URL` | profile address | valid origin-only HTTP(S) URL |
| `--download-directory` | `HOVERMAP_DOWNLOAD_DIRECTORY` | `~/hovermap_downloads` | usable local directory |
| `--request-timeout-seconds` | `HOVERMAP_REQUEST_TIMEOUT_SECONDS` | `5` | float in `(0, 300]` |
| `--download-timeout-seconds` | `HOVERMAP_DOWNLOAD_TIMEOUT_SECONDS` | `450` | float in `(0, 3600]` |
| `--max-json-bytes` | `HOVERMAP_MAX_JSON_BYTES` | `4194304` | integer in `[1, 67108864]` |
| `--max-download-bytes` | `HOVERMAP_MAX_DOWNLOAD_BYTES` | `137438953472` | integer in `[1, 1099511627776]` |
| `--log-level` | `HOVERMAP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

Profile device addresses are `10.9.0.1` for Wi-Fi, `192.168.2.115` for Fischer,
and `192.168.3.115` for USB. An explicit URL overrides the selected profile.
The download directory expands `~` once, becomes an absolute resolved path at
startup, and is created or validated before the server begins reading MCP
messages.

The base URL must use HTTP or HTTPS, identify one origin, and contain no
credentials, query, fragment, or API path. Tools never accept a host, URL,
endpoint, output directory, timeout, header, or raw HTTP parameter.

## Server invariants

- Only the six fixed, documented Hovermap endpoints may be called.
- Environment HTTP proxies are bypassed and redirects are rejected before they
  can contact another origin.
- Scan prefixes and scan names are validated before network or filesystem work.
- Mutating prefix/start/stop calls execute through one bounded, ordered lane.
- Blocking HTTP and download work does not block the MCP event loop.
- At most one scan download may be queued or running per server process; the
  admission guard is acquired before work enters an executor.
- Responses and downloads have explicit time and byte limits.
- Downloads remain beneath the configured root, are never extracted, and
  cannot escape through traversal or symlink behavior.
- Downloads stream to a `.part` file, validate length and ZIP CRC, flush the
  content file, and then publish with an atomic replace. Existing final or part
  paths that are symlinks are rejected.
- Shutdown cancels new work and asks an active streaming download to stop.
- Expected failures are returned as model-visible tool errors without Python
  tracebacks or unsanitized internals.

## Error contract

Every successful call returns an MCP `CallToolResult` with `isError` omitted or
`false`, concise text content, and structured content containing `ok: true`,
`operation: <tool-name>`, and exactly the tool-specific fields described above.

Every expected tool failure returns `CallToolResult(isError=true)` with concise
text content and a structured object containing `ok: false`, `operation`, and
an `error` object with:

- `code`.
- `message`.
- `retryable`.
- Optional `http_status`.

Supported error codes are `invalid_argument`, `transport_error`, `http_error`,
`protocol_error`, `busy`, `filesystem_error`, `cancelled`, and
`internal_error`. Expected failures do not escape as Python exceptions or
protocol-level JSON-RPC errors. Invalid MCP framing and other protocol failures
remain the SDK's responsibility.

## Tool metadata

Metadata is descriptive and advisory; it is not an authorization mechanism.

- `get_status` and `list_scans` are read-only.
- `set_scan_prefix`, `start_scan`, and `stop_scan` change device state.
- `download_scan` writes or replaces a local file.
- Descriptions state the immediate device or filesystem effect and the limited
  acknowledgement semantics.

## Packaging and repository shape

The `direct-api` branch is based on the hardware-validated `ros2-jazzy` work so
the HTTP client and its tests retain provenance. Its finished tree will be a
normal Python project, provisionally organized as:

```text
pyproject.toml
src/hovermap_direct/
  http_client.py
  profiles.py
src/hovermap_mcp/
  server.py
  configuration.py
  errors.py
tests/
scripts/preflight_network.sh
README.md
SPEC.md
THIRD_PARTY_NOTICES.md
```

The branch will extract and rename the reusable HTTP client, including its
ROS-specific user-agent string and shutdown wording. Extraction must also reject
the base-path form currently accepted by the old URL normalizer and move the
single-download admission guard ahead of executor submission. Inherited ROS 1,
ROS 2, and Mule packages, manifests, containers, workflows, and documentation
will be removed during implementation. The network preflight script and useful
network profiles will be retained and adapted.

Package metadata uses `requires-python = ">=3.10"` and `mcp>=2.2,<3`. A lockfile
records the exact development and CI resolution. The HTTP client otherwise
remains standard-library-only. The KINESIS-authored package remains proprietary
unless a separate licensing decision changes it.

## Out of scope for v1

- Live point clouds, odometry, transforms, occupancy data, or Mule transport.
- `get_live_summary`.
- ROS 1 or ROS 2 adapters.
- Arbitrary HTTP requests, URLs, endpoints, Mule topics, or payloads.
- Perception configuration or overlay controls.
- Scan deletion or archive extraction.
- Robot motion or autonomy controls.
- A network-accessible MCP HTTP transport, authentication server, or gateway.
- MCP resources, prompts, widgets, dashboards, or any other UI.
- Project-specific approval or tool-use policy.

## Verification and acceptance

Automated CI must run without Hovermap hardware or ROS and include:

1. Formatting/linting and unit tests on Python 3.10, 3.12, and 3.14, covering
   the minimum, primary deployment, and current supported interpreter.
2. Existing mocked HTTP coverage for controls, status/list parsing, redirects,
   proxy bypass, timeouts, malformed or oversized responses, and download
   length/CRC/atomicity/concurrency.
3. Configuration, path-confinement, symlink, mutation-ordering, shutdown, and
   structured-error tests.
4. A built-wheel installation smoke test in a clean environment.
5. A real MCP client launching the installed console script over `stdio`,
   completing initialization, discovering exactly six tools, checking their
   schemas and metadata, and exercising mocked calls.
6. An assertion that stdout contains only MCP protocol frames.
7. An import/dependency check proving no ROS or Mule runtime is required.

Hardware acceptance on an isolated Hovermap network must then verify:

1. A clean Python environment with no ROS can launch the server.
2. An MCP client discovers the six tools.
3. Status, prefix, start, stop, list, and download work against the device.
4. Requested state changes are observed separately through status.
5. A downloaded archive has the expected name and byte count, passes ZIP
   validation, and leaves no `.part` file after success.
6. The process opens no Mule discovery or transport ports.

The branch remains non-default until its README, package build, CI, and hardware
acceptance are complete.
