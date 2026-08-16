"""SQLite-backed context, planning, and cross-channel identity tests without external services."""

import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import planning_service

from email_output_service import EmailOutputService
from identity_service import IdentityService
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
        lower = text.lower()
        if not any(word in lower for word in ("opportunit", "internship", "job", "scholarship", "hackathon", "analyze")):
            return {"is_opportunity": False, "requested_top_n": None, "opportunities": [], "message": "Not an opportunity."}
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
        self.assertIn("Opportunity A", self.app.respond(conversation, "user-1", "Which one is best?"))
        self.assertIn("Opportunity B", self.app.respond(conversation, "user-1", "Tell me more about the second one."))
        self.assertIn("2026-08-30", self.app.respond(conversation, "user-1", "What is its deadline?"))
        self.assertIn("Opportunity C", self.app.respond(conversation, "user-1", "Compare the first and third ones."))
        self.app.respond("student-2", "user-2", "Hello")
        # The conversation history is stored per-conversation; verify the
        # original opportunity submission is in student-1's history and is
        # NOT present in student-2's isolated conversation history.
        self.assertTrue(any("three fictional" in item["content"] for item in self.store.recent_messages(conversation)))
        self.assertFalse(any("three fictional" in item["content"] for item in self.store.recent_messages("student-2")))

    def test_opportunity_memory_is_persistent_and_separate_from_tasks(self):
        conversation = "student-3"
        self.app.respond(conversation, "user-3", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        self.assertIn("Opportunity B", self.app.respond(conversation, "user-3", "Tell me more about the second one."))
        self.assertIn("Opportunity C", self.app.respond(conversation, "user-3", "What about the third one?"))
        self.assertEqual(self.store.find_items("user-3"), [])

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
        friday_delta = (4 - today.weekday()) % 7
        self.assertEqual(PlanningService._relative_date("this Friday"), (today + timedelta(days=friday_delta)).isoformat())
        if today.weekday() == 6:
            self.assertEqual(PlanningService._relative_date("next Monday"), (today + timedelta(days=8)).isoformat())
        self.assertIsNone(PlanningService._relative_date("no date here"))

    def test_upcoming_friday_and_tomorrow_resolve_deterministically(self):
        # Freeze "today" to Thursday 2026-08-13 so expected dates are fixed.
        class FixedDate(date):
            @classmethod
            def today(cls):
                return date(2026, 8, 13)

        with mock.patch.object(planning_service, "date", FixedDate):
            self.assertEqual(PlanningService._relative_date("upcoming Friday"), "2026-08-14")
            self.assertEqual(PlanningService._relative_date("this Friday"), "2026-08-14")
            self.assertEqual(PlanningService._relative_date("next Friday"), "2026-08-21")
            self.assertEqual(PlanningService._relative_date("tomorrow"), "2026-08-14")

    def test_llm_cannot_introduce_unsupported_opportunity_records(self):
        # The FakeLLM returns opportunities for any text containing "opportunit",
        # but the source text carries no listing content, so nothing is persisted.
        self.app.respond("conv-hall", "user-hall",
                         "I have mailed you some opportunities rank these opportunities and tell me which is best")
        self.assertEqual(self.store.latest_opportunities("user-hall"), [])

    def test_forwarded_email_opportunities_stored_as_opportunities_not_tasks(self):
        email_text = ("Fwd: Internship opportunities. Apply now! "
                      "Machine Learning Internship at Amrata, salary 10k/month. "
                      "Data Analytics Internship at Amrata, salary 10k/month.")
        self.app.respond("conv-email-opp", "user-email-opp", email_text)
        self.assertEqual(len(self.store.latest_opportunities("user-email-opp")), 3)
        self.assertEqual(self.store.find_items("user-email-opp"), [])

    def test_cross_channel_opportunity_retrieval_after_reorder(self):
        telegram_user = self.store.resolve_user_id("telegram", "tg-reorder")
        self.store.link_identity(telegram_user, "email", "reorder@example.com")
        email_user = self.store.resolve_user_id("email", "reorder@example.com")
        self.assertEqual(telegram_user, email_user)
        self.app.respond("email-conv-reorder", email_user,
                         "Fwd: Internship opportunities. Apply now! Machine Learning Internship at Amrata.")
        self.assertIn("Opportunity A", self.app.respond("tg-conv-reorder", telegram_user, "Which one is best?"))

    def test_normal_task_message_still_works(self):
        self.app.respond("conv-task", "user-task", "My ML project is due Friday.")
        items = self.store.find_items("user-task")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_type"], "task")
        self.assertIsNotNone(items[0]["deadline"])

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
        canonical = self.store.resolve_user_id("telegram", "tg-persist-task")
        planner.handle("c", canonical, "My ML project is due Friday.", history)
        planner.handle("c", canonical, "I have a meeting tomorrow at 4 PM.", history)
        store2 = StudentPilotStore(self.db_path)
        planner2 = PlanningService(FakeLLM(), store2)
        items = store2.find_items(canonical)
        self.assertEqual(len(items), 2)
        ml = next(item for item in items if "ML project" in item["title"])
        meeting = next(item for item in items if "meeting" in item["title"].lower())
        self.assertEqual(ml["item_type"], "task")
        self.assertIsNotNone(ml["deadline"])
        self.assertEqual(meeting["item_type"], "meeting")
        self.assertIsNotNone(meeting["event_date"])
        self.assertIsNone(meeting["deadline"])
        self.assertIn("ML project", planner2.handle("c", canonical, "Show upcoming tasks.", history))

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
        results = self.store.find_items_on("u", tomorrow)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Class")

    def test_routine_generation_with_stored_commitments(self):
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
        self.store.create_item("conv-rem", "user-rem", {"title": "ML project", "item_type": "task", "deadline": "2026-08-14"})
        result = reminders.handle("conv-rem", "user-rem", "Remind me about my ML project every day until Friday.", [])
        self.assertIn("Reminder set", result)
        self.assertEqual(len(self.store.find_reminders("user-rem")), 1)
        listed = reminders.handle("conv-rem", "user-rem", "Show my reminders.", [])
        self.assertIn("ML project", listed)
        stopped = reminders.handle("conv-rem", "user-rem", "Stop reminding me about the ML project.", [])
        self.assertEqual(stopped, "Reminder stopped.")
        self.assertEqual(len(self.store.find_reminders("user-rem")), 0)

    def test_reminder_postpone(self):
        reminders = ReminderService(self.llm, self.store)
        self.store.create_item("conv-rem2", "user-rem2", {"title": "ML project", "item_type": "task", "deadline": "2026-08-14"})
        reminders.handle("conv-rem2", "user-rem2", "Remind me about my ML project.", [])
        postponed = reminders.handle("conv-rem2", "user-rem2", "Postpone my ML project reminder.", [])
        self.assertEqual(postponed, "Reminder postponed.")
        active = self.store.find_reminders("user-rem2")
        self.assertEqual(active[0]["remind_at"], "2026-08-20 09:00")

    def test_future_same_day_reminder_not_due(self):
        """A reminder scheduled LATER today must not be considered due.

        remind_at is stored as 'YYYY-MM-DD HH:MM' (space separator) while the
        dispatcher's now uses 'YYYY-MM-DDTHH:MM:SS' (T separator). Lexicographic
        comparison would treat the space as less than 'T' and fire every reminder
        immediately; due_reminders must compare as parsed datetimes instead.
        """
        # Reminder at 17:40 (space format) checked at 17:31 (T format) — future.
        self.store.create_reminder("conv-due", "user-due", None, "meeting with Chan", "2026-08-16 17:40")
        due = self.store.due_reminders(now="2026-08-16T17:31:39")
        self.assertEqual(len(due), 0)

    def test_future_reminder_due_after_time(self):
        """The same future reminder becomes due once its time arrives."""
        self.store.create_reminder("conv-due2", "user-due2", None, "meeting with Chan", "2026-08-16 17:40")
        due = self.store.due_reminders(now="2026-08-16T17:41:00")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["title"], "meeting with Chan")

    def test_reminder_postpone_creates_new_when_missing(self):
        """Postponing a reminder whose active version no longer exists (e.g. it
        already fired) must create a new reminder at the requested time instead of
        reporting 'I couldn't find an active reminder for that.'"""

        class PostponeCNN:
            def complete_json(self, prompt, text, history=None):
                return {"action": "postpone", "target": "CNN assignment submission",
                        "remind_at": "2026-08-16 17:28", "needs_clarification": False}

        reminders = ReminderService(PostponeCNN(), self.store)
        result = reminders.handle("conv-pc", "user-pc", "Remind me about CNN assignment submission at 5:28 pm today", [])
        self.assertIn("Reminder set", result)
        self.assertIn("17:28", result)
        active = self.store.find_reminders("user-pc")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["title"], "CNN assignment submission")
        self.assertEqual(active[0]["remind_at"], "2026-08-16 17:28")

    def test_reminder_persistence_across_restart(self):
        reminders = ReminderService(self.llm, self.store)
        self.store.create_item("conv-rem3", "user-rem3", {"title": "ML project", "item_type": "task", "deadline": "2026-08-14"})
        reminders.handle("conv-rem3", "user-rem3", "Remind me about my ML project.", [])
        store2 = StudentPilotStore(self.db_path)
        self.assertEqual(len(store2.find_reminders("user-rem3")), 1)

    def test_email_request_classification(self):
        self.assertEqual(EmailOutputService._classify("email me the opportunities"), ("opportunities", None))
        self.assertEqual(EmailOutputService._classify("send tomorrow's routine to my email"), ("routine", None))
        self.assertEqual(EmailOutputService._classify("email me my reminders"), ("reminders", None))
        self.assertEqual(EmailOutputService._classify("email me the opportunities to priya@example.com"), ("opportunities", "priya@example.com"))
        self.assertIsNone(EmailOutputService._classify("tell me a story")[0])

    def test_email_opportunities_uses_persisted_memory(self):
        self.app.respond("conv-email", "user-email", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        captured = {}
        email = EmailOutputService(self.store, sender=lambda recipient, subject, body: captured.update(
            {"recipient": recipient, "subject": subject, "body": body}) or "Email sent.")
        result = email.handle("conv-email", "user-email", "email me the opportunities to priya@example.com")
        self.assertEqual(result, "Email sent.")
        self.assertEqual(captured["recipient"], "priya@example.com")
        self.assertIn("Opportunity A", captured["body"])
        self.assertIn("Opportunity summary", captured["subject"])

    def test_email_routine_and_reminders_content(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.store.create_item("conv-em2", "user-em2", {"title": "College", "item_type": "class", "event_date": tomorrow, "start_time": "09:00", "end_time": "15:00"})
        self.store.create_reminder("conv-em2", "user-em2", None, "ML project", "2026-08-20 09:00")
        email = EmailOutputService(self.store, sender=lambda recipient, subject, body: "Email sent.")
        routine_result = email.handle("conv-em2", "user-em2", "send tomorrow's routine to my email")
        self.assertEqual(routine_result, "Email sent.")
        reminders_result = email.handle("conv-em2", "user-em2", "email me my reminders")
        self.assertEqual(reminders_result, "Email sent.")

    def test_email_without_sender_prepares_content(self):
        self.app.respond("conv-em3", "user-em3", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        email = EmailOutputService(self.store)
        result = email.handle("conv-em3", "user-em3", "email me the opportunities")
        self.assertIn("Email sending isn't configured yet", result)
        self.assertIn("Opportunity A", result)

    def test_email_non_request_is_not_intercepted(self):
        email = EmailOutputService(self.store, sender=lambda recipient, subject, body: "Email sent.")
        self.assertIsNone(email.handle("conv-em4", "user-em4", "What is the deadline for opportunity B?"))

    def test_full_pipeline_integration_and_multi_user_isolation(self):
        conv_a = "conv-a"
        user_a = "user-a"
        self.app.respond(conv_a, user_a, "My ML project is due Friday.")
        self.app.respond(conv_a, user_a, "I have a meeting tomorrow at 4 PM.")
        self.app.respond(conv_a, user_a, "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        self.assertIn("Opportunity A", self.app.respond(conv_a, user_a, "Which one is best?"))
        self.assertIn("Opportunity B", self.app.respond(conv_a, user_a, "Tell me more about the second one."))
        self.assertIn("2026-08-30", self.app.respond(conv_a, user_a, "What is its deadline?"))
        self.assertIn("Opportunity C", self.app.respond(conv_a, user_a, "Compare the first and third ones."))
        self.assertIn("Opportunity A", self.app.respond(conv_a, user_a, "Which one is best???"))
        self.assertIn("Reminder set", self.app.respond(conv_a, user_a, "Remind me about my ML project."))
        routine_reply = self.app.respond(conv_a, user_a, "Plan my tomorrow.")
        self.assertIn("routine", routine_reply.lower())
        email_reply = self.app.respond(conv_a, user_a, "email me the opportunities")
        self.assertIn("Opportunity A", email_reply)

        conv_b = "conv-b"
        user_b = "user-b"
        self.app.respond(conv_b, user_b, "Hello")
        self.assertEqual(self.store.latest_opportunities(user_b), [])
        self.assertEqual(self.store.find_items(user_b), [])
        self.assertEqual(self.store.find_reminders(user_b), [])
        self.assertEqual(self.app.respond(conv_b, user_b, "Which one is best?"), "General response")
        self.assertEqual(len(self.store.latest_opportunities(user_a)), 3)
        self.assertEqual(len(self.store.find_items(user_a)), 2)
        self.assertEqual(len(self.store.find_reminders(user_a)), 1)

    # ── Cross-channel identity tests ─────────────────────────────────────────

    def test_identity_resolution_and_linking(self):
        telegram_user = self.store.resolve_user_id("telegram", "123456789")
        # Link the email to the telegram user's canonical id BEFORE resolving it.
        self.store.link_identity(telegram_user, "email", "user@example.com")
        self.assertEqual(self.store.resolve_user_id("email", "user@example.com"), telegram_user)
        self.assertTrue(self.store.email_linked_to_user(telegram_user, "user@example.com"))

    def test_otp_flow(self):
        user = self.store.resolve_user_id("telegram", "tg-otp")
        code = self.store.create_otp(user, "otp@example.com")
        self.assertEqual(len(code), 6)
        # Wrong code rejected.
        ok, msg = self.store.verify_otp(user, "otp@example.com", "000000")
        self.assertFalse(ok)
        # Correct code links the email.
        ok, msg = self.store.verify_otp(user, "otp@example.com", code)
        self.assertTrue(ok)
        self.assertTrue(self.store.email_linked_to_user(user, "otp@example.com"))
        # Reuse rejected.
        ok, msg = self.store.verify_otp(user, "otp@example.com", code)
        self.assertFalse(ok)

    def test_otp_expired(self):
        user = self.store.resolve_user_id("telegram", "tg-exp")
        code = self.store.create_otp(user, "exp@example.com", ttl_seconds=1)
        import time
        time.sleep(1.1)
        ok, msg = self.store.verify_otp(user, "exp@example.com", code)
        self.assertFalse(ok)
        self.assertIn("expired", msg.lower())

    def test_multiple_email_linking_and_unlinking(self):
        user = self.store.resolve_user_id("telegram", "tg-multi")
        code1 = self.store.create_otp(user, "personal@example.com")
        self.store.verify_otp(user, "personal@example.com", code1)
        code2 = self.store.create_otp(user, "college@example.com")
        self.store.verify_otp(user, "college@example.com", code2)
        self.assertTrue(self.store.email_linked_to_user(user, "personal@example.com"))
        self.assertTrue(self.store.email_linked_to_user(user, "college@example.com"))
        # Unlink one; data preserved.
        self.store.unlink_identity("email", "college@example.com")
        self.assertFalse(self.store.email_linked_to_user(user, "college@example.com"))
        self.assertTrue(self.store.email_linked_to_user(user, "personal@example.com"))

    def test_email_opportunity_visible_from_telegram(self):
        # Same canonical user via linked identities.
        telegram_user = self.store.resolve_user_id("telegram", "tg-shared")
        self.store.link_identity(telegram_user, "email", "shared@example.com")
        email_user = self.store.resolve_user_id("email", "shared@example.com")
        self.assertEqual(telegram_user, email_user)
        # Opportunity arrives via email conversation.
        self.app.respond("email-conv", email_user, "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        # Ask from a Telegram conversation (different conversation_id, same user).
        self.assertIn("Opportunity A", self.app.respond("tg-conv", telegram_user, "Which one is best?"))

    def test_telegram_opportunity_visible_from_email(self):
        telegram_user = self.store.resolve_user_id("telegram", "tg-shared2")
        self.store.link_identity(telegram_user, "email", "shared2@example.com")
        email_user = self.store.resolve_user_id("email", "shared2@example.com")
        self.app.respond("tg-conv2", telegram_user, "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        self.assertIn("Opportunity A", self.app.respond("email-conv2", email_user, "Which one is best?"))

    def test_tasks_reminders_shared_across_channels(self):
        telegram_user = self.store.resolve_user_id("telegram", "tg-shared3")
        self.store.link_identity(telegram_user, "email", "shared3@example.com")
        email_user = self.store.resolve_user_id("email", "shared3@example.com")
        # Task created via email.
        self.app.respond("email-conv3", email_user, "My ML project is due Friday.")
        # Visible from Telegram.
        self.assertIn("ML project", self.app.respond("tg-conv3", telegram_user, "Show upcoming tasks."))
        # Reminder created via Telegram.
        self.app.respond("tg-conv3", telegram_user, "Remind me about my ML project.")
        self.assertEqual(len(self.store.find_reminders(email_user)), 1)

    def test_conversation_histories_remain_isolated(self):
        telegram_user = self.store.resolve_user_id("telegram", "tg-hist")
        self.store.link_identity(telegram_user, "email", "hist@example.com")
        email_user = self.store.resolve_user_id("email", "hist@example.com")
        self.app.respond("tg-hist-conv", telegram_user, "Hello from telegram")
        self.app.respond("email-hist-conv", email_user, "Hello from email")
        tg_history = self.store.recent_messages("tg-hist-conv")
        email_history = self.store.recent_messages("email-hist-conv")
        self.assertTrue(any("telegram" in item["content"] for item in tg_history))
        self.assertTrue(any("email" in item["content"] for item in email_history))
        self.assertFalse(any("email" in item["content"] for item in tg_history))
        self.assertFalse(any("telegram" in item["content"] for item in email_history))

    def test_different_users_cannot_see_each_other_data(self):
        user_a = self.store.resolve_user_id("telegram", "tg-a")
        user_b = self.store.resolve_user_id("telegram", "tg-b")
        self.app.respond("conv-a", user_a, "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        self.app.respond("conv-a", user_a, "My ML project is due Friday.")
        self.assertEqual(self.store.latest_opportunities(user_b), [])
        self.assertEqual(self.store.find_items(user_b), [])
        self.assertEqual(self.store.find_reminders(user_b), [])

    def test_identity_persistence_across_restart(self):
        user = self.store.resolve_user_id("telegram", "tg-persist")
        self.store.link_identity(user, "email", "persist@example.com")
        store2 = StudentPilotStore(self.db_path)
        self.assertEqual(store2.resolve_user_id("email", "persist@example.com"), user)

    # ── Regression tests for reported issues ────────────────────────────────

    def test_mailed_opportunities_reference_returns_details_not_tasks(self):
        """When the user says 'I have mailed you some opportunities', the bot
        must return the stored opportunity details with links instead of
        listing old tasks/events."""
        # Seed an opportunity via email.
        self.app.respond("conv-mail-opp", "user-mail-opp",
                         "Here are three fictional internship opportunities: A, B, C. Analyze them.")
        # Seed an unrelated pending task.
        self.app.respond("conv-mail-opp", "user-mail-opp", "My ML project is due Friday.")
        # User references the mailed opportunities from Telegram.
        reply = self.app.respond("conv-mail-opp", "user-mail-opp",
                                 "I have mailed you some opportunities")
        # Must include opportunity details with links, not the ML project task.
        self.assertIn("Opportunity A", reply)
        self.assertIn("Links:", reply)
        self.assertNotIn("ML project", reply)
        # The task must still be pending (not accidentally completed).
        pending = self.store.find_items("user-mail-opp")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["title"], "ML project")

    def test_complete_without_target_completes_all_pending(self):
        """Saying 'I have completed my work' without naming a task should
        complete all pending items instead of asking which task."""
        planner = PlanningService(self.llm, self.store)
        history = []
        planner.handle("c", "u-complete-all", "My ML project is due Friday.", history)
        planner.handle("c", "u-complete-all", "I have a meeting tomorrow at 4 PM.", history)
        # FakeLLM returns target "ML project" for "complete" text, so use a
        # direct call to verify the no-target path via a custom LLM.
        class NoTargetLLM:
            def complete_json(self, prompt, text, history=None):
                return {"action": "complete", "target": None, "needs_clarification": False}
        planner_no_target = PlanningService(NoTargetLLM(), self.store)
        result = planner_no_target.handle("c", "u-complete-all", "I have completed my work", history)
        self.assertIn("Marked as completed:", result)
        self.assertIn("ML project", result)
        self.assertIn("Meeting", result)
        # All items should now be completed (not in pending).
        self.assertEqual(self.store.find_items("u-complete-all"), [])


if __name__ == "__main__":
    unittest.main()
