"""Structured, conversation-scoped opportunity follow-up handling."""

import re

from storage import StudentPilotStore


class OpportunityMemoryService:
    def __init__(self, store: StudentPilotStore) -> None:
        self._store = store

    def handle_followup(self, user_id: str, text: str) -> str | None:
        items = self._store.latest_opportunities(user_id)
        lower = text.lower()
        if not items or not self._is_followup(lower):
            return None
        if "compare" in lower:
            positions = self._positions(lower, items)
            if len(positions) < 2:
                return "Which two opportunities would you like to compare?"
            chosen = [items[position - 1] for position in positions[:2]]
            return self._comparison(chosen)
        if "best" in lower or "top" in lower:
            limit = self._top_limit(lower)
            chosen = sorted(items, key=lambda item: (item["rank"], -item["relevance_score"]))[:limit]
            return self._list(chosen)
        position = self._positions(lower, items)
        selected = position[0] if position else self._store.selected_opportunity_position(user_id)
        if selected is None:
            selected = 1
        selected = min(max(selected, 1), len(items))
        self._store.select_opportunity(user_id, selected)
        item = items[selected - 1]
        if "deadline" in lower or "due" in lower:
            return f"{item['title']} deadline: {item['deadline']}."
        return self._details(item)

    @staticmethod
    def _is_followup(text: str) -> bool:
        markers = ("best", "top", "compare", "first", "second", "third", "fourth", "fifth",
                   "previous opportunity", "this opportunity", "that opportunity", "this one", "that one",
                   "its deadline", "more about", "what about")
        if not any(marker in text for marker in markers):
            return False
        # A new opportunity submission should be analyzed, not treated as a follow-up.
        submission_markers = ("internship", "job", "hackathon", "scholarship", "competition",
                              "workshop", "apply", "hiring", "opportunity:", "opportunities:")
        return not any(marker in text for marker in submission_markers)

    @staticmethod
    def _positions(text: str, items: list[dict]) -> list[int]:
        names = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
        positions = [position for name, position in names.items() if re.search(rf"\b{name}\b", text)]
        positions += [int(number) for number in re.findall(r"\b(?:opportunity\s*)?(\d+)\b", text)]
        if "previous opportunity" in text:
            positions.append(len(items))
        return [position for position in positions if 1 <= position <= len(items)]

    @staticmethod
    def _top_limit(text: str) -> int:
        match = re.search(r"\b(?:top|best)\s+(\d+)\b", text)
        return int(match.group(1)) if match else 1

    @staticmethod
    def _details(item: dict) -> str:
        return (
            f"{item['position']}. {item['title']} — {item['category']}\n"
            f"Organization: {item['organization']}\nRole: {item['role']}\n"
            f"Eligibility: {item['eligibility']}\nSkills: {item['skills']}\n"
            f"Location: {item['location']}\nDeadline: {item['deadline']}\n"
            f"Relevance: {item['relevance_score']}/10 — {item['relevance_reason']}\n"
            f"Summary: {item['summary']}\nLinks: {item['links']}"
        )

    @staticmethod
    def _list(items: list[dict]) -> str:
        return "Top opportunities:\n" + "\n".join(
            f"{item['position']}. {item['title']} — relevance {item['relevance_score']}/10; deadline: {item['deadline']}"
            for item in items
        )

    @staticmethod
    def _comparison(items: list[dict]) -> str:
        return "Comparison:\n" + "\n".join(
            f"{item['position']}. {item['title']}: relevance {item['relevance_score']}/10, "
            f"deadline {item['deadline']}, skills {item['skills']}."
            for item in items
        )
