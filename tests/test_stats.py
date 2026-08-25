from datetime import UTC, datetime, timedelta

from frbot.bot.handlers.stats import cmd_stats
from frbot.db import repo
from frbot.db.models import Card, Review
from frbot.srs.scheduler import SrsScheduler
from tests.fakes import add_vocab_card, make_message


def now() -> datetime:
    return datetime.now(UTC)


async def test_stats_message(fake_bot, session_factory, settings):
    # One due card, one new.
    await add_vocab_card(session_factory, "dû", reviewed_days_ago=3, due=now() - timedelta(hours=1))
    await add_vocab_card(session_factory, "nouveau")

    async with session_factory() as session:
        # 4 reviews this week: 3 correct (Good/Easy), 1 Again.
        for rating in (3, 4, 3, 1):
            session.add(Review(card_id=1, rating=rating, reviewed_at=now(), elapsed_days=0.0))
        # An old review outside the window.
        session.add(
            Review(
                card_id=1,
                rating=1,
                reviewed_at=now() - timedelta(days=10),
                elapsed_days=0.0,
            )
        )
        # Two error cards from this month with a repeated type.
        srs = SrsScheduler(desired_retention=0.9)
        for i, err_type in enumerate(["preposition", "preposition", "gender"]):
            new = srs.new_card()
            session.add(
                Card(
                    text=f"phrase {i}",
                    lemma=f"err-{i}",
                    kind="error",
                    error_meta={"type": err_type, "original": "x", "corrected": "y"},
                    fsrs=new.fsrs,
                    due=new.due,
                    state=new.state,
                )
            )
        await session.commit()

    await cmd_stats(make_message("/stats", bot=fake_bot), session_factory, settings)
    text = fake_bot.session.sent_messages[0].text
    assert "Повторений за 7 дней: 4" in text
    assert "75%" in text
    assert "preposition — 2" in text
    assert "gender — 1" in text


async def test_stats_with_empty_db(fake_bot, session_factory, settings):
    await cmd_stats(make_message("/stats", bot=fake_bot), session_factory, settings)
    text = fake_bot.session.sent_messages[0].text
    assert "К повторению сегодня: 0" in text
    assert "—" in text  # no correct rate yet


async def test_effective_config_overrides(session_factory, settings):
    async with session_factory() as session:
        cfg = await repo.get_effective_config(session, settings)
        assert cfg.session_max == 30
        await repo.set_setting(session, "SESSION_MAX", "10")
        await repo.set_setting(session, "REMINDER_TIME", "07:15")
        await repo.set_setting(session, "DAILY_NEW_LIMIT", "not-a-number")
        await session.commit()
        cfg = await repo.get_effective_config(session, settings)
    assert cfg.session_max == 10
    assert cfg.reminder_time == "07:15"
    assert cfg.daily_new_limit == 15  # invalid override falls back to env
    assert cfg.writing_time == "19:00"
