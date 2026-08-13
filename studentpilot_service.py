"""Application orchestration independent of Caspian and Telegram."""

from email_output_service import EmailOutputService, EmailSender
from identity_service import IdentityService
from llm_service import FeatherlessLLM
from opportunity_memory_service import OpportunityMemoryService
from opportunity_service import OpportunityAnalyzer
from planning_service import PlanningService
from reminder_service import ReminderService
from routine_service import RoutineService
from storage import StudentPilotStore
from text_utils import normalize_message


class StudentPilotService:
    def __init__(self, llm: FeatherlessLLM, store: StudentPilotStore, history_limit: int = 16, email_sender: EmailSender | None = None) -> None:
        self._llm, self._store = llm, store
        self._history_limit = history_limit
        self._planner = PlanningService(llm, store)
        self._opportunities = OpportunityAnalyzer(llm)
        self._opportunity_memory = OpportunityMemoryService(store)
        self._routine = RoutineService(llm, store)
        self._reminders = ReminderService(llm, store)
        self._email = EmailOutputService(store, email_sender)
        self._identity = IdentityService(store, email_sender)

    def respond(self, conversation_id: str, user_id: str, text: str) -> str:
        try:
            text = normalize_message(text)
            history = self._store.recent_messages(conversation_id, self._history_limit)
            answer = self._identity.handle(conversation_id, user_id, text)
            if answer is None:
                answer = self._email.handle(conversation_id, user_id, text)
            if answer is None:
                answer = self._routine.handle(conversation_id, user_id, text, history)
            if answer is None:
                answer = self._reminders.handle(conversation_id, user_id, text, history)
            if answer is None:
                answer = self._planner.handle(conversation_id, user_id, text, history)
            if answer is None:
                answer = self._opportunity_memory.handle_followup(user_id, text)
            if answer is None:
                extracted = self._opportunities.extract(text, history)
                if extracted is not None:
                    opportunities, requested_top_n = extracted
                    if opportunities:
                        if requested_top_n:
                            opportunities = opportunities[:requested_top_n]
                        self._store.save_opportunities(conversation_id, user_id, [
                            self._opportunities.as_record(item) for item in opportunities
                        ])
                        answer = self._opportunities.format(opportunities)
            if answer is None:
                answer = self._llm.reply(text, history)
            self._store.add_message(conversation_id, user_id, "user", text)
            self._store.add_message(conversation_id, user_id, "assistant", answer)
            return answer
        except Exception:
            return "I’m having trouble accessing StudentPilot right now. Please try again shortly."