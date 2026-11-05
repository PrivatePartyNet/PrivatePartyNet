# November 2026

Status (November 5th): the plans below are proposals under review by K. Nothing in them is built. Questions still open are at
the end of the design and of the Discord plan.

## Plans

- `SPEC-2026-11-05_Dedicated-server-and-mesh.md`: K's requirements for PrivatePartyNet 4, in his own words, with his answers
  to each round of questions: a dedicated server with a console, many servers per node and many nodes per server, catching up
  by hashes, storage control, two levels of console control, every platform, DMs only on a person's own devices.
- `DESIGN-2026-11-05_PPN-4-nodes-mesh-devices.md`: the design for review. Every server becomes a signed log; authority rules
  for owners and admins tested in every delivery order; nodes and the live node; the catch-up system (range-based set
  reconciliation, files in hash trees); storage layers; the dedicated server app and its console; the account key, devices,
  backups and recovery; every platform; the order of work.
- `PLAN-2026-11-05_Discord-bridge.md`: moving a Discord server's history and files into PrivatePartyNet, a bridge that lets
  Discord users take part in text channels, and onboarding without a Discord account.

## Research (all November 5th)

- `2026-11-05_Mesh-sync-and-hashing.md`: Matrix's event graph and state resolution, Negentropy (NIP-77), Briar's sync,
  BitTorrent v2 hash trees, Hypercore, Willow and Meadowcap, Keyhive and p2panda-auth.
- `2026-11-05_Multi-device-identity-and-DMs.md`: Signal's Sesame and linked devices, WhatsApp's signed device list, the OSHI
  review of multi-device pitfalls, Matrix cross-signing and its 2022 attack, Keybase, Threema, MLS, pairing with SPAKE2 or
  CPace.
- `2026-11-05_Account-keys-backup-and-recovery.md`: Proton's recovery methods, Signal's backup key, WhatsApp's passkey
  backups, WebAuthn PRF support, Juicebox, Matrix's dehydrated devices, social recovery with a waiting period.
- `2026-11-05_Client-on-every-platform.md`: WebRTC in WebKitGTK and WKWebView, Qt WebEngine, Electron and CEF, Android's
  WebView and foreground services, iOS background limits and push, WireGuard on each system, Python on phones.
- `2026-11-05_Dedicated-server-service-and-keys.md`: starting with the system on Windows, Linux and macOS, and keeping keys
  with nobody signed in (DPAPI for services, systemd credentials, keyrings).
- `2026-11-05_Discord-bridge-and-migration.md`: Discord's developer rules, exporting with a bot, the history API and
  expiring attachment links, other importers, mautrix-discord's webhook relay, DAVE voice encryption, invite links.
