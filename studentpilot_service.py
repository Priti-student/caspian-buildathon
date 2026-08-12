"""Application orchestration independent of Caspian and Telegram."""

from llm_service import FeatherlessLLM
from opportunity_memory_service import OpportunityMemoryService
from opportunity_service import OpportunityAnalyzer
from planning_service import PlanningService
from storage import StudentPilotStore
from text_utils import normalize_message


class StudentPilotService:
    def __init__(self, llm: FeatherlessLLM, store: StudentPilotStore, history_limit: int = 16) -> None:
        self._llm, self._store = llm, store
        self._history_limit = history_limit
        self._planner = PlanningService(llm, store)
        self._opportunities = OpportunityAnalyzer(llm)
        self._opportunity_memory = OpportunityMemoryService(store)

    def respond(self, conversation_id: str, user_id: str, text: str) -> str:
        try:
            text = normalize_message(text)
            history = self._store.recent_messages(conversation_id, self._history_limit)
            planned = self._planner.handle(conversation_id, user_id, text, history)
            answer = planned
            if answer is None:
                answer = self._opportunity_memory.handle_followup(conversation_id, text)
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
