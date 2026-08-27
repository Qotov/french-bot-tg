"""Exam tracks: DELF B1, DELF B2, TCF — and the general track.

The pilot's defensible niche is learners with a dated, concrete goal. A track
changes what the bot asks of them: production tasks in the exam's own format
and length, correction weighted by the exam's marking criteria, and grammar
drills ordered by what that exam actually tests. It does not change the
spaced-repetition engine — the deck is still theirs.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Track:
    slug: str
    title: str
    blurb: str
    level: str  # the level this exam is pitched at
    word_target: tuple[int, int]  # min, max words expected in a written task
    criteria_ru: str  # what the correction should weigh
    tasks: tuple[str, ...] = field(default_factory=tuple)
    drill_priority: tuple[str, ...] = field(default_factory=tuple)


GENERAL = Track(
    slug="general",
    title="Обычные занятия",
    blurb="Без экзамена: бытовые темы, короткие тексты, разговор.",
    level="B1",
    word_target=(30, 80),
    criteria_ru="Понятность и корректность обычной бытовой речи.",
)

DELF_B1 = Track(
    slug="delf_b1",
    title="DELF B1",
    blurb="Производство письменного текста: личное мнение, 160–180 слов.",
    level="B1",
    word_target=(160, 180),
    criteria_ru=(
        "Критерии DELF B1: соответствие заданию, связность (связки и переходы), "
        "лексика бытовых тем, контроль présent/passé composé/imparfait/futur, "
        "простые сложноподчинённые предложения."
    ),
    tasks=(
        "Vous écrivez à un ami francophone pour raconter une expérience "
        "personnelle marquante et expliquer ce qu'elle vous a appris (160–180 mots).",
        "Sur le forum de votre ville, vous réagissez à un projet de fermeture "
        "d'une bibliothèque : donnez votre opinion et proposez une solution "
        "(160–180 mots).",
        "Vous écrivez au journal local pour raconter un événement auquel vous "
        "avez participé et donner votre avis (160–180 mots).",
        "Un ami hésite à s'installer dans votre ville. Écrivez-lui pour "
        "présenter les avantages et les inconvénients (160–180 mots).",
        "Vous racontez dans votre blog un voyage qui ne s'est pas passé comme "
        "prévu et ce que vous en retenez (160–180 mots).",
        "Votre entreprise veut supprimer le télétravail. Écrivez au responsable "
        "pour exposer votre point de vue (160–180 mots).",
    ),
    drill_priority=(
        "aux-passe-compose",
        "depuis-pendant-il-y-a",
        "pronoms-y-en",
        "relatifs-qui-que-dont",
        "si-clauses",
    ),
)

DELF_B2 = Track(
    slug="delf_b2",
    title="DELF B2",
    blurb="Аргументированный текст: эссе или официальное письмо, 250 слов.",
    level="B2",
    word_target=(240, 260),
    criteria_ru=(
        "Критерии DELF B2: чёткая аргументация с примерами, уместный регистр, "
        "богатые связки (néanmoins, en revanche, dans la mesure où), "
        "subjonctif и concordance des temps, точная лексика."
    ),
    tasks=(
        "Le maire de votre ville veut interdire les voitures au centre-ville. "
        "Vous lui écrivez une lettre formelle argumentée (250 mots).",
        "« Les réseaux sociaux nuisent au débat démocratique. » Vous rédigez un "
        "article argumenté pour un magazine (250 mots).",
        "Votre université envisage de remplacer les cours en présentiel par des "
        "cours en ligne. Rédigez une contribution argumentée (250 mots).",
        "Faut-il rendre le bénévolat obligatoire pour les jeunes ? Défendez une "
        "position nuancée dans un texte structuré (250 mots).",
        "Vous répondez à un article qui affirme que le télétravail détruit le "
        "lien social. Rédigez une réponse argumentée (250 mots).",
        "« Voyager loin n'apprend rien de plus que lire. » Discutez cette "
        "affirmation dans un essai structuré (250 mots).",
    ),
    drill_priority=(
        "subjonctif-present",
        "si-clauses",
        "futur-vs-conditionnel",
        "relatifs-qui-que-dont",
        "ordre-des-pronoms",
    ),
)

TCF = Track(
    slug="tcf",
    title="TCF",
    blurb="Expression écrite: три задания, от короткого сообщения до сравнения.",
    level="B2",
    word_target=(120, 180),
    criteria_ru=(
        "Критерии TCF: точное выполнение задания в заданном объёме, ясная "
        "структура, уместный регистр (tâche 1 — бытовой, tâche 2 — статья, "
        "tâche 3 — сопоставление и синтез), грамматическая точность."
    ),
    tasks=(
        "Tâche 1 : vous écrivez à votre voisin pour lui demander de surveiller "
        "votre appartement pendant vos vacances (60–120 mots).",
        "Tâche 2 : pour le journal de votre association, vous rédigez un article "
        "sur un changement récent dans votre quartier (120–150 mots).",
        "Tâche 3 : deux personnes s'opposent sur l'utilité des examens. "
        "Comparez leurs positions puis donnez la vôtre (120–180 mots).",
        "Tâche 1 : vous écrivez au service client pour signaler un problème et "
        "demander un dédommagement (60–120 mots).",
        "Tâche 2 : vous rédigez un texte présentant les avantages du vélo en "
        "ville pour un site d'information (120–150 mots).",
        "Tâche 3 : comparez deux points de vue sur le travail à distance, puis "
        "prenez position (120–180 mots).",
    ),
    drill_priority=(
        "subjonctif-present",
        "relatifs-qui-que-dont",
        "pronoms-y-en",
        "futur-vs-conditionnel",
        "de-apres-negation",
    ),
)

TRACKS: dict[str, Track] = {t.slug: t for t in (GENERAL, DELF_B1, DELF_B2, TCF)}
DEFAULT_TRACK = GENERAL.slug


def get(slug: str | None) -> Track:
    return TRACKS.get(slug or DEFAULT_TRACK, GENERAL)


def is_exam(slug: str | None) -> bool:
    return get(slug).slug != GENERAL.slug


def ordered_topic_slugs(slug: str | None, all_slugs: list[str]) -> list[str]:
    """Exam-relevant grammar first, then everything else in its usual order."""
    priority = [s for s in get(slug).drill_priority if s in all_slugs]
    return priority + [s for s in all_slugs if s not in priority]
