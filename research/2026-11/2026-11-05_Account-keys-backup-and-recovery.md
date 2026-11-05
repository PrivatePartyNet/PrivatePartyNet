# Owning an account: the account key, backups only the owner can read, and recovery that is hard to lose

Who ran it: the lead (Claude Opus 5.5), in its own turns, with WebSearch (K: "do a round of research. come up with the bet
possible solution for the design criteria i laid out").
When: 2026-11-05, from about 10:48.
Question (K's criteria, verbatim in SPEC-2026-11-05_Dedicated-server-and-mesh.md, round 4): a key a person uses to "invoke
ownership rights over their account and data", which may also encrypt and decrypt their messages; backups held by a
dedicated server "in a way where only the client can read it"; "Whatever the latest greatest recommendations are, without
making the process too difficult, making it too easy to lose an account permanently". No company server.
Where it is used: `DESIGN-2026-11-05_PPN-4-nodes-mesh-devices.md`, section 7.
Builds on `2026-11-05_Multi-device-identity-and-DMs.md` (Signal, WhatsApp, Matrix, Keybase, Threema, OSHI's review).

"Search summary" means the search tool's summary of the pages it found, not a page read in full.

## Proton: several recovery methods, each unlocking a backup copy of the key

Search summary (proton.me/support/set-account-recovery-methods, /recover-encrypted-messages-files, /device-data-recovery,
/recovery-phrase, proton.me/blog/data-recovery-end-to-end-encryption, Privacy Guides forum):
- Proton separates resetting the password from getting the encrypted data back, and recommends at least the recovery phrase,
  one more reset method and one more data-recovery method.
- Recovery phrase: 12 words (BIP39 on the client) that both reset the password and recover data, because they decrypt a
  backup copy of the account key the server keeps encrypted.
- Recovery file: the encrypted backup keychain as a downloadable file (data only); it carries no identifier, so a thief
  cannot tell whose it is.
- Device data backup (formerly device-based recovery): an encrypted backup keychain kept in the browser's storage on each
  trusted device; signing in on that device after a reset recovers the data automatically.
- Contact-assisted data recovery: trusted contacts verify a recovery request (data only). Signed-in reset from another
  active session.
- Lesson: several independent ways to unlock one key, set up before they are needed, and a trusted device counts as one.

## Signal: a 64-character recovery key for end-to-end encrypted backups

Search summary (BleepingComputer, Medianama, mjtsai.com, alternativeto, aboutsignal, mapletechie, windowsforum, Yahoo Tech):
- Secure Backups announced September 8th, 2025 (Android 7.56 from September 19th, iOS 7.86 from November 19th, everyone with
  8.0 from February 19th, 2026). A 64-character recovery key made on the device and never sent to Signal is the only way
  to decrypt; lose it and the backup is gone. A new key blocks the old one from downloading future backups.
- Daily backup replacing the previous; disappearing and view-once messages left out; media encrypted twice and padded;
  backups not linkable to a user. Free: all text plus 45 days of media; paid adds more.
- September 28th, 2026 ("Back to Backups: A Year in Review"): encrypted local backups on Windows, macOS, Linux, Android and
  iOS; the recovery key decrypts all past backups.

## WhatsApp: passkey-encrypted backups, and the warning against passkeys as the only key

Search summary (about.fb.com October 2025, blog.whatsapp.com, gHacks November 3rd, 2025, cybernews, gsmarena, phandroid,
lilting.ch, KeePassDX issue #2502, guptadeepak.com):
- Late October 2025: chat backups can be end-to-end encrypted with a passkey (face, fingerprint, screen lock) instead of a
  64-digit key or a password; iOS and Android. WhatsApp did not publish the mechanism; analyses say the WebAuthn PRF
  extension. Password managers without PRF show "Your password manager doesn't support passkey-encrypted backups".
- Tim Cappalli, co-editor of the W3C WebAuthn Level 3 spec, warns against keys derived only from a passkey's PRF: a deleted
  or lost passkey means lost data. Recommended instead: a random data key, wrapped separately under a key derived from each
  passkey (through HKDF) and under other methods, so losing one does not lose the data.

## WebAuthn PRF support (late 2026)

Search summary (Corbado's PRF guide and its credential-manager tests of August 2026, Yubico's developer guide, hakhub.net,
the Chromium "Intent to Ship", GitHub notes from kummahiih/private-circle and Cratefield/harness, daniel-yang.com):
- PRF returns a deterministic 32-byte value per credential and site (the salt is hashed with "WebAuthn PRF" first).
- Google Password Manager: every passkey has PRF (Chrome, Edge, Samsung Internet). iCloud Keychain: macOS Sequoia 15.4 with
  Safari 18.4 and Chrome or Edge 128 and later. Windows Hello: from the February 2026 update (KB5077181; build 26200.7840 and
  later), Chrome 147 also at creation, Firefox 148. 1Password, Proton and Keeper work; Bitwarden partial; hardware keys need
  hmac-secret (YubiKey 5.2 and later).
- Advice seen repeatedly: "always enrol two unlock methods"; the master key is random and each passkey wraps its own copy.
- For PPN (the lead's note): passkeys belong to a website's domain (the relying party id). PPN's pages come from 127.0.0.1,
  which cannot be one, so passkeys would need a project domain (GitHub Pages under the organisation could serve the
  association files) and platform APIs in each app. Optional, later.

## Juicebox: a short PIN, safely, with guesses counted by several independent holders

Search summary (eprint.iacr.org/2025/348, juicebox.xyz whitepaper revision 7 and blog posts, Matthew Green's June 2025 post,
HN 39980750, go.mau.fi/mautrix-twitter/pkg/juiceboxgo, FairForge/vaultaire PRs #894 and #895):
- "Distributed Storage and Recovery of Secrets Using Simple PIN Authentication". A secret is split across n independent
  "realms"; a threshold of them is needed to recover. Each realm holds only an encrypted fragment and counts wrong PINs; after
  the allowed guesses the data self-destructs. One realm alone cannot even test a PIN (threshold OPRF, 2HashDH, after
  Jarecki et al.). Hardware realms (HSMs) can be mixed with software realms. X/Twitter's encrypted messages use it.
- Open source: client SDK MIT; the software realm AGPL (per vaultaire's notes). A Go port uses ristretto255 OPRF with DLEQ
  proofs, Shamir sharing, Argon2id and Noise NK; recovery threshold must be a strict majority.
- For PPN (the lead's note): realms could be the person's own node plus friends' nodes, so a memorable PIN becomes safe
  without any company server. Advanced and optional; software realms on friends' PCs are weaker than HSMs.

## Matrix: a device that waits on the server while you are away (dehydrated devices)

Search summary (MSC3814 via implementation PRs in ixofoundation/jambo, tuna-os/spindle, element-meta #922, matrix-rust-sdk
docs, matrix-js-sdk DehydratedDeviceManager, the Dart matrix SDK, Element's EMS docs):
- A client creates an extra device whose private keys are encrypted with a "dehydration key" (kept in secret storage,
  unlocked by the recovery key) and leaves it on the server. Senders treat it as any other device, so messages' keys queue
  for it. A new login fetches it, decrypts it, reads the queue, imports the keys, and makes a fresh one; rotated weekly.
- Pitfall seen: two formats (v1, v2); an older client that cannot read v2 replaced it and lost the queued keys.
- For PPN (the lead's note): this is K's "DM mailbox": a mailbox device listed in the person's signed device list, its keys
  held by the person's own node only in sealed form; senders seal DMs to it as to any device; the person's devices collect
  and decrypt. The node cannot read anything.

## Friends who vouch, with a waiting period (social recovery)

Search summary (eco.com 2026 wallet guides, zelcore, spark.money glossary and research, chainscorelabs, mitosis university,
DEV Community write-up of an Argent-style exploit):
- Argent: M of N guardians (a majority) approve a recovery; guardians cannot use the funds. A waiting period (36 hours in
  most sources, 48 in one) lets the real owner cancel; adding or changing guardians is also delayed (36 hours); 24 to 72
  hours is common elsewhere. "That delay is the entire security model of social recovery." Weakness: colluding guardians, or
  too few guardians still around.
- A recent exploit: the waiting period could be changed by anyone and wrapped around; fix: only the owner may change it,
  and that change is itself delayed.
- Shamir sharing (SLIP-0039, Trezor): k of n shares rebuild the secret; no waiting period or cancel step built in.

## What the research points to for PPN (the lead's synthesis, written up in the design)

- One random account secret per person, never derived from anything a person types or holds; from it come the account
  signing key (ownership) and the account data key (encrypting their backups).
- Many independent unlockers, each wrapping its own copy (Proton, the PRF critics, Matrix secret storage): each signed-in
  device (default), a recovery phrase (on by default, shown once with a check), optionally a passkey, optionally trusted
  friends (k of n, with a waiting period the owner's devices can cancel), optionally a PIN guarded by several nodes
  (Juicebox).
- Backups and a DM mailbox held by the person's own node, sealed with the account data key (Signal, Matrix dehydrated
  devices): the node stores and forwards, never reads.
- Anything that adds a device without an existing device announces itself to all devices and contacts (OSHI, WhatsApp).
