"""Tomorrow's one-day routine generation from stored tasks/events and user input."""

from datetime import date, timedelta
from typing import Any

from llm_service import FeatherlessLLM, LLMResponseError
from storage import StudentPilotStore

ROUTINE_PROMPT = """You build a practical one-day schedule for {target_date} ({target_weekday}) for a student.
Return ONLY JSON. Schema: {{"blocks":[{{"start":"HH:MM","end":"HH:MM","activity":"string"}}],"needs_more":false,"clarification":"string or null"}}

Rules:
- Preserve every fixed-time commitment exactly from the supplied commitments; do not reschedule or overlap them.
- Fill in the remaining time with the user's stated work items and reasonable personal slots (morning routine, meals, rest).
- If there is insufficient information to build a meaningful day, set needs_more true and ask only for the missing essentials.
- Return blocks sorted by start time."""


class RoutineService:
    def __init__(self, llm: FeatherlessLLM, store: StudentPilotStore) -> None:
        self._llm, self._store = llm, store

    def handle(self, conversation_id: str, user_id: str, text: str, history: list[dict[str, str]]) -> str | None:
        if not self._is_routine_request(text):
            return None
        target_date = date.today() + timedelta(days=1)
        commitments = [dict(item) for item in self._store.find_items_on(conversation_id, target_date.isoformat())]
        try:
            result = self._llm.complete_json(
                ROUTINE_PROMPT.format(target_date=target_date.isoformat(), target_weekday=target_date.strftime("%A")),
                self._build_prompt(text, commitments),
                history,
            )
        except LLMResponseError:
            return None
        if result.get("needs_more"):
            clarification = result.get("clarification")
            return clarification if isinstance(clarification, str) else "What should I include in your tomorrow? Tell me your classes, meetings, and tasks."
        blocks = result.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return "I couldn't build a routine for you. Please try again."
        return self._format(blocks)

    @staticmethod
    def _is_routine_request(text: str) -> bool:
        lower = text.lower()
        markers = ("plan my tomorrow", "routine for tomorrow", "tomorrow's routine", "my schedule for tomorrow",
                   "schedule for tomorrow", "what do i have tomorrow", "focus on tomorrow",
                   "plan tomorrow", "create my routine", "tomorrow's schedule")
        return any(marker in lower for marker in markers)

    @staticmethod
    def _build_prompt(text: str, commitments: list[dict[str, Any]]) -> str:
        lines = [f"User request: {text}"]
        if commitments:
            lines.append("Stored commitments for the day:")
            for item in commitments:
                when = item["event_date"] or item.get("deadline")
                time_span = ""
                if item.get("start_time"):
                    time_span = f" {item['start_time']}"
                    if item.get("end_time"):
                        time_span += f"-{item['end_time']}"
                lines.append(f"- {item['title']} ({item['item_type']}){time_span}")
        return "\n".join(lines)

    @staticmethod
    def _format(blocks: list[dict[str, Any]]) -> str:
        lines = ["Your routine:"]
        for block in blocks:
            start = block.get("start") or "?"
            end = block.get("end") or "?"
            activity = block.get("activity") or "?"
            lines.append(f"{start}–{end} — {activity}")
        return "\n".join(lines)