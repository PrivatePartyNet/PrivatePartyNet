# PrivatePartyNet 4: dedicated servers, the server mesh, storage control, devices, every platform

last edited: 2026-11-05. Written by the lead (Claude Opus 5.5) for K's review. Nothing in it is built.
Requirements (K's words, verbatim): `SPEC-2026-11-05_Dedicated-server-and-mesh.md`. Research behind each part, filed in full:
`2026-11-05_Mesh-sync-and-hashing.md`, `..._Multi-device-identity-and-DMs.md`, `..._Client-on-every-platform.md`,
`..._Dedicated-server-service-and-keys.md`. The Discord plan is separate: `PLAN-2026-11-05_Discord-bridge.md`.

## 1. Words used here

- Server: a community (name, owner, members, roles, channels, chat, pictures, files), as today.
- Person: one human, with one account key and any number of devices.
- Device: one install of the client (a PC, later a phone), with keys of its own, signed in under a person.
- Node: any running copy that serves a server: a client that hosts (as today), a dedicated server, or a member's client
  whose owner switched on "help serve this server". A node can serve many servers and a server can have many nodes.
- Dedicated server: the PPN node app with no player at it, running as a system service, with a console.

## 2. What changes underneath: every server becomes a signed log

Today the host's PC is the server: it holds the only full copy and decides everything. That cannot work with several nodes
taking messages while apart, so the history becomes a log every node can hold and check.

- Every change is an event: a message, an edit, a deletion, a picture or file, a channel added, renamed or removed, a role
  given or taken, a ban, a setting, a node granted or removed, a storage rule.
- An event is signed by the device of the person who made it, named by the SHA-256 of its contents, and lists the newest
  events its author had seen (its parents). The events form a graph, as Matrix's rooms do; any node can take a new event and
  pass it on, and every node checks the signature and the author's right to do it before keeping it.
- Each event names its purpose inside what is signed (a message, a role change, a node grant), and keys name what they are
  for. Matrix's 2022 cross-signing attack came from ids and keys that could stand in for each other; PPN signs the kind of
  every statement so one can never pass for another.
- Display order inside a channel: by depth in the graph, then time, then id, so every node shows the same order.

### Who may do what, when nodes disagree

The rules for roles, bans and channels are the hardest part. Matrix had state resets for years (version 1 let banned users
back in; a 2025 fix, CVE-2025-49090); p2panda and Keyhive publish their rules and test every delivery order. PPN's rules:

- The owner can do everything; admins as today (let people in, remove members and guests, manage channels, switch members
  and guests); the owner alone makes admins, grants nodes and sets the server's storage limits.
- Strong removal: when someone loses a power, whatever they did with it at the same time as the removal (concurrently, not
  after it in the graph) is cancelled too. Two admins removing each other at once both go (p2panda's tested behaviour).
- Ties between actions that are concurrent and equal in rank are settled by event id, so every node reaches the same answer.
- The rules are one pure function from the authority events to "who holds what". Its tests run every case in every delivery
  order, including backdated events from a hostile node (arXiv 2604.23560's attack).

### Moving today's servers over

A 3.x server's saved chat, roles, channels and files are turned into a starting log signed by the owner, once, when the
owner's PC first runs 4.0. People keep their ids, roles and history.

## 3. Nodes and how they share a server

- Granting a node: the owner (or an admin, if the owner allows it) approves a node the way a join request is approved today,
  or pastes the pairing code the node's console shows. The grant is an event naming the node's key and its kind:
  - Full (K's choice as the normal case): the node gets everything any member of that server can see, and can act for the
    server while no owner is online: take join requests by the owner's rules, keep the game network's directory, run calls.
  - Storage only (later): the node keeps sealed copies it cannot read. This needs channel encryption with a group key
    (none exists today; Keyhive's BeeKEM and MLS are the models), so it comes after everything else.
- Direct messages never go to nodes (K: DMs stay on people's own devices).
- Members connect to whichever node they reach best; any node can take their messages.
- Live things (who is online, voice and screen signalling, the game network's directory) need one coordinator at a time: the
  "live node", which every node and member picks the same way from the nodes online (the owner's order first, then the most
  reachable, then the lowest id). If it drops, the next one takes over; people in a call reconnect within seconds.
- A member's own PC can help serve (K: opt-in per person, within their own caps). It then answers other members' catch-up
  and file requests for that server, nothing more; it gets no powers.

## 4. Catching up: the hashing system

K: "find who is serving the servers it has connected to or is responsible for, figure out who has the latest file images,
and happenings transcript, and then update accordingly."

1. Find the nodes: the node grants in the server's own log (with each node's last addresses), signed address records on the
   BitTorrent DHT (PPN already uses BEP 44 records to find hosts), nearby PCs on the LAN, and what other nodes report.
2. Compare: for each channel, and first for the authority events, two nodes compare their sets with range-based set
   reconciliation (Negentropy, as Nostr relays use, NIP-77): they swap fingerprints of ranges, split only the ranges that
   differ, and end knowing exactly which events each lacks. A node back after weeks finds the gap in a few round trips, and
   the traffic grows with the gap, not with the history. Fingerprints are kept in the store so nothing is rescanned.
3. Fetch and check: missing events come from whichever nodes have them; each is checked (signature, parents, the author's
   right) before it is kept. Authority events first, so later messages are judged by the right rules.
4. Files and pictures: each is named by the root of a SHA-256 hash tree over 16 KiB blocks (BitTorrent v2's layout, BEP 52).
   A node or member fetches blocks from every node that has the file at once, checks each block against the root as it
   arrives, and resumes where it stopped. Nodes advertise which files and blocks they have. Identical files are stored once.
5. Deletions travel as signed events; a node that hears one removes the content and keeps only the fact of the deletion, so
   it passes the deletion on and never serves the content again. A file goes once no remaining event uses it.

Limits on sync (K: both members and node operators set them): upload and download caps for chat sync and for file sync
separately, per machine, plus per-server caps on a node; optionally hours when file sync may run. Voice, screens and the game
network are never slowed by these caps.

## 5. Storage: one folder by default, split anything out later

K: "it should default to a monolithic folder setup, but an owner should be able to parse any of the type of files stored, or
any of the channels/servers apart as and when needed."

- Default: everything in the one PrivatePartyNet data folder, as today.
- Rules form layers, each layer overriding the one above for what it covers:
  machine default, then a server, then a channel; and across all of them, a kind: the chat database, pictures, other files,
  and cache (thumbnails and partial downloads).
- Each layer can set: its folder (any drive), a size cap, a clean-up rule (keep everything, delete older than N days, delete
  oldest first at the cap, or largest first at the cap), and whether this machine keeps copies at all (a member can keep only
  what they look at).
- Moving a layer to a new folder copies, checks every file by hash, switches over, then removes the old copy; it can be
  stopped and resumed.
- Two kinds of rule, kept apart in the interface:
  - The server's rules, set by the owner and admins, apply on every node: the largest file, the server's total cap, and a
    per-member quota.
  - This machine's rules, set by whoever runs the machine: folders, local caps, clean-up, and sync caps. Clean-up on one node
    removes only its own copy; the file stays available from the others.
- A node can be marked as keeping everything (a durable node). The owner's console warns when no online or recent node keeps
  a full copy of a server.
- A storage page on every client and console: use per server, channel and kind, where each lives, and the largest files.

## 6. The dedicated server app and its console

- One program on every system: the PPN node with no window, running as a service: a Windows service, a systemd unit on
  Linux, a LaunchDaemon on macOS (registered with SMAppService, which asks for approval). The console's tickbox "Start this
  server when the computer starts" switches that on and off.
- Its keys without anyone signed in: Windows DPAPI under the service's own account; Linux a systemd encrypted credential
  (sealed to the TPM2 chip where there is one); macOS the system keychain (to check). Where none exists, a key file readable
  only by the node's account, and the console says so plainly.
- The console is a set of pages the node serves to 127.0.0.1 only, opened in a window on that machine (or a browser there).
  Anyone using the machine can use it (K's rule), checked by the operating system (a local socket or pipe only that machine's
  users can open).
- From a client (K: "open dedicated server console" for owners and admins): a menu item opens the same pages through the
  client's signed-in connection. Two levels (K's choice): node admins, named in the console, control the whole node; a
  server's owner and admins control only that server on it.
- Pages: Overview (servers served, who is on, sync state, bandwidth, disk); Servers (add by pairing code or grant, remove,
  the owner's order for live nodes); Members and roles (appoint admins and guests, including people who are offline, from
  everyone who has joined; bans); Storage (section 5); Sync and bandwidth; Network (ports, relay, how reachable); Node
  admins; Logs; Updates; Startup.
- It looks like the client: dark by default, the same accent colour, text size and spacing settings.

## 7. Owning an account: the account key, devices, backups only you can read, recovery

K's criteria (round 4): a key that proves a person owns their account and data and can also encrypt their messages; backups
a dedicated server keeps that only the person's own clients can read; current best practice; easy; hard to lose for good.
Research: `2026-11-05_Account-keys-backup-and-recovery.md` (Proton, Signal, WhatsApp, WebAuthn PRF, Juicebox, Matrix
dehydrated devices, social recovery) and `..._Multi-device-identity-and-DMs.md`.

### 7.1 The account key

- Each person has one account secret: 32 random bytes made on their first device. Nothing a person types or holds is turned
  into it; it is only ever wrapped, so it can never be guessed from a weak password.
- From it come two keys:
  - The account signing key. It is the person's ownership: it signs their list of devices, their recovery settings, and every
    role a server gives them (an owner's ownership of a server is tied to it, so recovering the account keeps the servers).
  - The account data key. It encrypts everything kept about them elsewhere: backups, the DM mailbox, saved settings.
- Today's keys become the first account key and first device, so nobody's id, contacts or roles change.

### 7.2 Devices

- Each device has its own keys. The account signing key signs the list of devices, with a version number; the list goes out
  with DMs and in the person's DHT record. Contacts pin the account key, as they pin keys today. Every device shows a notice
  when the list changes, and the safety number covers it (WhatsApp, OSHI).
- Linking a new device from one you already have: the new device shows a QR code (its keys and a one-time address) that the
  old one scans, or the person types a short code that goes through SPAKE2 (a password-authenticated key exchange; Python's
  spake2 0.9). Both screens then show the same check words. The old device signs the new one in and sends the account secret,
  the DM history and settings directly.
- A DM is sealed to every device of the other person and to the sender's own other devices. A person's devices keep their DM
  history in step whenever they meet, with the reconciliation in section 4.
- Removing a device you still trust (sold after a wipe, retired): a new list without it.
- Removing a lost or stolen device: a new list without it and a new account secret, signed over by the old account key and
  wrapped again under the remaining unlockers, so the lost device can read nothing new. A thief holding the old secret could
  sign a competing change; contacts who see two such changes mark the account "check the safety number" until the person
  confirms in person or by a call. Each device keeps the account secret behind the system's protected storage and PPN's own
  lock (PIN or the system's face or fingerprint), so a stolen, locked device does not give it up easily.

### 7.3 Unlockers: many ways to open the same account, any one is enough

The account secret is stored only wrapped, separately under each unlocker, so losing one never loses the account (Proton's
model, and what the WebAuthn spec's co-editor recommends over passkey-only designs):

1. Each signed-in device (on by default): wrapped by the system's protected storage (DPAPI on Windows, the Keychain on Apple,
   the Keystore on Android, the Secret Service on Linux). Any device you still have can add a new one.
2. A recovery phrase (on by default): 24 words shown once at setup, with a check that you wrote them down, and a printable
   sheet. Restores everything on a brand-new device with no other device left.
3. Trusted friends (optional): pick, say, three people in PPN; any two can together approve a new device for you. Their apps
   hold only pieces of a recovery key (Shamir sharing), never the account. A friend-approved recovery waits 72 hours, and
   every device you still have is told and can cancel it; changing your friends list waits too (Argent's protection, with
   the waiting period itself protected).
4. A passkey (optional, later): face or fingerprint through Apple, Google or Windows Hello (all three support the WebAuthn
   PRF extension as of 2026). It needs a project web domain for the passkey to belong to.
5. A PIN guarded by several nodes (optional, later): Juicebox's method. Pieces sit on several independent nodes (your own and
   friends'), each counting wrong guesses and destroying its piece after a few; no single node can even test a PIN. This makes
   a short memorable PIN safe without any company server.

PPN keeps count: with only one unlocker it shows a warning on the account page until there are two.

### 7.4 Backups and the DM mailbox on your own node

- Backup: each device seals the person's data (DMs, contacts, settings, the wrapped unlockers) with the account data key and
  sends it, in pieces named by their hashes, to the backup holders the person chose: their own dedicated server by default,
  and any friend's node that agreed. A holder stores pieces it cannot open; it learns sizes and times, nothing else.
- DM mailbox (Matrix's "dehydrated device"): the person's own node holds a mailbox device listed in their device list. Its
  private key is sealed with the account data key, so the node cannot use it. Senders seal DMs to it like any device, and the
  node keeps them until one of the person's devices collects and opens them. An iPhone opened after a week gets everything
  from there, even with the PC off.
- Finding your backups on a new device: the account signing key signs a record on the DHT naming the holders, so the recovery
  phrase alone (or friends, or a passkey) is enough to find and open everything.

### 7.5 Losing everything

- The account is gone for good only if every device, the recovery phrase, every passkey and enough trusted friends are lost at
  once. With the defaults (devices plus the phrase), losing a phone and a PC in one fire still leaves the phrase.
- Any recovery that did not come from an existing device is announced to every remaining device and to contacts (the safety
  number changes), so a takeover cannot happen quietly.
- Forward secrecy is still missing in today's DMs (identity.py says so); a ratchet can be added in this step or later.

## 8. Every platform

What the research found decides most of this:

- The dedicated server is plain Python with no window engine: Windows, Linux (x64 and ARM, Raspberry Pi included) and macOS.
  Its console opens in a browser where there is no window engine.
- The Windows client stays on WebView2.
- Linux client: WebKitGTK (pywebview's GTK backend) ships without WebRTC in distribution builds, and its maintainers plan to
  turn LibWebRTC on in 2.56, about March 2027. So the Linux client needs Chromium: Electron, or Qt WebEngine.
- macOS client: WKWebView gives the microphone but refuses screen sharing without a private API. So Chromium again
  (Electron or Qt WebEngine), or native capture fed into WebRTC.
- One Chromium shell for Linux and macOS is simplest. A short trial decides between Qt WebEngine (stays in Python; screen
  picking is newer and failed once on macOS 15) and Electron (best-trodden; adds Node beside Python).
- The game network: wireguard-go on a TUN device on Linux, a utun or a Network Extension on macOS (a paid Apple developer
  account for the extension), Android's VpnService (WireGuard's own Apache-2.0 library), iOS's packet tunnel (paid account).
- Android: workable. A foreground service keeps PPN connected (type specialUse, which Google Play reviews; F-Droid or a
  direct download avoids that), MediaProjection shares the screen, VpnService carries the game network.
- iOS: an iPhone app cannot keep a connection while closed, and only Apple's push service can wake it; Apple lets only the
  app's publisher send those pushes (their team's key). K chose: an iPhone gets messages only while PPN is open, with no
  wake-up service. The DM mailbox (section 7.4) means it still gets everything the moment it opens.
- Phones in Python are possible (BeeWare's Briefcase with Chaquopy; cryptography has iOS wheels since late 2025), but the
  window, voice and screens on phones are native work either way. Deciding the phone toolkit waits for the device framework,
  as K allowed.

## 9. Order of work

Each step ends tested, with nothing released until K says so.

0. Three short trials: a Windows service inside the PyInstaller build; Qt WebEngine against Electron for voice and screens on
   Linux and macOS; reconciliation speed in Python on a million events.
1. The signed log, the authority rules (tested in every order), and moving 3.x servers into it. One node, as today. Today's
   keys become account keys here (section 7.1), so every event is signed under the account from the start.
2. Several nodes: grants, catch-up (reconciliation, files by hash tree), the live node, sync caps. Version 4.0.0 at the end
   of this step, because 3.x copies cannot join a server that runs this way.
3. The dedicated server app: service, keys, console (on the machine and from a client), appointing admins.
4. Storage layers and moves, on clients and nodes.
5. Accounts with several devices: linking, the recovery phrase and trusted friends, backups and the DM mailbox on your own
   node, DMs on every device. Passkeys and the guarded PIN after that.
6. Clients on Linux and macOS.
7. Phones: Android first, then iOS.
The Discord bridge (separate plan) runs on a node, so it can start after step 3.

## 10. Questions for K

Answered (round 4): iPhones get messages only while PPN is open; a dedicated server may keep backups and DMs that only the
person's clients can read; the account key and recovery follow the research (section 7).

Still open:
1. Storage-only nodes need channel encryption, a large piece of work. Is it fine for that to come last?
2. Is 4.0.0 not joining 3.x servers acceptable, given hosts and members already need matching versions?
3. Linux and macOS clients: should the trial pick between Qt WebEngine and Electron, or do you have a preference?
4. Section 7: are the defaults right (every device plus a 24-word recovery phrase; trusted friends optional; passkeys and a
   guarded PIN later)?
