"""Daily routine generation with preferences, defaults, and day-specific updates."""

import re
from datetime import date, datetime, timedelta
from typing import Any

from llm_service import FeatherlessLLM, LLMResponseError
from storage import StudentPilotStore

DEFAULT_ROUTINE_TIME = "01:00"

ROUTINE_PROMPT = """You build a one-day schedule for {target_date} ({target_weekday}) for a student.
Return ONLY JSON. Schema: {{"blocks":[{{"start":"HH:MM","end":"HH:MM","activity":"string"}}],"needs_more":false,"clarification":"string or null"}}

Rules:
- The schedule is strictly for {target_date} ({target_weekday}). Never reference, compute, or display any other date.
- ONLY include the activities the user has explicitly specified (their daily default routine and their commitments for this day).
- Do NOT invent, add, or assume any activities the user did not mention. Do NOT add breakfast, lunch, dinner, meals, rest, relaxation, morning routine, or free time unless the user explicitly told you to include them.
- If there are no defaults and no commitments for this day, set needs_more true and clarification explaining there is nothing scheduled for that day.
- Return blocks sorted by start time."""

ROUTINE_DEFAULTS_PROMPT = """Extract the user's recurring daily routine defaults. Return ONLY JSON.
Schema: {{"has_defaults":true,"blocks":[{{"start":"HH:MM","end":"HH:MM","activity":"string","days":["monday","tuesday",...]}}],"clarification":"string or null"}}

Rules:
- Extract recurring activities like college/gym/office/classes the user does on a regular schedule.
- "every day" or "daily" means all seven days. "weekdays" means Monday-Friday. "except Saturday and Sunday" means Mon-Fri.
- If the user says "no" or provides no daily default, return has_defaults false.
- If the user provides partial info (e.g. missing end time), set needs_clarification true and clarify.
- Never invent details beyond what the user said."""

# Activities the LLM tends to invent even though the user never asked for them.
# These are filtered out of generated routines unless the user explicitly
# included them in their defaults or commitments.
INVENTED_ACTIVITY_PATTERNS = [
    r"\bbreakfast\b", r"\blunch\b", r"\bdinner\b", r"\bmorning routine\b",
    r"\bfree time\b", r"\brelaxation\b", r"\bentertainment\b", r"\bmeal\b",
    r"\bbrunch\b", r"\bsnack\b", r"\btea\b", r"\bcoffee break\b",
    r"\bwake up\b", r"\bget ready\b", r"\bpersonal care\b", r"\bself care\b",
    r"\bmeditation\b", r"\breading\b", r"\bwatch tv\b", r"\bwatch television\b",
    r"\bsocial media\b", r"\bscrolling\b", r"\bchores\b", r"\bhousework\b",
    r"\bcleaning\b", r"\bstudy break\b", r"\brest\b", r"\bsleep\b",
    r"\bnap\b", r"\bwind down\b", r"\bunwind\b", r"\bexercise\b", r"\bworkout\b",
    r"\bget up\b", r"\bgo to bed\b", r"\bprepare\b", r"\bpreparation\b",
    r"\bpersonal hygiene\b", r"\bshower\b", r"\bdressing\b", r"\bcommute\b",
    r"\btravel\b", r"\btransport\b", r"\bphone time\b", r"\bscreen time\b",
    r"\bplay\b", r"\bgames\b", r"\bgaming\b", r"\bhobby\b", r"\bhobbies\b",
    r"\bleisure\b", r"\bweekend\b", r"\bholiday\b", r"\bvacation\b",
]


class RoutineService:
    def __init__(self, llm: FeatherlessLLM, store: StudentPilotStore) -> None:
        self._llm, self._store = llm, store

    def handle(self, conversation_id: str, user_id: str, text: str, history: list[dict[str, str]]) -> str | None:
        # 1. Preferred delivery time setup/change
        if self._is_preferred_time_request(text):
            return self._handle_preferred_time(conversation_id, user_id, text)

        # 2. Daily default routine setup
        if self._is_defaults_request(text):
            return self._handle_defaults(conversation_id, user_id, text, history)

        # 3. Delete/clear routine request (must come before add-to-today and
        #    standard routine requests so "delete all tasks from today's routine"
        #    is not misrouted to a routine display).
        if self._is_delete_routine_request(text):
            return self._handle_delete_routine(user_id, text)

        # 4. Add-to-today's-routine request
        if self._is_add_to_today_request(text):
            return self._handle_add_to_today(conversation_id, user_id, text, history)

        # 5. Standard routine request (tomorrow / today)
        if self._is_routine_request(text):
            return self._handle_routine_request(conversation_id, user_id, text, history)

        return None

    # ── Intent detection ────────────────────────────────────────────────────

    @staticmethod
    def _is_preferred_time_request(text: str) -> bool:
        lower = text.lower()
        # "send me my daily routine at 4am"
        # "send the routine at 11 pm"
        # "always send my routines to me at 6:43 pm"
        # The time pattern requires either minutes OR am/pm, and accepts
        # plurals like "routines"/"schedules". We also accept "always/at"
        # wording without an explicit send/change verb.
        time_pattern = r"(?:at|by)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
        if re.search(r"(?:routine|schedule|routines|schedules).*" + time_pattern, lower):
            return True
        return bool(re.search(r"(?:send|message|deliver|give|get).*(?:routine|schedule|routines|schedules).*(?:at|by)", lower))

    @staticmethod
    def _is_defaults_request(text: str) -> bool:
        lower = text.lower()
        markers = ("every day", "everyday", "daily", "weekdays", "except saturday", "except sunday",
                   "go to college", "go to gym", "go to office", "classes from", "my routine is",
                   "default routine", "i usually", "i typically")
        return any(marker in lower for marker in markers)

    @staticmethod
    def _is_add_to_today_request(text: str) -> bool:
        lower = text.lower()
        return ("add" in lower or "include" in lower or "update" in lower) and (
            "today" in lower or "routine" in lower or "schedule" in lower
        )

    @staticmethod
    def _is_delete_routine_request(text: str) -> bool:
        """Detect requests to delete/clear/remove tasks from a routine."""
        lower = text.lower()
        # Must contain a delete/remove/clear verb AND reference routine/schedule.
        has_delete_verb = any(verb in lower for verb in ("delete", "remove", "clear", "erase", "wipe"))
        has_routine_ref = any(ref in lower for ref in ("routine", "schedule", "tasks in", "tasks written"))
        if not (has_delete_verb and has_routine_ref):
            return False
        # "delete all tasks from today's routine", "remove everything from my routine",
        # "clear my routine", "delete all the tasks written in today's routine"
        if any(phrase in lower for phrase in ("all", "everything", "every", "all the")):
            return True
        # "delete the routine", "remove the routine", "clear the routine"
        if re.search(r"\b(delete|remove|clear|erase|wipe)\b.*\b(routine|schedule)\b", lower):
            return True
        return False

    @staticmethod
    def _is_routine_request(text: str) -> bool:
        lower = text.lower()
        markers = ("plan my tomorrow", "routine for tomorrow", "tomorrow's routine", "my schedule for tomorrow",
                   "schedule for tomorrow", "what do i have tomorrow", "focus on tomorrow",
                   "plan tomorrow", "create my routine", "tomorrow's schedule", "today's routine",
                   "show my routine", "what is my routine")
        return any(marker in lower for marker in markers)

    # ── Preferred time handling ─────────────────────────────────────────────

    def _handle_preferred_time(self, conversation_id: str, user_id: str, text: str) -> str | None:
        parsed = self._parse_time(text)
        if parsed is None:
            pref = self._store.get_routine_preference(user_id)
            if not pref["asked_for_preference"]:
                self._store.mark_routine_preference_asked(user_id)
                return ("I can send you a daily routine every morning. "
                        "What time would you like to receive it? "
                        "(e.g. \"send my daily routine at 5 am\"). "
                        "If you don't specify, I'll send it at 1 am by default.")
            return ("I can change your daily routine delivery time. "
                    "What time would you like? (e.g. \"send my routine at 4 am\")")
        self._store.set_routine_preferred_time(user_id, parsed, conversation_id)
        pref = self._store.get_routine_preference(user_id)
        reply = f"Got it. I'll send your daily routine at {parsed}."
        if not pref["asked_for_defaults"]:
            self._store.mark_routine_defaults_asked(user_id)
            reply += (" Do you have any daily default routine I should always include? "
                      'For example: "I go to college every day from 9 am to 5 pm except Saturday and Sunday."')
        return reply

    @staticmethod
    def _parse_time(text: str) -> str | None:
        """Extract HH:MM from text like '4am', '4:30 pm', '11 pm', '23:00'.

        Handles 12-hour clock correctly: "12:47 am" → "00:47", "12:47 pm" → "12:47".
        """
        lower = text.lower()
        # First try HH:MM with optional am/pm suffix (e.g. "12:47 am", "4:30 pm", "23:00").
        m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", lower)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            suffix = m.group(3)
            if suffix == "pm" and hour < 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        # Then try bare hour with am/pm (e.g. "4am", "11 pm").
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", lower)
        if m:
            hour = int(m.group(1))
            suffix = m.group(2)
            if suffix == "pm" and hour < 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23:
                return f"{hour:02d}:00"
        return None

    # ── Daily defaults handling ─────────────────────────────────────────────

    def _handle_defaults(self, conversation_id: str, user_id: str, text: str, history) -> str | None:
        if re.search(r"\b(no|none|nothing|not really|skip)\b", text.lower()):
            self._store.mark_routine_defaults_asked(user_id)
            return "No problem. I'll build your routine from your tasks and events each day."
        try:
            result = self._llm.complete_json(ROUTINE_DEFAULTS_PROMPT, text, history)
        except LLMResponseError:
            return None
        if result.get("needs_more") or not result.get("has_defaults"):
            clarification = result.get("clarification")
            return clarification if isinstance(clarification, str) else "Tell me your daily routine, e.g. \"I go to college from 9 to 5 every weekday.\""
        blocks = result.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return "Tell me your daily default routine and I'll remember it."
        self._store.delete_routine_defaults(user_id)
        days_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        created = []
        for block in blocks:
            start = block.get("start")
            end = block.get("end")
            activity = block.get("activity")
            if not start or not end or not activity:
                continue
            days = block.get("days") or []
            if not days:
                self._store.add_routine_default(user_id, None, start, end, activity)
                created.append(f"{start}-{end} {activity} (every day)")
            else:
                for day in days:
                    day_lower = day.lower()
                    if day_lower in days_map:
                        self._store.add_routine_default(user_id, days_map[day_lower], start, end, activity)
                        created.append(f"{start}-{end} {activity} ({day})")
        self._store.mark_routine_defaults_asked(user_id)
        return "Saved your daily default routine:\n" + "\n".join(created) if created else "I couldn't parse that. Tell me like: \"I go to college from 9 to 5 every weekday.\""

    # ── Delete routine handling ─────────────────────────────────────────────

    def _handle_delete_routine(self, user_id: str, text: str) -> str:
        """Delete/clear the routine for the requested day (defaults to today)."""
        lower = text.lower()
        # Determine which day's routine to clear.
        if "tomorrow" in lower:
            target_date = date.today() + timedelta(days=1)
        else:
            target_date = date.today()
        target_str = target_date.isoformat()

        # Clear the cached daily routine.
        self._store.delete_daily_routine(user_id, target_str)

        # Also clear any planner items that are part of that day's routine,
        # since the user asked to delete the tasks written in the routine.
        items = self._store.find_items_on(user_id, target_str)
        deleted_count = 0
        for item in items:
            self._store.delete_items(user_id, item["title"])
            deleted_count += 1

        if deleted_count:
            return (f"Deleted {deleted_count} task(s) from your routine for "
                    f"{target_str} ({target_date.strftime('%A')}).")
        return (f"Cleared your routine for {target_str} ({target_date.strftime('%A')}). "
                "No tasks were stored for that day.")

    # ── Add to today's routine ──────────────────────────────────────────────

    def _handle_add_to_today(self, conversation_id: str, user_id: str, text: str, history) -> str | None:
        today = date.today()
        today_str = today.isoformat()
        blocks = self._store.get_daily_routine(user_id, today_str)
        if blocks is None:
            blocks = self._generate_blocks(user_id, today_str, history, include_defaults=True)
        if blocks is None:
            return None
        try:
            result = self._llm.complete_json(
                ROUTINE_PROMPT.format(target_date=today_str, target_weekday=today.strftime("%A")),
                self._build_prompt(text, self._store.find_items_on(user_id, today_str), blocks),
                history,
            )
        except LLMResponseError:
            return None
        blocks = result.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return "I couldn't update today's routine. Please try again."
        # Filter out invented activities the user never specified.
        blocks = self._filter_invented_blocks(blocks, user_id, today_str)
        self._store.save_daily_routine(user_id, today_str, blocks)
        return self._format(blocks, title=f"Updated routine for today ({today_str}):")

    # ── Standard routine request ────────────────────────────────────────────

    def _handle_routine_request(self, conversation_id: str, user_id: str, text: str, history) -> str | None:
        lower = text.lower()
        if "today" in lower and "tomorrow" not in lower:
            target_date = date.today()
        else:
            target_date = date.today() + timedelta(days=1)
        target_str = target_date.isoformat()
        blocks = self._store.get_daily_routine(user_id, target_str)
        if blocks is None:
            blocks = self._generate_blocks(user_id, target_str, history, include_defaults=True)
        if blocks is None:
            return "I couldn't build a routine for you. Please try again."
        if blocks == []:
            return (f"You have nothing scheduled on {target_str} ({target_date.strftime('%A')}). "
                    "No default routine or tasks are set for this day. "
                    'To set your daily default routine, tell me e.g. "I go to college from 9 to 5 every weekday."')
        # Filter out invented activities even from cached routines.
        blocks = self._filter_invented_blocks(blocks, user_id, target_str)
        self._store.save_daily_routine(user_id, target_str, blocks)
        title = f"Your routine for {target_str} ({target_date.strftime('%A')}):"
        return self._format(blocks, title=title)

    def generate_for_date(self, user_id: str, target_str: str, include_defaults: bool = True) -> list[dict[str, Any]] | None:
        """Public wrapper to generate (or fetch) the routine blocks for a given date."""
        blocks = self._store.get_daily_routine(user_id, target_str)
        if blocks is not None:
            # Filter out invented activities even from cached routines.
            blocks = self._filter_invented_blocks(blocks, user_id, target_str)
            return blocks
        generated = self._generate_blocks(user_id, target_str, [], include_defaults=include_defaults)
        if generated:
            self._store.save_daily_routine(user_id, target_str, generated)
        return generated

    @staticmethod
    def format_blocks(blocks: list[dict[str, Any]], title: str = "Your routine:") -> str:
        """Public wrapper to format routine blocks for display."""
        return RoutineService._format(blocks, title=title)

    def _generate_blocks(self, user_id: str, target_str: str, history, include_defaults: bool = True) -> list[dict[str, Any]] | None:
        target_date = date.fromisoformat(target_str)
        commitments = [dict(item) for item in self._store.find_items_on(user_id, target_str)]
        defaults = self._store.find_routine_defaults(user_id, target_date.weekday()) if include_defaults else []
        # Do NOT assume or invent a routine when the user has nothing scheduled.
        if not commitments and not defaults:
            return []
        try:
            result = self._llm.complete_json(
                ROUTINE_PROMPT.format(target_date=target_str, target_weekday=target_date.strftime("%A")),
                self._build_prompt("", commitments, defaults),
                history,
            )
        except LLMResponseError:
            return None
        if result.get("needs_more"):
            return None
        blocks = result.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return None
        # Filter out invented activities the user never specified.
        blocks = self._filter_invented_blocks(blocks, user_id, target_str)
        return blocks

    def _filter_invented_blocks(self, blocks: list[dict[str, Any]], user_id: str, target_str: str) -> list[dict[str, Any]]:
        """Remove blocks whose activity matches an invented pattern unless the
        user explicitly included that activity in their defaults or commitments."""
        target_date = date.fromisoformat(target_str)
        commitments = [dict(item) for item in self._store.find_items_on(user_id, target_str)]
        defaults = self._store.find_routine_defaults(user_id, target_date.weekday())

        # Build a set of known activity keywords from the user's own data.
        known_activities = set()
        for item in commitments:
            title = item.get("title", "").lower()
            known_activities.add(title)
            for word in re.findall(r"[a-z]+", title):
                if len(word) > 2:
                    known_activities.add(word)
        for item in defaults:
            activity = item.get("activity", "").lower()
            known_activities.add(activity)
            for word in re.findall(r"[a-z]+", activity):
                if len(word) > 2:
                    known_activities.add(word)

        filtered = []
        for block in blocks:
            activity = block.get("activity", "")
            activity_lower = activity.lower()
            is_invented = any(re.search(pattern, activity_lower) for pattern in INVENTED_ACTIVITY_PATTERNS)
            if not is_invented:
                filtered.append(block)
                continue
            activity_words = set(re.findall(r"[a-z]+", activity_lower))
            if activity_words & known_activities:
                filtered.append(block)
        return filtered

    @staticmethod
    def _build_prompt(text: str, commitments: list[dict[str, Any]], defaults: list[dict[str, Any]] | None = None) -> str:
        lines = [f"User request: {text}"]
        if defaults:
            lines.append("Daily default routine / existing blocks:")
            for item in defaults:
                start = item.get("start_time") or item.get("start") or "?"
                end = item.get("end_time") or item.get("end") or "?"
                activity = item.get("activity") or "?"
                if "day_of_week" in item:
                    day_label = "every day" if item["day_of_week"] is None else f"weekday {item['day_of_week']}"
                    lines.append(f"- {start}-{end} {activity} ({day_label})")
                else:
                    lines.append(f"- {start}-{end} {activity}")
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
    def _format(blocks: list[dict[str, Any]], title: str = "Your routine:") -> str:
        lines = [title]
        for block in blocks:
            start = block.get("start") or "?"
            end = block.get("end") or "?"
            activity = block.get("activity") or "?"
            lines.append(f"{start}-{end} — {activity}")
        return "\n".join(lines)
