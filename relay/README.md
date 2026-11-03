# A relay for your group

Most of the time PrivatePartyNet members reach each other directly: on the same network, over IPv6, through a port the
router opened, or by both sides sending at once through their routers (hole punching). Two routers that each give a new
port for every destination (common with carrier-grade NAT, as on some mobile and satellite connections) cannot be crossed
that way. Then the members use a relay: first another member whose PC both can reach (a friend relay, which each person
can turn off in the control panel), and last a relay server your group runs itself. This folder is that server.

It is two programs on one small Linux server:

- `ppn_relay.py` carries the game network and the join tunnel: UDP port 3479, and TCP port 443 (TLS) for networks that
  let no UDP out.
- coturn carries voice and screens (TURN): UDP and TCP port 3478, with media on UDP 49160-49200.

## What the relay can and cannot see

Everything it forwards is encrypted end to end before it leaves a member's PC (WireGuard for the game network, QUIC with
the host's certificate for joining, DTLS-SRTP for voice and screens). The relay cannot read it or change it. It does see
the public addresses of the members using it, how much they send, and when. It keeps nothing on disk.

Only members can use it. The owner's PC holds the relay secret and gives each member short-lived credentials made from it
(valid for a day, then renewed while they are in the party); the secret itself never leaves the owner's PC. Each member's
traffic through one channel is limited (8 Mbit/s by default) and each member can hold at most 64 channels.

## Rent a $5 VPS, run this

Any provider's smallest Linux server is enough: 1 CPU, 512 MB to 1 GB of memory, and a public IPv4 address. A group of
friends playing and talking uses a few Mbit/s while someone needs the relay, and nothing otherwise. Pick a region near
most of the group, since every relayed datagram goes there and back.

1. Make an Ubuntu 24.04 server and sign in with SSH.
2. Get PrivatePartyNet's source onto it and run the setup script:

       sudo apt-get install -y git
       git clone <the PrivatePartyNet repository> privatepartynet
       cd privatepartynet/relay
       sudo sh setup-ubuntu.sh

3. The script prints an address, a secret and a certificate fingerprint. In PrivatePartyNet, as the owner, open the
   control panel, Relay server, and enter them. The setting is kept with the server; members receive it (signed by your
   PC) the next time they connect.
4. If your provider has a firewall of its own (a "security group"), allow UDP 3479, TCP 443, UDP and TCP 3478, and UDP
   49160-49200. The script opens them in ufw when ufw is on.

Run the script again after updating the repository; it keeps the secret and the certificate.

### With Docker instead

    cd privatepartynet/relay
    cp env.example .env        # put the secret and the server's public address in .env
    docker compose up -d --build

`docker compose logs ppn-relay` shows the certificate fingerprint on its first lines.

## Changing the secret

Anyone with the secret can use the relay, so change it if it may have got out (a lost laptop, for instance):

1. Make a new one: `python3 /opt/ppn-relay/ppn_relay.py --new-secret`.
2. Put it on the first line of `/etc/ppn-relay/secret` and leave the old one on the second line, so members keep working
   until they have new credentials. Put it in `/etc/turnserver.conf` too (coturn takes one secret).
3. `sudo systemctl restart ppn-relay coturn`, then enter the new secret in PrivatePartyNet.
4. A day later, remove the old line and restart again.

## Files

- `ppn_relay.py`: the relay server. Standard library only; its rules are in `../privatepartynet/relayproto.py`, the same
  file the app uses. `python3 ppn_relay.py --help` lists its options.
- `setup-ubuntu.sh`: the installer for a plain Ubuntu or Debian server.
- `systemd/ppn-relay.service`: the relay's service, running as its own user with only the right to use port 443.
- `systemd/coturn-ppn.conf`: a drop-in that starts Ubuntu's coturn with this configuration.
- `turnserver.conf`: coturn's configuration (TURN REST credentials from the same secret, limits, no relaying to private
  networks). The installer adds the secret and the public address to its own copy.
- `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `env.example`: the same with Docker.
