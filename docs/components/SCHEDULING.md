# Scheduling

The queue process reads two PostgreSQL tables and publishes execution requests at their times: `schedule` holds cron
definitions, and `reservation` holds one-shot reservations. Every published request is a named run
(`EventRequest(provider="internal", ...)`), the same kind of request the CLI and the maintenance sweep publish, and the
dispatch agent interprets it. The rules for claiming, ACK, and the dead stream are in [Queue](QUEUE.md).

There is no management API, dashboard view, CLI, or tool for `schedule`: rows are added, changed, and deleted with SQL.
Optional [reservation components](../EXTENSIONS.md#scheduled-components) can create and cancel reservations from a conversation.
The package defaults do not include those agents or tools.

## Components

| Part | File | Process |
|---|---|---|
| Table reads, the cycle, and publishing (`ScheduleTrigger`, the always-on connector `Connector("schedule")`) | `integration/schedule.py` | queue |
| ORM `Schedule` and `Reservation` | `queue/models.py` | — |
| Connector registration (`serve` adds it after HTTP and the other connectors; shutdown runs in reverse order) | `api/app.py` | queue |

- `process.restart_roles()` restarts only the queue for a change to `integration/schedule.py`.
- Cron times are computed with [cronsim](https://pypi.org/project/cronsim) (no dependencies, Debian cron semantics,
  daylight-saving handling). Table reads and the cycle are implemented in `integration/schedule.py`.
- Development runs must use a dedicated database and queue namespace, as in the [development procedure](../OPERATIONS.md#development-runs-isolated-from-the-live-service).

## The `schedule` table

| Column | Type | Content |
|---|---|---|
| `id` | text, primary key | Definition name; it goes into `external_id` |
| `cron` | text, required | Five-field cron expression. Day of week 0 and 7 are Sunday; names such as `MON` work |
| `timezone` | text, required, default `'Asia/Tokyo'` | IANA time zone in which `cron` is interpreted |
| `payload` | jsonb, required | Named-run payload: `agent_name`, optional `data` and `request`; additional fields are interpreted by the installed dispatch component |

Example (`docker compose exec db psql -U trackeverything trackeverything`):

```sql
INSERT INTO schedule (id, cron, payload) VALUES
  ('weekday-digest', '0 9 * * 1-5', '{"agent_name": "general", "request": "Summarize the current workspace"}');
```

At 09:00 (Tokyo) on weekdays this publishes:

```json
{"kind": "event.handle", "provider": "internal", "external_id": "schedule:weekday-digest:2026-10-14T09:00:00+09:00",
 "payload": {"agent_name": "general", "request": "Summarize the current workspace"}, "session_id": null, "parent_session_id": null}
```

No session is attached to a scheduled request.

### Documented rows

Rows are installation data; the Host creates no scheduled jobs automatically. Add only names declared by the installed
dispatch agent. The package defaults support `general` and `optimizer`.

An optional schedule for the Host-owned [optimizer](OPTIMIZER.md#startup) can select a check:

```sql
INSERT INTO schedule (id, cron, payload) VALUES
  ('optimizer-errors', '0 5 * * *', '{"agent_name": "optimizer", "data": {"checks": ["errors"]}}');
```

Maintenance, Sentry auto-fix, and conversation reminders require the corresponding
[scheduled components](../EXTENSIONS.md#scheduled-components); their row IDs and times belong to that installation.

## The cycle

After the queue process starts, one task repeats this cycle until shutdown:

1. Read the current time from the wall clock (`datetime.now(UTC)`) and read every row of both tables.
2. Compare each `schedule` row with its known definition (`cron`, `timezone`, `payload`).
   - A new row or a row whose definition changed gets the first time after now as its next fire time.
   - A row whose definition is unchanged keeps its next fire time. A row that disappeared is forgotten.
   - A row with an invalid cron expression (not five fields, or rejected by cronsim), an unknown time zone, or an
     invalid payload (not an object, or an `external_id` longer than 256 characters) never fires. The error is logged
     once per definition and not again until the definition changes. Other rows are not affected.
3. For each row whose next fire time has passed:
   - If the delay is within the grace period (`GRACE`, 60 seconds), publish one `EventRequest` and log its `message_id`.
     `external_id` is `schedule:<id>:<fire time in ISO 8601, in the row's time zone>`.
   - If the delay exceeds the grace period (sleep or a stall), do not publish; log a warning that the time was skipped.
   - Either way, move the next fire time to the first time after now. Any times in between are skipped together.
4. Wait for the shorter of 30 seconds (`POLL_SECONDS`) and the time until the nearest fire time (cron rows and
   reservations together). A shutdown request ends the wait.

- A publish failure (Redis outage) is logged as an error and not retried; that time does not run.
- A failure to read a table is logged as an error, and firing continues with that table's definitions from the previous
  cycle. The two tables are read separately, so a failure in one does not affect the other.
- Table changes take effect within 30 seconds.
- The wait is capped at 30 seconds because macOS `time.monotonic()` (`mach_absolute_time()`) does not advance during
  sleep. asyncio timers use that clock, so one long wait would be late by the time spent asleep. Every cycle compares
  against the wall clock instead.
- The trigger only reads the tables and never writes them. Next fire times live only in the process, and times that
  passed before startup, while stopped, or during sleep are never caught up.

## One-shot reservations

The same cycle reads the `reservation` table and publishes each row's payload once at its requested time.
The Host does not interpret the instruction or choose a notification destination. An installed component may create,
consume, or delete rows; SQL can also manage them directly.

### The `reservation` table

| Column | Type | Content |
|---|---|---|
| `id` | text, primary key | Reservation name; it goes into `external_id` |
| `at` | timestamptz, required | Fire time |
| `payload` | jsonb, required | Named-run payload |
| `created_at` | timestamptz, required, default `now()` | When the row was created |

Any named run can be reserved once with SQL (`docker compose exec db psql -U trackeverything trackeverything`):

```sql
INSERT INTO reservation (id, at, payload) VALUES
  ('general-once', '2026-10-15 07:30+09', '{"agent_name": "general", "request": "Summarize the current workspace"}');
```

At 2026-10-15 07:30 (Tokyo) this publishes:

```json
{"kind": "event.handle", "provider": "internal", "external_id": "reservation:general-once:2026-10-14T22:30:00+00:00",
 "payload": {"agent_name": "general", "request": "Summarize the current workspace"}, "session_id": null, "parent_session_id": null}
```

### Firing (queue process)

The cron rules above are unchanged; reservations add these rules to the cycle:

1. Each cycle reads `schedule` and `reservation` separately. If one read fails, the error is logged and only that table
   keeps the previous cycle's definitions.
2. Compare each reservation with its known definition (`at`, `payload`).
   - A new or changed row waits for `at` if `at` is after now. If `at` has already passed, it never fires (a time that
     passed before the first read is not caught up).
   - An unchanged row keeps its state. A row that has fired or been skipped never fires again, even if the row remains.
     A row that disappeared is forgotten.
   - A row whose payload is not an object, or whose `external_id` would exceed 256 characters, never fires. The error is
     logged once per definition.
3. When `at` has passed, publish once if the delay is within the grace period (60 seconds) and log the `message_id`.
   A longer delay is not published and a warning is logged. `external_id` is `reservation:<id>:<at in ISO 8601, UTC>`.
   A publish failure is logged as an error and not retried.
4. The wait is the shorter of 30 seconds and the time until the nearest fire time across cron rows and reservations.
5. A cron row and a reservation with the same `id` do not affect each other.

The queue never writes the table. Deleting or retaining a row after publication is the responsibility of SQL or the
installed component. Removing a row does not retract a request already published to Redis.

### Agent `reservation` (create, list, cancel)

This optional agent and its create/list/delete tools belong to the
[scheduled components](../EXTENSIONS.md#scheduled-components). They are not part of the Host or package defaults.
Their conversation inputs, time validation, destination capture, and cancellation policy are component contracts.

### Agent `reservation_fire` (after firing)

This optional dispatch child owns row consumption, delegation, delivery, and any check for cancellation after publication.
See [scheduled components](../EXTENSIONS.md#scheduled-components). The Host trigger neither deletes the row nor guarantees
that the target agent executes an instruction only once.

## Accepted errors

- Times that pass before the first read, while stopped, or more than 60 seconds before a cycle are not caught up.
  This includes a queue restart that misses a fire time. Rows remain until SQL or a component removes them.
- A failed publish does not run and is not retried by the trigger.
- Two queue processes against the same database can publish the same time twice; no lock prevents a second instance.
- There is no deduplication. `external_id` names the origin and time; the queue and runtime do not compare it.
- The trigger does not validate business payload fields such as `agent_name`. An undeclared name is a dispatch error
  at run time, as with the CLI.
- Cancelling or deduplicating work after publication requires a component-level check; deleting the row alone does
  not remove a message already in Redis.

## Test cases

Redis, the database, and time are stand-ins; PostgreSQL and real-time waits are not used. Each cycle is called directly
with a given time. All cases are in [`tests/queue/test_schedule.py`](../../tests/queue/test_schedule.py); the ST cases
read the reservation table as empty.

| ID | Scenario | Expected result |
|---|---|---|
| ST01 | Row `weekday-digest` (`0 9 * * 1-5`) first read on 2026-10-13 (Tue) at 08:59:50, then cycles at 09:00:00.2 and 09:00:30; time zone `Asia/Tokyo`, and `UTC` with `0 0 * * 1-5` | Nothing at 08:59:50 and a 10-second wait; one publish at 09:00:00.2 with `provider="internal"`, `external_id` `schedule:weekday-digest:2026-10-13T09:00:00+09:00` (UTC: `…T00:00:00+00:00`), and the row's payload unchanged; nothing at 09:00:30 |
| ST02 | The same row first read at 09:00:20 (startup just after the fire time) | No publish; the next fire is 09:00 on the next weekday |
| ST03 | A row waiting for 09:00 cycled at 09:01:00, and at 09:01:01 | 60 seconds late publishes; 61 seconds late does not and logs a warning; both move the next fire to 09:00 on the next weekday |
| ST04 | Table changes: 08:00 A (`0 9 * * *`); 08:30 A's cron becomes `30 9 * * *` and B (`0 9 * * *`) is added; 09:00 unchanged; 09:10 A's payload changes; 09:30 unchanged; 09:40 B is deleted; a cycle at 09:00 the next day | 09:00 publishes only B; 09:30 publishes only A with the new payload; nothing the next day at 09:00 |
| ST05 | Rows with an invalid cron (`61 * * * *`), an unknown time zone (`Mars/Base`), or a non-object payload (an array), read with a valid row over two cycles | Invalid rows never publish and log one error per definition; the valid row publishes normally |
| ST06 | The table read raises in the second cycle while a row's fire time has passed | An error is logged and the previous definitions publish; the third cycle reads the table again |
| ST07 | Two rows at the same time; publishing the first raises `QueueUnavailableError` | An error is logged and the first is not retried; the second publishes; the first's next fire is its next time |
| ST08 | The queue process's composition starts the schedule connector and stops it while waiting; table reads and publish are stand-ins | `serve`'s connectors include the `schedule` listener; start begins the cycle; it ends within 1 second of the stop request; nothing is read or published after stopping |
| RV01 | Reservation A (`at` 2026-10-13 09:00 Tokyo) first read at 08:59:50, then cycles at 09:00:00.2 and 09:00:30 | Nothing at 08:59:50 and a 10-second wait; one publish at 09:00:00.2 with `provider="internal"`, `external_id` `reservation:A:2026-10-13T00:00:00+00:00`, and the row's payload unchanged; nothing at 09:00:30 or later even though the row remains |
| RV02 | A 09:00 reservation first read at 09:00:20; also with a new trigger (a queue restart) | Neither publishes |
| RV03 | A reservation waiting for 09:00 cycled at 09:01:00, and at 09:01:01 | 60 seconds late publishes; 61 seconds late does not and logs a warning; neither publishes again |
| RV04 | Table changes: 08:00 A (09:00) and C (10:00); 08:30 A's `at` becomes 09:30 and B (09:00) is added; 09:10 A's payload changes; 09:50 C is deleted; cycles at each time and at 10:00 | 09:00 publishes only B; 09:30 publishes only A with the new payload; nothing at 10:00 |
| RV05 | A reservation with a non-object payload (an array), and one whose long `id` makes `external_id` exceed 256 characters, read with a valid reservation over two cycles | Invalid reservations never publish and log one error per definition; the valid one publishes normally |
| RV06 | The reservation table read raises in the second cycle; the cron table reads; a reservation and a cron row are both due | An error is logged; the reservation publishes from the previous definitions and the cron row from the fresh read |
| RV07 | Two reservations at the same time; publishing the first raises `QueueUnavailableError` | An error is logged and the first is not retried; the second publishes |
| RV08 | Cron row `daily` (09:00) and reservation `daily` (09:00:10) read at 08:59:55, then cycles at 09:00:00 and 09:00:10 | Waits of 5 then 10 seconds; 09:00:00 publishes only the cron row, 09:00:10 only the reservation |

### Checks outside pytest

Host schema and integration acceptance use an isolated scratch database:

| Step | Expected result |
|---|---|
| On an empty scratch database: `alembic upgrade head` → `downgrade -1` → `upgrade head` | The table is added, removed, and added again |
| `alembic check` | No differences |
| Start the queue on an empty scratch database with a dedicated `QUEUE_NAMESPACE` and empty Discord/Slack settings; insert an every-minute row and a row with an invalid cron | At second 0 of the next minute, one entry is published to that namespace's stream and its `message_id` is logged; the invalid row's error appears once; TERM stops the process |

Optional reservation-agent evaluations live with the [scheduled components](../EXTENSIONS.md#scheduled-components).
