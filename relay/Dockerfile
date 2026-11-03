# The PrivatePartyNet relay server. Build from the repository's top folder:
#   docker build -f relay/Dockerfile -t ppn-relay .
# (docker-compose.yml in this folder does that for you.)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/* \
    && useradd --system --home /data --shell /usr/sbin/nologin ppn-relay \
    && mkdir -p /data && chown ppn-relay /data

WORKDIR /app
COPY privatepartynet/__init__.py privatepartynet/relayproto.py /app/privatepartynet/
COPY relay/ppn_relay.py relay/docker-entrypoint.sh /app/
RUN chmod 755 /app/docker-entrypoint.sh

USER ppn-relay
VOLUME /data
EXPOSE 3479/udp 443/tcp
# docker-compose.yml runs it on the host's network, IPv6 and IPv4.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
