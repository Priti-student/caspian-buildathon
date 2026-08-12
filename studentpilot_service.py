"""Application orchestration independent of Caspian and Telegram."""

from llm_service import FeatherlessLLM
from opportunity_service import OpportunityAnalyzer
from planning_service import PlanningService
from storage import StudentPilotStore


class StudentPilotService:
    def __init__(self, llm: FeatherlessLLM, store: StudentPilotStore, history_limit: int = 16) -> None:
        self._llm, self._store = llm, store
        self._history_limit = history_limit
        self._planner = PlanningService(llm, store)
        self._opportunities = OpportunityAnalyzer(llm)

    def respond(self, conversation_id: str, user_id: str, text: str) -> str:
        try:
            history = self._store.recent_messages(conversation_id, self._history_limit)
            planned = self._planner.handle(conversation_id, user_id, text, history)
            answer = planned or self._opportunities.try_analyze(text, history) or self._llm.reply(text, history)
            self._store.add_message(conversation_id, user_id, "user", text)
            self._store.add_message(conversation_id, user_id, "assistant", answer)
            return answer
        except Exception:
            return "I’m having trouble accessing StudentPilot right now. Please try again shortly."
