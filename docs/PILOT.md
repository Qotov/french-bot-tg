# Running the pilot

The operational companion to the [pilot plan](https://claude.ai/code/artifact/d277d57d-6373-4cfc-8724-7dd424732a63).
Everything here is what you actually do, week by week, with 20–50 participants.

## Before the first invite

1. **Deploy.** A $5 VPS is plenty (see the systemd unit in the README). Set
   `ADMIN_USER_ID` to your own Telegram id; you are registered automatically.
2. **Set the roster cap.** `MAX_USERS=30` for the first cohort — a full slot is
   a better signal than an empty one, and you can raise it any time.
3. **Smoke-test as a participant.** Generate one invite, join from a second
   Telegram account, and run the whole loop once: capture a word, `/topic`,
   `/review`, `/write` with a voice answer, `/talk`, `/drill`. Fifteen minutes
   now saves a bad first impression for thirty people.
4. **Check the backup.** `ls data/backups/` after the first 03:00 run.

## The invite flow

```
/invite 10          → 10 single-use links
/invite 1 25        → 1 link usable by 25 people
```

Give **single-use links to people you know** (you can see who used which code)
and a **multi-use link to a community post**. Codes are recorded on the user
row as `invite_code`, so `/users` plus the code tells you which channel
produced people who actually stayed — the only acquisition number worth having.

Recommended sequence:

| Week | Invites | Where |
|---|---|---|
| 1 | 10 single-use | friends, colleagues, people who already asked |
| 2 | 10 single-use | RU French-learning chats: answer a question, then offer |
| 3 | 1 × 20 multi-use | one relocation / DELF-prep community post |

## The message you send

Short, honest, with obligations. Copy this:

> 🇫🇷 Собираю закрытую бету бота для французского.
>
> Кидаешь слово — получаешь карточку с примерами и произношением. Пишешь пару
> предложений в день — получаешь разбор ошибок по-русски. Можно голосом. Каждая
> твоя ошибка сама становится карточкой и вернётся на повторении. 10–15 минут в день.
>
> Условия: 6 недель бесплатно, заниматься хотя бы 4 дня в неделю и пару раз
> ответить на вопросы (/feedback). Мест 30.
>
> Ссылка: https://t.me/<bot>?start=CODE

Decline A0–A1 politely: the bot corrects production, and a true beginner has
none yet. Point them at a textbook first and offer the next cohort.

## Weekly rhythm

**Monday** — `/users`. Write down two numbers: how many were active last week,
and how many were active ≥4 days. These are the only metrics that decide
anything.

**Wednesday** — read the week's `/feedback` messages. Fix the single most-cited
problem; ignore the rest for now. One fix per week beats five half-fixes.

**Sunday evening** — the weekly summary goes out automatically with each
person's stats and next week's grammar topic. Watch for anyone whose numbers
collapsed and send them one human message. Not a nag: ask what got in the way.

## Reading the numbers

Run at week 4 and week 8 against the plan's gates:

- **Active in week 4 ≥ 40%** and **≥ 25% active 4+ days/week** → go to payments.
- **20–40%** → one focused iteration, re-measure at week 6.
- **< 20%** → the product is personal, not commercial. Stop spending.

Two caveats when reading `/users`:

- Activity counts **reviews**, not messages. Someone who captures words but
  never reviews is not learning and will churn — talk to them.
- A participant who joined on day 20 cannot show four weeks of activity. Judge
  cohorts by their own week number, not the calendar.

## Costs

Roughly **$0.55–1.30 per active user per month** in Gemini fees, plus the VPS.
Thirty active participants ≈ $25–40/month total. `DAILY_LLM_ACTIONS=150` caps
per-user spend; reviews and stats never consume it. If a bill surprises you,
grep the logs for `llm call model=` to see the volume, and for
`hit the daily LLM limit` to see who is hitting the ceiling.

## When something breaks

- **Someone reports the bot is silent.** Check they are registered
  (`/users`) and not over the daily cap. Unregistered senders get nothing by
  design.
- **A participant is stuck in a session.** Tell them `/stop` — it cancels any
  active flow.
- **Bad AI output.** The model is a config switch: point `MODEL_SMART` at a
  stronger Gemini model and restart. No code change.
- **Restore from backup.** Stop the service, copy
  `data/backups/frbot-YYYY-MM-DD.db` over `data/frbot.db`, start it again.
- **Remove someone.** Set `active = 0` on their `users` row; their data stays
  intact and they simply stop being served.

## What not to build during the pilot

Streaks, XP, leaderboards, a web app, more languages, referral rewards. None of
them move the week-4 retention number, and that number is the entire point.
