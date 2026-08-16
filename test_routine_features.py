"""Tests for daily routine preferences, defaults, and dispatcher."""

import unittest
from datetime import date, timedelta
from pathlib import Path

from routine_dispatcher import RoutineDispatcher
from routine_service import RoutineService
from storage import StudentPilotStore


class FakeLLM:
    def __init__(self):
        self.histories = []

    def complete_json(self, prompt, text, history=None):
        self.histories.append(history or [])
        if "daily routine defaults" in prompt:
            return self._defaults(text)
        if "one-day schedule" in prompt:
            return self._routine(text)
        return {"action": "none", "needs_clarification": False}

    def reply(self, text, history=None):
        self.histories.append(history or [])
        return "General response"

    @staticmethod
    def _defaults(text):
        lower = text.lower()
        if "college" in lower:
            return {
                "has_defaults": True,
                "blocks": [
                    {"start": "09:00", "end": "17:00", "activity": "College",
                     "days": ["monday", "tuesday", "wednesday", "thursday", "friday"]},
                ],
                "clarification": None,
            }
        if "gym" in lower:
            return {
                "has_defaults": True,
                "blocks": [
                    {"start": "05:00", "end": "06:00", "activity": "Gym", "days": []},
                ],
                "clarification": None,
            }
        return {"has_defaults": False, "blocks": [], "clarification": "Tell me your daily routine."}

    @staticmethod
    def _routine(text):
        return {
            "blocks": [
                {"start": "09:00", "end": "17:00", "activity": "College"},
                {"start": "18:00", "end": "19:00", "activity": "Meeting"},
            ],
            "needs_more": False,
        }


class RoutineFeatureTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(f".test_routine_{self._testMethodName}.db")
        self.db_path.unlink(missing_ok=True)
        self.store = StudentPilotStore(self.db_path)
        self.llm = FakeLLM()
        self.routine = RoutineService(self.llm, self.store)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_preferred_time_setup(self):
        """User sets preferred routine delivery time."""
        result = self.routine.handle("conv", "user1", "send me my daily routine at 4am", [])
        self.assertIn("04:00", result)
        pref = self.store.get_routine_preference("user1")
        self.assertEqual(pref["preferred_time"], "04:00")
        self.assertEqual(pref["asked_for_preference"], 1)

    def test_preferred_time_change(self):
        """User changes preferred routine delivery time."""
        self.routine.handle("conv", "user1", "send me my daily routine at 4am", [])
        result = self.routine.handle("conv", "user1", "send my routine at 11 pm", [])
        self.assertIn("23:00", result)
        pref = self.store.get_routine_preference("user1")
        self.assertEqual(pref["preferred_time"], "23:00")

    def test_default_routine_setup(self):
        """User sets daily default routine."""
        result = self.routine.handle("conv", "user2",
                                     "I go to college every day from 9 am to 5 pm except Saturday and Sunday", [])
        self.assertIn("College", result)
        defaults = self.store.find_routine_defaults("user2")
        self.assertEqual(len(defaults), 5)  # Mon-Fri
        self.assertEqual(defaults[0]["activity"], "College")
        self.assertEqual(defaults[0]["start_time"], "09:00")

    def test_default_routine_every_day(self):
        """User sets a daily default that applies every day."""
        result = self.routine.handle("conv", "user3", "I go to gym daily from 5 am to 6 am", [])
        self.assertIn("Gym", result)
        defaults = self.store.find_routine_defaults("user3")
        self.assertEqual(len(defaults), 1)
        self.assertIsNone(defaults[0]["day_of_week"])  # every day

    def test_routine_generation_includes_defaults_and_commitments(self):
        """Routine generation includes both defaults and day-specific commitments."""
        # Set up defaults
        self.routine.handle("conv", "user4", "I go to college every day from 9 am to 5 pm except Saturday and Sunday", [])
        # Add a meeting for tomorrow
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.store.create_item("conv", "user4", {
            "title": "Meeting with Chan", "item_type": "meeting",
            "event_date": tomorrow, "start_time": "18:00", "end_time": "19:00",
        })
        # Generate tomorrow's routine
        result = self.routine.handle("conv", "user4", "plan my tomorrow", [])
        self.assertIn("College", result)
        self.assertIn("Meeting", result)

    def test_add_to_today_routine(self):
        """User adds an event to today's routine."""
        today = date.today().isoformat()
        self.store.create_item("conv", "user5", {
            "title": "Meeting at 6pm", "item_type": "meeting",
            "event_date": today, "start_time": "18:00", "end_time": "19:00",
        })
        result = self.routine.handle("conv", "user5", "I have a meeting at 6 pm today add it to my today's routine", [])
        self.assertIn("Updated routine", result)
        self.assertIn("Meeting", result)
        # Verify persisted
        blocks = self.store.get_daily_routine("user5", today)
        self.assertIsNotNone(blocks)
        self.assertTrue(any("Meeting" in b.get("activity", "") for b in blocks))

    def test_routine_dispatcher_sends_at_preferred_time(self):
        """Routine dispatcher sends routine when preferred time is reached."""
        # Set up user with preferred time in the past
        self.store.set_routine_preferred_time("user6", "00:00", "conv6")
        # Add a commitment for today
        today = date.today().isoformat()
        self.store.create_item("conv6", "user6", {
            "title": "Meeting today", "item_type": "meeting",
            "event_date": today, "start_time": "15:00", "end_time": "16:00",
        })
        sent = []
        dispatcher = RoutineDispatcher(
            self.store,
            routine_service=self.routine,
            telegram_sender=lambda conv_id, msg: sent.append((conv_id, msg)) or True,
        )
        delivered = dispatcher.dispatch_due()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "conv6")
        self.assertIn("routine", sent[0][1].lower())

    def test_routine_dispatcher_does_not_resend_same_day(self):
        """Routine dispatcher only sends once per day per user."""
        self.store.set_routine_preferred_time("user7", "00:00", "conv7")
        sent = []
        dispatcher = RoutineDispatcher(
            self.store,
            routine_service=self.routine,
            telegram_sender=lambda conv_id, msg: sent.append((conv_id, msg)) or True,
        )
        dispatcher.dispatch_due()
        dispatcher.dispatch_due()
        self.assertEqual(len(sent), 1)  # Only sent once

    def test_routine_dispatcher_waits_for_preferred_time(self):
        """Routine dispatcher does not send before preferred time."""
        # Preferred time is in the future (23:59)
        self.store.set_routine_preferred_time("user8", "23:59", "conv8")
        sent = []
        dispatcher = RoutineDispatcher(
            self.store,
            routine_service=self.routine,
            telegram_sender=lambda conv_id, msg: sent.append((conv_id, msg)) or True,
        )
        delivered = dispatcher.dispatch_due()
        self.assertEqual(len(delivered), 0)
        self.assertEqual(len(sent), 0)


if __name__ == "__main__":
    unittest.main()
