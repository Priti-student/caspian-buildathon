"""Standalone verification for all reported fixes. Run: python verify_fixes.py"""
import sys
from pathlib import Path
from storage import StudentPilotStore
from planning_service import PlanningService
from reminder_service import ReminderService
from reminder_dispatcher import ReminderDispatcher
from studentpilot_service import StudentPilotService


class FakeLLM:
    def __init__(self):
        self.histories = []

    def complete_json(self, prompt, text, history=None):
        self.histories.append(history or [])
        if "tasks and events" in prompt:
            return self._plan(text)
        if "reminders" in prompt:
            return self._reminder(text)
        return self._opportunity(text)

    def reply(self, text, history=None):
        self.histories.append(history or [])
        return "General response"

    @staticmethod
    def _opportunity(text):
        lower = text.lower()
        if not any(w in lower for w in ("opportunit", "internship", "job", "scholarship", "hackathon", "analyze")):
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
        if "ml project" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "ML project", "item_type": "task", "deadline": "2026-08-14", "event_date": None, "start_time": None, "end_time": None, "priority": "high", "notes": None, "recurrence": None}]}
        if "meeting" in lower:
            return {"action": "add", "needs_clarification": False, "items": [{"title": "Meeting", "item_type": "meeting", "event_date": "2026-08-13", "start_time": "16:00", "end_time": None, "deadline": None, "priority": None, "notes": None, "recurrence": None}]}
        if "upcoming" in lower:
            return {"action": "list", "needs_clarification": False}
        return {"action": "none", "needs_clarification": False}

    @staticmethod
    def _reminder(text):
        lower = text.lower()
        if "remind" in lower:
            return {"action": "add", "target": "ML project", "remind_at": "2026-08-15 09:00", "recurrence": "daily", "needs_clarification": False}
        return {"action": "none", "needs_clarification": False}


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def main():
    results = []
    db = Path(".verify_fixes.db")
    db.unlink(missing_ok=True)
    store = StudentPilotStore(db)
    llm = FakeLLM()
    app = StudentPilotService(llm, store, history_limit=16)

    # FIX 1: Opportunities NOT saved as tasks
    app.respond("c1", "u1", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
    results.append(check("FIX1: opps stored", len(store.latest_opportunities("u1")) == 3))
    results.append(check("FIX1: no tasks from opps", len(store.find_items("u1")) == 0))

    # FIX 2+3: Complete removes from pending
    planner = PlanningService(llm, store)
    planner.handle("c2", "u2", "My ML project is due Friday.", [])
    planner.handle("c2", "u2", "I have a meeting tomorrow at 4 PM.", [])
    results.append(check("FIX2: 2 pending", len(store.find_items("u2")) == 2))
    planner.handle("c2", "u2", "Mark my ML project complete.", [])
    results.append(check("FIX2: 1 pending after complete", len(store.find_items("u2")) == 1))

    class NoTargetLLM:
        def complete_json(self, prompt, text, history=None):
            return {"action": "complete", "target": None, "needs_clarification": False}
    planner2 = PlanningService(NoTargetLLM(), store)
    r = planner2.handle("c2", "u2", "I have completed my work", [])
    results.append(check("FIX3: all completed", "Marked as completed:" in r, f"got: {r}"))
    results.append(check("FIX3: no pending left", len(store.find_items("u2")) == 0))

    # FIX 4: Mailed opportunities reference
    app.respond("c4", "u4", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
    app.respond("c4", "u4", "My ML project is due Friday.")
    reply = app.respond("c4", "u4", "I have mailed you some opportunities")
    results.append(check("FIX4: has Opportunity A", "Opportunity A" in reply, f"got: {reply[:80]}"))
    results.append(check("FIX4: has Links", "Links:" in reply, f"got: {reply[:80]}"))
    results.append(check("FIX4: no ML project", "ML project" not in reply, f"got: {reply[:80]}"))

    # FIX 5: Deadline not intercepted by ReminderService
    rs = ReminderService(llm, store)
    results.append(check("FIX5: submit X tomorrow not reminder", not rs._is_reminder_request("I have to submit my ML PROJECT tomorrow")))
    results.append(check("FIX5: X deadline is tomorrow not reminder", not rs._is_reminder_request("My ML project deadline is tomorrow")))
    results.append(check("FIX5: remind me IS reminder", rs._is_reminder_request("Remind me about my ML project")))

    # FIX 6: Reminder delivery
    store.create_reminder("c6", "u6", None, "ML project", "2020-01-01 00:00")
    store.link_identity("u6", "email", "test@example.com")
    emails, tgs = [], []
    disp = ReminderDispatcher(store,
        email_sender=lambda r, s, b: emails.append((r, s, b)) or "sent",
        telegram_sender=lambda c, m: tgs.append((c, m)) or True)
    delivered = disp.dispatch_due()
    results.append(check("FIX6: delivered", len(delivered) == 1, f"got {len(delivered)}"))
    results.append(check("FIX6: email sent", len(emails) == 1, f"got {len(emails)}"))
    results.append(check("FIX6: tg sent", len(tgs) == 1, f"got {len(tgs)}"))
    results.append(check("FIX6: deactivated", len(store.find_reminders("u6")) == 0))

    # FIX 7: History check — conversation isolation
    app.respond("s1", "u1", "Here are three fictional internship opportunities: A, B, C. Analyze them.")
    app.respond("s1", "u1", "Which one is best?")
    app.respond("s1", "u1", "Tell me more about the second one.")
    app.respond("s1", "u1", "What is its deadline?")
    app.respond("s1", "u1", "Compare the first and third ones.")
    app.respond("s2", "u2", "Hello")
    s1_history = store.recent_messages("s1")
    s2_history = store.recent_messages("s2")
    has = any("three fictional" in item["content"] for item in s1_history)
    isolated = not any("three fictional" in item["content"] for item in s2_history)
    results.append(check("FIX7: history has 'three fictional'", has, f"histories={s1_history}"))
    results.append(check("FIX7: conversation isolation", isolated, f"histories={s2_history}"))

    passed = sum(1 for r in results if r)
    print(f"\nPassed: {passed}/{len(results)}")
    db.unlink(missing_ok=True)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
