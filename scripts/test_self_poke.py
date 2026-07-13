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
