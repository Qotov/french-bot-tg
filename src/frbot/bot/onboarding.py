"""Copy and helpers shared by the two ways in: picking a level, or being
measured by the placement test. Both paths must end the same way — a deck to
review and one message telling the learner what happens next.
"""

BUILDING_DECK_TEXT = "Собираю тебе стартовый набор карточек, это займёт секунд десять…"
STARTER_DECK_TIMEOUT = 60  # seconds; the per-user isolation lock is held meanwhile

FIRST_STEPS_TEXT = (
    "Дальше всё в твоих руках:\n\n"
    "1️⃣ Присылай слова, которые встретил — текстом или голосом 🎙\n"
    "2️⃣ /topic и тема — соберу подборку под твой уровень\n"
    "3️⃣ Вечером пришлю задание на письмо, разберу ошибки\n\n"
    "10–15 минут в день. /help — если что-то забудешь.\n\n"
    "<i>Что я храню: твои карточки, тексты и голосовые — чтобы проверять их и "
    "составлять повторения. Никому не передаю. Удалить всё: /delete_me</i>"
)

DECK_FAILED_TEXT = (
    "Не получилось собрать стартовый набор — не страшно. "
    "Пришли любое французское слово или набери /topic с темой."
)
