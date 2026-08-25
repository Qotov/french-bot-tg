# frbot — French Learning Telegram Bot

Personal Telegram bot for French learning (B1 → B2), single user. Captures words
into spaced-repetition cards (FSRS), corrects short daily writing, drills grammar
with cloze exercises, and turns every mistake into a new card.

Full spec: [docs/TASK.md](docs/TASK.md).

## Features

- **Capture** — send any French word or phrase; it becomes a structured card
  (lemma, IPA, definition, RU/EN translation, examples, collocations) via the
  Claude API in a few seconds.
- **/review** — daily spaced-repetition sessions scheduled with FSRS-6.
- **/write** — one short daily writing prompt; corrections come back with
  per-error explanations in Russian, and each error becomes a new card.
- **/drill** — weekly grammar topic with 5 cloze exercises; wrong answers
  become cards.
- **/stats** — due counts, review accuracy, top error types.
- **/settings** — edit reminder/writing times and limits at runtime.

## Setup

Requirements: Python 3.13, [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url> frbot
cd frbot
uv sync
cp .env.example .env   # fill in the values below
uv run alembic upgrade head
uv run python -m frbot
```

## .env keys

| Key | Meaning | Default |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather | required |
| `ANTHROPIC_API_KEY` | Claude API key | required |
| `ALLOWED_USER_ID` | your numeric Telegram user id (the only whitelisted user) | required |
| `TZ` | display/job timezone | `Europe/Paris` |
| `DB_URL` | SQLAlchemy async URL | `sqlite+aiosqlite:///data/frbot.db` |
| `MODEL_FAST` | model for enrichment + cloze | `claude-haiku-4-5-20251001` |
| `MODEL_SMART` | model for writing correction | `claude-sonnet-5` |
| `DAILY_NEW_LIMIT` | max new cards introduced per day | `15` |
| `SESSION_MAX` | max cards per review session | `30` |
| `REMINDER_TIME` | daily due-count reminder (HH:MM, local TZ) | `08:30` |
| `WRITING_TIME` | daily writing prompt (HH:MM, local TZ) | `19:00` |
| `DESIRED_RETENTION` | FSRS target retention | `0.9` |

`REMINDER_TIME`, `WRITING_TIME`, `DAILY_NEW_LIMIT`, and `SESSION_MAX` can also be
changed at runtime via `/settings`; runtime values are stored in the DB and
override `.env`.

## Development

```bash
uv run pytest          # tests (no network; LLM + Telegram are faked)
uv run ruff check .    # lint
uv run ruff format .   # format
uv run alembic upgrade head   # apply migrations
```

## Running with systemd

Sample unit file (`/etc/systemd/system/frbot.service`):

```ini
[Unit]
Description=frbot French learning Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=frbot
WorkingDirectory=/opt/frbot
ExecStart=/usr/local/bin/uv run python -m frbot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now frbot
journalctl -u frbot -f
```

The bot uses long polling — no inbound ports or webhooks needed. The SQLite file
lives in `data/` (gitignored); nightly backups are written to `data/backups/`
and the last 14 are kept.
