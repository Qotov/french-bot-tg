"""Admin alerting.

During a pilot an outage is worse than a bug: participants go quiet, and in the
data that is indistinguishable from churn — so it corrupts the very retention
number the pilot exists to measure. These alerts exist so the operator finds
out from the bot, not from a complaint two days later.
"""

import html
import logging
from collections import deque
from datetime import UTC, datetime, timedelta

from aiogram import Bot

logger = logging.getLogger(__name__)

# Never send the same alert more than once per window, so a crash loop cannot
# turn into a thousand notifications.
ALERT_COOLDOWN = timedelta(minutes=30)
LLM_FAILURE_WINDOW = timedelta(minutes=15)
LLM_FAILURE_THRESHOLD = 5


def esc(value: object) -> str:
    """Escape anything that will be interpolated into an HTML alert."""
    return html.escape(str(value), quote=False)


def _strip_markup(text: str) -> str:
    return (
        text.replace("<b>", "").replace("</b>", "")
        .replace("<code>", "").replace("</code>", "")
        .replace("<i>", "").replace("</i>", "")
    )


class AdminAlerter:
    def __init__(self, admin_user_id: int) -> None:
        self.admin_user_id = admin_user_id
        self._last_sent: dict[str, datetime] = {}
        self._llm_failures: deque[datetime] = deque(maxlen=100)

    async def send(self, bot: Bot, kind: str, text: str) -> bool:
        """One alert of a given kind per cooldown window."""
        now = datetime.now(UTC)
        last = self._last_sent.get(kind)
        if last is not None and now - last < ALERT_COOLDOWN:
            return False
        self._last_sent[kind] = now
        try:
            await bot.send_message(self.admin_user_id, text)
            return True
        except Exception:
            # An alert that cannot be delivered is worse than no alerting at
            # all, because it is silent. Retry once as plain text: the usual
            # cause is stray markup in an exception string breaking the HTML
            # parse, and the message itself is still worth having.
            logger.warning("HTML admin alert %s rejected; retrying as plain text", kind)
            try:
                await bot.send_message(
                    self.admin_user_id, _strip_markup(text), parse_mode=None
                )
                return True
            except Exception:
                logger.exception("could not deliver admin alert %s", kind)
                return False

    async def record_llm_failure(self, bot: Bot, detail: str) -> None:
        """Alert when LLM calls start failing in bulk — an expired key or an
        exhausted quota silently breaks every feature at once."""
        now = datetime.now(UTC)
        self._llm_failures.append(now)
        recent = [t for t in self._llm_failures if now - t <= LLM_FAILURE_WINDOW]
        if len(recent) >= LLM_FAILURE_THRESHOLD:
            self._llm_failures.clear()
            await self.send(
                bot,
                "llm",
                f"🚨 <b>{len(recent)} ошибок LLM за "
                f"{int(LLM_FAILURE_WINDOW.total_seconds() // 60)} минут.</b>\n"
                f"Последняя: {esc(detail[:300])}\n\n"
                f"Проверь ключ Gemini и квоту — сейчас у людей не работает "
                f"почти всё, кроме повторений.",
            )
