"""Background dispatcher that delivers due reminders via email and Telegram."""

import logging
from typing import Callable

from storage import StudentPilotStore

logger = logging.getLogger(__name__)

EmailSender = Callable[[str, str, str], str | None]
TelegramSender = Callable[[str, str], bool]


class ReminderDispatcher:
    """Deliver due reminders to the user's linked email and Telegram chat."""

    def __init__(
        self,
        store: StudentPilotStore,
        email_sender: EmailSender | None = None,
        telegram_sender: TelegramSender | None = None,
    ) -> None:
        self._store = store
        self._email_sender = email_sender
        self._telegram_sender = telegram_sender

    def dispatch_due(self, now: str | None = None) -> list[str]:
        """Deliver all due reminders and return delivered titles."""
        delivered: list[str] = []
        for reminder in self._store.due_reminders(now):
            user_id = reminder["user_id"]
            title = reminder["title"]
            remind_at = reminder["remind_at"]
            conversation_id = reminder["conversation_id"]
            message = f"⏰ Reminder: {title}"

            email_ok = self._deliver_email(user_id, title, remind_at)
            tg_ok = self._deliver_telegram(conversation_id, message)

            if email_ok or tg_ok:
                self._store.deactivate_reminder(reminder["id"])
                delivered.append(title)
                logger.info("Delivered reminder %r (email=%s, telegram=%s)", title, email_ok, tg_ok)
            else:
                logger.warning("No channel available to deliver reminder %r", title)
        return delivered

    def _deliver_email(self, user_id: str, title: str, remind_at: str) -> bool:
        if self._email_sender is None:
            return False
        identities = self._store.identities_for_user(user_id)
        emails = [item["address"] for item in identities if item["channel"] == "email"]
        if not emails:
            return False
        subject = f"⏰ Reminder: {title}"
        body = f"Reminder: {title}\nScheduled for: {remind_at}"
        sent = False
        for email in emails:
            try:
                if self._email_sender(email, subject, body):
                    sent = True
            except Exception:
                logger.exception("Failed to email reminder to %s", email)
        return sent

    def _deliver_telegram(self, conversation_id: str, message: str) -> bool:
        if self._telegram_sender is None or not conversation_id:
            return False
        try:
            return self._telegram_sender(conversation_id, message)
        except Exception:
            logger.exception("Failed to send Telegram reminder to %s", conversation_id)
            return False
