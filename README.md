# PrivatePartyNet 3.2.0

A private place for a group of friends to chat, talk, share screens and play LAN games together. One person hosts a server, PCs on the same network find it by themselves, and friends elsewhere join with an invite code. There is no company server and no account to make. Traffic goes through someone else's machine only when two routers let nothing through, and then only through a relay the group chose, encrypted end to end.

## Overview

- Parties: the host presses Start party on Home. Other PCs see it on their Nearby PCs page, press Ask to join, and the host lets them in. The join link works once and expires after two minutes.
- Servers and channels: a party is a server that keeps its name, its channels, its roles and its chat between runs. It starts with a text channel and a voice channel; the owner and admins add, rename and remove channels. Anyone who joins later reads the history, and older messages load as you scroll up. The newest 200 messages of each channel are sent when you connect.
- Roles: Owner (the PC that hosts), Admin (lets people in, removes members, manages channels), Member, and Guest (reads the chat and watches screens, cannot post, talk or share). Roles are remembered for each person, and a banned person cannot ask again.
- Pictures in chat: drag pictures onto the window (anywhere in it; up to ten at once, one message each) or paste one into the message box. It is sent at its own size and its pixel dimensions are never reduced. It is saved again first, which removes what the file recorded about where and when it was taken, and a large file is saved at a slightly lower quality if it needs to fit. PNG, JPEG, GIF and WebP up to 12 MB.
- Voice: join a voice channel (six people at most). Echo cancellation, noise suppression, mute, deafen, push to talk (hold V while the window is in front), microphone and speaker choice, a volume slider for each person, and a speaking light. Voice runs on the same direct connections as the screens, at 32 kbps with silence not sent.
- Direct messages: write to anyone on your network with no party open. Each message is sealed to the other person's key and signed, kept on both PCs, and sent again until it arrives if they are away. A safety number lets two people check that nobody is in between.
- Joining from outside: the host copies an invite code (name, password, where to try, and the host's certificate to check). The guest pastes it, the owner or an admin sees the request and lets them in. The app asks the router to open the port where it can. When nothing can reach the host directly (carrier-grade NAT, as on Starlink), the two PCs swap their addresses through signed records on the BitTorrent DHT, IPv6 included, and open both routers or firewalls by sending at the same moment (hole punching), then carry the party over QUIC. Where that cannot work (two routers that both give a new port for every destination), the invite goes through a relay instead, if the group has one; without one the app says so.
- How members reach each other: every pair tries every way at once and uses the best that works, in this order: the same network, IPv6, a port the router opened, hole punching through both routers, a member's PC that relays for others (only if that person turned it on, up to a limit they choose), and last a relay server the group runs itself. A connection keeps checking and moves to a better way as soon as one answers. IPv6 has no shared addresses, so it gets past carrier-grade NAT where both PCs have it, as most such providers give. A way is used only once it has carried a full-size packet.
- Relays: a relay only ever passes on what is already encrypted (WireGuard, QUIC, DTLS-SRTP) and cannot read or change it. The group's own relay server runs on a small rented Linux server: `relay\setup-ubuntu.sh` sets it up on Ubuntu 24.04 (`relay\README.md` says how, from renting the server on). It also carries voice and screens (TURN) when nothing else can. The owner enters its address, secret and certificate fingerprint once; the secret stays on the owner's PC and members get credentials of their own that last six hours.
- Game network: each server is also a private network, so games that play over a LAN work between its members. Pick a server on the Game network page and press Use for games; this PC gets that server's game address (a green light shows which one), and games see an ordinary network card, marked as a Private network. Packets go straight from member to member, sealed by WireGuard, over the best way between them (above), not through the host; someone who is removed or banned is cut off by everyone at once. Broadcasts and multicast, which many games use to find each other, reach every member. Windows' own services (file and printer sharing, Remote Desktop, remote management, network discovery) are closed to members until you open a group under What party members can reach on this PC. The adapter's packets are at most 1270 bytes, so that one carried by a relay over IPv6 still fits a 1400-byte path. Only game traffic for that server goes over it, never internet traffic. Windows asks for permission once each time it is turned on, because adding a network adapter needs it.
- Encrypted storage: chats, pictures, contacts and keys live in your PrivatePartyNet folder, encrypted with a key that Windows protects for your sign-in; a password can be added. Backups are one compressed file sealed with a passphrase you choose, and importing only adds what is missing.
- Go Live: share a screen with the whole party. Each person can share up to four screens at once, chosen from the Screen menu. Choose the quality ceiling (Source, 1080p, 720p, 480p) and the frame rate (15, 30, 60) first. The mouse pointer is drawn into the picture by default and can be hidden.
- Receive size: each viewer picks how large each picture is. Fit my window follows the window, including maximized and minimized. The 100%, 75%, 50% and 25% choices set the tile to that share of the sender's own size and ask the sender for a picture of that size.
- Upscale: draws each received picture smoothly at the size of its tile, with a live readout of what that costs in processor time.
- Tile controls: point at any screen and its own settings, pop out and fullscreen buttons appear. Each one applies to that screen only. Settings on a received screen sets its receive size and upscale; on your own screen it sets that screen's quality, frame rate and pointer. Pop out opens it in a window of its own (with the same buttons), and fullscreen makes the window cover the whole display with that one screen, in a popped-out window too (Escape or the button again brings it back).
- The room: laid out the way Discord users expect. Channels, the voice band and your name on the left; the screens in the middle; join requests, your live screens and the Chat and Members tabs on the right. Voice is joined with a channel's Join button, never by clicking its name. The message box is the text and Send; pictures are dropped or pasted in, and the window says where a drop will go while files are dragged over it. A button in the top bar hides the right panel to give the screens the whole window, and narrow windows fold the side panels over the screens. Leave closes the party window and any windows popped out of it.
- Grid and focus views: screens start as tiles you can move and resize, with snapping to the stage edges and to each other (hold Shift to resize freely). Click a screen to make it big, with the others as small previews along the bottom, as in Discord. Right-click any screen, including your own, to hide it: a hidden screen is a black tile with a link to bring it back, and the sender stops sending it to you.
- Connection bar: the bottom bar shows the app's CPU use, then a chip per person with colored icons for the rates, frame rates and picture sizes that apply. Hover a name for the details, including the CPU spent encoding and decoding for that person. Crowded bars give up detail instead of overflowing.
- Capture at the size that is needed: the screen is captured at the smallest standard size that covers what the viewers asked for (their tile sizes, or a percentage), and at full size again as soon as someone needs it. On the development PC this halved the processor time of sharing an ultrawide screen to a small tile (10.8% to 5.5% of the PC).
- Fast capture: screens are captured through the graphics card (Windows Desktop Duplication) with the older GDI capture as a fallback, and encoded with OpenCV when it is available. An ultrawide screen reaches about 57 frames a second at a 60 target on the development PC.
- Picture in picture, fullscreen, Light, Dark or Match Windows appearance.
- The control panel: a list of pages on the left (Home, Join a party, Nearby PCs, Game network, Direct messages, Saved chats, Settings), with a count of people waiting to join and a light when a game network is in use. Settings has the appearance, the data folder, the About page (versions and folders) and the How PrivatePartyNet works page, which is updated with every version.
- Colors were chosen for contrast in both themes (measured; body text is kept below the near-white-on-black that is hard on some eyes), every light also has words, and Windows' reduce-motion setting is respected.

At run time nothing is sent to a company server. The only calls outside your network are a check of your public address (public STUN servers), the router port-open while a party or the game network is running, once an invite has been made, small signed address records on the BitTorrent DHT, the direct connections between members, and a relay: the group's own relay server once the owner sets one, or a member's PC that relays for others. The feature set was designed from Discord's screen share and server features.

## Release information

| Field | Value |
|---|---|
| Version | 3.2.0 |
| Platform | Windows 10 or 11, 64-bit |
| License | [Apache License 2.0](LICENSE) |
| Installer | `PrivatePartyNetSetup-3.2.0.exe`, per user, no administrator rights. It updates an existing PrivatePartyNet in place. |
| Portable | `PrivatePartyNet-3.2.0-portable.zip`, run from any folder, including a USB stick |

## System requirements and compatibility

- Windows 10 or 11, 64-bit. The installer carries the Microsoft Edge WebView2 runtime, the Visual C++ 2015-2022 runtime and .NET Framework 4.8, and installs only the ones a PC is missing.
- The PCs must be on the same network: a cable through a switch, a phone hotspot, or a home Wi-Fi network. Some Wi-Fi networks stop devices from seeing each other (client isolation). A cable or a hotspot avoids that.
- Ports: TCP 8765 (the party, changeable in the control panel), UDP 8766 (finding nearby PCs), and one UDP port of the system's choosing for the game network's paths, which the app asks the router to open where it can. Windows Firewall asks once; allow PrivatePartyNet on Private networks. A group's relay server uses UDP 3479 and TCP 443, and its coturn UDP and TCP 3478 and UDP 49160-49400, on the rented server only.
- A party has room for up to 16 people, including the host. Screens are sent directly between members, so the network needs room for them. Several 1080p screens to many people is a lot of traffic on Wi-Fi; a wired network works best.

## The windows

PrivatePartyNet runs in native windows, not in a browser: the control panel, the party room, and a window for each popped-out screen. The windows use the Microsoft Edge WebView2 runtime. Nothing is downloaded at run time.

## Upgrading

Run a newer `PrivatePartyNetSetup` over an installed copy. Setup recognises the existing install, says so, and keeps your settings, your certificate and your log. Setup asks you to close PrivatePartyNet first if it is open.

## Settings

The Settings page of the control panel sets the appearance (Dark, the default, Light, or Match Windows) and opens the data folder, the About page and the How PrivatePartyNet works page.

## Installation and deployment

1. Run `PrivatePartyNetSetup-3.2.0.exe`. It installs for your user account only.
2. Choose a desktop shortcut and whether to start with Windows. A Start menu entry is always made.
3. Start PrivatePartyNet.

Portable: unzip `PrivatePartyNet-3.2.0-portable.zip` and run `PrivatePartyNet.exe` in the folder. Nothing is written to the registry.

Uninstall from Settings, Apps, or the Start menu. Setup asks whether to delete PrivatePartyNet's data folder (settings, certificate, log). The default is to keep it. A silent uninstall always keeps it.

## Usage

Host a party:

1. Open PrivatePartyNet and type your name. Edits reach the network at once.
2. Press Start party on Home. Others on the network now see you on their Nearby PCs page.
3. When someone asks to join, press Let in. Press Open party room to open the room yourself.

Join a party:

1. On the Nearby PCs page, pick a PC that shows "party open" and press Ask to join.
2. When the host lets you in, the party opens in a window of its own.
3. Press Go Live to share a screen, or just watch and chat.

Everyone:

- Receive size (View options, in the top bar) sets how large each picture is. Fit my window is the default.
- Click a tile to enlarge it. Click again to shrink it. Pop out opens it in its own window.
- Leave (top right) leaves the party. The host sees you go offline.

Host only:

- Remove (the three dots beside a name on the Members tab) takes someone out of the party. They are told, and they cannot rejoin without asking again.

## Troubleshooting and offline use

- On one network nothing needs the internet. If a PC does not appear, check the cable or Wi-Fi between the PCs.
- "No network connection found" in the control panel: the PC has no LAN address. Plug both PCs into one switch, or join both to a phone hotspot.
- A nearby PC does not appear: allow PrivatePartyNet through the firewall on Private networks, check for client isolation, and make sure neither PC is on a VPN.
- The party uses a certificate made on the host's PC, which no authority has signed. The PrivatePartyNet windows accept it without a warning.
- A screen does not appear: stop it and press Go Live again. The bar at the bottom counts open connections (for example "Connected to 2 of 2").
- Logs: Settings, Open data folder opens `%LOCALAPPDATA%\PrivatePartyNet`, which contains `privatepartynet.log`. The About page lists every folder.

## Files

- `Start PrivatePartyNet.pyw`: run from source without a console window.
- `launcher.py`: entry point of the packaged build.
- `privatepartynet\app.py`: the control panel and the windows. `server.py`: the party server (members, chat, connection setup). `discovery.py`: finds nearby PCs. `feed.py` and `frames.py`: screen capture for the windows on this PC (dxcam, with mss as the fallback). `cpu.py`: the CPU readout. `paths.py`: where files live.
- `privatepartynet\web\`: the control panel (`control.html`), the control panel's pages (`nav.js`), the party room (`room.html`, `room.js`, its tabs and narrow-window drawers in `shell.js`, the tile layout in `layout.js` and the connection numbers in `netstats.js`), the direct messages window (`dm.html`, `dm.js`), the join message page (`login.html`), the WebRTC helpers (`rtc.js`), the About and How pages (`about.html`, `how.html`, `info.js`), the colors (`theme.css`), the shared components (`ui.css`), the icons drawn in code (`icons.js`), and each window's layout (`control.css`, `style.css`, `dm.css`, `info.css`).
- `privatepartynet\pathway.py`: the paths between members (direct ways and relays) that the game network uses. `relayproto.py` and `relayclient.py`: the relay protocol and a PC's side of it. `punch.py`: the join through both routers, and through a relay. `gamenet.py`, `gameclient.py` and `netlink.py`: the game network's members, its helper and the member's link to the host.
- `wghelper\`: the game network helper (Go, built on wireguard-go and Wintun), `PrivatePartyNetGameNet.exe`, which holds the adapter with Windows' permission and carries WireGuard over the app's paths.
- `relay\`: the relay server a group can run on a rented Linux server, with its setup script, systemd units, Docker files, coturn configuration and firewall, and `verify_memory.py`.
- `installer\PrivatePartyNet.iss`: the installer.
- `tests\`: Python tests; `tests\js\` holds the JavaScript tests for the layout and stats code, which a Python test runs when Node is installed. `tests\browser\e2e_call.mjs`: the three-person party test in headless Edge. `tests\browser\rtc_selftest.html`: the WebRTC self-test.

## Building the installer

Put the official offline prerequisites in `installer\prereq` before you build: the WebView2 Evergreen Standalone Installer (x64), the Visual C++ 2015-2022 x64 runtime and the .NET Framework 4.8 offline installer. The build checks each file's Microsoft signature and carries them into the installer and the portable zip. Without them, `py tools\build.py` stops; `--allow-missing-prereqs` builds anyway, and then the installer needs those components already on the PC. Build from a folder with a short path: Inno Setup cannot read a packaged file whose full path passes Windows' 260 characters, and the build stops and says so (`subst` gives a deep folder a drive letter of its own).

## Build and test

Build the release (tests, package, smoke test, installer, portable zip, checksums):

    py tools\build.py

Python tests:

    py -m unittest discover -s tests -v

Party end-to-end test (Node 22 or later and Microsoft Edge). It starts the real server, three browser contexts, and prints `RESULT PASS` or `RESULT FAIL n`:

    node tests\browser\e2e_call.mjs

Every version updates `privatepartynet\web\how.html`. A test fails the build when its version stamp differs from the app version.

## Contributing, security and the name

- [CONTRIBUTING.md](CONTRIBUTING.md) says how to set up, test and build, and what a change needs.
- [SECURITY.md](SECURITY.md) says how to report a problem privately, and what PrivatePartyNet assumes about the network it runs on.
- [TRADEMARKS.md](TRADEMARKS.md) says how the PrivatePartyNet name and icon may be used. The code is Apache-2.0; the name is not part of that grant.
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) lists the software PrivatePartyNet is built on and its licenses.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
