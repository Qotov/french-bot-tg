"""The 🔊 button: speak a word, cache it, never let it break a flow."""

import logging

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from frbot.bot.audio import AudioUnavailable, VoiceCache, ffmpeg_available, to_voice_ogg
from frbot.bot.telegram_utils import safe_answer
from frbot.config import Settings
from frbot.db import repo
from frbot.db.models import User
from frbot.db.session import SessionFactory
from frbot.llm.client import LLMClient, LLMError
from frbot.llm.schemas import Enrichment
from frbot.usage import UsageLimiter

logger = logging.getLogger(__name__)

UNAVAILABLE_TEXT = "Озвучка сейчас недоступна."
MAX_SPEAK_LEN = 300


async def speak(
    text: str,
    voice_cache: VoiceCache,
    llm: LLMClient,
    settings: Settings,
) -> bytes:
    """OGG/Opus for a phrase — from cache when possible, synthesised once."""
    text = text.strip()
    if not text:
        raise AudioUnavailable("nothing to say")
    cached = await voice_cache.get(text, settings.tts_voice)
    if cached is not None:
        return cached
    pcm = await llm.synthesize(text, model=settings.model_tts, voice=settings.tts_voice)
    ogg = await to_voice_ogg(pcm)
    await voice_cache.put(text, settings.tts_voice, ogg)
    return ogg


def _phrase_for(card, which: str) -> str | None:
    """`word` is the lemma; `ex` is the first example sentence."""
    if not card.enrichment:
        return card.text
    enrichment = Enrichment.model_validate(card.enrichment)
    if which == "ex" and enrichment.examples:
        return enrichment.examples[0].fr
    return enrichment.lemma


async def on_speak(
    query: CallbackQuery,
    user: User,
    session_factory: SessionFactory,
    llm: LLMClient,
    settings: Settings,
    usage: UsageLimiter,
    voice_cache: VoiceCache,
) -> None:
    _, which, card_id_raw = query.data.split(":")
    async with session_factory() as session:
        card = await repo.get_card(session, int(card_id_raw), user_id=user.id)
    if card is None:
        await safe_answer(query, "Карточка не найдена.")
        return

    phrase = (_phrase_for(card, which) or "")[:MAX_SPEAK_LEN]
    if not settings.tts_enabled or not phrase:
        await safe_answer(query, UNAVAILABLE_TEXT)
        return

    cached = await voice_cache.get(phrase, settings.tts_voice)
    # Only a real synthesis costs anything; a replay must never burn the quota.
    if cached is None and not usage.check_and_count(user.id):
        await safe_answer(query, "Дневной лимит запросов исчерпан.", show_alert=True)
        return

    await safe_answer(query, "🔊")
    try:
        ogg = cached if cached is not None else await speak(phrase, voice_cache, llm, settings)
    except (LLMError, AudioUnavailable) as exc:
        logger.warning("pronunciation failed for %r: %s", phrase[:40], exc)
        if isinstance(query.message, Message):
            await query.message.answer(UNAVAILABLE_TEXT)
        return

    if isinstance(query.message, Message):
        await query.message.answer_voice(
            BufferedInputFile(ogg, filename="prononciation.ogg"),
            caption=f"🔊 {phrase[:200]}",
        )


def create_router() -> Router:
    router = Router(name="pronounce")
    router.callback_query.register(on_speak, F.data.startswith("say:"))
    return router


def startup_check(settings: Settings) -> None:
    if settings.tts_enabled and not ffmpeg_available():
        logger.warning(
            "TTS is enabled but ffmpeg is not installed — pronunciation will be "
            "unavailable. Install it (apt install ffmpeg) or set TTS_ENABLED=false."
        )
