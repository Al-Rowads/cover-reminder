# Cover Reminder

Checks one Instagram account's **Reels every hour**, using its existing Composio
connection. Sends Telegram reminders **24 hours and 48 hours after publication**
when no cover change has been confirmed. The 48-hour reminder is the final one.

## Behavior

- A new Reel's first successfully downloaded thumbnail becomes its baseline.
- Covers are compared as normalized 64×64 RGB images, not by their CDN URLs.
  The default difference threshold is 5% mean absolute pixel difference.
- Two consecutive hourly observations must differ from the baseline and agree
  with each other before a change cancels the remaining reminders. An unchanged
  image, failed check, or gap longer than 90 minutes resets confirmation.
- A tentative change defers a due reminder for another check. Otherwise, reminders
  arrive on the first successful hourly check at or after each deadline, normally
  within an hour. Outages, rate limits, and verification failures can delay delivery.
- Missing/unreadable thumbnails and API errors never count as unchanged covers.
  The service defers delivery until it can check the current cover successfully.
- After an outage that spans both deadlines, only the 48-hour reminder is sent.
- Only Reels published after the **first successful activation** are eligible.
  Restarts preserve that activation time, post state, and the polling schedule.

The API does not provide a documented cover-edit flag. An edit before the first
check, a visually subtle edit, or a cached thumbnail can result in an unnecessary
reminder. The message therefore says that no change was **detected**. The worker
does not publish posts or change covers.

## Set up on an always-on Docker server

1. Create a Telegram bot with BotFather if needed, start its private chat, and
   obtain that chat's numeric ID. An existing group destination also works if the
   bot can send messages there. Use a dedicated destination for this monitor.
2. Find the Composio project API key and the connected-account ID for your
   Instagram Business/Creator account in your Composio dashboard.
3. Copy `.env.example` to `.env` and fill in the four empty credential/destination
   fields. Keep `.env` private and out of Git. The pinned toolkit version is
   `20260819_00`; `INSTAGRAM_USER_ID=me` selects the connected Instagram account.
4. Build and check the real connections:

   ```sh
   docker compose build
   docker compose run --rm reminder check
   docker compose run --rm reminder send-test
   ```

`check` reads Instagram media and checks the Telegram bot token. It prints recent
Reel IDs for the next step. `send-test` sends a real notification to your configured
destination and verifies delivery permission. No AI-model key is needed.

### Verify that Instagram exposes cover edits

Monitoring stays disabled until this live test passes. This avoids silently
assuming that the API's thumbnail reflects your edits.

Choose a Reel whose cover you intend to change. Use its numeric `media_id` from
`check`, not the shortcode in its Instagram URL. In your shell, set `REEL_ID` to
that numeric ID, then capture the cover:

```sh
docker compose run --rm reminder capture-cover "$REEL_ID" --output /data/verification/before.img
```

Change that Reel's cover in Instagram. Once the change is visible, capture it:

```sh
docker compose run --rm reminder capture-cover "$REEL_ID" --output /data/verification/after.img
```

Leave the new cover unchanged and capture it again one hour later:

```sh
docker compose run --rm reminder capture-cover "$REEL_ID" --output /data/verification/confirmation.img
docker compose run --rm reminder compare-covers /data/verification/before.img /data/verification/after.img
docker compose run --rm reminder verify-cover /data/verification/before.img /data/verification/after.img /data/verification/confirmation.img
```

Each capture saves the actual thumbnail bytes and a sidecar containing its media
ID, account identity, capture time, detector settings, and image checksum. It does
not save API keys, bot tokens, or signed image URLs. Verification requires both
later covers to exceed the baseline threshold and agree with each other.

If verification fails, take fresh captures after allowing for API caching; use
new filenames because captures cannot be overwritten. If changes remain invisible,
this detection method cannot be enabled for your account. Do not bypass the gate.
Changing the toolkit version or comparison threshold requires new verification.
Before selecting a different threshold, also compare two captures taken without
an edit to check that normal image variation stays below it.

Start the monitor after verification:

```sh
docker compose up -d
docker compose logs --tail=100 reminder
docker compose exec reminder python -m cover_reminder status
```

The verification Reel predates activation and will not generate reminders.
The container runs as a non-root user; SQLite, captures, and verification evidence
live in the persistent `reminder-data` volume.

## Configuration

| Variable | Meaning |
| --- | --- |
| `COMPOSIO_API_KEY` | Composio project API key; required |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | Existing Instagram connected-account ID; required |
| `INSTAGRAM_USER_ID` | `me` by default, or the numeric Instagram account ID |
| `INSTAGRAM_TOOLKIT_VERSION` | Pinned dated version; default `20260819_00` |
| `TELEGRAM_BOT_TOKEN` | BotFather token; required |
| `TELEGRAM_CHAT_ID` | Telegram destination; required |
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
  repeated after restart. If Telegram accepts a message but the response is lost,
  a retry can produce a duplicate; the Bot API has no send idempotency key.
- Temporary HTTP failures receive up to three attempts with backoff. Long
  `Retry-After` delays are stored across restarts and respected. Unsent reminders
  remain pending and are rechecked against the current cover before another attempt.
- `status` shows post/reminder counts and the last cycle's errors. Health is unhealthy
  if the last cycle failed or no successful cycle completed in 125 minutes.
  Docker reports health; `restart: unless-stopped` restarts exited processes, not
  unhealthy running processes. Have your server monitor container health.
- Logs contain media IDs and sanitized error codes, not credentials, signed URLs,
  captions, or raw provider responses. For `composio:tool_execution_failed`, inspect
  the execution in your Composio dashboard for the provider's detailed error.
- An inaccessible/deleted Reel remains unverifiable. It does not produce a cover
  reminder; its repeated check failures appear in logs and health.
- Discovery follows all pages and advances its checkpoint only after the whole scan
  succeeds. A one-hour overlap accommodates timestamp boundaries and brief indexing
  delays. Longer provider indexing delays can still cause missed posts.
- Stop the worker before backing up the entire data volume. Restore that volume
  to preserve activation, verification, and reminder history. Do not remove the
  volume during routine redeployment. Use a separate database for a different
  account or Telegram destination.

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
Pillow reference photographs. They do not fabricate Instagram/Telegram responses.
The optional live test reads your connected account:

```sh
RUN_LIVE_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_live.py -v
```

Live acceptance requires: successful `check` and `send-test`; passing cover-edit
verification; an unchanged Reel receiving both reminders; and an edited Reel
cancelling the applicable remaining reminder. Do not claim those checks passed
based on the offline suite. `run --once` respects the persisted hourly schedule and
the live-verification gate.

## API references

- [Composio Instagram actions and fields](https://docs.composio.dev/toolkits/instagram)
- [Composio REST tool execution](https://docs.composio.dev/reference/api-reference/tools/postToolsExecuteByToolSlug)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Pillow image operations](https://pillow.readthedocs.io/en/stable/reference/Image.html)
- [Docker Compose service configuration](https://docs.docker.com/reference/compose-file/services/)
