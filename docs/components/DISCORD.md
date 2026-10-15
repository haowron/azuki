# Discord intake and Host integration

[Docs index](../README.md) / [Shared contracts](../ARCHITECTURE.html)

This document owns the Discord Gateway connection, raw publishing, session assignment, and Host configuration.
Conversation routing, API tools, feedback decisions, and delivery belong to the installed
[conversation components](../EXTENSIONS.md#conversations).

## Structure

`api/app.py` builds `build_discord_gateway(producer)` and starts one receiving connection as a Connector listener.
If `DISCORD_BOT_TOKEN` is empty, receiving is skipped; when it is set, a positive `DISCORD_ALLOWED_USER_ID` is required.
The SDK owns the connection, heartbeat, and reconnection on the queue process's event loop. The intake parses raw
Gateway frames in `on_socket_raw_receive`, enabled by `enable_debug_events`, and publishes through `Producer.publish`.
It does not inspect message content to choose a business route. Acceptance continues while the runtime is stopped.

`start()` waits up to 30 seconds for the Gateway to become ready. Shutdown closes the connection and awaits the SDK's
cleanup; it does not stop the runtime process. Run one queue process per database to avoid duplicate connections.
See [Queue](QUEUE.md) for startup, shutdown, and execution boundaries.

## Input and execution

| Gateway event | Acceptance | Published `EventRequest` |
|---|---|---|
| `MESSAGE_CREATE` | Only `DISCORD_ALLOWED_USER_ID`; reject bots and webhooks | `provider="discord"`, `external_id` is the message ID, `payload` is the raw `d` object |
| `MESSAGE_REACTION_ADD` / `MESSAGE_REACTION_REMOVE` | Only the allowed user | Raw `d`; `external_id` combines the event name, message ID, and emoji ID or name, capped at 256 characters |
| Component `INTERACTION_CREATE` | Reject bots and other users; defer the owner's interaction before publishing | Raw interaction with its top-level `token` removed; `external_id` is the interaction ID |

Bulk reaction removal events have no acting user and are not accepted. Empty message bodies and attachment-only
messages are accepted; fetching attachments and deciding what to do with them belong to components. There is no
queue deduplication, and the external ID does not make repeated deliveries idempotent.

The raw interaction is paired with the SDK interaction before publication. Missing raw data, session-resolution
failure, or publish failure is reported to the owner through the deferred interaction. Successful publication confirms
acceptance, not authorization of a later operation. The component must validate any requested approval against its own
saved state. No internal authorization object is accepted from the external event.

For all accepted event types, the Host resolves sessions as follows:

- `channel` maps `(provider="discord", channel_id)` to one session through `resolve_session`.
- The general channel (`DISCORD_GENERAL_CHANNEL`, falling back to `DISCORD_NOTIFY_CHANNEL`) gets no session and does
  not read the mapping table. The runtime assigns a new session to each such request.
- For a thread, the SDK cache supplies the parent channel. Its resolved session becomes `parent_session_id`, except
  when the parent is the general channel. A normal channel has no parent session.
- A resolution failure prevents publication. Parent relations for existing sessions follow
  [Session](SESSION.md#7-channel-mapping-and-source-links).

The intake does not wait for an agent to finish. Runs in one session can execute concurrently, and queue reclaiming
means execution order is not guaranteed. The runtime passes the raw event to the version's dispatch agent and records
its result. Components own channel classification, history injection, child selection, replies, and business deduplication.

## Conversation agents

Conversation behavior is supplied by the installed [conversation components](../EXTENSIONS.md#conversations).
The package defaults accept raw events but do not include Discord delivery tools.

### Project channel Q&A (project_qa)

Project Q&A and document announcements belong to [project document components](../EXTENSIONS.md#project-documents).
The Host preserves the `project_channel` mapping and the session-store contracts in [Session](SESSION.md).

### Code fixes (code_fix / code_push / code_discard)

Fix proposals, confirmation, publication, and workspace cleanup belong to
[development components](../EXTENSIONS.md#development-and-reviews).
The Gateway only checks sender identity, defers the interaction, and publishes the raw event.

## Feedback on bot messages

The Host publishes the owner's reaction additions and removals without interpreting the emoji.
`core/orm/models.py` owns `discord_message` (delivery records) and `run_feedback` (ratings linked to runs and sessions);
the schema was added by migration `c1646cbc9caa`. These models are available to components, and their rows are not
written by the Gateway. The [Session contract](SESSION.md#owner-ratings-of-runs) and
[log queries](../LOGGING.md#query-examples) describe the stored associations.

Emoji selection, reconciliation with Discord, asking for reasons, and reply routing belong to the
[feedback components](../EXTENSIONS.md#conversations). Deploy compatible Host models before loading components that
import them, following [Versioning](VERSIONING.md#deployment-and-restart).

## API tools

Discord REST tools are supplied by [conversation components](../EXTENSIONS.md#conversations). Their endpoint policy and
write permissions are component contracts. The Host Gateway uses the bot token only for its receiving connection and
interaction acknowledgments; installing a bot connection does not install those tools.

## Delivery

Replies, notifications, translation, message paging, and delivery records are owned by
[conversation components](../EXTENSIONS.md#conversations). Published HTML files use the Host's
[report HTTP boundary](FASTAPI.md#public-pages-pr-reviews-documents-issue-investigation-reports).

## Configuration

1. Create an Application and a Bot in the Discord Developer Portal.
2. Enable the bot's **Message Content Intent**.
3. Add the bot with the OAuth2 `bot` scope. Grant View Channels and Read Message History; interaction responses and
   optional component delivery also require Send Messages / Send Messages in Threads. Grant additional permissions
   such as Manage Channels only for installed components that need them.
4. Copy your user ID and the channel IDs in Discord developer mode, then configure the untracked `.env`.

| Environment variable | Host use |
|---|---|
| `DISCORD_BOT_TOKEN` | Starts the Gateway when non-empty; may also be read by installed components |
| `DISCORD_ALLOWED_USER_ID` | Positive user ID allowed to submit input; required with the bot token |
| `DISCORD_GENERAL_CHANNEL` | Channel that receives no persistent session |
| `DISCORD_NOTIFY_CHANNEL` | Fallback general-channel ID; also an optional notification destination for components |

The Gateway enables `guilds`, `messages`, `message_content`, and `reactions` intents. The reactions intent is not
privileged. `DISCORD_GUILD_ID` and the language/history policy values are component settings documented in
[Configuration](../CONFIGURATION.md#webhook-ingress).

## Verification

RI06–RI09 and RI13–RI16 in `tests/queue/test_discord_ingress.py` cover raw messages, sessions, empty messages, threads,
the general channel, sender pruning, owner and non-owner buttons, reactions, bulk-removal pruning, and failed session
resolution. [Business tests](../BUSINESS_TESTS.md) owns the case index.

The SDK, DB, and publisher are stand-ins. Real Gateway behavior and business decisions of installed components are
outside this suite. Component evaluations belong with their implementations.

## Output language

The Host's fixed interaction acknowledgments are defined in `integration/discord.py`. Generated conversation content
uses the installed component's language and translation policy; the retained `OUTPUT_LANGUAGE` and
`DISCORD_LANGUAGE` settings are documented in [Configuration](../CONFIGURATION.md#output-language).
