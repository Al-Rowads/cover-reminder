# Cover Reminder

Checks one Instagram account's **Reels every hour**, using its existing Composio
connection. Sends a Telegram alert when a cover change is first detected. If the
cover remains unchanged, it sends reminders **24 hours and 48 hours after publication**.
The 48-hour check is final. Alerts and reminders go to every active private-chat
subscriber who has sent `/start` to the bot. No configured chat ID is needed.
Send `/stop` to unsubscribe.

## Behavior

- A new Reel's first successfully downloaded thumbnail becomes its baseline.
- Covers are compared as normalized 64×64 RGB images, not by their CDN URLs.
  The default difference threshold is 5% mean absolute pixel difference.
- The first hourly observation above the threshold sends one change alert and
  cancels any remaining unchanged-cover reminders. The new cover's content is not
  classified or attached to the alert.
- Reminders arrive on the first successful hourly check at or after each deadline,
  normally within an hour. Outages and rate limits can delay delivery.
- Missing/unreadable thumbnails and API errors never count as unchanged covers.
  The service defers delivery until it can check the current cover successfully.
- After an outage that spans both deadlines, the first successful check sends
  either a change alert or the 48-hour unchanged-cover reminder.
- Only Reels published after the **first successful activation** are eligible.
  Restarts preserve that activation time, post state, and the polling schedule.
- Each alert or reminder takes a snapshot of active subscribers when it is first queued.
  New subscribers receive subsequent broadcasts, including upcoming reminders
  for already monitored Reels. Broadcasts already queued or completed are not replayed.
  With no subscribers, that milestone is skipped; the final milestone still
  completes monitoring.
- Delivery is tracked per recipient. One unreachable subscriber does not cause
  repeat sends to everyone else. Blocking the bot or a forbidden delivery response
  deactivates that subscriber; `/start` reactivates them for future broadcasts.
  Unblocking alone does not resubscribe them.

The API does not provide a documented cover-edit flag. An edit before the first
check, a visually subtle edit, a cached thumbnail, or image variation above the
threshold can cause a missed detection or false alert. Messages therefore say that
a change was **detected**, not that Instagram confirmed an edit. The worker does
not publish posts or change covers.

## Set up on an always-on Docker server

1. Create a dedicated Telegram bot with BotFather if needed. Each recipient sends
   `/start` in its private chat. Group commands are ignored. Use this worker as the
   bot's only update consumer; an existing webhook or competing poller prevents
   subscription collection and is reported as an error.
2. Find the Composio project API key and the connected-account ID for your
   Instagram Business/Creator account in your Composio dashboard.
3. Copy `.env.example` to `.env` and fill in the four empty credential/connection
   fields. Keep `.env` private and out of Git. The pinned toolkit version is
   `20260819_00`; `INSTAGRAM_USER_ID=me` selects the connected Instagram account.
4. Build and check the real connections:

   ```sh
   docker compose build
   docker compose run --rm reminder check
   docker compose run --rm reminder send-test
   ```

`check` reads Instagram media and checks the Telegram bot token. `send-test` collects
pending subscriptions and sends a real notification to every active subscriber. It
reports sent, failed, inactive, and unattempted counts. If nobody is subscribed,
send `/start` and rerun it.
Stop the worker before running `send-test`; both commands use the same exclusive
lock. Repeating `send-test` intentionally sends another test notification.
No AI-model key is needed. Start the monitor after the connection and delivery checks:

```sh
docker compose up -d
docker compose logs --tail=100 reminder
docker compose exec reminder python -m cover_reminder status
```

The container runs as a non-root user; SQLite state lives in the persistent
`reminder-data` volume.
The running worker collects subscriptions between hourly cover checks, using
10-second Telegram long polling. `/start` and `/stop` receive confirmation messages.
Registration is stored even if a confirmation message cannot be delivered.
Before monitoring is running, `send-test` can collect subscriptions.

Telegram retains incoming updates for at most 24 hours. It does not provide a list
of everyone who ever started a bot. Anyone whose previous `/start` update has
expired or was consumed elsewhere must send `/start` again while this worker runs
or shortly before `send-test`.

## Configuration

| Variable | Meaning |
| --- | --- |
| `COMPOSIO_API_KEY` | Composio project API key; required |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | Existing Instagram connected-account ID; required |
| `COMPOSIO_USER_ID` | Composio user ID associated with that connection; required (distinct from the Instagram user ID) |
| `INSTAGRAM_USER_ID` | `me` by default, or the numeric Instagram account ID |
| `INSTAGRAM_TOOLKIT_VERSION` | Pinned dated version; default `20260819_00` |
| `TELEGRAM_BOT_TOKEN` | BotFather token; required |
| `COVER_DIFFERENCE_THRESHOLD` | Fraction between 0 and 1; default `0.05` |
| `DATABASE_PATH` | `/data/reminders.sqlite3` in Docker; `data/reminders.sqlite3` by default locally |

Polling is fixed at one hour, and milestones are fixed at 24/48 hours. There are
about 720 discovery requests per 30 days for a single-page account, plus additional
pages, pending-Reel lookups, and retries. Composio usage and server hosting may incur
charges according to your existing plans.

## Operations and recovery

- Keep a single worker per database. An exclusive file lock prevents accidental
  concurrent workers. Use a local disk volume; SQLite and the lock are not intended
  for shared network storage or multiple replicas.
- A successful delivery records Telegram's message ID. Confirmed sends are not
  repeated for that recipient after restart. If Telegram accepts a message but the response is lost,
  a retry can produce a duplicate; the Bot API has no send idempotency key.
- Temporary HTTP failures receive up to three attempts with backoff. Long
  `Retry-After` delays are stored across restarts and respected. Unsent 24-hour
  reminders are rechecked against the current cover; final reminders and change
  alerts retry from their durable recipient snapshots.
- Sends are paced to at most 20 per second overall and one per second per chat;
  Telegram retry delays take precedence. Paid broadcasts are not enabled.
- `status` shows post, reminder, change-alert, subscriber, and per-recipient delivery
  counts, update-poll status, and the last cycle's errors. Health is unhealthy
  if the latest cover cycle or update poll failed or either is older than 125 minutes.
  Docker reports health; `restart: unless-stopped` restarts exited processes, not
  unhealthy running processes. Have your server monitor container health.
- Logs contain media IDs and sanitized error codes, not credentials, signed URLs,
  captions, or raw provider responses. For `composio:tool_execution_failed`, inspect
  the execution in your Composio dashboard for the provider's detailed error.
- An inaccessible/deleted Reel does not produce an alert or reminder; its repeated
  check failures appear in logs and health.
- Discovery follows all pages and advances its checkpoint only after the whole scan
  succeeds. A one-hour overlap accommodates timestamp boundaries and brief indexing
  delays. Longer provider indexing delays can still cause missed posts.
- Stop the worker before backing up the entire data volume. Restore that volume
  to preserve activation, alert, and reminder history. Do not remove the
  volume during routine redeployment. Use a separate database for a different
  Instagram account or Telegram bot. Bot identity is checked using `getMe`; rotating
  the token for the same bot preserves its subscriber list.

### Upgrading an existing database

Stop the worker and back up its data volume before deploying this version. The
first database open migrates schema versions 1 or 2 to 3 transactionally, preserving
activation, Reel state, polling schedule, subscribers, and historical sends. Stored
cover-verification evidence and old capture files are ignored and are not deleted.
Reels already recorded as changed do not generate retroactive change alerts.

When upgrading directly from schema version 1, the previous destination is not
automatically subscribed. Remove `TELEGRAM_CHAT_ID` from `.env` and have recipients
send `/start` again; an old environment value is ignored. Previously sent milestones
are not replayed. A legacy pending milestone captures active subscribers after its
next successful cover check.

Older applications cannot open a version 3 database. To roll back, stop the worker
and restore the pre-upgrade backup with the previous application version.

## Local development and validation

Requires Python 3.12 or newer on macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m cover_reminder --help
```

For live commands outside Docker, export the variables from `.env` using your
shell or secret manager and set `DATABASE_PATH=data/reminders.sqlite3`.
The application does not automatically load `.env`; Compose does that for Docker.
On a macOS Python installation missing its root certificates, configure its trusted
CA bundle (for example, `SSL_CERT_FILE=/etc/ssl/cert.pem`). TLS verification must
remain enabled.

The offline tests exercise actual SQLite persistence, domain transitions, and real
Pillow reference photographs, including subscriber persistence, recipient snapshots,
partial deliveries, cancellation, and migration rollback. They do not fabricate
Instagram/Telegram responses.
The optional live test reads your connected account:

```sh
RUN_LIVE_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_live.py -v
```

Live acceptance requires: successful `check`; two private users sending `/start`
and both receiving `send-test`; `/stop` excluding one user from subsequent tests;
an unchanged Reel delivering both reminders without repeating recorded deliveries
after restart; and an edited Reel sending one change alert and cancelling applicable
remaining reminders. Also verify that blocking the bot removes that recipient and
group commands do not subscribe a group. Do not claim those checks passed based on
the offline suite. `run --once` respects the persisted hourly schedule.

## API references

- [Composio Instagram actions and fields](https://docs.composio.dev/toolkits/instagram)
- [Composio REST tool execution](https://docs.composio.dev/reference/api-reference/tools/postToolsExecuteByToolSlug)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Pillow image operations](https://pillow.readthedocs.io/en/stable/reference/Image.html)
- [Docker Compose service configuration](https://docs.docker.com/reference/compose-file/services/)
