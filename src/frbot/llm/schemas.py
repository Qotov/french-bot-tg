"""Pydantic schemas for LLM JSON output.

Validation is strict where a retry can realistically fix the output (missing
fields, wrong item counts, correct option not in options) and lenient where a
silent fix is better than a user-visible failure (unknown error type, too many
collocations).
"""

import warnings
from typing import Literal

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------- enrichment


class Example(BaseModel):
    fr: str
    ru: str


# The JSON contract requires a field literally named "register", which pydantic
# flags as shadowing a BaseModel attribute; the shadowing is intentional.
warnings.filterwarnings("ignore", message='Field name "register"')


class Enrichment(BaseModel):
    lemma: str
    pos: Literal["noun", "verb", "adj", "adv", "expression", "other"]
    gender: Literal["m", "f"] | None = None
    ipa: str
    definition_fr: str
    translation_ru: str
    translation_en: str
    examples: list[Example]
    collocations: list[str] = []
    register: Literal["neutre", "familier", "soutenu"]
    notes: str = ""

    @field_validator("gender", mode="before")
    @classmethod
    def _normalize_gender(cls, v: object) -> object:
        if v in ("", "null", "none", "None"):
            return None
        return v

    @field_validator("lemma")
    @classmethod
    def _lemma_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("lemma must not be empty")
        return v

    @field_validator("examples")
    @classmethod
    def _exactly_three_examples(cls, v: list[Example]) -> list[Example]:
        if len(v) != 3:
            raise ValueError(f"expected exactly 3 examples, got {len(v)}")
        return v

    @field_validator("collocations")
    @classmethod
    def _at_most_five(cls, v: list[str]) -> list[str]:
        return v[:5]


# ------------------------------------------------------------- writing corr.

ERROR_TYPES = (
    "gender",
    "auxiliary",
    "preposition",
    "tense",
    "agreement",
    "vocab",
    "spelling",
    "word_order",
    "other",
)


class WritingError(BaseModel):
    original: str
    corrected: str
    type: Literal[ERROR_TYPES]  # type: ignore[valid-type]
    explanation_ru: str

    @field_validator("type", mode="before")
    @classmethod
    def _unknown_type_is_other(cls, v: object) -> object:
        if isinstance(v, str) and v not in ERROR_TYPES:
            return "other"
        return v


class WritingCorrection(BaseModel):
    corrected_text: str
    errors: list[WritingError] = []
    comment_ru: str = ""


# -------------------------------------------------------------------- cloze

GAP = "___"


class ClozeItem(BaseModel):
    sentence_with_gap: str
    options: list[str]
    correct: str
    explanation_ru: str

    @field_validator("sentence_with_gap")
    @classmethod
    def _has_gap(cls, v: str) -> str:
        if GAP not in v:
            raise ValueError(f"sentence must contain the gap marker {GAP!r}")
        return v

    @field_validator("options")
    @classmethod
    def _three_unique_options(cls, v: list[str]) -> list[str]:
        if len(v) != 3:
            raise ValueError(f"expected exactly 3 options, got {len(v)}")
        if len(set(v)) != 3:
            raise ValueError("options must be unique")
        return v


class ClozeSet(BaseModel):
    items: list[ClozeItem]

    @field_validator("items")
    @classmethod
    def _exactly_five(cls, v: list[ClozeItem]) -> list[ClozeItem]:
        if len(v) != 5:
            raise ValueError(f"expected exactly 5 items, got {len(v)}")
        for item in v:
            if item.correct not in item.options:
                raise ValueError(
                    f"correct answer {item.correct!r} is not among options {item.options}"
                )
        return v
