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
interface="${2:-}"

case "${profile}" in
    wifi)
        client_address="10.9.0.99"
        hovermap_address="10.9.0.1"
        ;;
    fischer)
        client_address="192.168.2.100"
        hovermap_address="192.168.2.115"
        ;;
    usb)
        client_address="192.168.3.100"
        hovermap_address="192.168.3.115"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if [ -n "${interface}" ]; then
    if ! ip link show dev "${interface}" >/dev/null 2>&1; then
        echo "Interface ${interface} does not exist." >&2
        exit 1
    fi
    if ! ip -4 addr show dev "${interface}" | grep -Fq "${client_address}/24"; then
        echo "${interface} does not have required address ${client_address}/24." >&2
        ip -4 addr show dev "${interface}" >&2
        exit 1
    fi
fi

mapfile -t matches < <(
    ip -o -4 addr show \
        | awk -v expected="${client_address}/24" '$4 == expected {print $2 ":" $4}'
)
if [ "${#matches[@]}" -eq 0 ]; then
    echo "No host interface has the required address ${client_address}/24." >&2
    echo "Configure ${client_address}/24 on the Hovermap-facing interface." >&2
    exit 1
fi
if [ "${#matches[@]}" -ne 1 ]; then
    echo "Expected exactly one Hovermap-facing interface; found ${#matches[@]}." >&2
    printf '  %s\n' "${matches[@]}" >&2
    echo "Remove the duplicate address before starting Mule interface discovery." >&2
    exit 1
fi

echo "Matching interface address: ${matches[0]}"
echo "Pinging Hovermap at ${hovermap_address}..."
ping -c 5 -W 2 "${hovermap_address}"
echo "Network preflight passed for ${profile}."
