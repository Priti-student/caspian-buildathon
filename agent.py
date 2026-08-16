"""Caspian message gateway integration.

Set ``your_agent_logic`` to call your actual agent.  The same handler services
every Caspian channel that is connected below.
"""

import logging
import os
import threading
import time
from pathlib import Path

from caspian_sdk import CommClient, CommError
from llm_service import FeatherlessLLM
from reminder_dispatcher import ReminderDispatcher
from routine_dispatcher import RoutineDispatcher
from storage import StudentPilotStore
from studentpilot_service import StudentPilotService

logger = logging.getLogger(__name__)


def get_env_setting(name: str) -> str:
    """Read a setting from the process environment or local ignored .env file."""
    if value := os.getenv(name):
        return value

    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip('"').strip("'")

    raise RuntimeError(f"Missing required environment variable: {name}")


def get_optional_env_setting(name: str, default: str | None = None) -> str | None:
    """Read an optional setting; returns default instead of raising."""
    try:
        return get_env_setting(name)
    except RuntimeError:
        return default


def set_env_setting(name: str, value: str) -> None:
    """Persist a setting to the local .env file (idempotent)."""
    env_path = Path(__file__).with_name(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found = False
    for index, line in enumerate(lines):
        key, separator, _ = line.partition("=")
        if separator and key.strip() == name:
            lines[index] = f"{name}={value}"
            found = True
            break
    if not found:
        lines.append(f"{name}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


client = CommClient()
llm = FeatherlessLLM(api_key=get_env_setting("FEATHERLESS_API_KEY"))
store = StudentPilotStore()


def send_email_via_caspian(recipient: str | None, subject: str, body: str) -> str | None:
    """Send an email through the connected Caspian email channel (outbound only)."""
    recipient = recipient or get_optional_env_setting("CASPIAN_EMAIL_RECIPIENT")
    email_connection_id = get_optional_env_setting("CASPIAN_EMAIL_CONNECTION_ID")
    if not recipient or not email_connection_id:
        return None
    try:
        client.initiate(email_connection_id, recipient, f"Subject: {subject}\n\n{body}")
        return f"Email sent to {recipient}."
    except CommError:
        return None


studentpilot = StudentPilotService(llm, store, email_sender=send_email_via_caspian)


def send_telegram_reminder(conversation_id: str, message: str) -> bool:
    """Send a proactive Telegram message to the originating conversation."""
    try:
        client.send_message(conversation_id, message)
        return True
    except CommError:
        logger.exception("Failed to send Telegram reminder to %s", conversation_id)
        return False


reminder_dispatcher = ReminderDispatcher(
    store,
    email_sender=send_email_via_caspian,
    telegram_sender=send_telegram_reminder,
)

routine_dispatcher = RoutineDispatcher(
    store,
    routine_service=studentpilot._routine,
    telegram_sender=send_telegram_reminder,
)


def reminder_loop(stop_event: threading.Event) -> None:
    """Background loop that dispatches due reminders every 30 seconds."""
    while not stop_event.is_set():
        try:
            reminder_dispatcher.dispatch_due()
        except Exception:
            logger.exception("Reminder dispatch failed")
        stop_event.wait(30)


def routine_loop(stop_event: threading.Event) -> None:
    """Background loop that sends daily routines at each user's preferred time."""
    while not stop_event.is_set():
        try:
            routine_dispatcher.dispatch_due()
        except Exception:
            logger.exception("Routine dispatch failed")
        stop_event.wait(60)


def your_agent_logic(text: str, conversation_id: str, user_id: str) -> str:
    """Use isolated, persistent context for every channel conversation."""
    return studentpilot.respond(conversation_id, user_id, text)


@client.on_message
def handle(message):
    """Reply on the originating channel while preserving its conversation."""
    sender = message.sender
    channel = message.channel or "unknown"
    address = sender.get("address", "unknown") if isinstance(sender, dict) else str(sender)
    # Resolve the canonical StudentPilot user for this channel identity.
    user_id = store.resolve_user_id(channel, address)
    # Preserve subject and fall back to HTML when plain text is absent.
    text = message.text or message.html or ""
    if message.subject:
        text = f"{message.subject}\n\n{text}"
    answer = your_agent_logic(text, message.conversation_id, user_id)
    message.reply(answer)


if __name__ == "__main__":
    telegram = client.connect_telegram(bot_token=get_env_setting("TELEGRAM_BOT_TOKEN"))
    print(f"Telegram bot connected: {telegram['address']}")
    # Start the background reminder dispatcher.
    stop_event = threading.Event()
    reminder_thread = threading.Thread(target=reminder_loop, args=(stop_event,), daemon=True)
    reminder_thread.start()
    print("Reminder dispatcher started.")
    # Start the background routine dispatcher.
    routine_thread = threading.Thread(target=routine_loop, args=(stop_event,), daemon=True)
    routine_thread.start()
    print("Routine dispatcher started.")
    # Email is a first-class channel. Reuse the existing connection if present;
    # otherwise create the mailbox once.
    email_connection_id = get_optional_env_setting("CASPIAN_EMAIL_CONNECTION_ID")
    if email_connection_id:
        print(f"Email connected: studentpilot@agents.trycaspianai.com (connection {email_connection_id})")
    else:
        email_settings = {
            "customer_id": get_optional_env_setting("CASPIAN_EMAIL_CUSTOMER_ID"),
            "agent_id": get_optional_env_setting("CASPIAN_EMAIL_AGENT_ID"),
            "username": get_optional_env_setting("CASPIAN_EMAIL_USERNAME"),
        }
        optional = {key: value for key, value in email_settings.items() if value}
        if optional:
            try:
                email = client.connect_email(**optional)
                print(f"Email connected: {email['address']}")
                connection_id = email.get("id")
                if connection_id:
                    set_env_setting("CASPIAN_EMAIL_CONNECTION_ID", str(connection_id))
                    print(f"Email connection id saved: {connection_id}")
            except CommError as error:
                print(f"Email connection skipped: {error}")
    # `client.behavior_prompt()` can be appended to your agent's system prompt.
    client.listen()
