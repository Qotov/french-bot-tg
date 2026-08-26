import pytest
from pydantic import ValidationError

from frbot.config import Settings


def _base_kwargs(**overrides):
    kwargs = {
        "_env_file": None,
        "bot_token": "42:TEST-TOKEN",
        "gemini_api_key": "k",
        "allowed_user_id": 1,
    }
    kwargs.update(overrides)
    return kwargs


def test_defaults():
    s = Settings(**_base_kwargs())
    assert s.tz == "Europe/Paris"
    assert s.model_fast == "gemini-3.5-flash-lite"
    assert s.model_smart == "gemini-3.5-flash-lite"
    assert s.daily_new_limit == 15
    assert s.session_max == 30
    assert s.reminder_time == "08:30"
    assert s.writing_time == "19:00"
    assert s.desired_retention == 0.9


@pytest.mark.parametrize("bad_time", ["8:30", "24:00", "08:60", "morning", ""])
def test_rejects_bad_times(bad_time):
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(reminder_time=bad_time))


def test_rejects_bad_retention():
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(desired_retention=1.5))
