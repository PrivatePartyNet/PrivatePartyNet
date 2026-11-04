#!/bin/sh
# Keep what coturn relays away from the VPS itself. coturn must be allowed to relay to the VPS's own public address, or
# two members who both relay through it could not meet; this firewall narrows that to coturn's own media ports, and
# shuts it out of everything else on the machine: SSH, the PrivatePartyNet relay's ports, loopback.
#
#     sudo sh turn-firewall.sh            (setup-ubuntu.sh installs it, and ppn-turn-firewall.service runs it at boot)
#
# It matches coturn's packets by the user coturn runs as (TURN_USER, "turnserver" on Ubuntu; for the Docker image give
# its numeric id, TURN_USER=65534). Running it again replaces its rules instead of adding more.
set -eu

TURN_USER=${TURN_USER:-turnserver}
MIN_PORT=${MIN_PORT:-49160}  # keep in step with min-port and max-port in turnserver.conf
MAX_PORT=${MAX_PORT:-49400}
CHAIN=PPN-TURN

if [ "$(id -u)" != 0 ]; then
    echo "Run this as root: sudo sh $0" >&2
    exit 1
fi

# Every address this machine has, each family on its own (prefix lengths removed).
own() {
    ip -o "-$1" addr show 2>/dev/null | awk '{ split($4, a, "/"); print a[1] }'
}

apply() {
    tool=$1 family=$2 loopback=$3
    command -v "$tool" >/dev/null 2>&1 || return 0
    "$tool" -N "$CHAIN" 2>/dev/null || "$tool" -F "$CHAIN"
    for address in $(own "$family"); do
        # The VPS's own addresses: only coturn's media ports, for two members who both relay here.
        "$tool" -A "$CHAIN" -d "$address" -p udp --dport "$MIN_PORT:$MAX_PORT" -j RETURN
        "$tool" -A "$CHAIN" -d "$address" -j DROP
    done
    "$tool" -A "$CHAIN" -d "$loopback" -j DROP
    if ! "$tool" -C OUTPUT -m owner --uid-owner "$TURN_USER" -j "$CHAIN" 2>/dev/null; then
        "$tool" -I OUTPUT 1 -m owner --uid-owner "$TURN_USER" -j "$CHAIN"
    fi
}

apply iptables 4 127.0.0.0/8
apply ip6tables 6 ::1/128
echo "coturn ($TURN_USER) may reach this machine only on UDP $MIN_PORT-$MAX_PORT."
