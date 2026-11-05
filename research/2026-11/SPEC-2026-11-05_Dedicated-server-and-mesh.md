# Dedicated server, console and server mesh: K's requirements and the draft design

last edited: 2026-11-05. Started by the lead (Claude Opus 5.5) in session SID-20261105-4b7e1c, after the 3.3.0 release.
Status: requirements gathered, research filed, design written (DESIGN-2026-11-05_PPN-4-nodes-mesh-devices.md) for K to review; nothing built.

## K's words, verbatim, in order (November 5th)

1. "we need to develop a dedicated server console companion for the application as well."
2. "give it a nice ui, and the ability to appoint admins, have good, granular control over where files are stored, etc."
3. Answers to the first four questions:
   - Where it runs: "The client should be able to host, but the dedicated server should also be able to host. A admin or owner
     should be able to give a dedicated server permission to obtain the complete server files, and server everything up. I want
     it the dedicated server and client to both be able to run on as many operating systems as possible. The dedicated console
     ui should have a tickbox to run the server on system startup."
   - How the console is reached: "Should be reachable from a client (open dedicated server console) if the client is an
     owner/admin. Of course the console itself should be able to be interacted with by anyone using that machine"
   - Servers per dedicated server: "one to many, and many to many. We want this to be a hive/mesh type setup, where a dedicated
     server acts as a durable server node, and if others have been offline, it automatically updates the others when the others
     come online with what they missed. Users (client users) and console/dedicated owners should both be able to set parameters
     about max download/upload for file and chat syncing as well. We need a complex hashing system for a client or dedicated
     server console that has been offline for sometime to come back online, find who is serving the servers it has connected to
     or is responsible for, figure out who has the latest file images, and happenings transcript, and then update accordingly.
     let me know if any of this begs further questions"
   - File storage control: picked all four (folder per server, folder per channel, pictures apart from files, quotas and
     clean-up).
4. "on the "Which kinds of control over where files are stored do you want? Pick every one that applies.", it should default to
   a monolithic folder setup, but an owner should be able to parse any of the type of files stored, or any of the
   channels/servers apart as and when needed. Be sure that the default setup is granular enough so that a client or dedicated
   owner can change how things are stored on their machine at any time. make plenty of configuration settings to ensure plenty
   granularity of control is given to dedicated operators and server clients alike"

## What exists today (3.3.0), checked in the code

- One host PC is the whole server: `server.Party` (server.py) keeps members, roles (`host`, `admin`, `member`, `guest`),
  channels, chat, pictures and files in the host's encrypted `Store`, and serves the room over HTTPS. The host is a member
  record with role `host`. `set_role` works only on members who are online (by member id), owner makes admins, admins switch
  members and guests.
- The app (`app.Controller.start`) hosts one server per PC (`server_id` in settings), keyed to the PC's `Identity`, with a
  `Vault` whose key Windows protects (DPAPI) for the signed-in user.
- Members reach the host on the LAN, by IPv6, by an opened router port, by hole punching (signed records on the BitTorrent DHT,
  QUIC), or through relays. Voice and screens are WebRTC between members, signalled through the host. The game network is
  WireGuard between members, with the host keeping the directory.
- Windows only: WebView2 window, DPAPI, dxcam capture, Wintun adapter.

## Draft design (the lead's proposal, for K to confirm)

- Words: a server is the community (name, owner, members, channels, chat, files). A node is any running copy that serves it:
  a client that hosts (as today) or a dedicated server. A node can serve many servers, and a server many nodes.
- The history becomes a signed event log per server: each message, edit, deletion, file or picture, role change, channel
  change, setting and node grant is an event, named by the SHA-256 of its contents, signed by its author, and naming the
  latest events its author had seen. Any node can take new events; an event counts only if its author had that power when
  it was made (owner over admins; a fixed order breaks ties).
- Catching up: two nodes compare summaries (a hash tree over each channel's events by time) and exchange only what one lacks,
  so a node away for weeks finds the gap in a few round trips.
- Files and pictures: named by their SHA-256, split into 1 MiB pieces under a hash tree, fetched from every node that has
  them at once, each piece checked as it arrives (as BitTorrent does). Deletions travel as signed events.
- Finding who serves a server: signed DHT records listing its nodes (the DHT is already used), each node's own list, LAN
  discovery, and invites that name several nodes.
- Live things (voice, screens, who is online, the game network's directory) are coordinated by one node at a time, chosen
  the same way by everyone from the nodes online (owner's priority first, then how reachable).
- Granting a dedicated server: the owner or an admin approves it, as a join request is approved today, and it then pulls
  the complete history and files.
- Limits: each machine (client or node) sets upload and download caps for chat sync and for file sync. Live voice and
  screens are not throttled by them.
- Storage, per machine: one data folder by default; any server, channel or kind of file (pictures, other files, chat,
  cache) can be split out to its own folder with its own cap and clean-up rule, and moved later with every file checked.
  Quotas per member and per server; clean-up by age or oldest first.
- Console: pages served by the node on 127.0.0.1, in a window on that machine, and opened from the PPN client by those
  allowed. A tickbox starts the node with the system (Windows service or scheduled task, systemd on Linux, launchd on
  macOS).
- Version 4.0.0: the protocol changes, so 3.x copies cannot join servers that run this way.

## Questions asked on November 5th (answers go below)

See the conversation. Answers:

Round 2 (10:1x), K's answers verbatim:
- Node trust: "Owner chooses per node, assuming full permission from the owner, the dedicated node should get everything that's "public" to anyone in that server. Direct messages should be stored by users on their own pc's only so those stay private."
- Members' PCs passing history and files on: "Yes, opt-in per person (Recommended)".
- Console control: "Two levels (Recommended)" (anyone at the machine plus node admins they name control the whole node; a server's owner and admins, from their client, control that server on it and nothing else).
- Platforms: "Everything at once, it would be good to add phones too....for this purpose, we need to develop some kind of system so that a single user's pc and phone can talk to each other, and each can store all of their dm's, since those are the largest security concern and most touchy with regard to trust. Feel free to hold back on these particular topics as needed while we develop a better framework about how to authenticate a single user to their dm's and personal information/claim to the account securely, using our open-source open-platform model. feel free to conduct background research as needed as well"

What follows from round 2:
- A node granted full permission receives everything visible to every member of that server. Direct messages never go to nodes; they stay on the two people's own devices.
- One person, several devices (PC and phone): each device keeps all of that person's DMs. Needs a framework for linking devices to one identity and proving the account is theirs, open source and on every platform. K allows holding back on phones and multi-device DMs until that framework is designed. Research first.

Round 3, K's answers:
- Research: "I research it myself" (the lead researches in its own turns; no subagents, no Gemini handoff).
- Build: "Wait for the research" (build nothing until the research is in and K has reviewed a full design).

Research plan (the lead's), each topic filed in full under this folder with an index line:
1. One person on several devices: linking a phone and a PC to one identity, every device keeping all DMs, proving the account
   is theirs, recovery and revocation (Signal linked devices and Sesame, Matrix cross-signing and key backup, MLS, Keybase
   sigchains, Threema multi-device).
2. The mesh: signed event logs and authority (Matrix event DAG and state resolution), set reconciliation by hashes
   (range-based set reconciliation, Negentropy), per-file hash trees and swarm downloads (BitTorrent v2), append-only logs
   (Hypercore, Scuttlebutt), bandwidth caps.
3. The client on macOS, Linux, Android and iOS: window engines and WebRTC (pywebview backends, WKWebView, WebKitGTK,
   QtWebEngine, Tauri, Electron, Capacitor, Flutter), screen capture on each system, the game network on each (TUN, VpnService,
   NetworkExtension), phones in the background, app-store rules.
4. The dedicated server on every system: running as a service at startup (Windows service, systemd, launchd), protecting its
   keys without a signed-in user.

Round 4 (about 10:47), K's answers to the design's questions, verbatim:
- iPhone: "Only while open (Recommended)" (no wake-up service; an iPhone gets messages when PPN is opened).
- DM mailbox: "We can have backup done by a dedicated server, but this should be done in a way where only the client can read it....this might go back to the bit i mentioned earlier about securing/authenticating a user to their account. We might need some kind of key or something for a user to have to invoke ownership rights over their account and data, which may also be the key that encrypts or decrypts their messages or something. Whatever the latest greatest recommendations are, without making the process too difficult, making it too easy to lose an account permanently, etc."
- Recovery: "read my above input re dm mailbox, and do a round of research. come up with the bet possible solution for the design criteria i laid out"
Criteria from these: one key (or key system) that proves ownership of an account and unlocks its data; backups held by a dedicated server that only the person's own clients can read; current best practice; easy to use; hard to lose permanently.
