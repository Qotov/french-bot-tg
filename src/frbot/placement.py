"""The placement test.

A curated, fixed bank rather than generated items: placement decides which
level every later prompt is calibrated to, so it must be stable, checkable and
identical for everyone. Items target the structures that actually separate the
levels for a Russian-speaking learner — auxiliaries, article/gender agreement,
pronoun placement, tense concordance, subjunctive — not vocabulary trivia.

Each band has six items. The reported level is the highest band the learner
controls (>= 4/6), because a level you can only half-produce is not your level.
"""

from dataclasses import dataclass

LEVELS_ORDER = ("A2", "B1", "B2")
PER_BAND = 6
PASS_MARK = 4


@dataclass(frozen=True)
class Item:
    level: str
    skill: str
    sentence: str  # contains ___
    options: tuple[str, str, str]
    correct: str
    explanation_ru: str


BANK: tuple[Item, ...] = (
    # ---------------------------------------------------------------- A2
    Item(
        "A2", "auxiliaire",
        "Hier, je ___ allé au cinéma avec des amis.",
        ("suis", "ai", "étais"), "suis",
        "Aller — глагол движения, в passé composé с être.",
    ),
    Item(
        "A2", "genre",
        "Elle a acheté ___ nouvelle voiture.",
        ("une", "un", "des"), "une",
        "Voiture — женского рода: une voiture.",
    ),
    Item(
        "A2", "négation",
        "Je n'ai pas ___ frères.",
        ("de", "des", "les"), "de",
        "После отрицания des превращается в de.",
    ),
    Item(
        "A2", "préposition",
        "Nous habitons ___ Paris depuis deux ans.",
        ("à", "en", "au"), "à",
        "С городами — à: à Paris, à Moscou.",
    ),
    Item(
        "A2", "accord",
        "Mes sœurs sont ___ ce matin.",
        ("parties", "parti", "partis"), "parties",
        "С être причастие согласуется: женский род, множественное число.",
    ),
    Item(
        "A2", "présent",
        "Vous ___ très bien le français.",
        ("parlez", "parlent", "parlons"), "parlez",
        "Vous → -ez: vous parlez.",
    ),
    # ---------------------------------------------------------------- B1
    Item(
        "B1", "imparfait vs pc",
        "Quand j'étais petit, je ___ souvent chez ma grand-mère.",
        ("allais", "suis allé", "irai"), "allais",
        "Регулярное действие в прошлом — imparfait.",
    ),
    Item(
        "B1", "pronom en",
        "— Tu as besoin de ces documents ? — Oui, j'___ ai besoin.",
        ("en", "y", "les"), "en",
        "Avoir besoin de → заменяется на en.",
    ),
    Item(
        "B1", "relatif",
        "C'est le livre ___ je t'ai parlé.",
        ("dont", "que", "qui"), "dont",
        "Parler de → dont заменяет дополнение с de.",
    ),
    Item(
        "B1", "temps",
        "Ça fait trois ans ___ j'apprends le français.",
        ("que", "depuis", "pendant"), "que",
        "Ça fait … que — устойчивая конструкция.",
    ),
    Item(
        "B1", "si-clause",
        "Si j'avais le temps, je ___ plus souvent.",
        ("voyagerais", "voyagerai", "voyageais"), "voyagerais",
        "Si + imparfait → главная часть в conditionnel présent.",
    ),
    Item(
        "B1", "ordre des pronoms",
        "Ce cadeau est pour Marie : je vais ___ donner demain.",
        ("le lui", "lui le", "la lui"), "le lui",
        "COD (le) идёт перед COI (lui) в третьем лице.",
    ),
    # ---------------------------------------------------------------- B2
    Item(
        "B2", "subjonctif",
        "Il faut que tu ___ plus attentif la prochaine fois.",
        ("sois", "es", "seras"), "sois",
        "Il faut que требует subjonctif: que tu sois.",
    ),
    Item(
        "B2", "concordance",
        "Il m'a dit qu'il ___ le lendemain.",
        ("viendrait", "viendra", "vient"), "viendrait",
        "Прошедшее в главной части → будущее в прошедшем (conditionnel).",
    ),
    Item(
        "B2", "accord du participe",
        "Les lettres que j'ai ___ sont arrivées hier.",
        ("écrites", "écrit", "écrits"), "écrites",
        "COD стоит перед avoir — причастие согласуется с ним.",
    ),
    Item(
        "B2", "connecteur",
        "Il a insisté, ___ nous avons accepté.",
        ("si bien que", "malgré que", "bien que"), "si bien que",
        "Следствие — si bien que; bien que требует subjonctif и вводит уступку.",
    ),
    Item(
        "B2", "subjonctif passé",
        "Je suis ravi que vous ___ venir hier.",
        ("ayez pu", "avez pu", "pouviez"), "ayez pu",
        "Эмоция + прошедшее действие → subjonctif passé.",
    ),
    Item(
        "B2", "registre",
        "Dans une lettre formelle, on écrit : « Je vous serais reconnaissant de bien "
        "vouloir ___ ma demande. »",
        ("examiner", "regarder", "voir"), "examiner",
        "В официальном регистре — examiner une demande.",
    ),
)


def items_in_order() -> list[Item]:
    """Easiest band first: an early wall is discouraging, and a learner who
    stops halfway has still produced a usable signal."""
    return [item for level in LEVELS_ORDER for item in BANK if item.level == level]


def score_by_band(answers: list[tuple[str, bool]]) -> dict[str, int]:
    counts = dict.fromkeys(LEVELS_ORDER, 0)
    for level, correct in answers:
        if correct:
            counts[level] += 1
    return counts


def level_from_answers(answers: list[tuple[str, bool]]) -> str:
    """Highest band with >= PASS_MARK correct; A2 when even A2 is shaky."""
    counts = score_by_band(answers)
    achieved = "A2"
    for level in LEVELS_ORDER:
        attempted = sum(1 for band, _ in answers if band == level)
        if attempted and counts[level] >= min(PASS_MARK, attempted):
            achieved = level
        else:
            break
    return achieved


def weakest_skills(answers_detail: list[tuple[str, str, bool]], limit: int = 3) -> list[str]:
    """Skills the learner missed — the honest 'work on this' list."""
    missed = [skill for _level, skill, correct in answers_detail if not correct]
    seen: list[str] = []
    for skill in missed:
        if skill not in seen:
            seen.append(skill)
    return seen[:limit]
