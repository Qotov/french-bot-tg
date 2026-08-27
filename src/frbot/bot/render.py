"""Message formatting. All dynamic strings are HTML-escaped; parse mode is HTML."""

import html
import re

from frbot.db.models import Card, CardKind
from frbot.llm.schemas import Enrichment

POS_LABELS = {
    "noun": "сущ.",
    "verb": "гл.",
    "adj": "прил.",
    "adv": "нареч.",
    "expression": "выражение",
    "other": "",
}


def esc(value: str) -> str:
    return html.escape(value, quote=False)


def _header(enr: Enrichment) -> str:
    gender = f" ({enr.gender})" if enr.gender else ""
    pos = POS_LABELS.get(enr.pos, "")
    pos_part = f" · {pos}" if pos else ""
    return f"<b>{esc(enr.lemma)}</b>{gender}{pos_part}"


def card_preview(card: Card, *, existing: bool = False) -> str:
    """Compact preview after capture: lemma+gender, IPA, definition, RU, one example."""
    enr = Enrichment.model_validate(card.enrichment)
    lines = []
    if existing:
        lines.append("🔁 Такая карточка уже есть:")
    lines.append(_header(enr))
    if enr.ipa:
        lines.append(f"/{esc(enr.ipa)}/")
    lines.append(f"📖 {esc(enr.definition_fr)}")
    lines.append(f"🇷🇺 {esc(enr.translation_ru)}")
    if enr.examples:
        example = enr.examples[0]
        lines.append(f"💬 <i>{esc(example.fr)}</i> — {esc(example.ru)}")
    return "\n".join(lines)


def vocab_card_back(card: Card) -> str:
    """Full back for review: definition, translations, all examples, collocations, register."""
    enr = Enrichment.model_validate(card.enrichment)
    lines = [_header(enr)]
    if enr.ipa:
        lines.append(f"/{esc(enr.ipa)}/")
    lines.append(f"📖 {esc(enr.definition_fr)}")
    lines.append(f"🇷🇺 {esc(enr.translation_ru)} · 🇬🇧 {esc(enr.translation_en)}")
    for example in enr.examples:
        lines.append(f"💬 <i>{esc(example.fr)}</i> — {esc(example.ru)}")
    if enr.collocations:
        lines.append(f"🔗 {esc(', '.join(enr.collocations))}")
    lines.append(f"🎙 {esc(enr.register)}")
    if enr.notes:
        lines.append(f"⚠️ {esc(enr.notes)}")
    return "\n".join(lines)


def make_gapped(text: str, span: str) -> str | None:
    """Replace the first whole-word occurrence of `span` in `text` with a gap.

    Word boundaries prevent a short span like "a" from matching inside "Marie".
    Returns None when the span is not found as a whole word.
    """
    span = span.strip()
    if not span:
        return None
    pattern = re.compile(rf"(?<!\w){re.escape(span)}(?!\w)", re.IGNORECASE)
    replaced, count = pattern.subn("___", text, count=1)
    return replaced if count else None


def error_card_front(card: Card) -> str:
    """Front for error/drill_error cards: the sentence with the error span gapped."""
    meta = card.error_meta or {}
    label = "✍️" if card.kind == CardKind.error.value else "📚"
    front = meta.get("front") or make_gapped(card.text, meta.get("corrected", ""))
    if front:
        return f"{label} Заполни пропуск:\n<i>{esc(front)}</i>"
    # No reliable gap position: ask to correct the original fragment instead.
    original = meta.get("original") or card.text
    return f"{label} Исправь:\n<i>{esc(original)}</i>"


def error_card_back(card: Card) -> str:
    """Back for error/drill_error cards: correct form + one-line explanation."""
    meta = card.error_meta or {}
    lines = [f"✅ <b>{esc(meta.get('corrected', ''))}</b>"]
    original = meta.get("original", "")
    if original:
        lines.append(f"❌ <s>{esc(original)}</s>")
    lines.append(f"<i>{esc(card.text)}</i>")
    explanation = meta.get("explanation_ru", "")
    if explanation:
        lines.append(f"💡 {esc(explanation)}")
    return "\n".join(lines)


MAX_ERRORS_SHOWN = 8
CORRECTED_TEXT_MAX = 3000
MESSAGE_BUDGET = 4000  # hard guard under Telegram's 4096-char message limit


def fit_lines(lines: list[str], budget: int = MESSAGE_BUDGET) -> str:
    """Join lines, dropping the tail once the budget is reached.

    Every line is tag-balanced, so dropping whole lines keeps the HTML valid.
    """
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > budget:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def correction_message(correction, created_cards: int) -> str:
    """Corrected text, numbered error list with RU explanations, comment."""
    corrected = correction.corrected_text
    if len(corrected) > CORRECTED_TEXT_MAX:
        corrected = corrected[:CORRECTED_TEXT_MAX] + "…"
    lines = []
    if correction.errors:
        lines.append("📝 <b>Исправлено:</b>")
        lines.append(f"<i>{esc(corrected)}</i>")
        lines.append("")
        lines.append("Ошибки:")
        for i, error in enumerate(correction.errors[:MAX_ERRORS_SHOWN], start=1):
            lines.append(f"{i}. ❌ {esc(error.original)} → ✅ <b>{esc(error.corrected)}</b>")
            lines.append(f"   💡 {esc(error.explanation_ru)}")
        hidden = len(correction.errors) - MAX_ERRORS_SHOWN
        if hidden > 0:
            lines.append(f"… и ещё {hidden}.")
    else:
        lines.append("🎉 Отлично, ошибок нет!")
        lines.append(f"<i>{esc(corrected)}</i>")
    if correction.comment_ru:
        lines.append("")
        lines.append(f"💬 {esc(correction.comment_ru)}")
    if created_cards:
        lines.append(f"➕ Новых карточек из ошибок: {created_cards}")
    return fit_lines(lines)


def weekly_summary(stats) -> str:
    """The Sunday push: same numbers as /stats, framed as a week in review."""
    rate = "—" if stats.correct_rate_7d is None else f"{round(stats.correct_rate_7d * 100)}%"
    lines = ["📅 <b>Итоги недели</b>"]
    if stats.reviews_7d:
        lines.append(f"Занимался(ась) дней: {stats.active_days_7d} из 7")
        lines.append(f"Повторений: {stats.reviews_7d} · правильных: {rate}")
    else:
        lines.append("На этой неделе повторений не было — начни с /review 🙂")
    lines.append(f"Новых карточек: {stats.new_cards_7d} · всего в колоде: {stats.total_cards}")
    if stats.top_error_types_30d:
        top = ", ".join(f"{render_type}" for render_type, _ in stats.top_error_types_30d[:3])
        lines.append(f"Над чем поработать: {esc(top)}")
    return "\n".join(lines)


def stats_message(stats) -> str:
    rate = "—" if stats.correct_rate_7d is None else f"{round(stats.correct_rate_7d * 100)}%"
    lines = [
        "📊 <b>Статистика</b>",
        f"К повторению сегодня: {stats.due_today}",
        f"Повторений за 7 дней: {stats.reviews_7d}",
        f"Правильных (Good + Easy): {rate}",
        f"Новых карточек за 7 дней: {stats.new_cards_7d}",
        f"Всего карточек: {stats.total_cards}",
        f"Дней с занятиями за 7 дней: {stats.active_days_7d}",
    ]
    if stats.top_error_types_30d:
        lines.append("Частые ошибки за 30 дней:")
        lines.extend(
            f"  • {esc(err_type)} — {count}" for err_type, count in stats.top_error_types_30d
        )
    else:
        lines.append("Ошибок за 30 дней: нет данных")
    return "\n".join(lines)


def card_front(card: Card) -> str:
    if card.kind == CardKind.vocab.value:
        enr = Enrichment.model_validate(card.enrichment)
        return f"<b>{esc(enr.lemma)}</b>"
    return error_card_front(card)


def card_back(card: Card) -> str:
    if card.kind == CardKind.vocab.value:
        return vocab_card_back(card)
    return error_card_back(card)
