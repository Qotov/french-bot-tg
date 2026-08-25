"""Message formatting. All dynamic strings are HTML-escaped; parse mode is HTML."""

import html

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


def error_card_front(card: Card) -> str:
    """Front for error/drill_error cards: the sentence with the error span gapped."""
    meta = card.error_meta or {}
    sentence = card.text
    corrected_span = meta.get("corrected", "")
    if corrected_span and corrected_span in sentence:
        sentence = sentence.replace(corrected_span, "___", 1)
    label = "✍️" if card.kind == CardKind.error.value else "📚"
    return f"{label} Заполни пропуск:\n<i>{esc(sentence)}</i>"


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


def stats_message(stats) -> str:
    rate = "—" if stats.correct_rate_7d is None else f"{round(stats.correct_rate_7d * 100)}%"
    lines = [
        "📊 <b>Статистика</b>",
        f"К повторению сегодня: {stats.due_today}",
        f"Повторений за 7 дней: {stats.reviews_7d}",
        f"Правильных (Good + Easy): {rate}",
        f"Новых карточек за 7 дней: {stats.new_cards_7d}",
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
