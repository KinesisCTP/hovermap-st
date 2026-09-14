#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") <wifi|fischer|usb> [interface]"
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    usage >&2
    exit 2
fi

profile="$1"
requested_interface="${2:-}"

case "${profile}" in
    wifi)
        client_address="10.9.0.99"
        hovermap_address="10.9.0.1"
        network_cidr="10.9.0.0/24"
        ;;
    fischer)
        client_address="192.168.2.100"
        hovermap_address="192.168.2.115"
        network_cidr="192.168.2.0/24"
        ;;
    usb)
        client_address="192.168.3.100"
        hovermap_address="192.168.3.115"
        network_cidr="192.168.3.0/24"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

for command in ip python3 curl; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Required command is unavailable: ${command}" >&2
        exit 1
    fi
done

if [ -n "${requested_interface}" ]; then
    if ! ip link show dev "${requested_interface}" >/dev/null 2>&1; then
        echo "Interface ${requested_interface} does not exist." >&2
        exit 1
    fi
    if ! ip -4 addr show dev "${requested_interface}" | grep -Fq "${client_address}/24"; then
        echo "${requested_interface} does not have ${client_address}/24." >&2
        ip -4 addr show dev "${requested_interface}" >&2
        exit 1
    fi
fi

mapfile -t matches < <(
    ip -j -4 addr show | python3 -c '
import ipaddress
import json
import sys

target = ipaddress.ip_network(sys.argv[1])
for interface in json.load(sys.stdin):
    interface_name = interface["ifname"]
    for address in interface.get("addr_info", []):
        if address.get("family") != "inet":
            continue
        local = address["local"]
        prefix_length = address["prefixlen"]
        cidr = f"{local}/{prefix_length}"
        configured = ipaddress.ip_interface(cidr)
        if configured.network == target:
            print(f"{interface_name}:{cidr}")
' "${network_cidr}"
)

if [ "${#matches[@]}" -eq 0 ]; then
    echo "No host interface is configured on ${network_cidr}." >&2
    echo "Configure ${client_address}/24 on the Hovermap-facing interface." >&2
    exit 1
fi
if [ "${#matches[@]}" -ne 1 ]; then
    echo "Expected one Hovermap-facing interface; found ${#matches[@]}." >&2
    printf '  %s\n' "${matches[@]}" >&2
    exit 1
fi
if [[ "${matches[0]}" != *":${client_address}/24" ]]; then
    echo "The ${network_cidr} interface has the wrong workstation address:" >&2
    echo "  ${matches[0]}" >&2
    echo "Configure ${client_address}/24." >&2
    exit 1
fi

echo "Matching interface address: ${matches[0]}"
echo "Checking Hovermap status endpoint at http://${hovermap_address}/status ..."
response="$(
    curl \
        --disable \
        --silent \
        --show-error \
        --noproxy '*' \
        --max-filesize 4194304 \
        --max-time 5 \
        --write-out $'\n%{http_code}' \
        "http://${hovermap_address}/status"
)"
status_code="${response##*$'\n'}"
status_json="${response%$'\n'*}"
if [[ ! "${status_code}" =~ ^2[0-9][0-9]$ ]]; then
    echo "Hovermap status endpoint returned HTTP ${status_code}." >&2
    exit 1
fi
python3 -c 'import json, sys; json.load(sys.stdin)' <<<"${status_json}"
echo "Network and HTTP API preflight passed for ${profile}."
