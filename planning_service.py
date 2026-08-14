"""Natural-language task and event extraction; no channel or SQL in the LLM."""

import re
from datetime import date, timedelta
from typing import Any

from llm_service import FeatherlessLLM, LLMResponseError
from storage import StudentPilotStore


PLANNING_PROMPT = """You extract and manage a student's tasks and events. Return ONLY JSON.
Today is {today}. Resolve unambiguous relative dates to ISO YYYY-MM-DD. If a date/time is genuinely ambiguous, set needs_clarification true and do not add/update/delete anything.
Schema: {{"action":"add|list|query|update|delete|complete|none","target":"string or null","items":[{{"title":"string","item_type":"task|deadline|meeting|class|interview|personal event|other","event_date":"YYYY-MM-DD or null","start_time":"HH:MM or null","end_time":"HH:MM or null","deadline":"YYYY-MM-DD or null","priority":"high|medium|low or null","notes":"string or null","recurrence":"string or null"}}],"updates":{{"field":"value"}},"needs_clarification":false,"clarification":"string or null"}}
Rules: detect only commitments the user wants remembered or management requests. A due task has title/item_type task and its due date in deadline, not event_date. An event has its date in event_date. Never invent missing details. For unrelated messages use action none."""


class PlanningService:
    def __init__(self, llm: FeatherlessLLM, store: StudentPilotStore) -> None:
        self._llm, self._store = llm, store

    def handle(self, conversation_id: str, user_id: str, text: str, history: list[dict[str, str]]) -> str | None:
        try:
            result = self._llm.complete_json(PLANNING_PROMPT.format(today=date.today().isoformat()), text, history)
        except LLMResponseError:
            return None
        action = result.get("action")
        if action == "none":
            return None
        if result.get("needs_clarification"):
            clarification = result.get("clarification")
            return clarification if isinstance(clarification, str) else "Could you clarify the date or time?"
        if action == "add":
            items = [self._clean_item(item, text) for item in result.get("items", []) if isinstance(item, dict)]
            items = [item for item in items if item]
            if not items:
                return "I need a task or event description to save."
            for item in items:
                self._store.create_item(conversation_id, user_id, item)
            return "Saved: " + "; ".join(item["title"] for item in items) + "."
        target = result.get("target")
        if action == "list":
            return self._format_items(self._store.find_items(user_id))
        if action == "complete":
            pending = self._store.find_items(user_id)
            if not pending:
                return "You have no pending tasks or events to mark as complete."
            if isinstance(target, str) and target.strip():
                count = self._store.update_items(user_id, target, {"status": "completed"})
                return "Marked as completed." if count else "I couldn't find that task or event."
            # The user said they completed their work but didn't name a task.
            # Complete all pending items (the natural interpretation of a blanket
            # "I'm done" that must be removed from the pending list).
            for item in pending:
                self._store.update_items(user_id, item["title"], {"status": "completed"})
            return "Marked as completed: " + "; ".join(item["title"] for item in pending) + "."
        if not isinstance(target, str) or not target.strip():
            return "Which task or event do you mean?"
        if action == "query":
            return self._format_items(self._store.find_items(user_id, target))
        if action == "delete":
            count = self._store.delete_items(user_id, target)
            return "Deleted." if count else "I couldn't find that task or event."
        if action == "update":
            updates = result.get("updates") if isinstance(result.get("updates"), dict) else {}
            count = self._store.update_items(user_id, target, updates)
            return "Updated." if count else "I couldn't find that task or event."
        return None

    @staticmethod
    def _clean_item(item: dict[str, Any], source_text: str) -> dict[str, Any] | None:
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        result = {key: item.get(key) for key in ("title", "item_type", "event_date", "start_time", "end_time", "deadline", "priority", "notes", "recurrence")}
        result["title"] = title.strip()
        result["item_type"] = result["item_type"] if result["item_type"] in {"task", "deadline", "meeting", "class", "interview", "personal event", "other"} else "other"
        relative_date = PlanningService._relative_date(source_text)
        if relative_date:
            # A deterministic Python-resolved date overrides any date the LLM
            # supplied, and the other date field is cleared so the stored item
            # never carries a conflicting LLM-invented date.
            if PlanningService._is_deadline(source_text):
                result["deadline"] = relative_date
                result["event_date"] = None
            else:
                result["event_date"] = relative_date
                result["deadline"] = None
        return result

    @staticmethod
    def _is_deadline(text: str) -> bool:
        """Detect deadline semantics: due/by/before/submit/finish/complete."""
        lower = text.lower()
        if re.search(r"\b(due|deadline|before|by|submit|finish)\b", lower):
            return True
        return bool(re.search(r"\bcomplete\b.*\bby\b", lower))

    @staticmethod
    def _relative_date(text: str) -> str | None:
        """Resolve common relative date phrases to an ISO date."""
        lower = re.sub(r"\s+", " ", text.lower()).strip()
        today = date.today()

        if re.search(r"\bday after tomorrow\b", lower):
            return (today + timedelta(days=2)).isoformat()
        if re.search(r"\btomorrow\b", lower):
            return (today + timedelta(days=1)).isoformat()
        if re.search(r"\b(today|tonight|this evening)\b", lower):
            return today.isoformat()

        # "in three days", "in 3 days", "in a week", "in 2 weeks"
        number_words = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        match = re.search(r"\bin\s+(?:about\s+)?(?:an?\s+)?(\d+|[a-z]+)\s+(day|week)s?\b", lower)
        if match:
            amount_text, unit = match.group(1), match.group(2)
            amount = int(amount_text) if amount_text.isdigit() else number_words.get(amount_text)
            if amount:
                days = amount * 7 if unit == "week" else amount
                return (today + timedelta(days=days)).isoformat()
        if re.search(r"\bin\s+an?\s+(day|week)\b", lower):
            unit = re.search(r"\bin\s+an?\s+(day|week)\b", lower).group(1)
            days = 7 if unit == "week" else 1
            return (today + timedelta(days=days)).isoformat()

        if re.search(r"\bnext week\b", lower):
            return (today + timedelta(days=7)).isoformat()

        weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
        # "upcoming Friday" is treated like "this Friday" (the next occurrence),
        # so it resolves deterministically instead of being left to the LLM.
        match = re.search(r"\b(this|next|upcoming)\s+(" + "|".join(weekdays) + r")\b", lower)
        if match:
            prefix, weekday = match.group(1), match.group(2)
            target = weekdays[weekday]
            days = (target - today.weekday()) % 7
            if prefix == "next" and days <= 2:
                # "next Monday" said on Sunday means the Monday after tomorrow's Monday.
                days += 7
            return (today + timedelta(days=days)).isoformat()
        return None

    @staticmethod
    def _format_items(items: list[dict[str, Any]]) -> str:
        if not items:
            return "You have no matching upcoming tasks or events."
        lines = ["Upcoming tasks and events:"]
        for item in items:
            when = item["event_date"] or item["deadline"] or "Not specified"
            time = f" at {item['start_time']}" if item["start_time"] else ""
            suffix = f" (deadline: {item['deadline']})" if item["deadline"] and item["event_date"] else ""
            lines.append(f"• {item['title']} — {item['item_type']}, {when}{time}{suffix}; {item['status']}")
        return "\n".join(lines)