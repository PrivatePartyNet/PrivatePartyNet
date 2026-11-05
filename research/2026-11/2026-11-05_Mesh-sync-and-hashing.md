# The server mesh: signed event logs, authority, catching up by hashes, files in pieces

Who ran it: the lead (Claude Opus 5.5), in its own turns, with WebSearch and WebFetch (K: "I research it myself").
When: 2026-11-05, from about 10:30.
Question: how can several nodes (hosting clients, dedicated servers, members who opt in) each keep a full copy of a
server's history and files, take new messages while apart, and catch each other up after being offline, quickly and
verifiably, with owners' and admins' powers still holding? What do Matrix, Nostr's Negentropy, Briar, BitTorrent v2,
Hypercore and Scuttlebutt do?
Where it is used: `SPEC-2026-11-05_Dedicated-server-and-mesh.md` (K: "a complex hashing system for a client or
dedicated server console that has been offline for sometime to come back online, find who is serving the servers it has
connected to or is responsible for, figure out who has the latest file images, and happenings transcript, and then update
accordingly").

"Search summary" means the search tool's summary of the pages it found; "fetched" means the page itself was read.

## Matrix: the room event graph and state resolution

Search summary (spec.matrix.org rooms v1, v2, v11, v12; MSC1442; the server-server API):
- Room events form a directed acyclic graph (the "event graph"). Each event lists `prev_events`: the latest events its
  sending server had that nothing yet followed. The partial order is the room's chronological order.
- Federation events are signed and hashed, and list `auth_events`: the ids of the events that authorise this one (at most
  10): the create event, power levels, the sender's membership, join rules and the like.
- State (a table keyed by event type and state_key) before an event = the resolution of the states after each of its
  prev_events. A message event does not change state; a state event replaces its (type, state_key) entry.
- Room version 1 could reset a room's state: joined users removed, admins losing power, banned users able to rejoin.
  State resolution v2 (room version 2, MSC1442) fixed it. Servers need not keep the whole DAG; resolution is a pure function
  from sets of state to one state, relying on each event's full auth chain.
- v2 steps: split into unconflicted and conflicted state, adding the "auth difference"; order conflicted power events
  (power levels, join rules, kicks and bans of others) in reverse topological power order and apply them with iterative
  auth checks; order the remaining conflicted events by the resolved power-levels "mainline" and apply them the same way;
  re-apply the unconflicted state. Sender power level, origin_server_ts and event id only break ties. An event that fails
  the auth rules at its point is ignored.
- Room version 12 uses state resolution v2.1 (MSC4297): auth checks start from an empty state and a "conflicted state
  subgraph" joins the conflicted set; this fixed state resets (CVE-2025-49090). Lesson: authority resolution in a
  multi-writer graph is where the real bugs were, for years.

## Catching up by hashes: range-based set reconciliation and Negentropy

Search summary (github.com/hoytech/negentropy, NIP-77 at github.com/nostr-protocol/nips/blob/master/77.md, arXiv
2603.19820, the Go and Perl packages, nostream PRs):
- Negentropy implements Aljoscha Meyer's range-based set reconciliation (RBSR). NIP-77 uses it for client-to-relay and
  relay-to-relay sync; implemented in strfry, nostr-tools and Citrine; explored for Waku Sync.
- Both sides sort items by (created_at, id). A message is a list of ranges, each with an upper bound and a mode: Skip,
  Fingerprint (a digest of all ids in the range), or IdList (all ids). Where fingerprints differ, the receiver splits the
  range and answers; small ranges are sent as id lists. At the end each side knows which ids it has that the other lacks
  and which it needs.
- Fingerprints are an incremental hash, so they can be cached in a tree (strfry keeps aggregates in LMDB branch pages).
  Not checked here, the search tool's own memory: v1 adds all 32-byte ids mod 2^256, appends the count as a varint, takes
  SHA-256 and keeps 16 bytes.
- Why Nostr needed it: syncing by "since" timestamps misses events through clock drift, equal timestamps and out-of-order
  arrival; comparing the sets does not.
- Practical: strfry answers in 500,000-byte frames of about 23,000 ranges.
- For PPN: the round trips grow with the logarithm of the set size and the traffic with the size of the difference, so a
  node back after weeks finds what it lacks in a few exchanges per channel.

## Briar: the Bramble Synchronisation Protocol

Search summary (ETH Zurich report by Yuanming Song, March 5th, 2023; Wikipedia; briar-spec on code.briarproject.org;
Briar Mailbox wiki; the briar_core Rust crate):
- BSP syncs data between members of a group over an encrypted, authenticated channel (BTP), designed for delay-tolerant
  networks; Bluetooth, Wi-Fi and removable storage when there is no internet. The Briar Mailbox stores a stream of
  encrypted BSP messages as a file for a contact to fetch later (a mailbox the user owns).
- The spec is probably `protocols/BSP.md` in briar-spec (not opened). Not checked, the tool's memory: record types ACK,
  MESSAGE, OFFER, REQUEST, VERSIONS, PRIORITY, and message dependencies.

## Files in pieces: BitTorrent v2 (BEP 52)

Search summary (bittorrent.org/beps/bep_0052.html quoted, libtorrent's feature reference, Wikipedia "Torrent file",
Seedarr issues, jtorrent and naoshiquan explainers):
- `pieces root`: "For non-empty files this is the the root hash of a merkle tree with a branching factor of 2, constructed
  from 16KiB blocks of the file." Leaves past the end of the file are zero hashes. "As of meta version 2 SHA2-256 is used".
  Each inner node is SHA-256 of its two children.
- "piece layers": for each file larger than the piece size, the concatenated hashes of one layer of its tree, keyed by the
  root; checked against the root. Piece length is a power of two of at least 16 KiB; leaves are always 16 KiB.
- Wire: a `hash request` (pieces root, base layer, index, length, proof layers) and a `hashes` reply with the proof, so a
  peer can fetch only the hashes it needs and check each 16 KiB block as it arrives. "All current implementations use
  2^14 (16 kiB), and close connections which request an amount greater than that."
- Every file has its own tree, so identical files have the same root anywhere (de-duplication comes free). libtorrent
  supports v2 since 2.0.
- For PPN: a file's name inside a server = its SHA-256 tree root; a node fetches blocks from every node that has the file,
  checks each block against the root, and resumes anywhere. The store already seals files in 1 MiB pieces (store.py), so
  the sealed piece and the hash-tree block sizes need to line up (16 KiB leaves, 1 MiB = layer 6).

## Signed append-only logs: Hypercore (and Scuttlebutt)

Search summary (hypercore-protocol.github.io/new-website/protocol/, DEP-0002 at datprotocol.com, github.com/holepunchto/
hypercore, docs.pears.com, the tradle FAQ, arXiv 2206.04049):
- Hypercore is a log with get(index), push(data) and length; old entries never change. A Merkle tree (BLAKE2b-256) over
  the entries; each new root is signed with the writer's Ed25519 key (the signed payload covers the core's key, tree hash,
  length and fork).
- A reader verifies any entry with its ancestor hashes and a signed root. A signed update that conflicts with a tree
  already verified marks the feed "corrupt" and replication stops (so a writer that forks its own history is caught).
- Sparse replication: a peer downloads only the part of the log it wants and passes those parts on; compact bitfields say
  which parts a peer has.
- One writer per core by default (the tradle FAQ calls this a limitation); newer versions have quorum signing
  (`core.signedLength`). Scuttlebutt instead chains entries by content-hash pointers (a linked list per identity).
- For PPN: one log per writer is simple to verify and to sync (compare lengths), but a server has many writers; a per-
  author log inside each server, plus a merged view, is one option; the Matrix-style shared DAG is the other.

## PAKE in Python

Search summary (pypi.org/project/spake2, libraries.io, Snyk, Socket, Ubuntu, NetBSD, conda-forge):
- `spake2` 0.9 (September 25th, 2024), pure Python, MIT, depends only on `hkdf`; Python 3.9 to 3.12 and PyPy3; source
  github.com/warner/python-spake2; published by meejah. Earlier 0.8 was 2018. Snyk marks it inactive (no release in 12
  months); about 778,000 weekly downloads (Socket); used by magic-wormhole (0.24.0, May 5th, 2026). Packaged by Ubuntu
  (Resolute Raccoon, 0.9-1), NetBSD and conda-forge.

## Willow, Meadowcap and Earthstar (Aljoscha Meyer's family)

Search summary (willowprotocol.org data model and comparison pages, worm-blossom.org devlog, LWN 1005639 "Offline
applications with Earthstar", willow-rs, willow-go, gwil.garden, arXiv 2603.19820):
- Prefix pruning: writing an entry at blog/idea deletes earlier entries under it ("overwriting a directory in a file system
  with an empty file"); formally, an entry is removed when another entry in the same subspace, with a newer timestamp, has
  a path that is a prefix of its path. One mechanism gives updates and deletion, whole folders included. LWN's caveat:
  deletion works only if every peer holding the data receives the update and honours it.
- Willow avoids content addressing at the protocol level and avoids proofs of completeness, so data can be removed without
  a trace (its comparison page says no other protocol it compared does prefix pruning).
- Meadowcap: capabilities; "owned namespaces" act like a fediverse instance's curator without being tied to a machine;
  delegation spreads the work. Tension the devlog describes: a capability limited to /blog/summer cannot learn that /blog
  was pruned ("Meadowcap really doesn't let you learn about something outside of the granted area"); signed deletions shared
  outside the scope are being weighed.
- Sync: "3d range-based set reconciliation" (subspace, path, time), no forced linear order.
- Implementations: willow-rs (Willow'25: data model, Meadowcap, persistent store, sideloading, General Purpose Sync),
  willow-js and meadowcap-js, willow-go (newer, partial). Earthstar's new spec builds on Willow and Meadowcap.
- For PPN: K wants deletions to take files off hosts. Prefix-style deletion (a channel, a person's uploads, everything
  before a date) maps cleanly onto storage clean-up; a node that never hears a deletion keeps the data, so a deletion must
  be a durable signed event that every node checks before serving.

## Authority without a server: Keyhive and p2panda-auth

Search summary (inkandswitch.com/project/keyhive and its lab notebook, RECAP'25 contribution, p2panda.org posts of
July 28th and August 27th, 2025, crates.io p2panda-auth, infinitxlabs/revokecheck-spike, arXiv 2604.23560):
- Keyhive (Ink & Switch project 035, 2024 to 2026, led by Brooklyn Zelenka): grant and revoke the ability to sync, read,
  write or administer local-first data, offline and without a central server. "Convergent capabilities", between object
  and certificate capabilities. Groups and individuals; groups are transitive (a group can be a member of a group); any
  member can delegate what it holds; revocation by the delegator or anyone with "manage". A peer checks an action by the
  capability, the signatures, and that it has heard of no revocation of it. Levels: Read, Write, Admin. BeeKEM: its group
  key agreement for end-to-end encrypted groups of thousands.
- p2panda-auth 0.7.1 (Rust): levels Pull, Read, Write, Manage, each including those below, each with optional conditions
  (for moderation rules). Concurrent group operations are settled by a "resolver"; the first release has one, "strong
  removal". Operations form a DAG; concurrent "bubbles" are found by a depth-first walk. "No messages will be displayed
  concurrent to a removal"; what came before must be removed or edited by hand.
- Independent test of p2panda-auth 0.7.1 (revokecheck-spike, every causal order): a manager removed while adding someone
  has that add invalidated; two managers removing each other at once both get removed; a manager removed then re-added
  stays a member but the concurrent add stays invalid.
- p2panda's older design advises expiring capabilities and least authority over revocation, and appending a revocation to
  the documents it applies to for a deterministic point.
- arXiv 2604.23560 (formal verification of local-first access control): a Byzantine peer can backdate events so a
  revocation does not seem to apply; authorised revocations must affect concurrent events as well as later ones; and two
  concurrent revocations can de-authorise and then re-authorise an event (an anomaly to design around).
- For PPN: owner and admins are already roles; in the mesh each grant, role change, ban and node grant becomes a signed
  event in the server's authority graph, with a strong-removal rule (a removal also cancels what the removed person did
  concurrently) and the owner above everyone. This is the part to design most carefully, given Matrix's history above.

## Still to research for this topic

- Authority without a server: owner-signed grants, admin powers, revocation while partitioned.
- Bandwidth caps (token buckets) per direction and per kind.
