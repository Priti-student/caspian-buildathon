"""SQLite-backed context and planning tests without external services."""

import unittest
from datetime import date, timedelta
from pathlib import Path

from opportunity_memory_service import OpportunityMemoryService
from planning_service import PlanningService
from reminder_service import ReminderService
from routine_service import RoutineService
from storage import StudentPilotStore
from studentpilot_service import StudentPilotService
from text_utils import normalize_message


class FakeLLM:
    def __init__(self) -> None:
        self.histories = []

    def complete_json(self, prompt, text, history=None):
        self.histories.append(history or [])
        if "tasks and events" in prompt:
            return self._plan(text)
        if "one-day schedule" in prompt:
            return self._routine(text)
        if "reminders" in prompt:
            return self._reminder(text)
        return self._opportunity(text)

    def reply(self, text, history=None):
        self.histories.append(history or [])
        return "General response"

    @staticmethod
    def _opportunity(text):
        titles = ["Opportunity A", "Opportunity B", "Opportunity C"]
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
        if "meeting" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "Meeting", "item_type": "meeting", "event_date": "2026-08-13", "start_time": "16:00", "end_time": None, "deadline": None, "priority": None, "notes": None, "recurrence": None}]}
        if "classes" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "Classes", "item_type": "class", "event_date": "2026-08-13", "start_time": "09:00", "end_time": "16:00", "deadline": None, "priority": None, "notes": None, "recurrence": None}]}
        if "movie" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "Movie with friends", "item_type": "personal event", "event_date": "2026-08-14", "start_time": "19:00", "end_time": None, "deadline": None, "priority": None, "notes": None, "recurrence": None}]}
        if "upcoming" in lower:
            return {"action": "list", "needs_clarification": False}
        return {"action": "none", "needs_clarification": False}

    @staticmethod
    def _routine(text):
        return {"blocks": [
            {"start": "09:00", "end": "15:00", "activity": "College"},
            {"start": "15:30", "end": "17:30", "activity": "ML project"},
            {"start": "18:00", "end": "19:00", "activity": "Meeting"},
        ], "needs_more": False}

    @staticmethod
    def _reminder(text):
        lower = text.lower()
        if "stop" in lower:
            return {"action": "stop", "target": "ML project", "needs_clarification": False}
        if "postpone" in lower:
            return {"action": "postpone", "target": "ML project", "remind_at": "2026-08-20 09:00", "needs_clarification": False}
        if "show" in lower or "what reminders" in lower:
            return {"action": "list", "needs_clarification": False}
        if "remind" in lower:
            return {"action": "add", "target": "ML project", "remind_at": "2026-08-15 09:00", "recurrence": "daily", "needs_clarification": False}
        return {"action": "none", "needs_clarification": False}


class MemoryAndPlanningTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(f".test_{self._testMethodName}.db")
        self.db_path.unlink(missing_ok=True)
        self.store = StudentPilotStore(self.db_path)
        self.llm = FakeLLM()
        self.app = StudentPilotService(self.llm, self.store, history_limit=16)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_followup_context_and_isolation(self):
        conversation = "student-1"
        self.app.respond(conversation, "user-1", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        # Best-ranked opportunity is rank 1 (Opportunity A).
        self.assertIn("Opportunity A", self.app.respond(conversation, "user-1", "Which one is best?"))
        self.assertIn("Opportunity B", self.app.respond(conversation, "user-1", "Tell me more about the second one."))
        self.assertIn("2026-08-30", self.app.respond(conversation, "user-1", "What is its deadline?"))
        self.assertIn("Opportunity C", self.app.respond(conversation, "user-1", "Compare the first and third ones."))
        self.app.respond("student-2", "user-2", "Hello")
        self.assertTrue(any("three fictional" in item["content"] for history in self.llm.histories for item in history))
        self.assertFalse(any("three fictional" in item["content"] for item in self.store.recent_messages("student-2")))

    def test_opportunity_memory_is_persistent_and_separate_from_tasks(self):
        conversation = "student-3"
        self.app.respond(conversation, "user-3", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        # Opportunities are stored persistently and retrievable via ordinal follow-ups.
        self.assertIn("Opportunity B", self.app.respond(conversation, "user-3", "Tell me more about the second one."))
        self.assertIn("Opportunity C", self.app.respond(conversation, "user-3", "What about the third one?"))
        # Task/event memory is separate: no planner items were created by opportunity messages.
        self.assertEqual(self.store.find_items(conversation), [])

    def test_repeated_punctuation_does_not_break_intent_detection(self):
        conversation = "student-4"
        self.app.respond(conversation, "user-4", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        self.assertIn("Opportunity A", self.app.respond(conversation, "user-4", "Which one is best???"))
        self.assertIn("Opportunity B", self.app.respond(conversation, "user-4", "Tell me more about the second one!!!"))
        self.assertIn("2026-08-30", self.app.respond(conversation, "user-4", "What is its deadline??"))

    def test_normalize_message(self):
        self.assertEqual(normalize_message("Which one is best???"), "Which one is best?")
        self.assertEqual(normalize_message("Tell me more!!!  about it"), "Tell me more! about it")
        self.assertEqual(normalize_message("Compare  the   first and third ones"), "Compare the first and third ones")

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

    def test_relative_date_resolution(self):
        today = date.today()
        self.assertEqual(PlanningService._relative_date("tomorrow"), (today + timedelta(days=1)).isoformat())
        self.assertEqual(PlanningService._relative_date("day after tomorrow"), (today + timedelta(days=2)).isoformat())
        self.assertEqual(PlanningService._relative_date("in three days"), (today + timedelta(days=3)).isoformat())
        self.assertEqual(PlanningService._relative_date("in 2 weeks"), (today + timedelta(days=14)).isoformat())
        self.assertEqual(PlanningService._relative_date("in a week"), (today + timedelta(days=7)).isoformat())
        self.assertEqual(PlanningService._relative_date("today"), today.isoformat())
        self.assertEqual(PlanningService._relative_date("next week"), (today + timedelta(days=7)).isoformat())
        # "this Friday" resolves to the upcoming Friday (or today if today is Friday).
        friday_delta = (4 - today.weekday()) % 7
        self.assertEqual(PlanningService._relative_date("this Friday"), (today + timedelta(days=friday_delta)).isoformat())
        # "next Monday" said on Sunday should be 8 days out, not 1.
        if today.weekday() == 6:  # Sunday
            self.assertEqual(PlanningService._relative_date("next Monday"), (today + timedelta(days=8)).isoformat())
        self.assertIsNone(PlanningService._relative_date("no date here"))

    def test_deadline_vs_event_distinction(self):
        self.assertTrue(PlanningService._is_deadline("My ML project is due Friday."))
        self.assertTrue(PlanningService._is_deadline("I need to submit my application by Sunday."))
        self.assertTrue(PlanningService._is_deadline("Complete my project before August 20."))
        self.assertTrue(PlanningService._is_deadline("I have to finish the report by tomorrow."))
        self.assertFalse(PlanningService._is_deadline("I have a meeting tomorrow at 4 PM."))
        self.assertFalse(PlanningService._is_deadline("Day after tomorrow I have a movie with my friends at 7 PM."))

    def test_task_event_persistence_across_restart(self):
        planner = PlanningService(self.llm, self.store)
        history = []
        planner.handle("c", "u", "My ML project is due Friday.", history)
        planner.handle("c", "u", "I have a meeting tomorrow at 4 PM.", history)
        # Simulate an application restart: new store/service instances on the same DB file.
        store2 = StudentPilotStore(self.db_path)
        planner2 = PlanningService(FakeLLM(), store2)
        items = store2.find_items("c")
        self.assertEqual(len(items), 2)
        ml = next(item for item in items if "ML project" in item["title"])
        meeting = next(item for item in items if "meeting" in item["title"].lower())
        self.assertEqual(ml["item_type"], "task")
        self.assertIsNotNone(ml["deadline"])
        self.assertEqual(meeting["item_type"], "meeting")
        self.assertIsNotNone(meeting["event_date"])
        self.assertIsNone(meeting["deadline"])
        self.assertIn("ML project", planner2.handle("c", "u", "Show upcoming tasks.", history))

    def test_routine_request_detection(self):
        routine_markers = [
            "Plan my tomorrow",
            "Create my routine for tomorrow",
            "What do I have tomorrow?",
            "What should I focus on tomorrow?",
            "Show my schedule for tomorrow",
            "Plan tomorrow",
        ]
        for marker in routine_markers:
            self.assertTrue(RoutineService._is_routine_request(marker), marker)
        self.assertFalse(RoutineService._is_routine_request("Tell me a joke"))
        self.assertFalse(RoutineService._is_routine_request("What is the deadline?"))

    def test_find_items_on_returns_day_commitments(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.store.create_item("c", "u", {"title": "Class", "item_type": "class", "event_date": tomorrow, "start_time": "09:00", "end_time": "12:00"})
        self.store.create_item("c", "u", {"title": "Other day", "item_type": "task", "event_date": (date.today() + timedelta(days=3)).isoformat()})
        results = self.store.find_items_on("c", tomorrow)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Class")

    def test_routine_generation_with_stored_commitments(self):
        # Store a real commitment for tomorrow, then generate a routine.
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.store.create_item("conv-r", "user-r", {"title": "College", "item_type": "class", "event_date": tomorrow, "start_time": "09:00", "end_time": "15:00"})
        routine = RoutineService(self.llm, self.store)
        result = routine.handle("conv-r", "user-r", "Plan my tomorrow: I have college from 9 to 3, and I need to work on my ML project. I also have a meeting at 6 PM.", [])
        self.assertIn("routine", result.lower())
        self.assertIn("College", result)

    def test_reminder_request_detection(self):
        reminder_markers = [
            "Remind me about my ML project every day until Friday.",
            "Remind me tomorrow at 9 AM.",
            "Stop reminding me about the ML project.",
            "Show my reminders.",
            "What reminders do I have?",
            "Postpone my ML project reminder.",
        ]
        for marker in reminder_markers:
            self.assertTrue(ReminderService._is_reminder_request(marker), marker)
        self.assertFalse(ReminderService._is_reminder_request("Tell me a joke"))

    def test_reminder_add_list_stop(self):
        reminders = ReminderService(self.llm, self.store)
        # Add a reminder for a stored task.
        self.store.create_item("conv-rem", "user-rem", {"title": "ML project", "item_type": "task", "deadline": "2026-08-14"})
        result = reminders.handle("conv-rem", "user-rem", "Remind me about my ML project every day until Friday.", [])
        self.assertIn("Reminder set", result)
        self.assertEqual(len(self.store.find_reminders("conv-rem")), 1)
        # List reminders.
        listed = reminders.handle("conv-rem", "user-rem", "Show my reminders.", [])
        self.assertIn("ML project", listed)
        # Stop the reminder.
        stopped = reminders.handle("conv-rem", "user-rem", "Stop reminding me about the ML project.", [])
        self.assertEqual(stopped, "Reminder stopped.")
        self.assertEqual(len(self.store.find_reminders("conv-rem")), 0)

    def test_reminder_postpone(self):
        reminders = ReminderService(self.llm, self.store)
        self.store.create_item("conv-rem2", "user-rem2", {"title": "ML project", "item_type": "task", "deadline": "2026-08-14"})
        reminders.handle("conv-rem2", "user-rem2", "Remind me about my ML project.", [])
        postponed = reminders.handle("conv-rem2", "user-rem2", "Postpone my ML project reminder.", [])
        self.assertEqual(postponed, "Reminder postponed.")
        active = self.store.find_reminders("conv-rem2")
        self.assertEqual(active[0]["remind_at"], "2026-08-20 09:00")

    def test_reminder_persistence_across_restart(self):
        reminders = ReminderService(self.llm, self.store)
        self.store.create_item("conv-rem3", "user-rem3", {"title": "ML project", "item_type": "task", "deadline": "2026-08-14"})
        reminders.handle("conv-rem3", "user-rem3", "Remind me about my ML project.", [])
        # Simulate restart: new store on same DB file.
        store2 = StudentPilotStore(self.db_path)
        self.assertEqual(len(store2.find_reminders("conv-rem3")), 1)


if __name__ == "__main__":
    unittest.main()
