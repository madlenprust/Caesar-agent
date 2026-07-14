"""Fix A+B: agent не разоружается (tools always on) + self-poke на action-intent.

Болезнь «агент остановился на полпути»: analyzer снимал инструменты → smart_chat
рожал текст-обещание «найду все машины» без tool-call и сдавался как финальный ответ.
Фикс: tools всегда + если ответ = обещание действия без tool-call → пнём действовать.
"""
from caesar.core.orchestrator import Orchestrator


def _orch():
    return Orchestrator.__new__(Orchestrator)


def test_action_intent_detected():
    o = _orch()
    assert o._looks_like_action_intent("Сначала найду все машины в сети.")
    assert o._looks_like_action_intent("Сейчас подключусь и проверю.")
    assert o._looks_like_action_intent("Сделаю это.")
    assert o._looks_like_action_intent("Выполню команду и скажу.")


def test_not_action_intent():
    o = _orch()
    assert not o._looks_like_action_intent("Готово: подключился, whoami = agent.")
    assert not o._looks_like_action_intent("Привет! Как дела?")
    assert not o._looks_like_action_intent("")
    assert not o._looks_like_action_intent("Это не требует действий — просто информация.")


def test_ends_with_permission_question():
    """Длинный статус + хвост 'Сейчас восстановлю?' → self-poke (раньше len<200 отбрасывал)."""
    o = _orch()
    long_resp = (
        "Почти всё хорошо, но есть один нюанс: caesar-daemon работает, 9 минут аптайма. "
        "А вот cron-задач ноль. Та самая, которую переносили — пропала. Сейчас восстановлю?"
    )
    assert o._ends_with_permission_question(long_resp)
    assert o._ends_with_permission_question("Сделать?")
    assert o._ends_with_permission_question("Восстановить?")


def test_short_action_intent_still_caught():
    """Короткое обещание БЕЗ '?' ('Найду все машины') — ловится через _looks_like_action_intent."""
    o = _orch()
    assert o._looks_like_action_intent("Найду все машины в сети.")
    assert o._looks_like_action_intent("Сделаю это сейчас.")


def test_not_permission_question():
    """Легитимные вопросы (нужны данные) — self-poke НЕ должен срабатывать."""
    o = _orch()
    assert not o._ends_with_permission_question("Готово: whoami = agent.")
    assert not o._ends_with_permission_question("Какой IP нужной машины?")
    assert not o._ends_with_permission_question("")
    assert not o._ends_with_permission_question("Привет!")
