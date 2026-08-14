"""Reminder scheduling and state management for tasks and deadlines."""

import re
from datetime import date, datetime, timedelta
from typing import Any

from llm_service import FeatherlessLLM, LLMResponseError
from storage import StudentPilotStore

REMINDER_PROMPT = """You manage a student's reminders. Today is {today}. Return ONLY JSON.
Schema: {{"action":"add|list|stop|postpone|none","target":"string or null","remind_at":"YYYY-MM-DD HH:MM or null","recurrence":"daily|weekly|none or null","needs_clarification":false,"clarification":"string or null"}}
Rules:
- "Remind me about X" → action add, target X.
- "Show my reminders" / "What reminders do I have" → action list.
- "Stop reminding me about X" → action stop, target X.
- "Postpone X" / "Remind me later about X" → action postpone, target X.
- "What deadlines are approaching" / "deadlines this week" → action list (deadlines).
- For unrelated messages use action none."""


class ReminderService:
    def __init__(self, llm: FeatherlessLLM, store: StudentPilotStore) -> None:
        self._llm, self._store = llm, store

    def handle(self, conversation_id: str, user_id: str, text: str, history: list[dict[str, str]]) -> str | None:
        if not self._is_reminder_request(text):
            return None
        try:
            result = self._llm.complete_json(
                REMINDER_PROMPT.format(today=date.today().isoformat()), text, history
            )
        except LLMResponseError:
            return None
        action = result.get("action")
        if action == "none":
            return None
        if result.get("needs_clarification"):
            clarification = result.get("clarification")
            return clarification if isinstance(clarification, str) else "Could you clarify the reminder details?"
        if action == "list":
            return self._list_reminders(user_id)
        target = result.get("target")
        if not isinstance(target, str) or not target.strip():
            return "Which task or event should I remind you about?"
        if action == "add":
            return self._add_reminder(conversation_id, user_id, target, result)
        if action == "stop":
            count = self._store.update_reminders(user_id, target, {"active": 0})
            return "Reminder stopped." if count else "I couldn't find an active reminder for that."
        if action == "postpone":
            remind_at = result.get("remind_at")
            if not remind_at:
                return "When should I remind you instead?"
            count = self._store.update_reminders(user_id, target, {"remind_at": remind_at})
            return "Reminder postponed." if count else "I couldn't find an active reminder for that."
        return None

    @staticmethod
    def _is_reminder_request(text: str) -> bool:
        """Detect actual reminder-management requests.

        A bare statement like "My ML project deadline is tomorrow" or
        "I have to submit X tomorrow" is a task-add request, not a reminder
        request, so it must NOT be intercepted here.
        """
        lower = text.lower()
        # Explicit reminder-management phrases.
        if any(marker in lower for marker in ("remind me", "reminder", "stop reminding", "postpone", "remind")):
            return True
        # Deadline *listing* requests (not task-add statements).
        if "deadline" in lower or "deadlines" in lower:
            # "My X deadline is tomorrow" / "X is due tomorrow" are task adds.
            if re.search(r"\b(?:my|the|this|that)\s+\w+\s+deadline\s+(?:is|was)\b", lower):
                return False
            if re.search(r"\b(?:i have to|i need to|i must|i will|i am going to)\b", lower):
                return False
            # "what deadlines", "show deadlines", "deadlines this week" are lists.
            if any(phrase in lower for phrase in ("what deadlines", "show deadlines", "deadlines this week",
                                                   "approaching deadlines", "upcoming deadlines", "list deadlines")):
                return True
            return False
        return False

    def _add_reminder(self, conversation_id: str, user_id: str, target: str, result: dict[str, Any]) -> str:
        items = self._store.find_items(user_id, target)
        item_id = items[0]["id"] if items else None
        remind_at = result.get("remind_at") or self._default_remind_at()
        recurrence = result.get("recurrence")
        if recurrence not in {"daily", "weekly", "none"}:
            recurrence = None
        self._store.create_reminder(conversation_id, user_id, item_id, target, remind_at, recurrence)
        return f"Reminder set for {target} at {remind_at}."

    def _list_reminders(self, user_id: str) -> str:
        reminders = self._store.find_reminders(user_id)
        if not reminders:
            return "You have no active reminders."
        lines = ["Your reminders:"]
        for reminder in reminders:
            recurrence = f" ({reminder['recurrence']})" if reminder["recurrence"] else ""
            lines.append(f"• {reminder['title']} — {reminder['remind_at']}{recurrence}")
        return "\n".join(lines)

    @staticmethod
    def _default_remind_at() -> str:
        return (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")