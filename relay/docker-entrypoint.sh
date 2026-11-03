#!/bin/sh
# Start the relay in its container: make its TLS certificate the first time, then run it.
set -eu

SECRET_FILE=/run/secrets/relay_secret
if [ ! -s "$SECRET_FILE" ]; then
    echo "No relay secret: put it alone in relay/secret beside docker-compose.yml (see README.md)." >&2
    exit 1
fi
if [ ! -f /data/relay.crt ] || [ ! -f /data/relay.key ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 -subj "/CN=ppn-relay" \
        -keyout /data/relay.key -out /data/relay.crt 2>/dev/null
    chmod 600 /data/relay.key
fi
exec python3 /app/ppn_relay.py --secret-file "$SECRET_FILE" --udp-port "${PPN_RELAY_UDP_PORT:-3479}" --tls-port "${PPN_RELAY_TLS_PORT:-443}" \
    --cert /data/relay.crt --key /data/relay.key --rate-kbps "${PPN_RELAY_RATE_KBPS:-8000}" "$@"
