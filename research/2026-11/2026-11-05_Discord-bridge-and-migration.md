# Discord: migrating a server's history and files, and bridging Discord users in

Who ran it: the lead (Claude Opus 5.5), in its own turns, with WebSearch (K: "I research it myself").
When: 2026-11-05, from about 10:33.
Question: how can K's Discord server (its history and files) move into a PPN server, and can Discord users "ghost join"
the PPN server through the Discord server (a bridge), within Discord's rules? If not, what onboarding is easier than making
a Discord account?
Where it is used: `PLAN-2026-11-05_Discord-bridge.md`.

"Search summary" means the search tool's summary of the pages it found, not a page read in full. Policy wording below is
quoted from those summaries; check the live pages before relying on any one sentence.

## Discord's rules for apps

Developer Policy (support-dev.discord.com/hc/en-us/articles/8563934450327, also discord.com/developers/docs/policies/
developer-policy), search summary:
- Under "Handle Data with Care": do not use API Data to profile users or their relationships; do not re-identify or
  de-anonymise; "Do not mine or scrape any data, content, or information available on or through Discord services"; do not
  use message content to train machine learning or AI models without Discord's express permission; notify Discord and users
  of unauthorised access.
- Use of data covers how you "access, collect, store, retain, transmit, share, and otherwise process it" (so a bridge,
  which transmits, is covered).
- Item 17: no disclosure to data brokers or advertising; item 18: no selling or commercialising API Data.
- "Do not contact users outside of Discord without their explicit permission."
- The 2022 version also said: "Do not use API Data in any way that goes against Discord users' expectations."
- Every app needs a public privacy policy (third-party guides say so; checked at verification).

Developer Terms of Service (support-dev.discord.com/hc/en-us/articles/8562894815383; markdown copy in discord/discord-api-
docs), search summary:
- Section 5(b), sharing: "You will not share API Data with any third party, except ... (i) with a Service Provider; (ii) to
  the extent required under applicable laws or regulations; and (iii) when a user of your Application expressly directs you
  to share their API Data with the third party" (2022 wording; the search tool's reading is that a bridge relaying messages
  elsewhere needs that express direction or consent; Discord says nothing about bridges by name).
- Section 5(b), retention: update API Data on request; delete it promptly when no longer needed for the app's stated
  function, when the app stops operating, and (per a GitHub issue's paraphrase of the cut-off text) when Discord or the user
  asks, or the law requires. Give users an easy way to ask for modification and deletion. Section 5(c): encryption at rest.
- Section 9(b): on termination, delete any cached or stored API Data.
- 2017 terms (historical): delete End User Data on request and within 7 days of the user's account ending; "You may only
  retain chat logs as necessary for the operation of your Applications."

Reading for PPN (the lead's, to put to K): a one-time copy of a server's history into a place outside Discord is "storing
and transmitting API Data" for a purpose Discord's terms do not name. It is K's own community and its members' messages, so
consent from the members (announced in the server, with a way to opt out and to have one's messages left out or removed)
is the safe ground. Stoat (Revolt) refused to build history import (May 2025, maintainer): "Scraping channel history is a
no-no from discord's pov, and also raises privacy concerns for user data."

## Getting the data out

DiscordChatExporter (github.com/Tyrrrz/DiscordChatExporter), search summary of its README, discussions #889 and #1338,
issues #1497 and #1508, Bellingcat's toolkit:
- README: "automating user accounts is against Discord TOS and may result in you getting banned"; use a bot instead. A bot
  token is compliant. A bot reads only channels it can see and needs the Message Content intent to read text. DMs cannot
  be exported without a user token (a violation).
- Enforcement got stricter in 2026: a March 2026 issue reports an account blocked after exporting with a user token; a
  contributor links it to many people exporting "to leave the platform after the latest age verification news". Bellingcat
  notes the same risk. (So for K: use a bot in his own server, never his user token.)
- discordchatexporter.org is a different, unrelated web tool that uses the user API.

The API, search summary (docs.discord.com/developers/resources/message, discord-api-types, discord.py docs, Userdoccers):
- `GET /channels/{id}/messages`: `before`, `after` or `around` (one at a time, a message id) and `limit` 1 to 100 (default
  50). Pins have their own paginated endpoint with a timestamp `before`.
- Archived threads: public archived threads by timestamp, joined private archived threads by id, `limit` 2 to 100. Active
  threads per guild (`GET /guilds/{id}/threads/active`, from the tool's memory, not checked).
- Rate limits: per route bucket, reported in `X-RateLimit-*` headers, plus a global limit (about 50 requests a second, from
  the tool's memory, not checked). An exporter must follow `Retry-After`.

Message Content intent (Discord's 2022 announcement, GitHub discussion #5412, plus 2026 third-party blogs):
- An unverified app needs no approval for Message Content; it must be switched on in the Developer Portal. Unverified bots
  are capped at 100 servers. Third-party blogs (not Discord's docs) say that since June 2026 privileged intents need review
  only once a bot reaches 10,000 users. K's own bot in K's own server is far below both.

Attachments (Discord's announcement of September 29th, 2023; press coverage; Userdoccers; useapi/discord-cdn-proxy):
- Attachment links are signed and expire (`?ex=` expiry, `is=` issued, `hm=` signature; about 24 hours for links shared
  outside). A message fetched through the API carries fresh links, so an exporter should download each file right after
  fetching its message. A refresh endpoint exists (`POST /attachments/refresh-urls`, documented by Userdoccers, unofficial).
  Avatars, icons and emoji are not signed. A file is gone once its message is deleted.

## Importers others built

Search summary (github.com/macdows/import-discord-to-matrix, zulip issue #38150 and PR #40494, zulip.com/help/migrating-
from-other-chat-tools, stoatchat discussion #358):
- Matrix: import-discord-to-matrix reads a DiscordChatExporter JSON export and writes into a Matrix room through the
  Application Service API; downloads attachments and re-uploads them; maps Discord names to existing Matrix users or makes
  "ghost" users (@discord_<username>:server); dry-run mode. Whether it keeps original timestamps was not confirmed.
- Zulip: an importer is in progress (issue #38150 open; PR #40494 maps users to placeholder addresses
  <discord-user-id>@discordexport.zulip.com; people who left or deleted their accounts become deactivated "dummy" users so
  their history stays; a later Discord login matches people back). Zulip imports only into a new organisation.
- Stoat (Revolt): will not import history (above).

## Bridging: how Matrix does it (mautrix-discord)

Search summary (docs.mau.fi/bridges/go/discord/relay.html, github.com/mautrix/discord, mautrix docs, matrixdocs.github.io):
- Discord users appear on the other side as "ghost" users; people on the other side can log in to control their own Discord
  accounts (puppeting, with a user token or a bot).
- Relay mode on Discord uses webhooks: a webhook can change its name and avatar per message, so each relayed message shows
  the sender's own name and picture. Someone must be logged in with a bot or user account that can create webhooks
  (`!discord set-relay --create`); the webhook's name may not contain "discord". Avatars need a public HTTPS address serving
  them. Being ported to the bridgev2 framework (2025).

## Voice

Search summary (BetaNews, Shacknews, discord.js issue #11419, daveprotocol.com mentions, 2026 news):
- DAVE (Discord's end-to-end encryption for audio and video, using WebRTC encoded transforms and MLS) arrived in September
  2024, was extended to bots and apps in 2025, and since early March 2026 every call (DMs, group DMs, voice channels, Go Live)
  is end-to-end encrypted by default; clients must support DAVE to join. Stage channels are the exception. Announced around
  May 18th to 19th, 2026.
- A bot in a call that implements DAVE can hear it like any participant; libraries lag: discord.js issue #11419 (edited
  February 13th, 2026) reports zero audio received with DAVE in @discordjs/voice 0.19.x. A voice bridge would mean a bot
  that joins both calls and mixes audio across, with DAVE support in whatever library it uses.

## Webhook limits (for relaying PPN messages into Discord)

Search summary (inventivehq, cybrancee, peakbot.pro, discord-webhook.com, space-node.net guides; logitland/logitland issue
#61; discord/discord-api-docs issue #5557; pypi discord-webhook):
- Discord publishes no fixed numbers and says not to hard-code them: read the rate-limit headers and back off on 429 with
  Retry-After. Guides quote about 30 requests a minute per webhook, about 5 per 5 seconds shared per channel, 50 a second
  globally. A hands-on test about two weeks ago: every execute reported x-ratelimit-limit 5, bucket per webhook, 36 posts a
  minute in one channel without a 429; at most 15 webhooks per channel (the 16th failed with error 30007).
- A webhook can edit and delete its own messages if the message id is kept, so a bridge keeps a map of PPN message id to
  Discord message id for edits, deletions and replies.

## Links that open the app (for onboarding)

Search summary (github.com/matrix-org/matrix.to README and issue #355, anarcat's Matrix notes):
- matrix.to is "a simple stateless privacy-protecting URL redirecting service": the target sits after `#/`, so the web
  server never receives it; JavaScript in the page works out where to send you and lets you choose a client. A landing page
  ("You're invited to talk on Matrix"). Caveat in its README: fetching previews can leak your IP to the servers involved.
- For PPN: an invite link such as https://<project page>/join#<invite> keeps the invite out of every server's logs; the
  page offers "Open in PrivatePartyNet" (a ppn: link) or the download for this system, with the invite carried through.

Search summary (Airbridge guides, Microsoft .NET MAUI docs, Expo docs, DEV.to and other 2026 guides):
- A custom scheme (ppn://) can be registered by any app, with no proof of ownership.
- iOS Universal Links and Android App Links need a JSON file on a domain the project controls
  (/.well-known/apple-app-site-association, /.well-known/assetlinks.json) plus the app's entitlement or verified intent
  filter; then no other app can take the link. macOS 11 and later support them too.
- For PPN: a project domain (or GitHub Pages under the organisation) would be needed for verified links on phones; desktop
  installers can register ppn: themselves.

## Still to research

- The current Developer Terms text in full (Section 5), and whether a bridge between a Discord server and another app is
  an "Application" use Discord accepts (look for Discord's statements on Matrix bridges).
- Discord's export of a server's own settings (roles, channel permissions, emoji) through the API.
- DAVE in Python libraries (discord.py, Pycord) for a voice bridge.
