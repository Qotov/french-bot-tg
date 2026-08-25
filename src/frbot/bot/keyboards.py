"""Inline keyboards. Callback data conventions:

- capture:  card:<action>:<card_id>      (delete / regen)
- review:   review:start, review:show:<card_id>, review:grade:<card_id>:<rating>
- drill:    drill:answer:<item_index>:<option_index>
- settings: settings:edit:<key>
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def card_preview_kb(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Delete", callback_data=f"card:delete:{card_id}"),
                InlineKeyboardButton(text="🔄 Regenerate", callback_data=f"card:regen:{card_id}"),
            ]
        ]
    )


def start_review_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Start review", callback_data="review:start")]
        ]
    )


def show_answer_kb(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👁 Show answer", callback_data=f"review:show:{card_id}")]
        ]
    )


def grade_kb(card_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Again", callback_data=f"review:grade:{card_id}:1"),
                InlineKeyboardButton(text="Hard", callback_data=f"review:grade:{card_id}:2"),
                InlineKeyboardButton(text="Good", callback_data=f"review:grade:{card_id}:3"),
                InlineKeyboardButton(text="Easy", callback_data=f"review:grade:{card_id}:4"),
            ]
        ]
    )


def drill_options_kb(item_index: int, options: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for opt_index, option in enumerate(options):
        builder.button(text=option, callback_data=f"drill:answer:{item_index}:{opt_index}")
    builder.adjust(len(options))
    return builder.as_markup()


def settings_kb(values: dict[str, str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, value in values.items():
        builder.button(text=f"{key}: {value}", callback_data=f"settings:edit:{key}")
    builder.adjust(1)
    return builder.as_markup()
