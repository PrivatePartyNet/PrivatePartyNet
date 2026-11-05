# One person on several devices: identity, DMs on every device, proving the account, recovery

Who ran it: the lead (Claude Opus 5.5), in its own turns, with WebSearch and WebFetch (K: "I research it myself").
When: 2026-11-05, from about 10:25.
Question: how can one person's PC and phone (and more devices) be one identity, each keep all of that person's DMs, and
prove the account is theirs, in an open-source app with no company server? What do Signal, WhatsApp, Matrix, Keybase,
Threema and MLS do, and what goes wrong?
Where it is used: `SPEC-2026-11-05_Dedicated-server-and-mesh.md` (multi-device DMs, phones), the identity
redesign for 4.0.

Each finding says where it came from. "Search summary" means the search tool's summary of the pages it found, not a page
read in full. "Fetched" means the page itself was read (through WebFetch's summariser).

## PrivatePartyNet today (read in the code, identity.py and dm.py)

- Each install has an Ed25519 signing pair and an X25519 pair. `user_id` = first 16 bytes of SHA-256("PrivatePartyNet user"
  + sign_public + dh_public). So today the identity IS one device's keys: a second device cannot be the same person.
- `seal_to` uses a fresh ephemeral X25519 key per message and the sender signs; no forward secrecy (identity.py says so).
- DMs go PC to PC over a small TLS listener; the envelope names the sender's public keys, which are pinned on first sight;
  an outbox resends until the other PC answers 200 after saving. A safety number (12 groups of 5 digits) compares two cards.

## Signal: Sesame and linked devices

Search summary (signal.org/docs/specifications/sesame/sesame.pdf and papers):
- "The Sesame Algorithm: Session Management for Asynchronous Message Encryption", Revision 2, 2017-04-14, Moxie Marlinspike
  and Trevor Perrin. Generic: works with any session-based encryption meeting its conditions; Signal uses it with X3DH and
  the Double Ratchet.
- Each device keeps an "active" session for each other device it talks to; a DeviceRecord holds a list of sessions with one
  active and the rest inactive.
- The device list comes from the server: "the server informs the sending device of the old DeviceIDs and new DeviceIDs
  needed to make the sending device's records current, and the identity public keys corresponding to any new DeviceIDs."
- A message to a UserID is encrypted for every non-stale device of that user (fan-out); a missing session is set up by X3DH.
- The spec covers per-user (one identity key on all devices) and per-device identity keys. Sources disagree on which Signal
  uses; the SoK (ePrint 2021/498) says Signal uses the per-user model, the identity key shared across devices.
- Cremers et al., USENIX Security 2023 (ePrint 2022/1710): first formal model including the session-management layer; Signal
  stores 40 sessions per device. ePrint 2021/626 ("Help, my Signal has bad Device!") attacks Signal's post-compromise
  security through it. ePrint 2024/1886: Signal could get better post-compromise guarantees with a small change to session
  management. A 2019 Radboud thesis: Sesame's session recovery is not implemented.

Search summary (bleepingcomputer, androidpolice, androidauthority, cyberinsider and others), history on new devices:
- January 27th, 2025: a newly linked device (desktop, iPad) can receive the past messages plus the last 45 days of media
  from the primary phone. The primary makes an encrypted archive, sends it with a one-time 256-bit AES key; a provisioning
  message is encrypted to the public key in the QR code the new device shows; the archive goes over a direct channel with
  Signal's servers "only acting as a temporary relay". Large media are not in the archive; it links to copies kept on
  Signal's servers for 45 days, so older media cannot be synced.
- The choice (Transfer Message History or Don't Transfer) is offered once, while linking. Up to five linked devices per
  registered phone. Later (Android 8.20, iOS 8.22) a second phone or tablet can be linked too. A linked device cannot be
  the source of a backup or transfer.

## WhatsApp: per-device identity keys and a signed device list

Search summary (engineering.fb.com/2021/07/14/security/whatsapp-multi-device/, the whitepaper via Scribd copies,
ePrint 2025/794, arXiv 2504.07323), and the OSHI article below:
- With multi-device "each device now has its own identity key". Up to four companion devices besides the phone. The server
  maps an account to its device identities; a sender fetches the recipient's device list.
- Linking: the primary signs the new device's identity key (Account Signature), the companion signs the primary's (Device
  Signature), and the primary signs the new "Signed Device List Data" (ListSignature = CURVE25519_SIGN(Iprimary, 0x0602 ||
  ListData)). Servers store the list and signatures; every client keeps a verified device list for each account it talks to.
- The list expires ("a Time to Live of 35 days or less"); a stale list fails closed (then clients talk only to the primary).
  Messages carry the timestamp of the sender's newest signed device list. Removing a device = a new signed list without it.
- One-to-one: fan-out, the client encrypts N times for N devices. Groups: Sender Keys, sent once per member device over
  pairwise sessions; when a member leaves, every participant clears its Sender Key and starts over.
- History: at link time the primary encrypts a bundle of recent chats; its key arrives in an end-to-end encrypted message;
  keys are deleted after the companion stores the messages. Ongoing: the server keeps an end-to-end encrypted copy of each
  "application state" all of a person's devices can read.
- The 60-digit safety number covers every device key of both parties, so it changes when the device set changes.
- Backups: OPAQUE-protected HSM vault (the password never reaches the server) or a 64-digit key; only from the primary.
- Prekey Pogo (arXiv 2504.07323): a sender can query anyone's device list with "usync"; gaps in device ids reveal past
  devices.

## The OSHI article, "The second device is the whole problem" (fetched)

https://oshi-messenger.com/blog/multi-device-e2ee/ , September 13th, 2026, by the OSHI project ("The Messenger Security
Review" series). Points as given:
- Securing one device is largely solved; with several, the problem becomes the roster: who belongs to an account, who
  controls the roster, how recovery works. The Double Ratchet spec (rev. 4, Nov 2025) covers only two parties sharing a key.
- Fan-out against sender keys: fan-out costs grow with devices but removing a device is just "stop encrypting to it"; with
  a sender key the removed device still has the key, so everyone must rotate.
- Pitfall: a server-controlled device list lets the operator choose your endpoints ("More is Less", EuroS&P 2018: group
  membership messages "only encrypted on the transport layer", so a server could add itself; GCHQ officials later proposed
  an "extra 'end'").
- Recovery is the deliberate weak point: someone who lost every device and an attacker who has everything except a device
  look the same to the operator, usually separated only by a short memorable secret. Apple's iCloud Keychain escrow allows
  only 10 attempts, then "the keychain is lost forever". Signal's Secure Value Recovery uses enclaves to count guesses.
- OSHI's own gaps it reports: no alert on a new linked device until September 13th, 2026; an unsigned session list
  endpoint; an unsigned `mobile_auth` frame.
- Recommendations: the device list is a signed object owned by a device, not a server row; it expires so a stale list fails
  closed; the safety number covers the full device set; tell people when a device is added; rotate keys on removal; put
  escrowed keys behind hardware with a small guess limit. Four questions: are new devices announced, is the list signed by
  something you hold, does removing a device change keys, and what is the smallest secret that restores the account and who
  counts the guesses.

## Matrix: cross-signing

Search summary (spec.matrix.org v1.7 client-server API, MSC1756, matrix.org docs, the JS SDK guide, ePrint 2023/485):
- Three Ed25519 keys per user. Master key: the user's identity in cross-signing, signs the other two. Self-signing key:
  signs the user's own devices. User-signing key: signs other users' master keys, and is not shown to other users.
- Trust chain example: Alice trusts Bob's device when her master key signed her user-signing key, which signed Bob's master
  key, which signed Bob's self-signing key, which signed Bob's device key.
- Verifying a person (emoji or QR) signs their master key with your user-signing key; devices signed by their owner's
  self-signing key count as verified.
- ePrint 2023/485, "Practically-exploitable Cryptographic Vulnerabilities in Matrix", attacked this area (details not read
  yet).

## Keybase: sigchains, per-device keys, paper keys, per-user keys

Search summary (book.keybase.io/docs/teams/puk, book.keybase.io/docs/crypto/kbfs, keybase.io/blog/keybase-new-key-model,
keybase.io/blog/chat-apps-softer-than-tofu, Brown CSCI-2390 notes, HN comments by its developers):
- Each device has its own signing and encryption pairs; secret keys never leave it. Public halves are announced in the
  user's signature chain (sigchain), a public append-only list of signed statements.
- Sibling keys can provision or revoke other devices; subkeys cannot delegate. The first is the "eldest" key.
- Provisioning: the new device makes its own keys; an existing device signs it in and the new key countersigns (a reverse
  signature over the whole link). An existing device must approve each new one, except the first device and a paper key.
- Paper keys: full-powered device keys kept offline as words; as many as you like; announced and removed publicly like
  devices; a device added with a paper key still makes its own key.
- Per-User Key (PUK): its secret half is encrypted for every active device and paper key; a new device simply gets it
  encrypted to its key. Revoking a device makes a new PUK, encrypted for the remaining devices, written to the sigchain
  with the revocation. "As long as you don't lose all your devices (and a paper key counts as a device here), you won't
  lose your files."

## Threema: multi-device with a mediator

Search summary (threema.com cryptography whitepaper, threema.com/en/blog/md-architectural-overview, Olvid's benchmark,
threema-desktop via DeepWiki):
- A mediator server sits between the chat server and a person's devices. A new device is linked from an existing one (QR).
  The first device samples a random 32-byte Device Group Key (DGK) (whitepaper); an older blog post says it was derived from
  the ID's private key. Keys derived from the DGK authenticate devices to the mediator ("Device Group ID" is anonymous to
  it) and encrypt "reflected" messages between a person's devices. Contacts, groups, distribution lists and settings sync.
  No forward secrecy for this yet. Olvid's benchmark: as of September 2026 still beta, at most two computers.

## MLS and devices

Search summary (RFC 9420, RFC 9750, draft-mcmillion-mls-subgroups-00, draft-fondevik-mls-pairedmls-02,
draft-ietf-mls-protocol-00):
- RFC 9420 (Proposed Standard, July 2023): each member is a leaf of the tree, with its own credential and signing key;
  members are identified inside the protocol by leaf index, valid for one epoch. Several devices of one user "could all
  present the user's application-layer identifiers".
- RFC 9750 (architecture, Informational, April 2025): membership is per client, usually one per device; a user with
  several devices is usually several clients. A new device "will not gain access to the history even if it is owned by
  someone who owns another member of the group. MLS does not provide direct support for restoring history"; applications
  may add that outside MLS. Per-device signature keys "can be signed by an identity key recognized by other participants".
  Privacy cost (section 8.2.4): other members can tell which of a user's devices sent each message.
- draft-mcmillion-mls-subgroups-00 and the working-group draft-ietf-mls-virtual-clients-00: a user's devices act as one
  "virtual client", hiding device changes and which device sent; notes that adding or removing all of a user's devices
  atomically is hard. Not yet a standard (status not checked).
- draft-fondevik-mls-pairedmls-02 (Paired MLS): devices under a shared "anchor" leaf; security caveats.

## Matrix key backup and secret storage (SSSS)

Search summary (MSC1946, MSC1219, MSC2474, matrix-sdk docs, Black Hat EU 2022 slides, ePrint 2023/485):
- SSSS keeps secrets on the server encrypted with keys the client supplies (account data `m.secret_storage.key.<id>`,
  a default key). Standard secrets: the three cross-signing private keys and the backup recovery key
  (`m.megolm_backup.v1`). The key description should be signed with the master cross-signing key.
- Key backup (MSC1219): an X25519 recovery key pair, from a passphrase or from SSSS; devices encrypt their group-session
  keys to its public half and upload them; only the private half decrypts. MSC2474 fixes the two getting out of sync.
- Three recovery paths: SSSS for the cross-signing secrets; device-to-device key requests between a user's devices; the
  server-side backup. A new device can get the backup key from a verified device (`m.secret.request` / `m.secret.send`).
- Not checked against the spec, from the search tool's own memory: AES-256-CTR plus HMAC-SHA-256 with HKDF; PBKDF2 for a
  passphrase; recovery key shown as base58.

## Matrix's cross-signing attack (Albrecht, Celi, Dowling, Jones)

Search summary (ePrint 2023/485, IEEE S&P 2023 pp. 164-181, Black Hat EU 2022, Royal Holloway news, HN):
- Announced September 28th, 2022, in Matrix's spec and Element. Root cause: device ids and key ids used interchangeably,
  no domain separation; in the SAS verification a malicious homeserver gets the target to sign a cross-signing identity
  the server controls (a key presented as a device id treated as a master key), so it can sit in the middle.
- Also: a homeserver can add a verified user to rooms unnoticed. The same paper's backup attack: when a device asks for the
  backup key through secret sharing, the homeserver impersonates a trusted device and answers with its own key.
- A later formal analysis (ePrint 2023/1300) found the problems spread across the subprotocols and libraries.
- Lesson for PPN: every signed statement and every key carries its purpose inside what is signed (domain separation),
  and verification compares a value that covers the keys themselves.

## Pairing a new device: QR codes, short codes and PAKEs

Search summary (GitHub projects Sealbin #41, dragos-catalin/device-pairing, myous_pake, DartPilot #124, FFA PR #8,
flick-drop #256; Magic Wormhole docs; RFC 9382; draft-irtf-cfrg-cpace):
- A short code a person can type is too short to be a key; with a PAKE every guess needs a live attempt, the code is used
  once and expires in minutes. Magic Wormhole turns its short code ("7-guitar-sunset" style, nameplate plus words) into a
  strong key with SPAKE2 (RFC 9382); its servers never learn the code. CPace (draft-irtf-cfrg-cpace) is the CFRG-selected
  balanced PAKE and the other choice.
- Common pattern: a QR code carries high-entropy data (keys, endpoints, a one-time token); a typed or spoken code goes
  through SPAKE2 or CPace; codes are single-use, short-lived, few attempts; afterwards both screens show a short check value
  (words or emoji) to compare. Documented risk: someone who sees the code and connects first pairs instead, and the real
  device then fails visibly.
- To check: a maintained SPAKE2 or CPace library for Python (and for the phone languages) with test vectors.

## Still to research for this topic

- How to sync DMs between one person's devices with no server: direct, over the mesh as sealed blobs, or via other nodes.
- Phones: background limits on iOS and Android and push notifications (covered with the platforms topic).
