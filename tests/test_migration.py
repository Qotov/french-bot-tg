"""The upgrade path from a real single-user install.

An existing personal deck must survive becoming user #1 of a cohort: cards keep
their owner and /settings choices are not silently reset to the .env defaults.
"""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent


def _config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture
def legacy_db(tmp_path, monkeypatch) -> Path:
    """A database as it looked before the pilot: one implicit user."""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(_config(db_path), "0001")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)",
        [
            ("REMINDER_TIME", "07:15"),
            ("WRITING_TIME", "21:45"),
            ("SESSION_MAX", "12"),
            ("chat_id", "555"),
        ],
    )
    conn.execute(
        "INSERT INTO cards (text, lemma, kind, fsrs, due, state, created_at, suspended) "
        "VALUES ('maison','maison','vocab','{}','2026-01-01','New','2026-01-01',0)"
    )
    conn.execute(
        "INSERT INTO writings (prompt, created_at) VALUES ('Décris ta journée.','2026-01-01')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_upgrade_assigns_ownership_and_keeps_settings(legacy_db, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "555")
    command.upgrade(_config(legacy_db), "head")

    conn = sqlite3.connect(legacy_db)
    user = conn.execute(
        "SELECT id, chat_id, reminder_time, writing_time, session_max, daily_new_limit FROM users"
    ).fetchone()
    cards = conn.execute("SELECT lemma, user_id FROM cards").fetchall()
    writings = conn.execute("SELECT user_id FROM writings").fetchall()
    conn.close()

    assert user == (555, 555, "07:15", "21:45", 12, None)
    assert cards == [("maison", 555)]
    assert writings == [(555,)]


def test_upgrade_without_an_owner_configured_is_still_safe(legacy_db, monkeypatch):
    """A fresh install with no owner configured: the schema still upgrades and
    rows are left unclaimed rather than assigned to an arbitrary id."""
    import dotenv

    # alembic/env.py calls load_dotenv(), which would re-populate the very
    # variables this test removes; neutralise it for this run only.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    monkeypatch.delenv("ALLOWED_USER_ID", raising=False)
    command.upgrade(_config(legacy_db), "head")

    conn = sqlite3.connect(legacy_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    owners = conn.execute("SELECT user_id FROM cards").fetchall()
    conn.close()

    assert {"users", "invites"} <= tables
    assert owners == [(None,)]  # unclaimed, not misassigned
