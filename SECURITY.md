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
  server's pages. It sees the public addresses of the members using it, how much they send and when. A member's PC
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
  public address and that you run PrivatePartyNet. With Automatic updates off (Settings), GitHub is only asked when you
  press Check for updates.
- There are no accounts. The app sends nothing to a company server.

The one window (3.3.0 on):

- The app's own pages (the rail, Home, direct messages, About, How) are served to its window by the app itself on
  http://127.0.0.1 at a port Windows picks. Only the web folder's own files of known types are served, by name, to
  requests addressed to that exact address, and they may not be put in a frame by any other page (frame-ancestors).
- Each server's room is a frame of that window, from the server's own address. A room asks the app for things only
  through the window's page, by postMessage: only the calls a room may make (close itself, fullscreen, keep the chat
  and its pictures in this PC's store, read this person's look, read this person's own picture to hand its server,
  and say its server's name and icon for the rail), only from that room's frame and origin, and only for that room. A
  room cannot reach another room, Home or the app's other calls. The name and icon a room says are checked (printable
  text up to 40 characters; a PNG, JPEG, GIF or WebP up to 2 MB, recognised by its bytes) and kept, encrypted, with
  that server's entry on this PC only.
- So that an HTTPS room can sign in inside the frame, its sign-in cookie is SameSite=None; Secure, and each server
  names its cookies after itself, so two servers on one address cannot overwrite each other's. To keep pages from
  elsewhere from using that cookie, the server refuses any POST whose Origin is not its own and any body that is not
  declared as JSON (or as raw bytes for an upload).
- Nothing opens a second window (4.0 may add one for a dedicated server's console). A host too old to sign in inside a
  frame shows a card in its place. A room whose certificate is not the one its join checked is stopped in its own
  place.

Pictures of people and server icons (3.4.0 on):

- Each person's app hands every server they open their picture, which that server keeps by person and shows to its
  members; the server's owner and admins set its icon. Both are PNG, JPEG, GIF or WebP up to 2 MB, recognised by
  their bytes (never by what the sender claims), and are served like chat pictures: with nosniff and a sandbox policy,
  to the server's members only.

Files (3.3.0 on):

- A file sent on a server is kept on the host's PC, encrypted with the rest of the store in sealed pieces of 1 MiB, each
  bound to its file and place, so a changed, cut, swapped or moved piece is refused. Members' PCs keep only its name,
  size and type. The host can read every file sent in its server, as it can read the chat.
- A file is always given back as a download (Content-Disposition: attachment, application/octet-stream, nosniff, and
  a sandbox policy), never as a page, so nothing a member sends runs in anyone's window. Its name is cleaned of folders
  and characters Windows reserves. Save goes through Windows' Save dialog.
- Each server's owner and admins can cap the largest file and all files together; the host's disk always keeps half a
  gigabyte free. The sender, the owner and admins can delete a message and its file from the host; copies people saved
  stay theirs.

How updates are checked:

- The app takes the newest release of PrivatePartyNet/PrivatePartyNet on GitHub that is newer than the one running
  (never older). Since 3.3.0 no release key signs it: whoever can publish a release on the repository can update every
  copy, so the maintainers' GitHub accounts are what this rests on (keep two-factor sign-in on them). 3.2.0 asked for an
  Ed25519 signature as well; copies still on 3.2.0 need that once, or a new install by hand.
- A download is used only when its name is the one the build gives it and its size and SHA-256 are the ones GitHub
  publishes for that file (the release asset's digest), checked again just before it runs. No setting, environment
  variable or command line changes where updates come from. It talks HTTPS only, with Windows' certificates, and takes
  files only from github.com and GitHub's download hosts. Release notes are shown as text.

Known limits:

- A server's HTTPS certificate is made on the host PC and signed by nobody. Every join checks it against a pin (the
  certificate's or its public key's SHA-256), and each server's room accepts only the certificate its join checked; any
  other is refused. The pin comes from the invite code, or the host's network beacon for a Nearby PC; a join by address
  alone trusts the first certificate it sees and remembers it. So a join by address, or a Nearby join on a network
  someone else controls, is only as safe as that first contact: use invite codes on networks you do not trust.
- The game network adds a virtual network adapter, which Windows only allows with administrator permission. A small helper is
  started with that permission when you turn the game network on, and it ends with the app. It only creates the adapter,
  moves other adapters' priority while the game network is on, and runs WireGuard on it. WireGuard opens no connection of
  its own: its sealed packets go to the app, which sends them to each member. The helper checks what the app gives it, so
  it can only ever reach addresses inside that server's game network.
- Each member has a WireGuard key for each server, kept sealed with the rest of the keys. The host signs the list of
  members and their keys, and WireGuard only accepts from each member packets that carry that member's own address.
- The installer is not code-signed, so Windows may show an unknown publisher.
