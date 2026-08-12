"""Career-opportunity analysis independent from Caspian message delivery."""

from dataclasses import dataclass
from typing import Any

from llm_service import FeatherlessLLM, LLMResponseError


CATEGORIES = {"Internship", "Job", "Hackathon", "Scholarship", "Competition", "Workshop", "Other"}
URGENCY_LEVELS = {"High", "Medium", "Low", "Not specified"}
NOT_SPECIFIED = "Not specified"
ANALYSIS_ERROR = "I couldn't analyze that opportunity right now. Please try again shortly."

ANALYSIS_PROMPT = """You analyze career opportunities for students. Return ONLY one JSON object.

First decide whether the user's supplied content contains one or more career opportunities. Typical opportunities include internships, jobs, hackathons, scholarships, competitions, and workshops. Casual conversation, study advice, or unrelated messages are not opportunities.

Use exactly this schema:
{
  "is_opportunity": true,
  "requested_top_n": null,
  "opportunities": [
    {
      "rank": 1,
      "title": "string or Not specified",
      "organization": "string or Not specified",
      "category": "Internship | Job | Hackathon | Scholarship | Competition | Workshop | Other",
      "role": "string or Not specified",
      "eligibility": "string or Not specified",
      "required_skills": ["string"],
      "location": "string or Not specified",
      "deadline": "string or Not specified",
      "urgency": "High | Medium | Low | Not specified",
      "links": ["URL"],
      "summary": "concise factual summary",
      "relevance_score": 1,
      "relevance_reason": "brief reason based only on supplied information"
    }
  ],
  "message": "brief explanation"
}

Rules:
- Never invent details. Use exactly "Not specified" for an unavailable scalar field and [] for unavailable lists.
- Preserve deadline wording exactly; do not calculate or invent a date. Mark urgency High only when the supplied deadline is clearly soon/urgent, Medium when a deadline is present but not clearly urgent, Low when it is clearly distant, otherwise Not specified.
- Score relevance 1-10 only from the supplied opportunity details and any student interests explicitly stated in the input. If no interests are stated, use a neutral evidence-based score and say that in relevance_reason.
- If the user asks for top N, set requested_top_n to that positive integer. Otherwise use null.
- When multiple opportunities are supplied, return each separately and rank them with 1 as best, considering relevance first and urgency second.
- If it is not an opportunity, return is_opportunity false, opportunities [], requested_top_n null, and a polite message.
"""


@dataclass
class Opportunity:
    rank: int
    title: str
    organization: str
    category: str
    role: str
    eligibility: str
    required_skills: list[str]
    location: str
    deadline: str
    urgency: str
    links: list[str]
    summary: str
    relevance_score: int
    relevance_reason: str


class OpportunityAnalyzer:
    """Analyze pasted or forwarded opportunity text and format Telegram output."""

    def __init__(self, llm: FeatherlessLLM) -> None:
        self._llm = llm

    def analyze(self, text: str, history: list[dict[str, str]] | None = None) -> str:
        return self.try_analyze(text, history) or "This does not appear to be a career opportunity. Send an opportunity message and I’ll analyze it."

    def try_analyze(self, text: str, history: list[dict[str, str]] | None = None) -> str | None:
        result = self.extract(text, history)
        if result is None:
            return None
        opportunities, requested_top_n = result
        if not opportunities:
            return "I couldn't find a complete opportunity to analyze. Please paste or forward the opportunity details."
        if requested_top_n:
            opportunities = opportunities[:requested_top_n]
        return self._format(opportunities)

    def extract(
        self, text: str, history: list[dict[str, str]] | None = None
    ) -> tuple[list[Opportunity], int | None] | None:
        try:
            result = self._llm.complete_json(ANALYSIS_PROMPT, text, history)
            if result.get("is_opportunity") is not True:
                return None
            opportunities = self._validated_opportunities(result.get("opportunities"))
            opportunities.sort(key=lambda item: item.rank)
            requested_top_n = result.get("requested_top_n")
            top_n = requested_top_n if isinstance(requested_top_n, int) and requested_top_n > 0 else None
            return opportunities, top_n
        except LLMResponseError:
            return None

    @staticmethod
    def as_record(item: Opportunity) -> dict[str, Any]:
        return {
            "rank": item.rank, "title": item.title, "organization": item.organization,
            "category": item.category, "role": item.role, "eligibility": item.eligibility,
            "skills": ", ".join(item.required_skills) if item.required_skills else NOT_SPECIFIED,
            "location": item.location, "deadline": item.deadline, "urgency": item.urgency,
            "links": "\n".join(item.links) if item.links else NOT_SPECIFIED,
            "summary": item.summary, "relevance_score": item.relevance_score,
            "relevance_reason": item.relevance_reason,
        }

    def _validated_opportunities(self, raw_items: Any) -> list[Opportunity]:
        if not isinstance(raw_items, list):
            return []

        opportunities: list[Opportunity] = []
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                continue
            category = self._text(raw.get("category"))
            if category not in CATEGORIES:
                category = "Other"
            urgency = self._text(raw.get("urgency"))
            if urgency not in URGENCY_LEVELS:
                urgency = NOT_SPECIFIED
            score = raw.get("relevance_score")
            score = score if isinstance(score, int) and 1 <= score <= 10 else 5
            rank = raw.get("rank")
            rank = rank if isinstance(rank, int) and rank > 0 else index
            opportunities.append(
                Opportunity(
                    rank=rank,
                    title=self._text(raw.get("title")),
                    organization=self._text(raw.get("organization")),
                    category=category,
                    role=self._text(raw.get("role")),
                    eligibility=self._text(raw.get("eligibility")),
                    required_skills=self._text_list(raw.get("required_skills")),
                    location=self._text(raw.get("location")),
                    deadline=self._text(raw.get("deadline")),
                    urgency=urgency,
                    links=self._text_list(raw.get("links")),
                    summary=self._text(raw.get("summary")),
                    relevance_score=score,
                    relevance_reason=self._text(raw.get("relevance_reason")),
                )
            )
        return opportunities

    @staticmethod
    def _text(value: Any) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else NOT_SPECIFIED

    @staticmethod
    def _text_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    def _format(self, opportunities: list[Opportunity]) -> str:
        heading = "Opportunity analysis" if len(opportunities) == 1 else f"Top {len(opportunities)} opportunities"
        sections = [heading]
        for item in opportunities:
            skills = ", ".join(item.required_skills) if item.required_skills else NOT_SPECIFIED
            links = "\n".join(item.links) if item.links else NOT_SPECIFIED
            sections.append(
                f"{item.rank}. {item.title} — {item.category}\n"
                f"Organization: {item.organization}\n"
                f"Role: {item.role}\n"
                f"Eligibility: {item.eligibility}\n"
                f"Skills: {skills}\n"
                f"Location: {item.location}\n"
                f"Deadline: {item.deadline} | Urgency: {item.urgency}\n"
                f"Relevance: {item.relevance_score}/10 — {item.relevance_reason}\n"
                f"Summary: {item.summary}\n"
                f"Links: {links}"
            )
        return "\n\n".join(sections)
