def subscription_command(text: str, username: str) -> bool | None:
    parts = text.split(maxsplit=1)
    if not parts:
        return None
    command, separator, target = parts[0].partition("@")
    if separator and target.casefold() != username.casefold():
        return None
    if command == "/start":
        return True
    if command == "/stop":
        return False
    return None


def subscription_change(update: dict, username: str) -> tuple[int | None, bool | None]:
    message = update.get("message")
    membership = update.get("my_chat_member")
    event = message if isinstance(message, dict) else membership
    if not isinstance(event, dict):
        return None, None
    chat = event.get("chat")
    if (not isinstance(chat, dict) or chat.get("type") != "private"
            or type(chat.get("id")) is not int or chat["id"] <= 0):
        return None, None
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, str):
            return chat["id"], subscription_command(text, username)
    else:
        member = event.get("new_chat_member")
        if isinstance(member, dict) and member.get("status") == "kicked":
            return chat["id"], False
    return None, None
