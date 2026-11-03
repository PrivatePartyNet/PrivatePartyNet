#!/bin/sh
# Set up a PrivatePartyNet relay on a fresh Ubuntu 22.04 or 24.04 VPS (Debian 12 works too). Run as root from this
# folder of the repository:
#
#     sudo sh setup-ubuntu.sh
#
# It installs Python and coturn from Ubuntu, puts the relay in /opt/ppn-relay, makes a secret and a certificate in
# /etc/ppn-relay (kept if they are already there), opens the firewall if ufw is on, and starts both services. Running
# it again updates the relay and keeps the secret. At the end it prints what to type into PrivatePartyNet.
#
# Set PUBLIC_IP first if the VPS's public address is not on its own network card (some providers use 1:1 NAT). The
# relay and coturn listen on IPv6 as well when the VPS has it (PUBLIC_IP6, found by itself), so members with only IPv6
# and members with only IPv4 meet through it.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
UDP_PORT=${UDP_PORT:-3479}
TLS_PORT=${TLS_PORT:-443}

if [ "$(id -u)" != 0 ]; then
    echo "Run this as root: sudo sh $0" >&2
    exit 1
fi
if [ ! -f "$ROOT/privatepartynet/relayproto.py" ]; then
    echo "Run this from the relay folder of a PrivatePartyNet checkout (privatepartynet/relayproto.py is missing)." >&2
    exit 1
fi

echo "Installing python3, openssl and coturn..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 openssl coturn

id ppn-relay >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ppn-relay

install -d -m 755 /opt/ppn-relay /opt/ppn-relay/privatepartynet
install -m 644 "$HERE/ppn_relay.py" /opt/ppn-relay/ppn_relay.py
install -m 644 "$ROOT/privatepartynet/__init__.py" "$ROOT/privatepartynet/relayproto.py" /opt/ppn-relay/privatepartynet/

install -d -m 750 -g ppn-relay /etc/ppn-relay
if [ ! -s /etc/ppn-relay/secret ]; then
    python3 /opt/ppn-relay/ppn_relay.py --new-secret > /etc/ppn-relay/secret
fi
chown root:ppn-relay /etc/ppn-relay/secret
chmod 640 /etc/ppn-relay/secret
if [ ! -f /etc/ppn-relay/relay.crt ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 -subj "/CN=ppn-relay" \
        -keyout /etc/ppn-relay/relay.key -out /etc/ppn-relay/relay.crt 2>/dev/null
fi
chown root:ppn-relay /etc/ppn-relay/relay.key /etc/ppn-relay/relay.crt
chmod 640 /etc/ppn-relay/relay.key
chmod 644 /etc/ppn-relay/relay.crt
printf 'UDP_PORT=%s\nTLS_PORT=%s\nRATE_KBPS=%s\n' "$UDP_PORT" "$TLS_PORT" "${RATE_KBPS:-8000}" > /etc/ppn-relay/options

SECRET=$(head -n 1 /etc/ppn-relay/secret)
if [ -z "${PUBLIC_IP:-}" ]; then
    PUBLIC_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
fi
if [ -z "$PUBLIC_IP" ]; then
    echo "Could not tell the VPS's public address. Run again with PUBLIC_IP=x.x.x.x" >&2
    exit 1
fi
if [ -z "${PUBLIC_IP6:-}" ]; then
    PUBLIC_IP6=$(ip -6 route get 2001:4860:4860::8888 2>/dev/null | sed -n 's/.* src \([0-9a-fA-F:]*\).*/\1/p')
fi

echo "Configuring coturn..."
if [ -f /etc/turnserver.conf ] && ! grep -q "coturn for PrivatePartyNet" /etc/turnserver.conf \
        && [ ! -f /etc/turnserver.conf.before-ppn ]; then
    cp -p /etc/turnserver.conf /etc/turnserver.conf.before-ppn
    echo "Kept the previous coturn configuration as /etc/turnserver.conf.before-ppn"
fi
umask 027
{
    cat "$HERE/turnserver.conf"
    echo "static-auth-secret=$SECRET"
    echo "external-ip=$PUBLIC_IP"
    echo "allowed-peer-ip=$PUBLIC_IP"
    if [ -n "$PUBLIC_IP6" ]; then
        echo "allowed-peer-ip=$PUBLIC_IP6"
    fi
} > /etc/turnserver.conf
umask 022
chown root:turnserver /etc/turnserver.conf 2>/dev/null || true
chmod 640 /etc/turnserver.conf
if [ -f /etc/default/coturn ]; then
    sed -i 's/^#\?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn
fi
install -d -m 755 /etc/systemd/system/coturn.service.d
install -m 644 "$HERE/systemd/coturn-ppn.conf" /etc/systemd/system/coturn.service.d/ppn.conf
install -m 644 "$HERE/systemd/ppn-relay.service" /etc/systemd/system/ppn-relay.service

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    echo "Opening the firewall (ufw)..."
    ufw allow "$UDP_PORT/udp" comment "PrivatePartyNet relay"
    ufw allow "$TLS_PORT/tcp" comment "PrivatePartyNet relay over TLS"
    ufw allow 3478 comment "coturn"
    ufw allow 49160:49400/udp comment "coturn media"
fi

systemctl daemon-reload
systemctl enable --now ppn-relay
systemctl restart ppn-relay
systemctl enable --now coturn
systemctl restart coturn

PIN=$(openssl x509 -in /etc/ppn-relay/relay.crt -outform DER | sha256sum | cut -d' ' -f1)
cat <<EOF

The relay is running. In PrivatePartyNet, as the owner: Server settings, Relay server, then enter

    Address:     $PUBLIC_IP${PUBLIC_IP6:+, $PUBLIC_IP6}
    Secret:      $SECRET
    Certificate: $PIN

The secret stays on your PC; members get short-lived credentials from it. Keep it private, and clear this
terminal's scrollback once you have copied it (or read it again later with: sudo head -n 1 /etc/ppn-relay/secret).
Check on it with: systemctl status ppn-relay coturn   and   journalctl -u ppn-relay
Your provider's own firewall (if it has one) must allow UDP $UDP_PORT, TCP $TLS_PORT, UDP and TCP 3478, UDP 49160-49400.
EOF
