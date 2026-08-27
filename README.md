# frbot — French Learning Telegram Bot

A Telegram tutor for French (A2 → B2). Words you meet during the day become
spaced-repetition cards, short daily writing gets corrected, grammar is drilled
weekly — and **every mistake you make becomes a new card**. Invite-only: one
bot instance serves a small closed cohort (default cap: 50 learners), each with
their own deck, level, and schedule.

Powered by [aiogram](https://aiogram.dev) (long polling, no webhooks),
[FSRS-6](https://github.com/open-spaced-repetition/py-fsrs) scheduling, and
Gemini 3.5 Flash-Lite for enrichment, correction, and conversation — including
voice-note understanding.

Docs: [build spec](docs/TASK.md) · [running a pilot](docs/PILOT.md).

## The learning loop

1. **Capture** — text the bot any French word or phrase, or say it in a voice
   note. In a few seconds it becomes a card: lemma with gender, IPA, a simple
   French definition, RU/EN translations, three example sentences,
   collocations, register, and pitfalls for Russian speakers.
2. **Review** — a morning reminder tells you what's due; `/review` runs a
   classic show-answer → *Again / Hard / Good / Easy* session, scheduled by
   FSRS-6 with your daily-new and session-size limits.
3. **Produce** — the evening `/write` prompt (answer by text or voice) and
   free-form `/talk` conversation make you use the language; corrections come
   back with one-line explanations in Russian.
4. **Close the loop** — errors from writing, conversation, and drills turn
   into cloze cards that show up in your next review.

## Commands

| Command | What it does |
|---|---|
| *(any text or voice note)* | capture a word/phrase as a card |
| `/review` | spaced-repetition session for everything due |
| `/write` | writing prompt of the day; reply by text or voice |
| `/talk` | free conversation: French replies + parallel corrections |
| `/topic ресторан 10` | generate a pack of B2 words on any topic, pick which to keep |
| `/drill` | weekly grammar topic, 5 fill-the-gap exercises |
| `/stats` | due today, 7-day accuracy, top error types |
| `/settings` | change reminder/writing times and limits on the fly |
| `/stop` | cancel the current dialogue/session |
| `/level` | switch level (A2 / B1 / B2) — recalibrates every AI prompt |
| `/feedback` | write to the bot's author |
| `/help` | command reference |

Admin-only (invisible to participants): `/invite [count] [uses]` generates
join links, `/users` shows the roster with each person's 7-day activity, and
`/broadcast <text>` messages the cohort.

Every card preview has **Delete** and **Regenerate** buttons; duplicates are
detected by lemma, so sending the same word twice never creates two cards.

## On a schedule (Europe/Paris by default)

- **08:30** — reminder with the number of cards due and a *Start review* button
- **19:00** — the daily writing prompt
- **Sunday 18:00** — each participant's week in review + next week's grammar topic
- **03:00** — SQLite online backup to `data/backups/` (last 14 kept)

The two daily times are **per participant** — each person sets their own in
`/settings`, and the change takes effect within the minute. The weekly grammar
topic is shared by the whole cohort and derived from the ISO week number, so
everyone drills the same thing at the same time.

## Setup

You need Python 3.13, [uv](https://docs.astral.sh/uv/), and three values:

1. **Telegram bot token** — talk to [@BotFather](https://t.me/BotFather),
   `/newbot`, copy the token.
2. **Gemini API key** — create one in
   [Google AI Studio](https://aistudio.google.com/apikey).
3. **Your Telegram user id** — message [@userinfobot](https://t.me/userinfobot)
   (a number like `123456789`). You become the admin automatically; everyone
   else needs an invite.

```bash
git clone <repo-url> frbot
cd frbot
uv sync
cp .env.example .env   # fill in BOT_TOKEN, GEMINI_API_KEY, ADMIN_USER_ID
uv run python -m frbot
```

Migrations run automatically on startup (or manually:
`uv run alembic upgrade head`). Send `/start` once to register yourself.

### Inviting people

```
/invite 10        → ten single-use join links
/invite 1 25      → one link that 25 people can use
```

Each link is `https://t.me/<yourbot>?start=CODE`. New participants pick their
level on arrival and get a three-step first-run message. `/users` tells you who
is actually practising — the number that matters during a pilot.

## Configuration (.env)

| Key | Meaning | Default |
|---|---|---|
| `BOT_TOKEN` | Telegram bot token from @BotFather | required |
| `GEMINI_API_KEY` | Gemini API key (Google AI Studio) | required |
| `ADMIN_USER_ID` | your numeric Telegram user id (bot owner, auto-registered as admin) | required |
| `MAX_USERS` | hard cap on registrations | `50` |
| `DAILY_LLM_ACTIONS` | per-user daily cap on AI actions (reviews are free) | `150` |
| `TZ` | timezone for display and scheduled jobs | `Europe/Paris` |
| `DB_URL` | SQLAlchemy async URL | `sqlite+aiosqlite:///data/frbot.db` |
| `MODEL_FAST` | model for enrichment, cloze, topic packs, voice transcription | `gemini-3.5-flash-lite` |
| `MODEL_SMART` | model for writing correction and /talk conversation | `gemini-3.5-flash-lite` |
| `DAILY_NEW_LIMIT` | max new cards introduced per day | `15` |
| `SESSION_MAX` | max cards per review session | `30` |
| `REMINDER_TIME` | daily due-count reminder (HH:MM, local TZ) | `08:30` |
| `WRITING_TIME` | daily writing prompt (HH:MM, local TZ) | `19:00` |
| `DESIRED_RETENTION` | FSRS target retention (0.5–0.995) | `0.9` |

`REMINDER_TIME`, `WRITING_TIME`, `DAILY_NEW_LIMIT`, and `SESSION_MAX` can also
be changed from the chat via `/settings`; runtime values override `.env`.

Both model knobs default to `gemini-3.5-flash-lite`. If you ever want stronger
corrections or conversation, point `MODEL_SMART` at a bigger Gemini model — no
code changes needed.

## Running as a service (systemd)

`/etc/systemd/system/frbot.service`:

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

Long polling only — no inbound ports, no TLS, no reverse proxy.

## Data

Everything lives in one SQLite file, `data/frbot.db` (gitignored): participants,
invites, cards with their FSRS state, review log, writings with corrections,
and drill topics. Every card, review, and writing is owned by exactly one user;
the query layer requires an explicit owner on every read, so no participant can
see another's deck.

Scale: SQLite plus long polling is comfortable to roughly 5,000 daily users —
about 100× the default pilot cap — so nothing here needs Postgres or webhooks
before then. A nightly job snapshots it with SQLite's online-backup API to
`data/backups/frbot-YYYY-MM-DD.db` and prunes to the last 14 — restoring is
just copying a snapshot back.

## Development

```bash
uv run pytest          # 216 tests; no network — Telegram and the LLM are faked
uv run ruff check .    # lint
uv run ruff format .   # format
uv run alembic upgrade head   # apply migrations manually
```

Layout (`src/frbot/`): `bot/handlers/` — one module per command plus capture;
`bot/middleware.py` — the invite/roster gate that resolves each sender;
`llm/` — the Gemini client, prompt templates, and strict pydantic schemas for
every JSON contract (malformed output triggers one corrective retry, never a
crash); `srs/` — the FSRS wrapper and review-queue builder; `db/` — SQLAlchemy
models and queries; `jobs/` — the APScheduler jobs. Tests fake the Telegram
API with a recording session and the LLM with fixture JSON, so the whole bot —
routing, FSM sessions, jobs — runs under pytest in about a second.
