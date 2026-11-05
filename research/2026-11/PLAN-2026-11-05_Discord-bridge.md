# Discord bridge: migrating K's Discord server, and letting Discord users in

last edited: 2026-11-05. Started by the lead (Claude Opus 5.5) in session SID-20261105-4b7e1c.
Status: plans only, research under way. Nothing built (K: build nothing until the research is in and K has reviewed it).

## K's words, verbatim (November 5th, mid-turn, about 10:32)

"Also, I want you to start developing Discord bridge plans:
1. A plan to migrate our server, all its history, and all of its files (shouldn't be too many files actually stored on the
   discord server itself)
2. A plan to allow users to "ghost join" our server somehow through joining our actual discord server, or  if that isn't
   possible, develop an onboarding process for people who want to join our PPN server that makes the transition easier than
   setting up a discord account"

## Questions the research has to answer

- What Discord's Terms of Service and Developer Policy allow: a bot reading a server's full history, keeping copies outside
  Discord, relaying messages both ways, posting as other names through webhooks. User-account tokens ("self-bots") are a
  known violation; check the current wording.
- The API: reading history (endpoints, page size, rate limits), the Message Content privileged intent and when it needs
  approval, attachments (CDN links that expire, and how to fetch them in time), threads, forum channels, pins, reactions,
  edits, replies, embeds, stickers, custom emoji, roles and channel permissions.
- Existing tools: DiscordChatExporter (formats, bot-token mode), Matrix bridges (mautrix-discord, matrix-appservice-discord,
  how they map users with webhooks), other migrations (to Stoat/Revolt, Matrix, Zulip) and what they kept or lost.
- Ghost joining: a bridge bot that mirrors chosen channels both ways (Discord user appears in PPN as a bridged person; PPN
  person appears in Discord through a webhook with their name and picture); voice cannot be bridged simply (check).
- Who the bridge runs on (a dedicated node), privacy of Discord users who never agreed to PPN, and consent.
- Onboarding without Discord: an invite link or QR that downloads PPN and joins in one step; pick a name; no account.

## Draft plan (for K's review; research: `2026-11-05_Discord-bridge-and-migration.md`)

### What Discord's rules mean for this

- Everything goes through a bot: K's own Discord application, added to K's server by K. Exporting with a user account
  breaks Discord's rules, and Discord has been blocking accounts that do it since about March 2026. A bot in one server needs
  no approval to read message text (the Message Content intent is switched on in the Developer Portal).
- Discord's Developer Terms let an app share message data with another party only in a few cases, one being when the user
  "expressly directs" it; they also require deleting stored data when it is no longer needed or when a user asks. Nothing
  in them names bridges or migrations. Stoat (Revolt) refused to build history import for this reason. The Developer Policy
  also says "Do not mine or scrape any data". K decided (question 1): export the whole server, everyone included. The risk
  then falls on the bot application (Discord can disable it), never on K's own account, as long as only the bot is used.

### 1. Moving the server: history and files

1. K makes a Discord application with a bot, gives it read access to the channels to move (and Read Message History), and
   switches on Message Content. The token stays on K's PC.
2. (K's decision: no opt-in or opt-out step; the whole history is copied. A note in the server that it is moving is still
   a courtesy K may choose.)
3. The exporter (part of the PPN node; or DiscordChatExporter run with the bot token, whose JSON format the importer reads)
   walks every chosen channel 100 messages at a time, plus threads, forum posts and pins, following Discord's rate-limit
   headers. Each attachment is downloaded right after its message is fetched, because attachment links expire after about a
   day. Custom emoji and stickers are downloaded too (they do not expire).
4. The importer writes the history into the PPN server's log as events signed by the owner and marked "imported from
   Discord", each carrying the original author's name, avatar and time. Files become PPN files (hashed, stored by the storage rules); pictures keep their
   pixels. Replies stay replies; edits keep the latest text; reactions become counts; embeds keep their links.
5. Roles: Discord roles map onto Owner, Admin, Member and Guest by a table K fills in on the import screen. Channel order
   and names carry over; Discord's per-channel permissions have no PPN match yet and are listed for K.
6. Claiming: when a Discord member later joins PPN, they prove who they are through the bot (it sends them a one-time code
   in a Discord DM; they enter it in PPN), and their imported messages show under their PPN name from then on.
7. After the import the raw export is deleted; the PPN server is the only copy. Running the import again later only adds
   what is new, so the move can happen early and be topped up at the switch-over.

### 2. Ghost joining: Discord people in the PPN server without installing PPN

Possible for text. The bridge is a bot running on a PPN node (the dedicated server), mirroring chosen channels both ways:

- Discord to PPN: the bot reads each new message and posts it into the PPN channel as a bridged person: their Discord name
  and picture, marked "via Discord". Bridged people have no keys and no powers: they cannot join voice, share screens or
  use the game network; owners and admins can mute or remove them on the PPN side.
- PPN to Discord: one webhook per bridged channel posts each PPN message under the sender's own name (webhooks may change
  name and picture per message; at most 15 webhooks per channel; about 5 posts per few seconds per webhook, read from the
  headers). Pictures show only from a public web address, which PPN does not have, so PPN people appear with a default
  picture unless one is uploaded to Discord.
- Edits, deletions and replies are matched through a table of PPN and Discord message ids. Files go across both ways within
  Discord's upload limit (to check).
- Voice is not bridged in the first version: Discord encrypts every call end to end since March 2026 (DAVE), a bot must
  support DAVE to hear one, and library support is still uneven. Possible later as a bot that sits in both calls.
- The bridged channels say so in their topics, so people posting there know their messages also reach PPN.

### 3. Joining PPN without Discord: the onboarding

- One invite link, https://<project page>/join#<invite>, on a static page (GitHub Pages under the PrivatePartyNet
  organisation). The invite sits after the #, so GitHub never receives it, as matrix.to does. The page offers "Open in
  PrivatePartyNet" if it is installed (a ppn: link the installer registers) or the download for the visitor's system, and
  keeps the invite for after the install.
- First run: type a name; no account, no email, no phone number. The invite asks the host (or a node) and lets them in.
- From Discord: `/join` in the Discord server sends that person their own one-time PPN invite by Discord DM, and links
  their Discord name so their imported history is theirs from the start (step 1.6).
- A QR code for phones, once there is a phone client.

### Questions for K

1. (Answered: copy everything; see below.)
2. Which channels move, and which are bridged?
3. Invite links: GitHub Pages under the organisation (free, no server to run), or a domain of your own?
4. Are you the owner of the Discord server, or an admin with Manage Server? A bot can only be added by one of those.

## K's answers

- Consent: K chose to export the whole history, everyone included, through the server's own bot (never a user token); no opt-in or opt-out step.
