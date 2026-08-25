# TASK.md - French Learning Bot (frbot)

Personal Telegram bot for French learning (B1 to B2). Single user. The bot turns words captured from daily life into spaced-repetition cards, corrects short writing, and drills grammar. Every mistake becomes a new card.

This spec is written for Claude Code. Build phase by phase. One branch per phase (`phase-0`, `phase-1`, ...). A phase is done when its acceptance criteria pass, tests are green, and `ruff check` is clean.

---

## 1. Goals

- Capture: save any French word or phrase in under 5 seconds by sending it to the bot.
- Enrich: each capture becomes a structured card via the Claude API.
- Review: daily spaced-repetition sessions scheduled with FSRS.
- Write: one short daily writing prompt with correction and per-error explanations.
- Drill: weekly grammar topics as cloze exercises (fill-the-gap sentences).
- Error loop: errors from writing and drills become new cards automatically.

## 2. Non-goals

- No multi-user support beyond a whitelist of one user id.
- No gamification: no streaks, no XP, no leagues.
- No webhook server. Long polling only.
- No audio in v1 (planned as a later extension).
- No web UI.

## 3. Tech stack (verified August 2026)

| Component | Choice | Version |
|---|---|---|
| Language | Python | 3.13 (3.14 is still RC) |
| Package manager | uv | latest |
| Bot framework | aiogram | >= 3.30 (Bot API 10.2, fully async) |
| Spaced repetition | fsrs (py-fsrs) | >= 6.3.1 (FSRS-6 algorithm) |
| LLM | anthropic SDK | latest |
| DB | SQLite via SQLAlchemy 2.0 async + aiosqlite | latest |
| Migrations | alembic | latest |
| Config | pydantic-settings (pydantic v2) | latest |
| Scheduler | APScheduler (AsyncIOScheduler) | 3.x stable |
| Lint + format | ruff | latest |
| Tests | pytest + pytest-asyncio | latest |

Models (Claude API, current IDs from https://platform.claude.com/docs/en/models/overview):

- `MODEL_FAST = claude-haiku-4-5-20251001` for enrichment and cloze generation ($1/$5 per MTok).
- `MODEL_SMART = claude-sonnet-5` for writing correction ($2/$10 per MTok).

Both are env-configurable. If an ID is retired, check the models overview page and update `.env`.

## 4. Repository layout

```
frbot/
  pyproject.toml            # uv-managed
  README.md
  docs/TASK.md              # this file
  .env.example
  alembic/                  # migrations
  src/frbot/
    __main__.py             # entry point: python -m frbot
    config.py               # pydantic-settings
    db/
      models.py             # SQLAlchemy models
      session.py            # engine + async session factory
      repo.py               # query functions
    llm/
      client.py             # anthropic wrapper, JSON parsing, retries
      prompts.py            # all prompt templates
      schemas.py            # pydantic schemas for LLM JSON output
    srs/
      scheduler.py          # fsrs wrapper (Scheduler, Card serialization)
      queue.py              # builds the daily review queue
    bot/
      middleware.py          # whitelist
      handlers/
        capture.py
        review.py
        write.py
        drill.py
        stats.py
        settings.py
      keyboards.py          # inline keyboards
    jobs/
      reminders.py          # APScheduler jobs
  tests/
    fixtures/               # canned LLM JSON outputs, incl. malformed ones
    test_*.py
  data/                     # sqlite file + backups (gitignored)
```

## 5. Configuration (.env)

```
BOT_TOKEN=
ANTHROPIC_API_KEY=
ALLOWED_USER_ID=            # single Telegram user id
TZ=Europe/Paris
DB_URL=sqlite+aiosqlite:///data/frbot.db
MODEL_FAST=claude-haiku-4-5-20251001
MODEL_SMART=claude-sonnet-5
DAILY_NEW_LIMIT=15          # max new cards introduced per day
SESSION_MAX=30              # max cards per review session
REMINDER_TIME=08:30         # daily due-count reminder
WRITING_TIME=19:00          # daily writing prompt
DESIRED_RETENTION=0.9
```

## 6. Data model

All timestamps in UTC. `Europe/Paris` is applied only for display and job triggers.

```
cards
  id            int pk
  text          str            # original capture or generated front
  lemma         str            # normalized form, used for dedupe
  kind          enum: vocab | error | drill_error
  enrichment    json           # schema in section 8.1, null for error cards
  error_meta    json           # for error cards: {type, original, corrected, explanation_ru}
  fsrs          json           # fsrs Card.to_dict()
  due           datetime       # duplicated from fsrs for indexed queries
  state         str            # duplicated from fsrs (New/Learning/Review/Relearning)
  created_at    datetime
  suspended     bool default false

reviews
  id, card_id fk, rating int (1-4), reviewed_at datetime, elapsed_days float

writings
  id, prompt str, answer str, corrections json, created_at datetime

drill_topics
  id, slug str, title_fr str, position int, active_week date null

app_settings
  key str pk, value str        # runtime-editable settings (/settings)
```

Dedupe rule: before creating a vocab card, lower-case and strip the input, ask the enrichment call for the lemma, and check `cards.lemma`. If it exists, reply with the existing card instead of creating a duplicate.

## 7. Bot UX

All handlers are restricted by whitelist middleware. Messages from other user ids get no reply.

### 7.1 Capture (default handler)

Any plain text message that is a command is routed to its handler. Any other text message is a capture.

1. User sends a word or phrase (example: `au fur et à mesure`).
2. Bot calls enrichment (MODEL_FAST), saves the card, and replies with a compact preview: lemma with gender, IPA, short French definition, Russian translation, one example.
3. Inline buttons under the preview: `Delete`, `Regenerate`.

Latency target: preview within a few seconds. Show a `typing` chat action while waiting.

### 7.2 /review

1. Build queue: all cards with `due <= now`, ordered by `due`, capped at SESSION_MAX. Then add new cards (state New) up to DAILY_NEW_LIMIT.
2. For each card, send the front. Vocab card front: the French lemma. Error card front: the corrected sentence with the error span replaced by a gap.
3. Button `Show answer` reveals the back: definition, translation, examples, collocations, register (vocab) or the correct form plus the one-line explanation (error).
4. Grade buttons: `Again`, `Hard`, `Good`, `Easy`. Map to fsrs `Rating.Again/Hard/Good/Easy`, update the card, log the review, send the next card.
5. Show progress in each message (`7/23`). End with a short summary: reviewed, again-count, next due count for tomorrow.

Use aiogram FSM for session state. A session survives bot restarts only as far as the DB state allows; it is fine to re-run /review.

### 7.3 /write

1. Pick 3 words: prefer cards due today, else recent captures.
2. Send a one-line situation prompt in French plus the 3 words to use.
3. User replies with 2 or 3 sentences.
4. Correction call (MODEL_SMART). Reply format: corrected text, then a numbered error list, each with a one-line explanation in Russian.
5. Each error becomes an error card (max 5 per day, dedupe on `type + corrected`).

The daily WRITING_TIME job sends the same prompt automatically. /write triggers it on demand.

### 7.4 /drill

1. One grammar topic per week, rotating through `drill_topics` by `position`.
2. Generate 5 cloze items (MODEL_FAST) for the topic. Reuse the user's own vocabulary in the sentences where possible.
3. Each item: sentence with a gap, 3 option buttons, one correct. Immediate feedback plus a one-line explanation in Russian.
4. Wrong answers become `drill_error` cards.

Seed topics, in this order (first block covers known weak points):

1. avoir vs être in passé composé
2. Noun gender
3. depuis / pendant / il y a
4. de after negation (pas de, plus de)
5. Tense agreement in conditionals (si-clauses)
6. Subjonctif présent after il faut que, vouloir que
7. Pronouns y and en
8. Object pronoun order (COD / COI)
9. Relative pronouns qui / que / dont
10. Futur simple vs conditionnel

### 7.5 /stats

Reply with: due today, reviews in the last 7 days, correct rate (Good + Easy share), new cards in the last 7 days, top 5 error types over 30 days.

### 7.6 /settings

Inline-keyboard editing of: REMINDER_TIME, WRITING_TIME, DAILY_NEW_LIMIT, SESSION_MAX. Values persist in `app_settings` and override .env at runtime.

### 7.7 /start, /help

Short usage text. /start also stores the chat id for scheduled jobs.

## 8. Claude API usage

General rules for `llm/client.py`:

- `max_tokens=1500`, temperature 0.2 for enrichment and drills, 0.0 for correction.
- Prompts instruct: respond with JSON only, no markdown fences, no preamble.
- Parse into pydantic schemas (`llm/schemas.py`). On validation error: one retry with the validation error appended to the prompt. On second failure: log and tell the user something went wrong.
- Log input/output token counts per call.

### 8.1 Enrichment (MODEL_FAST)

System prompt: French tutor for a Russian-speaking B1 learner. Return JSON:

```json
{
  "lemma": "str",
  "pos": "noun|verb|adj|adv|expression|other",
  "gender": "m|f|null",
  "ipa": "str",
  "definition_fr": "simple French, max 15 words",
  "translation_ru": "str",
  "translation_en": "str",
  "examples": [{"fr": "B1-level sentence", "ru": "translation"}],
  "collocations": ["up to 5 frequent word partners"],
  "register": "neutre|familier|soutenu",
  "notes": "false friends, gender hints, common mistakes; empty string if none"
}
```

`examples` has exactly 3 items.

### 8.2 Writing correction (MODEL_SMART)

Input: the prompt and the user answer. Return JSON:

```json
{
  "corrected_text": "str",
  "errors": [
    {
      "original": "str",
      "corrected": "str",
      "type": "gender|auxiliary|preposition|tense|agreement|vocab|spelling|word_order|other",
      "explanation_ru": "one short line"
    }
  ],
  "comment_ru": "one encouraging line, no flattery"
}
```

### 8.3 Cloze generation (MODEL_FAST)

Input: topic, list of up to 20 user lemmas to reuse. Return JSON:

```json
{
  "items": [
    {
      "sentence_with_gap": "Je ___ allé au marché hier.",
      "options": ["suis", "ai", "es"],
      "correct": "suis",
      "explanation_ru": "one short line"
    }
  ]
}
```

`items` has exactly 5 entries. Exactly one option is correct.

## 9. FSRS integration (`srs/scheduler.py`)

- `from fsrs import Scheduler, Card, Rating` (package `fsrs`, FSRS-6, 21 parameters).
- One `Scheduler(desired_retention=DESIRED_RETENTION)` instance with default parameters. Custom parameter optimization is out of scope for v1.
- Persist cards with `Card.to_dict()` into `cards.fsrs`; restore with `Card.from_dict()`. After every review, also copy `due` and `state` into their dedicated columns.
- Rating map: Again=1, Hard=2, Good=3, Easy=4.
- Error and drill_error cards use the same scheduler as vocab cards.

## 10. Scheduled jobs (`jobs/reminders.py`)

APScheduler `AsyncIOScheduler` with timezone Europe/Paris, started next to the polling loop.

- Daily at REMINDER_TIME: message with due count and a `Start review` button (deep link to /review).
- Daily at WRITING_TIME: the writing prompt (section 7.3).
- Weekly, Sunday 18:00: stats summary (section 7.5) and rotation to the next drill topic.
- Daily at 03:00: copy the sqlite file to `data/backups/frbot-YYYY-MM-DD.db`, keep the last 14.

## 11. Error handling and logging

- Stdlib logging, INFO level, one line per event (update received, LLM call, job run).
- Telegram API errors: aiogram retries transient errors; log and skip otherwise.
- Anthropic API errors: 3 retries with exponential backoff on 429/5xx, then a user-visible failure message.
- The bot must never crash on a malformed LLM response. Tests cover this path with fixtures.

## 12. Testing

- pytest + pytest-asyncio, in-memory sqlite (`sqlite+aiosqlite:///:memory:`).
- No live network calls. The anthropic client is mocked with fixture JSON files, including malformed ones.
- Required unit coverage: dedupe logic, queue builder (due ordering, new-card limit), fsrs wrapper round-trip (to_dict/from_dict, rating updates move `due` forward), all three JSON schemas (valid, invalid, retry path), error-card creation from corrections.
- Handler tests: call handlers directly with constructed aiogram objects and a fake bot; assert reply text and keyboards.

## 13. Phases

### Phase 0: scaffold

uv project, ruff, pytest, config, alembic with initial migration, bot skeleton, whitelist middleware, /start and /help.

Accept: bot runs with long polling; /start answers the allowed user; any other user id gets nothing; tests and ruff pass.

### Phase 1: capture and enrichment

Capture handler, anthropic client with JSON parsing and retry, enrichment schema, card storage with dedupe, preview message, Delete and Regenerate buttons.

Accept: sending a word returns an enriched preview within seconds; sending the same word again returns the existing card; Delete removes it; malformed-LLM fixture test passes.

### Phase 2: review and FSRS

fsrs wrapper, queue builder, /review session with FSM, grade buttons, reviews log, /stats (basic), daily reminder job, backup job.

Accept: a card graded `Good` gets a later `due` than one graded `Again`; session respects SESSION_MAX and DAILY_NEW_LIMIT; reminder fires at REMINDER_TIME with the correct due count.

### Phase 3: writing loop

/write flow, scheduled writing prompt, correction call, correction rendering, error cards with dedupe and daily cap.

Accept: an answer with a known error (example: `je suis allé au marché depuis hier`) produces a correction, an explanation in Russian, and a new error card that appears in the next /review.

### Phase 4: drills, settings, weekly stats

drill_topics seed and weekly rotation, /drill flow with option buttons, drill_error cards, /settings, weekly stats job.

Accept: /drill serves 5 items for the active topic; a wrong answer creates a drill_error card; changing REMINDER_TIME via /settings moves the job.

## 14. Definition of done (global)

- `uv run python -m frbot` starts the bot on a clean clone with a filled .env.
- `uv run pytest` and `uv run ruff check .` pass on every phase branch before merge.
- README documents setup, .env keys, and how to run with systemd (a sample unit file is included).
- No secrets in the repo. `data/` is gitignored.
