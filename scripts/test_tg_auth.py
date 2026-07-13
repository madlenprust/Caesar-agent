"""authorize_tg_message — политика доступа TG (привязка/владелец/группа).

Pure-функция без I/O — покрывает все ветки: unpaired-open, owner, stranger,
group-disabled, group-owner-only (владелец ок / чужой молча отказ), group-all.
"""
from types import SimpleNamespace

from caesar.channels.telegram_adapter import authorize_tg_message


def _cfg(allowed=None, owner_uid=0, allow_groups=False, group_access="owner"):
    return SimpleNamespace(
        allowed_chat_ids=allowed or [],
        owner_user_id=owner_uid,
        allow_group_chats=allow_groups,
        group_access=group_access,
    )


def test_unpaired_is_open():
    """Не привязан → открыт (opt-in, без abrupt-локаута на обновлении)."""
    ok, reason = authorize_tg_message(123, 999, "private", _cfg())
    assert ok and reason == "unpaired-open"


def test_owner_private_ok():
    ok, reason = authorize_tg_message(123, 999, "private", _cfg(allowed=[123], owner_uid=999))
    assert ok and reason == "owner"


def test_stranger_private_rejected():
    ok, _ = authorize_tg_message(999, 888, "private", _cfg(allowed=[123], owner_uid=999))
    assert not ok


def test_group_disabled_rejected():
    ok, _ = authorize_tg_message(-100, 888, "supergroup", _cfg(allowed=[123], owner_uid=999, allow_groups=False))
    assert not ok


def test_group_owner_only_owner_ok():
    ok, reason = authorize_tg_message(
        -100, 999, "supergroup",
        _cfg(allowed=[123], owner_uid=999, allow_groups=True, group_access="owner"),
    )
    assert ok and reason == "group-owner"


def test_group_owner_only_stranger_silently_rejected():
    ok, reason = authorize_tg_message(
        -100, 888, "supergroup",
        _cfg(allowed=[123], owner_uid=999, allow_groups=True, group_access="owner"),
    )
    assert not ok and reason == "group-not-owner"


def test_group_all_allows_anyone():
    ok, reason = authorize_tg_message(
        -100, 888, "supergroup",
        _cfg(allowed=[123], owner_uid=999, allow_groups=True, group_access="all"),
    )
    assert ok and reason == "group-all"


def test_group_detected_by_negative_chat_id():
    """Даже без явного type — отрицательный chat_id = группа."""
    ok, reason = authorize_tg_message(
        -100200, 999, "private",
        _cfg(allowed=[123], owner_uid=999, allow_groups=True, group_access="owner"),
    )
    assert ok and reason == "group-owner"
