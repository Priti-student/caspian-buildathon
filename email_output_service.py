"""Channel-independent email OUTPUT service.

Builds email content from persisted StudentPilot memory (opportunities,
tomorrow's routine, reminders) and hands it to an injected sender callable.
The sender is wired to Caspian only in agent.py, so this module never imports
the SDK and the user's inbox is never read.
"""

import re
from datetime import date, timedelta
from typing import Callable

from storage import StudentPilotStore

EmailSender = Callable[[str, str, str], str | None]


class EmailOutputService:
    """Detect 'email me …' requests and deliver via the configured sender."""

    def __init__(self, store: StudentPilotStore, sender: EmailSender | None = None) -> None:
        self._store = store
        self._sender = sender

    def handle(self, conversation_id: str, user_id: str, text: str) -> str | None:
        lowered = text.lower().strip()
        if not self._is_email_request(lowered):
            return None
        content_type, recipient = self._classify(lowered)
        if content_type is None:
            return "What would you like me to email you? For example: \"email me the opportunities\" or \"send tomorrow's routine to my email\"."
        body = self._build_content(user_id, content_type)
        if body is None:
            return "There is nothing to email for that yet."
        if self._sender is None:
            return "Email sending isn't configured yet. I've prepared the content below:\n\n" + body
        subject = self._subject(content_type)
        confirmation = self._sender(recipient, subject, body)
        if confirmation:
            return confirmation
        return "I couldn't send the email right now. Please try again shortly."

    @staticmethod
    def _is_email_request(text: str) -> bool:
        lower = text.lower()
        # A routine/schedule delivery-time preference (e.g. "send my routines to
        # me at 6:43 pm") is NOT an email request.
        if re.search(r"(?:routine|schedule).*(?:at|by)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", lower):
            return False
        # Explicit email-request phrases only.
        markers = ("email me", "email it", "email the", "email my", "send me an email",
                   "send to my email", "send it to my email", "send my routine to my email",
                   "send my schedule to my email", "send tomorrow's routine to my email",
                   "send tomorrow's schedule to my email", "email my routine", "email my schedule")
        return any(marker in lower for marker in markers)

    @staticmethod
    def _classify(text: str) -> tuple[str | None, str | None]:
        recipient = None
        match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
        if match:
            recipient = match.group(0)
        if "tomorrow" in text or "routine" in text or "schedule" in text:
            return "routine", recipient
        if any(word in text for word in ("opportunit", "internship", "job", "scholarship", "hackathon")):
            return "opportunities", recipient
        if "remind" in text or "deadline" in text:
            return "reminders", recipient
        return None, None

    def _build_content(self, user_id: str, content_type: str) -> str | None:
        if content_type == "opportunities":
            return self._opportunities_content(user_id)
        if content_type == "routine":
            return self._routine_content(user_id)
        if content_type == "reminders":
            return self._reminders_content(user_id)
        return None

    def _opportunities_content(self, user_id: str) -> str | None:
        items = self._store.latest_opportunities(user_id)
        if not items:
            return None
        lines = ["Opportunity summary"]
        for item in items:
            lines.append(
                f"{item['position']}. {item['title']} — {item['category']}\n"
                f"   Organization: {item['organization']}\n"
                f"   Role: {item['role']}\n"
                f"   Location: {item['location']}\n"
                f"   Deadline: {item['deadline']}\n"
                f"   Relevance: {item['relevance_score']}/10 — {item['relevance_reason']}\n"
                f"   Summary: {item['summary']}\n"
                f"   Links: {item['links']}"
            )
        return "\n\n".join(lines)

    def _routine_content(self, user_id: str) -> str | None:
        target_date = date.today() + timedelta(days=1)
        items = self._store.find_items_on(user_id, target_date.isoformat())
        if not items:
            return None
        lines = [f"Tomorrow's commitments ({target_date.isoformat()}):"]
        for item in items:
            time_span = ""
            if item.get("start_time"):
                time_span = f" {item['start_time']}"
                if item.get("end_time"):
                    time_span += f"-{item['end_time']}"
            lines.append(f"- {item['title']} ({item['item_type']}){time_span}")
        return "\n".join(lines)

    def _reminders_content(self, user_id: str) -> str | None:
        reminders = self._store.find_reminders(user_id)
        if not reminders:
            return None
        lines = ["Your reminders:"]
        for reminder in reminders:
            recurrence = f" ({reminder['recurrence']})" if reminder["recurrence"] else ""
            lines.append(f"- {reminder['title']} — {reminder['remind_at']}{recurrence}")
        return "\n".join(lines)

    @staticmethod
    def _subject(content_type: str) -> str:
        if content_type == "opportunities":
            return "StudentPilot: Opportunity summary"
        if content_type == "routine":
            return "StudentPilot: Tomorrow's routine"
        return "StudentPilot: Your reminders"