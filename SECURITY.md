# Security

## Reporting a problem

Please report a security problem privately, so it can be fixed before it is public. On GitHub, open this repository's Security
tab and choose "Report a vulnerability". Do not open a public issue for it.

Include the version (Info, About), what you did, and what you saw. A reply should come within a few days. PrivatePartyNet is a
small project looked after by a few people, so a fix may take longer than that.

## What to know about how PrivatePartyNet is built

PrivatePartyNet is made for groups of friends who trust each other. Read this before using it with people you do not know.

Who can read what:

- A server's chat is stored on the host's PC, which relays it, so the host can read every channel of that server. Direct
  messages are different: each one is sealed to the other person's key and signed by the sender, and only the two PCs can
  read it.
- Chats, pictures, contacts and keys are encrypted on disk. The key is protected by Windows for the signed-in user, and a
  password can be added on top. This protects the files from other Windows accounts and from a copied disk. It does not
  protect against malware running as you, which can ask Windows for the same key.
- A forgotten password cannot be recovered. Backups are sealed with a passphrase chosen at export time, and the same
  applies to them.
- Screens and voice go directly between PCs over WebRTC, which encrypts the media (DTLS-SRTP). The host relays only the
  connection setup. When two PCs cannot reach each other and the group runs a relay server, the media goes through the
  coturn beside it, still encrypted end to end.
- A relay (a member who turned relaying on, or the group's relay server) passes on WireGuard and QUIC traffic that is
  encrypted end to end, and cannot read or change it: not the game network's addresses, not what games send, not the
  party's pages. It sees the public addresses of the members using it, how much they send and when. A member's PC
  relays for others only if that person turned it on, and only up to the limit they set. Only members can use it: the owner's PC gives each one credentials that last six hours, and what a
  member sends to a relay proves they hold them without showing them. Anyone with the relay server's secret can use
  that server, so the owner keeps it to themselves and changes it if it may have got out (relay/README.md).

Who can join:

- Nearby PCs join by asking the host, and the host presses Let in. The link that results works once and expires after two
  minutes.
- From outside the network, a person needs an invite code and the server's password, and the owner or an admin still has to
  approve the request. The invite carries the host's certificate fingerprint, and the joiner checks it.
- Roles are Owner, Admin, Member and Guest. A removed person can be banned, and a banned person cannot ask again.

What leaves your PC:

- A lookup of your public address through public STUN servers, a request to your router to open a port (UPnP), and signed
  address records on the public BitTorrent DHT while an invite is active. These records contain your public address and port,
  signed with the server's key, and anyone watching the DHT can see them.
- A relay, only when the group set one up or a member turned relaying on (both are off until someone chooses them).
- With Automatic updates on (the default), a check of PrivatePartyNet's releases on GitHub (api.github.com) shortly
  after the app starts and every six hours, and the download of a newer version from github.com. GitHub sees your
  public address and that you run PrivatePartyNet. With Automatic updates off (Options), GitHub is only asked when you
  press Check for updates.
- There are no accounts. The app sends nothing to a company server.

How updates are checked:

- Each release carries `PrivatePartyNet-<version>-update.json`, naming each file with its size and SHA-256, and an
  Ed25519 signature of it. The public key is built into the app; no setting, environment variable or command line can
  change it, or the address updates come from. The private key is kept by the maintainer, off this repository, sealed
  with Windows data protection.
- The app installs a download only when the signature checks out, its size and SHA-256 are the signed ones (checked
  again just before it runs), its version is newer than the one running (never older) and the manifest's version and
  tag are the release's. It talks HTTPS only, with Windows' certificates, and takes files only from github.com and
  GitHub's download hosts. Release notes are shown as text.
- A signed manifest may name the next signing key, signed by the current one, so the key can be replaced without
  a new install. If the key were lost, installed copies could not be updated; they would keep working as they are.

Known limits:

- On a local network, the party's HTTPS certificate is made on the host PC and signed by nobody, and the app's own windows
  accept it without checking it (Chromium's `--ignore-certificate-errors` is set for them). Anyone who can sit between two
  PCs on a network could read and change what is sent over a local join, so use local joins only on networks you trust.
- The game network adds a virtual network adapter, which Windows only allows with administrator permission. A small helper is
  started with that permission when you turn the game network on, and it ends with the app. It only creates the adapter,
  moves other adapters' priority while the game network is on, and runs WireGuard on it. WireGuard opens no connection of
  its own: its sealed packets go to the app, which sends them to each member. The helper checks what the app gives it, so
  it can only ever reach addresses inside that server's game network.
- Each member has a WireGuard key for each server, kept sealed with the rest of the keys. The host signs the list of
  members and their keys, and WireGuard only accepts from each member packets that carry that member's own address.
- The installer is not code-signed, so Windows may show an unknown publisher.
