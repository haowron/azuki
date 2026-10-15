# Slack intake and publishing

[Docs guide](../README.md) / [Shared contracts](../ARCHITECTURE.html) / [Configuration](../CONFIGURATION.md#slack-socket-mode)

Using Socket Mode of the official `slack_sdk`, TrackEverything monitors one channel (`SLACK_CHANNEL_ID`) in one
workspace (`SLACK_TEAM_ID`). It needs no public HTTP entry and no @mention, and publishes new events received while
connected to the queue as raw events.

## Intake and publishing

`api/app.py` composes `build_slack_gateway(producer)` and the connector. When Socket Mode starts, it checks the
workspace with `auth.test`, and the SDK manages connection and reconnection. When `SLACK_APP_TOKEN` is empty, the
connection is skipped.
The Events API route `POST /webhook/slack` is always registered.

```text
Socket Mode events_api delivery
  → are team_id and event.channel the configured values? (metadata pruning)
  → publish the raw envelope (without the top-level token) as an EventRequest through the shared Producer (durable enqueue)
  → Slack SDK ACK
  → the runtime passes the event to the installed dispatch agent
  → the dispatch agent ACKs the queue message (XACK, XDEL)
```

Deliveries from other workspaces/channels and deliveries without `event_id` are not published and are SDK-ACKed. A
delivery whose publish fails is not ACKed and waits for the SDK's redelivery.
The intake does not judge content such as text, type, or subtype. File bodies and the verification token are not
carried.
`external_id` is `<team_id>:<event_id>`. The queue has no deduplication, so the same redelivery is published as a new
message. No session is attached.

The Events API (`POST /webhook/slack`) answers `url_verification` synchronously with `challenge` and does not publish
it. For everything else, it returns 422 when there is no `event_id`; otherwise it uses `event_id` as `external_id` and
publishes the raw envelope (without the top-level `token`).
For the rules of each entry, see [Queue](QUEUE.md#intake).

## Summarizing mentions of the operator

Mention detection and investigation are optional [conversation components](../EXTENSIONS.md#conversations).
The Host publishes matching workspace/channel events without judging mentions, text, event type, or subtype.

### Chinese forwarding to Discord

Cross-service forwarding, translation, and Discord delivery are owned by the installed
[conversation components](../EXTENSIONS.md#conversations). Neither the Socket Mode listener nor the Events API route
forwards a message to another service.

## Read API tool

`slack_api` is an optional component tool. Its allowed endpoints, channel scope, argument validation, and result
contract are maintained with the [conversation components](../EXTENSIONS.md#conversations).
The Host listener itself uses `auth.test` to validate `SLACK_TEAM_ID` before connecting and performs no business API calls.

## Search tool for related information

`slack_search` is an optional component tool. Search scope and pagination belong to its
[component contract](../EXTENSIONS.md#conversations); they do not broaden the Host's configured intake channel.

## Approval-gated sharing

Posting tools and their approval rules are optional [conversation components](../EXTENSIONS.md#conversations).
The Host intake has no message-posting, reaction-writing, or read-state-changing operation. Its Socket Mode ACK is a
transport acknowledgment and does not authorize a component to post.

## Verification and configuration

RI04, RI05, and Q29 in `tests/queue/test_ingress.py` check the Events API handshake, raw publishing, Socket Mode pruning,
and ACK after publishing. The SDK, store, and API are mocked. Real connections and end-to-end shutdown behavior are
outside that suite; component tools and business decisions are evaluated in the component repository.

For scopes and App settings, use the [setup procedure](../CONFIGURATION.md#slack-socket-mode).

Official references: [Python SDK Socket Mode](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/),
[message.channels](https://docs.slack.dev/reference/events/message.channels/),
[auth.test](https://docs.slack.dev/reference/methods/auth.test/).
