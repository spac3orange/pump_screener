from screener.domain.commercial import StartedUser, UserRole
from screener.infrastructure.telegram.commercial_texts import started_users_messages


def test_started_users_message_shows_count_nick_and_id() -> None:
    users = [
        StartedUser(1001, "alice_nick", "Alice", UserRole.USER),
        StartedUser(1, None, "Admin", UserRole.ADMIN),
        StartedUser(1002, None, None, UserRole.USER),
    ]
    text = started_users_messages(users)[0]
    assert "Запускали бота: 3" in text
    assert '@alice_nick</a> — <code>1001</code>' in text
    assert "Admin</a> — <code>1</code> · админ" in text
    assert "без ника</a> — <code>1002</code>" in text


def test_started_users_message_when_nobody() -> None:
    text = started_users_messages([])[0]
    assert "Запускали бота: 0" in text
    assert "никто" in text
