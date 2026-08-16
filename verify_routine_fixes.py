"""Standalone verification for the 4 routine fixes. Run: python verify_routine_fixes.py"""
import sys
from datetime import date, timedelta
from pathlib import Path

from routine_service import RoutineService
from planning_service import PlanningService
from storage import StudentPilotStore


class FakeLLM:
    """Simulates the LLM. Returns invented activities (breakfast, lunch, etc.)
    to test the filtering logic, and handles routine generation."""
    def __init__(self):
        self.histories = []

    def complete_json(self, prompt, text, history=None):
        self.histories.append(history or [])
        if "daily routine defaults" in prompt:
            return {
                "has_defaults": True,
                "blocks": [
                    {"start": "09:00", "end": "16:00", "activity": "Go to college",
                     "days": ["monday", "tuesday", "wednesday", "thursday", "friday"]},
                ],
                "clarification": None,
            }
        if "one-day schedule" in prompt:
            # Simulate the LLM inventing breakfast/lunch/dinner even though
            # the user never asked for them.
            blocks = [
                {"start": "08:00", "end": "09:00", "activity": "Morning Routine"},
                {"start": "09:00", "end": "10:00", "activity": "Breakfast"},
                {"start": "09:00", "end": "16:00", "activity": "Go to college"},
                {"start": "12:00", "end": "13:00", "activity": "Lunch"},
                {"start": "16:00", "end": "17:00", "activity": "Meeting with Chan"},
                {"start": "18:00", "end": "20:00", "activity": "Dinner"},
                {"start": "20:00", "end": "22:00", "activity": "Relaxation/Entertainment"},
            ]
            # Include ML project in the routine if it's a commitment for this day.
            if "ML project" in text:
                blocks.append({"start": "10:00", "end": "12:00", "activity": "Work on ML Project"})
            return {"blocks": blocks, "needs_more": False}
        if "tasks and events" in prompt:
            lower = text.lower()
            if "complete" in lower or "submitted" in lower:
                return {"action": "complete", "target": "ML project", "needs_clarification": False}
            if "ml project" in lower:
                return {"action": "add", "needs_clarification": False, "items": [
                    {"title": "ML project", "item_type": "task", "deadline": "2026-08-15",
                     "event_date": None, "start_time": None, "end_time": None,
                     "priority": "high", "notes": None, "recurrence": None}
                ]}
            if "meeting" in lower:
                return {"action": "add", "needs_clarification": False, "items": [
                    {"title": "Meeting with Chan", "item_type": "meeting", "event_date": "2026-08-17",
                     "start_time": "16:00", "end_time": "17:00", "deadline": None,
                     "priority": None, "notes": None, "recurrence": None}
                ]}
            return {"action": "none", "needs_clarification": False}
        return {"action": "none", "needs_clarification": False}

    def reply(self, text, history=None):
        self.histories.append(history or [])
        return "General response"


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def main():
    results = []
    db = Path(".verify_routine_fixes.db")
    db.unlink(missing_ok=True)
    store = StudentPilotStore(db)
    llm = FakeLLM()
    routine = RoutineService(llm, store)
    planner = PlanningService(llm, store)

    # ── FIX 1: Invented activities (breakfast, lunch, dinner, etc.) filtered out ──
    # Set up daily defaults (college Mon-Fri)
    routine.handle("c1", "u1", "I go to college from 9 am to 4 pm except Sunday", [])
    # Add a meeting for tomorrow
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    store.create_item("c1", "u1", {
        "title": "Meeting with Chan", "item_type": "meeting",
        "event_date": tomorrow, "start_time": "16:00", "end_time": "17:00",
    })
    # Generate tomorrow's routine
    result = routine.handle("c1", "u1", "Send me my tomorrow's routine", [])
    print(f"\n--- FIX 1: Tomorrow's routine ---\n{result}\n")
    # The LLM returned invented activities, but they should be filtered out.
    results.append(check("FIX1: no Breakfast", "Breakfast" not in result, f"got: {result}"))
    results.append(check("FIX1: no Lunch", "Lunch" not in result, f"got: {result}"))
    results.append(check("FIX1: no Dinner", "Dinner" not in result, f"got: {result}"))
    results.append(check("FIX1: no Morning Routine", "Morning Routine" not in result, f"got: {result}"))
    results.append(check("FIX1: no Relaxation", "Relaxation" not in result, f"got: {result}"))
    results.append(check("FIX1: has Go to college", "Go to college" in result, f"got: {result}"))
    results.append(check("FIX1: has Meeting with Chan", "Meeting with Chan" in result, f"got: {result}"))

    # ── FIX 2: Time parsing "12:47 am" → "00:47" ──
    print("\n--- FIX 2: Time parsing ---")
    r = routine.handle("c2", "u2", "Everyday send me my routine at 12:47 am everyday", [])
    print(f"12:47 am → {r}")
    results.append(check("FIX2: 12:47 am → 00:47", "00:47" in r, f"got: {r}"))
    results.append(check("FIX2: not 12:47", "12:47" not in r, f"got: {r}"))

    r = routine.handle("c2", "u2", "Everyday send me my routine at 12:50 AM", [])
    print(f"12:50 AM → {r}")
    results.append(check("FIX2: 12:50 AM → 00:50", "00:50" in r, f"got: {r}"))
    results.append(check("FIX2: not 12:50", "12:50" not in r, f"got: {r}"))

    r = routine.handle("c2", "u2", "Everyday send me my routine at 6:52 am", [])
    print(f"6:52 am → {r}")
    results.append(check("FIX2: 6:52 am → 06:52", "06:52" in r, f"got: {r}"))

    r = routine.handle("c2", "u2", "Everyday send me my routine at 4 pm", [])
    print(f"4 pm → {r}")
    results.append(check("FIX2: 4 pm → 16:00", "16:00" in r, f"got: {r}"))

    # ── FIX 3: Delete all tasks from today's routine ──
    print("\n--- FIX 3: Delete routine ---")
    today = date.today().isoformat()
    # Add a task for today
    store.create_item("c3", "u3", {
        "title": "Work on ML Project", "item_type": "task",
        "event_date": today, "start_time": "10:00", "end_time": "12:00",
    })
    # Generate today's routine
    routine.handle("c3", "u3", "What is my today's routine", [])
    # Now delete all tasks from today's routine
    r = routine.handle("c3", "u3", "Delete all the tasks written in today's routine", [])
    print(f"Delete request → {r}")
    results.append(check("FIX3: delete response mentions deleted", "Deleted" in r or "Cleared" in r, f"got: {r}"))
    # Verify the cached routine is gone
    cached = store.get_daily_routine("u3", today)
    results.append(check("FIX3: cached routine deleted", cached is None, f"got: {cached}"))
    # Verify the planner items are gone
    items = store.find_items_on("u3", today)
    results.append(check("FIX3: planner items deleted", len(items) == 0, f"got: {len(items)} items"))

    # Also test "Remove all the tasks written in today's routine"
    store.create_item("c3", "u3", {
        "title": "Work on ML Project", "item_type": "task",
        "event_date": today, "start_time": "10:00", "end_time": "12:00",
    })
    r = routine.handle("c3", "u3", "Remove all the tasks written in today's routine", [])
    print(f"Remove request → {r}")
    results.append(check("FIX3: remove response mentions deleted", "Deleted" in r or "Cleared" in r, f"got: {r}"))
    items = store.find_items_on("u3", today)
    results.append(check("FIX3: remove deleted planner items", len(items) == 0, f"got: {len(items)} items"))

    # ── FIX 4: Completed task removed from routine ──
    print("\n--- FIX 4: Completed task removed from routine ---")
    # Add ML project with deadline tomorrow
    planner.handle("c4", "u4", "My ML project is due tomorrow", [])
    # Generate tomorrow's routine (should include ML project)
    result = routine.handle("c4", "u4", "Send me my tomorrow's routine", [])
    print(f"Before completion:\n{result}\n")
    results.append(check("FIX4: ML project in routine before completion", "ml project" in result.lower(), f"got: {result}"))
    # Mark ML project as completed
    r = planner.handle("c4", "u4", "ML project had been submitted yesterday", [])
    print(f"Complete response: {r}")
    # Generate tomorrow's routine again - should NOT include ML project
    result = routine.handle("c4", "u4", "Send me my tomorrow's routine", [])
    print(f"After completion:\n{result}\n")
    results.append(check("FIX4: ML project removed from routine", "ML project" not in result, f"got: {result}"))

    passed = sum(1 for r in results if r)
    print(f"\nPassed: {passed}/{len(results)}")
    db.unlink(missing_ok=True)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()