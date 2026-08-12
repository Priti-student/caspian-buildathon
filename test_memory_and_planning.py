"""SQLite-backed context and planning tests without external services."""

import unittest
from pathlib import Path

from planning_service import PlanningService
from storage import StudentPilotStore
from studentpilot_service import StudentPilotService


class FakeLLM:
    def __init__(self) -> None:
        self.histories = []

    def complete_json(self, prompt, text, history=None):
        self.histories.append(history or [])
        if "tasks and events" in prompt:
            return self._plan(text)
        return self._opportunity(text)

    def reply(self, text, history=None):
        self.histories.append(history or [])
        return "General response"

    @staticmethod
    def _opportunity(text):
        titles = ["Opportunity A", "Opportunity B", "Opportunity C"]
        if "Which one is best" in text:
            titles = ["Opportunity B"]
        if "second one" in text:
            titles = ["Opportunity B"]
        if "deadline" in text:
            titles = ["Opportunity B"]
        if "first and third" in text:
            titles = ["Opportunity A", "Opportunity C"]
        return {"is_opportunity": True, "requested_top_n": None, "opportunities": [
            {"rank": i + 1, "title": title, "organization": "Example Org", "category": "Internship",
             "role": "Intern", "eligibility": "Students", "required_skills": [], "location": "Remote",
             "deadline": "2026-08-30" if title == "Opportunity B" else "Not specified", "urgency": "High",
             "links": [], "summary": "Sample", "relevance_score": 8, "relevance_reason": "Sample data"}
            for i, title in enumerate(titles)
        ]}

    @staticmethod
    def _plan(text):
        lower = text.lower()
        if "complete" in lower:
            return {"action": "complete", "target": "ML project", "needs_clarification": False}
        if "delete" in lower:
            return {"action": "delete", "target": "Movie", "needs_clarification": False}
        if "change" in lower:
            return {"action": "update", "target": "Interview", "updates": {"start_time": "16:00"}, "needs_clarification": False}
        if "ml project" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "ML project", "item_type": "task", "deadline": "2026-08-14", "event_date": None, "start_time": None, "end_time": None, "priority": "high", "notes": None, "recurrence": None}]}
        if "interview" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "Interview", "item_type": "interview", "event_date": "2026-08-17", "start_time": "15:00", "end_time": None, "deadline": None, "priority": None, "notes": None, "recurrence": None}]}
        if "classes" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "Classes", "item_type": "class", "event_date": "2026-08-13", "start_time": "09:00", "end_time": "16:00", "deadline": None, "priority": None, "notes": None, "recurrence": None}]}
        if "movie" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "Movie with friends", "item_type": "personal event", "event_date": "2026-08-14", "start_time": "19:00", "end_time": None, "deadline": None, "priority": None, "notes": None, "recurrence": None}]}
        if "upcoming" in lower:
            return {"action": "list", "needs_clarification": False}
        return {"action": "none", "needs_clarification": False}


class MemoryAndPlanningTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(f".test_{self._testMethodName}.db")
        self.db_path.unlink(missing_ok=True)
        self.store = StudentPilotStore(self.db_path)
        self.llm = FakeLLM()
        self.app = StudentPilotService(self.llm, self.store, history_limit=16)

    def test_followup_context_and_isolation(self):
        conversation = "student-1"
        self.app.respond(conversation, "user-1", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        self.assertIn("Opportunity B", self.app.respond(conversation, "user-1", "Which one is best?"))
        self.assertIn("Opportunity B", self.app.respond(conversation, "user-1", "Tell me more about the second one."))
        self.assertIn("2026-08-30", self.app.respond(conversation, "user-1", "What is its deadline?"))
        self.assertIn("Opportunity C", self.app.respond(conversation, "user-1", "Compare the first and third ones."))
        self.app.respond("student-2", "user-2", "Hello")
        self.assertTrue(any("three fictional" in item["content"] for history in self.llm.histories for item in history))
        self.assertFalse(any("three fictional" in item["content"] for item in self.store.recent_messages("student-2")))

    def test_task_event_management(self):
        planner = PlanningService(self.llm, self.store)
        history = []
        planner.handle("c", "u", "My ML project is due Friday.", history)
        planner.handle("c", "u", "I have an interview next Monday at 3 PM.", history)
        planner.handle("c", "u", "Tomorrow I have classes from 9 AM to 4 PM.", history)
        planner.handle("c", "u", "Day after tomorrow I have a movie with my friends at 7 PM.", history)
        self.assertIn("ML project", planner.handle("c", "u", "Show upcoming tasks.", history))
        self.assertEqual("Updated.", planner.handle("c", "u", "Change my interview to 4 PM.", history))
        self.assertEqual("Marked as completed.", planner.handle("c", "u", "Mark my ML project complete.", history))
        self.assertEqual("Deleted.", planner.handle("c", "u", "Delete the movie.", history))
        self.assertIsNone(planner.handle("c", "u", "Tell me a joke.", history))


if __name__ == "__main__":
    unittest.main()
