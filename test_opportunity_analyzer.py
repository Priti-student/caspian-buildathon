"""Live, fictional-sample checks for the opportunity analyzer."""

import unittest

from agent import get_env_setting
from llm_service import FeatherlessLLM
from opportunity_service import OpportunityAnalyzer


class OpportunityAnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.analyzer = OpportunityAnalyzer(
            FeatherlessLLM(api_key=get_env_setting("FEATHERLESS_API_KEY"))
        )

    def test_internship_with_deadline_and_missing_details(self) -> None:
        result = self.analyzer.analyze(
            "Nova Labs is hiring a Data Science Intern. Eligible: final-year BTech students. "
            "Apply by 30 August 2026 at https://example.com/nova-intern."
        )
        self.assertIn("Internship", result)
        self.assertIn("30 August 2026", result)
        self.assertIn("Not specified", result)

    def test_hackathon(self) -> None:
        result = self.analyzer.analyze(
            "BuildSprint 2026 hackathon by CodeForge: form a team of up to four and build an "
            "education tool. It is online; registration closes 15 September 2026."
        )
        self.assertIn("Hackathon", result)

    def test_multiple_opportunities_and_ranking(self) -> None:
        result = self.analyzer.analyze(
            "Show me the best 2. Opportunity A: PixelWorks offers a remote frontend internship "
            "for students with React skills; deadline 5 September 2026. Opportunity B: GreenFuture "
            "Scholarship awards tuition support to students with financial need; deadline 20 October 2026."
        )
        self.assertIn("Top 2 opportunities", result)
        self.assertIn("Internship", result)
        self.assertIn("Scholarship", result)

    def test_non_opportunity(self) -> None:
        result = self.analyzer.analyze("Can you suggest a quick breakfast before my morning class?")
        self.assertIn("opportunity", result.lower())


if __name__ == "__main__":
    unittest.main()
