# PrivatePartyNet 3.4.0

A private place for a group of friends to chat, talk, share screens and play LAN games together. One person hosts a server, PCs on the same network find it by themselves, and friends elsewhere join with an invite code. There is no company server and no account to make. Traffic goes through someone else's machine only when two routers let nothing through, and then only through a relay the group chose, encrypted end to end.

## Overview

- One window, as Discord has, and never a second one (starting the app again brings it to the front): a rail of servers down the left edge (Home, your own server, every server you joined), with the room shown beside it. A server stays on the rail for good until you leave it, and the app reaches every one again by itself each time it starts; one that is off or out of reach is dimmed, and its place says so with Try again, its saved messages, and Leave. A server stays connected while another one or Home is shown, so a call goes on, with its Mute, Deafen and Leave at the foot of the rail. Each server's button shows its icon, unread messages, people waiting to join and the call. Right-click leaves a server for good (asked once more). About and How open over the window.
- Your server: name it and press Create your server on Home. Your PC runs it, it starts with the app (unless you untick that), and Stop server turns it off without taking it off anyone's list. Other PCs see it on their Nearby PCs page, press Ask to join, and the owner or an admin lets them in; after that they come straight back in. The join link works once and expires after two minutes.
- Servers and channels: a server keeps its name, its icon, its channels, its roles and its chat between runs. The owner and admins rename it and give it an icon (Server settings, in the menu under its name). It starts with a text channel and a voice channel; the owner and admins add, rename and remove channels. A text channel's chat fills the middle of the room when you pick it; a voice channel shows its screens there instead. Anyone can mute a text channel for themselves (right-click it), which stops its unread count. Anyone who joins later reads the history, and older messages load as you scroll up. The newest 200 messages of each channel are sent when you connect.
- Roles: Owner (the PC that hosts), Admin (lets people in, removes members, manages channels, names the server), Member, and Guest (reads the chat, may join a voice channel to watch and listen, cannot post, talk or share).
- Pictures of people: choose yours on Home (PNG, JPEG, GIF or WebP up to 2 MB, kept as it is). It shows beside your name on every server you are in: in the member list, beside your messages and in voice. Roles are remembered for each person, and a banned person cannot ask again.
- Pictures and files in chat: drag pictures onto the window (anywhere in it; up to ten at once, one message each) or paste one into the message box. It is sent at its own size and its pixel dimensions are never reduced. It is saved again first, which removes what the file recorded about where and when it was taken, and a large file is saved at a slightly lower quality if it needs to fit. PNG, JPEG, GIF and WebP up to 12 MB.
- Files: drag any file onto a room to send it, with its progress and Stop; the host keeps it, encrypted, and everyone sees a card with Save (Windows' Save dialog; a file is never opened in the window). Under the server's name a meter shows how much the host keeps, against the cap or the disk's free space. The owner and admins set the largest file and the cap for all files (each none or a size; the cap at least ten times the largest file; pictures count). The sender, the owner and admins can delete a message, and its file or picture goes from the host too.
- Sounds: for joining and leaving voice (yours and others'), mute and deafen, messages you are not looking at, and someone asking to join. Settings has the volume and a switch for each. The sounds are public domain (CC0), from Kenney.
- Voice: join a voice channel (six people at most). Echo cancellation, noise suppression, mute, deafen, push to talk (hold V while the window is in front), microphone and speaker choice, a volume slider for each person, and a speaking light. Voice runs on the same direct connections as the screens, at 32 kbps with silence not sent.
- Direct messages: write to anyone on your network with no server needed. Each message is sealed to the other person's key and signed, kept on both PCs, and sent again until it arrives if they are away. A safety number lets two people check that nobody is in between.
- Joining from outside: the host copies an invite code (name, password, where to try, and the host's certificate to check). The guest pastes it, the owner or an admin sees the request and lets them in. The app asks the router to open the port where it can. When nothing can reach the host directly (carrier-grade NAT, as on Starlink), the two PCs swap their addresses through signed records on the BitTorrent DHT, IPv6 included, and open both routers or firewalls by sending at the same moment (hole punching), then carry the server's connections over QUIC. Where that cannot work (two routers that both give a new port for every destination), the invite goes through a relay instead, if the group has one; without one the app says so.
- How members reach each other: every pair tries every way at once and uses the best that works, in this order: the same network, IPv6, a port the router opened, hole punching through both routers, a member's PC that relays for others (only if that person turned it on, up to a limit they choose), and last a relay server the group runs itself. A connection keeps checking and moves to a better way as soon as one answers. IPv6 has no shared addresses, so it gets past carrier-grade NAT where both PCs have it, as most such providers give. A way is used only once it has carried a full-size packet.
- Relays: a relay only ever passes on what is already encrypted (WireGuard, QUIC, DTLS-SRTP) and cannot read or change it. The group's own relay server runs on a small rented Linux server: `relay\setup-ubuntu.sh` sets it up on Ubuntu 24.04 (`relay\README.md` says how, from renting the server on). It also carries voice and screens (TURN) when nothing else can. The owner enters its address, secret and certificate fingerprint once; the secret stays on the owner's PC and members get credentials of their own that last six hours.
- Game network: each server is also a private network, so games that play over a LAN work between its members. Pick a server on the Game network page and press Use for games; this PC gets that server's game address (a green light shows which one), and games see an ordinary network card, marked as a Private network. Packets go straight from member to member, sealed by WireGuard, over the best way between them (above), not through the host; someone who is removed or banned is cut off by everyone at once. Broadcasts and multicast, which many games use to find each other, reach every member. Windows' own services (file and printer sharing, Remote Desktop, remote management, network discovery) are closed to members until you open a group under What members of your servers can reach on this PC. The adapter's packets are at most 1270 bytes, so that one carried by a relay over IPv6 still fits a 1400-byte path. Only game traffic for that server goes over it, never internet traffic. Windows asks for permission once each time it is turned on, because adding a network adapter needs it.
- Encrypted storage: chats, pictures, contacts and keys live in your PrivatePartyNet folder, encrypted with a key that Windows protects for your sign-in; a password can be added. Backups are one compressed file sealed with a passphrase you choose, and importing only adds what is missing.
- Go Live: share a screen with the people in your voice channel; screens belong to a voice channel, and only the people in it see them. Join one first; leaving or moving to another ends what you shared there. Each person can share up to four screens at once, chosen from the Screen menu. Choose the quality ceiling (Source, 1080p, 720p, 480p) and the frame rate (15, 30, 60) first. The mouse pointer is drawn into the picture by default and can be hidden.
- Receive size: each viewer picks how large each picture is. Fit my window follows the window, including maximized and minimized. The 100%, 75%, 50% and 25% choices set the tile to that share of the sender's own size and ask the sender for a picture of that size.
- Upscale: draws each received picture smoothly at the size of its tile, with a live readout of what that costs in processor time.
- Tile controls: point at any screen and its own settings and fullscreen buttons appear. Each one applies to that screen only. Settings on a received screen sets its receive size and upscale; on your own screen it sets that screen's quality, frame rate and pointer. Fullscreen makes the window cover the whole display with that one screen (Escape or the button again brings it back).
- The room: laid out the way Discord users expect. The server's name and icon (its menu), channels, the voice band and your name on the left; the channel you picked in the middle (a text channel's chat, or a voice channel's screens, with LIVE marks in the channel list); join requests, your live screens and the members on the right. Clicking a voice channel's name shows it and never joins it; its Join button does. Screens are received only while you look at your voice channel. The message box is the text and Send; pictures are dropped or pasted in, and the window says where a drop will go while files are dragged over it. A button in the top bar hides the member list, and narrow windows fold the side panels over the middle.
- Grid and focus views: screens start as tiles you can move and resize, with snapping to the stage edges and to each other (hold Shift to resize freely). Click a screen to make it big, with the others as small previews along the bottom, as in Discord. Right-click any screen, including your own, to hide it: a hidden screen is a black tile with a link to bring it back, and the sender stops sending it to you.
- Connection bar: the bottom bar shows the app's CPU use, then a chip per person with colored icons for the rates, frame rates and picture sizes that apply. Hover a name for the details, including the CPU spent encoding and decoding for that person, the ping, packet loss and jitter. Crowded bars give up detail instead of overflowing.
- Ping: the members list shows each person's ping beside their name every 2 seconds, with a dot (green under 80 ms, yellow from 80, red from 150 or over 5% loss, grey when not known). The voice channel's cards show it too, and its list in the sidebar has the dot. It is the ping on your own connection to them, or else their ping to the host; your own row is your ping to the host. "unstable" or "reconnecting…" shows instead while a connection is shaky. Hovering says the jitter, loss, the way the connection goes and where the number comes from. A server on the rail gets an amber dot while your own connection to it is unstable or reconnecting. The Game network page of Home lists each member with the game network's own ping to them (for example Alex, direct over IPv6, 23 ms). None of it sends anything extra: the numbers come from the connections, heartbeats and path checks that run anyway.
- Capture at the size that is needed: the screen is captured at the smallest standard size that covers what the viewers asked for (their tile sizes, or a percentage), and at full size again as soon as someone needs it. On the development PC this halved the processor time of sharing an ultrawide screen to a small tile (10.8% to 5.5% of the PC).
- Fast capture: screens are captured through the graphics card (Windows Desktop Duplication) with the older GDI capture as a fallback, and encoded with OpenCV when it is available. An ultrawide screen reaches about 57 frames a second at a 60 target on the development PC.
- Bad internet does not throw anyone out: the host checks on every member every 2 s, and a connection counts as unstable after 4 s without an answer and reconnecting after 10 s. A member whose connection drops keeps their place, role, voice channel and screens for two minutes (the reconnect_grace_s setting), and nobody sees them leave and come back. Their room says "Reconnecting... (n s)" and its place on the rail says reconnecting; the room reconnects by itself: it reaches the host through its own address on this PC, which finds the host again by the best way that works (the host's addresses, IPv6, through both routers, a relay), even after a new address or another Wi-Fi. After 10 s away the app signs the way back in with this PC's identity, so nobody has to approve the member again, and the chat missed meanwhile arrives. Voice and screens find a new way through (an ICE restart) without starting over, and screens are sent smaller while a connection is struggling. The game network keeps its adapter up, falls over to a relay while the direct way is down and goes back afterwards. Past the grace the room says so and offers Rejoin. This needs 3.5 or later on both PCs: with an older version on either side (3.4.0 included) a dropout works as it did before.
- Updates: with Automatic updates on (the default), a newer version is downloaded in the background, checked, and offered in a banner at the top of Home with Restart now or Later (which installs it when the app next closes). Nothing restarts while your server runs, during a call or while the game network is on. It takes the newest release on GitHub, checks each file against the size and SHA-256 GitHub publishes for it, and never installs an older version. Settings turns automatic updates and test versions on or off and has Check for updates; with automatic updates off, a newer version offers Update, Later or Skip this version.
- Fullscreen, Dark (the default), Light or Match Windows, and each person's own accent color, text size and spacing.
- Home: a list of pages (Home, Join a server, Nearby PCs, Game network, Direct messages, Saved chats, Settings), with a count of people waiting to join and a light when a game network is in use. Settings has the appearance, sounds, updates, the data folder, the About page (versions and folders) and the How PrivatePartyNet works page, which is updated with every version.
- Colors were chosen for contrast in both themes (measured; body text is kept below the near-white-on-black that is hard on some eyes), every light also has words, and Windows' reduce-motion setting is respected.

At run time nothing is sent to a company server. The only calls outside your network are a check of your public address (public STUN servers), the router port-open while your server or the game network is running, once an invite has been made, small signed address records on the BitTorrent DHT, the direct connections between members, a relay (the group's own relay server once the owner sets one, or a member's PC that relays for others), and, with Automatic updates on, GitHub: a few minutes after the app starts and every six hours it asks api.github.com for PrivatePartyNet's releases, and it downloads a newer one from github.com. Turn Automatic updates off in Settings and GitHub is only asked when you press Check for updates. The feature set was designed from Discord's screen share and server features.

## Release information

| Field | Value |
|---|---|
| Version | 3.4.0 |
| Platform | Windows 10 or 11, 64-bit |
| License | [Apache License 2.0](LICENSE) |
| Installer | `PrivatePartyNetSetup-3.4.0.exe`, per user, no administrator rights. It updates an existing PrivatePartyNet in place. |
| Portable | `PrivatePartyNet-3.4.0-portable.zip`, run from any folder, including a USB stick |

## System requirements and compatibility

- Windows 10 or 11, 64-bit. The installer carries the Microsoft Edge WebView2 runtime, the Visual C++ 2015-2022 runtime and .NET Framework 4.8, and installs only the ones a PC is missing.
- The PCs must be on the same network: a cable through a switch, a phone hotspot, or a home Wi-Fi network. Some Wi-Fi networks stop devices from seeing each other (client isolation). A cable or a hotspot avoids that.
- Ports: TCP 8765 (your server, changeable on Home), UDP 8766 (finding nearby PCs), and one UDP port of the system's choosing for the game network's paths, which the app asks the router to open where it can. Windows Firewall asks once; allow PrivatePartyNet on Private networks. A group's relay server uses UDP 3479 and TCP 443, and its coturn UDP and TCP 3478 and UDP 49160-49400, on the rented server only.
- A server has room for up to 16 people, including the host. Screens are sent directly between members, so the network needs room for them. Several 1080p screens to many people is a lot of traffic on Wi-Fi; a wired network works best.

## The windows

PrivatePartyNet runs in a native window, not in a browser: one window, with the rail of servers, Home and each server's room in it. Nothing opens a second window, and starting the app again brings the first one to the front. It uses the Microsoft Edge WebView2 runtime. The app's own pages are part of the app, served to the window on 127.0.0.1 only; a server's room comes from the host's PC; none is loaded from the internet.

## Upgrading

Run a newer `PrivatePartyNetSetup` over an installed copy. Setup recognises the existing install, says so, and keeps your settings, your certificate and your log. Setup asks you to close PrivatePartyNet first if it is open.

## Settings

The Settings page of Home (the gear at the foot of the rail) sets the appearance (Dark, the default, Light, or Match Windows), turns Automatic updates and Include test versions on or off (both on by default), has Check for updates and the sounds, and opens the data folder, the About page and the How PrivatePartyNet works page. The About page also has Check for updates, with when it last checked and what it found.

## Installation and deployment

1. Run `PrivatePartyNetSetup-3.4.0.exe`. It installs for your user account only.
2. Choose a desktop shortcut and whether to start with Windows. A Start menu entry is always made.
3. Start PrivatePartyNet.

Portable: unzip `PrivatePartyNet-3.4.0-portable.zip` and run `PrivatePartyNet.exe` in the folder. Nothing is written to the registry.

Uninstall from Settings, Apps, or the Start menu. Setup asks whether to delete PrivatePartyNet's data folder (settings, certificate, log). The default is to keep it. A silent uninstall always keeps it.

## Usage

Make your server:

1. Open PrivatePartyNet and type your name, and choose a picture if you like. Edits reach the network at once.
2. Name your server and press Create your server on Home. It goes first on your rail and stays there. Others on the network now see you on their Nearby PCs page.
3. When someone asks to join, press Let in.

Join a server:

1. On the Nearby PCs page, pick a PC that shows "server open" and press Ask to join, or paste an invite on Join a server.
2. When the host lets you in, the server goes on your rail for good and opens there.
3. Pick a text channel to chat. To share a screen, press Join beside a voice channel, then Go Live.

Everyone:

- Receive size (View options, in a voice channel's top bar) sets how large each picture is. Fit my window is the default.
- Click a tile to enlarge it. Click again to shrink it.
- Leave server (the menu under the server's name, or right-click it on the rail) leaves it for good. The host sees you go.

Owner and admins:

- Remove (the three dots beside a name in the member list) takes someone off the server. They are told, and they cannot rejoin without asking again.
- Server settings (the menu under the server's name) renames the server and sets its icon.

## Troubleshooting and offline use

- On one network nothing needs the internet. If a PC does not appear, check the cable or Wi-Fi between the PCs.
- "No network connection found" on Home: the PC has no LAN address. Plug both PCs into one switch, or join both to a phone hotspot.
- A nearby PC does not appear: allow PrivatePartyNet through the firewall on Private networks, check for client isolation, and make sure neither PC is on a VPN.
- A server uses a certificate made on the host's PC, which no authority has signed. The PrivatePartyNet windows accept it without a warning.
- A screen does not appear: stop it and press Go Live again. The bar at the bottom counts open connections (for example "Connected to 2 of 2").
- Logs: Settings, Open data folder opens `%LOCALAPPDATA%\PrivatePartyNet`, which contains `privatepartynet.log`. The About page lists every folder.
- An invite does not connect: the message says what was tried and what was missing (no IPv6 address in the invite, no relay, an older PrivatePartyNet on the host). The log has each step on both PCs: the invite's contents (never its password or keys), each address tried, the DHT lookup, the router kinds found, and which way worked or why each did not. Both PCs should run the same, newest version. Friends who share a VPN for LAN games (Radmin VPN, Hamachi, ZeroTier, Tailscale) reach each other over it: invites carry those addresses, and a host also seen under Nearby PCs is tried there first.

## Files

- `Start PrivatePartyNet.pyw`: run from source without a console window.
- `launcher.py`: entry point of the packaged build.
- `privatepartynet\app.py`: the app's actions and its windows. `shellserver.py`: serves the app's own pages to the one window on 127.0.0.1. `server.py`: the party server (members, chat, connection setup). `discovery.py`: finds nearby PCs. `feed.py` and `frames.py`: screen capture for the windows on this PC (dxcam, with mss as the fallback). `cpu.py`: the CPU readout. `paths.py`: where files live.
- `privatepartynet\web\`: the one window (`shell.html`, `appshell.js`, `shell.css`; `framebridge.js` lets the app's own pages use its bridge, `roombridge.js` lets a room ask the app through it), Home (`control.html`), Home's pages (`nav.js`), the party room (`room.html`, `room.js`, its tabs and narrow-window drawers in `shell.js`, the tile layout in `layout.js` and the connection numbers in `netstats.js`), direct messages (`dm.html`, `dm.js`), the sounds (`sound-*.mp3`, `sound-*.ogg`), the join message page (`login.html`), the WebRTC helpers (`rtc.js`), the About and How pages (`about.html`, `how.html`, `info.js`), the colors (`theme.css`), the shared components (`ui.css`), the icons drawn in code (`icons.js`), and each window's layout (`control.css`, `style.css`, `dm.css`, `info.css`).
- `privatepartynet\pathway.py`: the paths between members (direct ways and relays) that the game network uses. `relayproto.py` and `relayclient.py`: the relay protocol and a PC's side of it. `punch.py`: the join through both routers, and through a relay. `gamenet.py`, `gameclient.py` and `netlink.py`: the game network's members, its helper and the member's link to the host.
- `wghelper\`: the game network helper (Go, built on wireguard-go and Wintun), `PrivatePartyNetGameNet.exe`, which holds the adapter with Windows' permission and carries WireGuard over the app's paths.
- `privatepartynet\linkstate.py`: each member's link to the host (heartbeat, round trip, loss, ok/unstable/reconnecting). `partylink.py`: a joined server's room's own address on this PC, which finds the host again after a dropout. `web\link.js`: the room's side (answering the heartbeat, reconnecting, the banner).
- `privatepartynet\updater.py`: updates (checking GitHub, downloading and checking against GitHub's published SHA-256, installing).
- `relay\`: the relay server a group can run on a rented Linux server, with its setup script, systemd units, Docker files, coturn configuration and firewall, and `verify_memory.py`.
- `installer\PrivatePartyNet.iss`: the installer.
- `research\`: plans for what comes next and the research behind them, one folder per month; `research\README.md` says how it is used.
- `tests\`: Python tests; `tests\js\` holds the JavaScript tests for the layout and stats code, which a Python test runs when Node is installed. `tests\browser\e2e_call.mjs`: the three-person party test in headless Edge. `tests\browser\rtc_selftest.html`: the WebRTC self-test.

## Building the installer

Put the official offline prerequisites in `installer\prereq` before you build: the WebView2 Evergreen Standalone Installer (x64), the Visual C++ 2015-2022 x64 runtime and the .NET Framework 4.8 offline installer. The build checks each file's Microsoft signature and carries them into the installer and the portable zip. Without them, `py tools\build.py` stops; `--allow-missing-prereqs` builds anyway, and then the installer needs those components already on the PC. Build from a folder with a short path: Inno Setup cannot read a packaged file whose full path passes Windows' 260 characters, and the build stops and says so (`subst` gives a deep folder a drive letter of its own).

## Build and test

Build the release (tests, package, smoke test, installer, portable zip, checksums):

    py tools\build.py

Python tests:

    py -m unittest discover -s tests -v

Server end-to-end test (Node 22 or later and Microsoft Edge). It starts the real server, three browser contexts, and prints `RESULT PASS` or `RESULT FAIL n`:

    node tests\browser\e2e_call.mjs

Every version updates `privatepartynet\web\how.html`. A test fails the build when its version stamp differs from the app version.

## Contributing, security and the name

- [CONTRIBUTING.md](CONTRIBUTING.md) says how to set up, test and build, and what a change needs.
- [SECURITY.md](SECURITY.md) says how to report a problem privately, and what PrivatePartyNet assumes about the network it runs on.
- [TRADEMARKS.md](TRADEMARKS.md) says how the PrivatePartyNet name and icon may be used. The code is Apache-2.0; the name is not part of that grant.
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists the software PrivatePartyNet is built on and its licenses.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
