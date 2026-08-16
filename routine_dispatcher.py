"""Background dispatcher that sends daily routines to users at their preferred time."""

import logging
from datetime import datetime
from typing import Any, Callable

from routine_service import RoutineService
from storage import StudentPilotStore

logger = logging.getLogger(__name__)

TelegramSender = Callable[[str, str], bool]


class RoutineDispatcher:
    """Deliver today's daily routine to users at their preferred delivery time."""

    def __init__(
        self,
        store: StudentPilotStore,
        routine_service: RoutineService,
        telegram_sender: TelegramSender | None = None,
    ) -> None:
        self._store = store
        self._routine = routine_service
        self._telegram_sender = telegram_sender
        self._last_sent: dict[str, str] = {}

    def dispatch_due(self) -> list[str]:
        """Send daily routines to every user whose preferred time has been reached today.

        Each user is only sent one routine per calendar day. Returns the list of
        user_ids that received a routine.
        """
        now = datetime.now()
        today = now.date().isoformat()
        current_hhmm = f"{now.hour:02d}:{now.minute:02d}"
        delivered: list[str] = []

        # Find all users who have a routine preference (i.e., have interacted).
        # We query the routine_preferences table directly for all users.
        rows = self._store.find_all_routine_preferences()
        for row in rows:
            user_id = row["user_id"]
            preferred = row["preferred_time"] or "01:00"
            conversation_id = row.get("conversation_id")
            # Only send if the preferred time has been reached and we haven't
            # already sent today's routine to this user.
            if preferred > current_hhmm:
                continue
            last_sent_date = self._last_sent.get(user_id)
            if last_sent_date == today:
                continue
            if not conversation_id or self._telegram_sender is None:
                continue
            # Generate today's routine and send it.
            try:
                blocks = self._routine.generate_for_date(user_id, today, include_defaults=True)
                if blocks is None:
                    continue
                title = f"Your routine for today ({today}):"
                message = self._routine.format_blocks(blocks, title=title)
                if self._telegram_sender(conversation_id, message):
                    self._last_sent[user_id] = today
                    delivered.append(user_id)
                    logger.info("Sent daily routine to %s at %s", user_id, current_hhmm)
            except Exception:
                logger.exception("Failed to send routine to %s", user_id)
        return delivered

    @staticmethod
    def users_with_preferences(store: StudentPilotStore) -> list[dict[str, Any]]:
        """Return all users who have set a routine preference."""
        return store.find_all_routine_preferences()
