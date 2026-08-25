import pytest
from pydantic import ValidationError

from frbot.llm.schemas import ClozeSet, Enrichment, WritingCorrection
from tests.fakes import load_fixture_json

# ---------------------------------------------------------------- enrichment


def test_enrichment_valid_fixture():
    enr = Enrichment.model_validate(load_fixture_json("enrichment_valid.json"))
    assert enr.lemma == "au fur et à mesure"
    assert enr.gender is None
    assert len(enr.examples) == 3


def test_enrichment_gender_null_string_normalized():
    data = load_fixture_json("enrichment_valid.json")
    data["gender"] = "null"
    assert Enrichment.model_validate(data).gender is None


def test_enrichment_missing_lemma_fails():
    with pytest.raises(ValidationError):
        Enrichment.model_validate(load_fixture_json("enrichment_missing_lemma.json"))


def test_enrichment_two_examples_fails():
    with pytest.raises(ValidationError):
        Enrichment.model_validate(load_fixture_json("enrichment_two_examples.json"))


def test_enrichment_empty_lemma_fails():
    data = load_fixture_json("enrichment_valid.json")
    data["lemma"] = "   "
    with pytest.raises(ValidationError):
        Enrichment.model_validate(data)


def test_enrichment_collocations_truncated_to_five():
    data = load_fixture_json("enrichment_valid.json")
    data["collocations"] = [f"c{i}" for i in range(8)]
    assert len(Enrichment.model_validate(data).collocations) == 5


# ------------------------------------------------------------- writing corr.


def test_correction_valid_fixture():
    corr = WritingCorrection.model_validate(load_fixture_json("correction_valid.json"))
    assert len(corr.errors) == 2
    assert corr.errors[0].type == "preposition"
    assert corr.comment_ru


def test_correction_unknown_error_type_becomes_other():
    data = load_fixture_json("correction_valid.json")
    data["errors"][0]["type"] = "article_mystery"
    corr = WritingCorrection.model_validate(data)
    assert corr.errors[0].type == "other"


def test_correction_malformed_errors_fails():
    with pytest.raises(ValidationError):
        WritingCorrection.model_validate(load_fixture_json("correction_malformed.json"))


def test_correction_no_errors_is_valid():
    corr = WritingCorrection.model_validate(
        {"corrected_text": "Parfait.", "errors": [], "comment_ru": "Отлично."}
    )
    assert corr.errors == []


# -------------------------------------------------------------------- cloze


def test_cloze_valid_fixture():
    cloze = ClozeSet.model_validate(load_fixture_json("cloze_valid.json"))
    assert len(cloze.items) == 5
    assert all(item.correct in item.options for item in cloze.items)


def test_cloze_correct_not_in_options_fails():
    with pytest.raises(ValidationError):
        ClozeSet.model_validate(load_fixture_json("cloze_wrong_correct.json"))


def test_cloze_four_items_fails():
    data = load_fixture_json("cloze_valid.json")
    data["items"] = data["items"][:4]
    with pytest.raises(ValidationError):
        ClozeSet.model_validate(data)


def test_cloze_missing_gap_fails():
    data = load_fixture_json("cloze_valid.json")
    data["items"][0]["sentence_with_gap"] = "Je suis allé au marché hier."
    with pytest.raises(ValidationError):
        ClozeSet.model_validate(data)


def test_cloze_duplicate_options_fails():
    data = load_fixture_json("cloze_valid.json")
    data["items"][0]["options"] = ["suis", "suis", "ai"]
    with pytest.raises(ValidationError):
        ClozeSet.model_validate(data)
