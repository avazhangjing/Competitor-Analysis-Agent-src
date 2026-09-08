from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Depth = Literal["broad", "deep"]
SourceType = Literal["llm_knowledge", "web_search", "web_fetch"]
Credibility = Literal["high", "medium", "low"]

MAX_RESEARCH_ROUNDS = 2


@dataclass
class Source:
    label: str
    url: str
    type: SourceType


@dataclass
class Evidence:
    id: str
    competitor: str
    source_type: SourceType
    title: str
    url: str
    snippet: str
    credibility: Credibility
    collected_at: str


@dataclass
class CompetitorInfo:
    name: str
    features: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    pricing: str = ""
    pricing_freetier: str = ""
    pricing_paid_min: str = ""
    target_users: str = ""
    positioning: str = ""
    key_selling_points: list[str] = field(default_factory=list)
    differentiation: str = ""
    tech_stack: str = ""
    api_openness: str = ""
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    sentiment: str = ""
    evidence_notes: list[str] = field(default_factory=list)
    evidences: list[Evidence] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sources"] = [asdict(s) for s in self.sources]
        data["evidences"] = [asdict(e) for e in self.evidences]
        return data


@dataclass
class AnalysisResult:
    positioning_rows: list[dict[str, str]] = field(default_factory=list)
    feature_matrix: dict[str, dict[str, str]] = field(default_factory=dict)
    evaluation_dimensions: list[dict[str, str]] = field(default_factory=list)
    scorecard_rows: list[dict[str, Any]] = field(default_factory=list)
    capability_rows: list[dict[str, Any]] = field(default_factory=list)
    battlecard_rows: list[dict[str, Any]] = field(default_factory=list)
    opportunity_rows: list[dict[str, str]] = field(default_factory=list)
    pricing_rows: list[dict[str, str]] = field(default_factory=list)
    user_rows: list[dict[str, str]] = field(default_factory=list)
    swot: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    tech_rows: list[dict[str, str]] = field(default_factory=list)
    sentiment_rows: list[dict[str, str]] = field(default_factory=list)
    insights: list[str] = field(default_factory=list)


@dataclass
class ResearchDimension:
    name: str
    key_questions: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    priority: str = "high"


@dataclass
class ResearchPlan:
    track_intent: str = ""
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    report_outline: list[str] = field(default_factory=list)
    search_strategy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_intent": self.track_intent,
            "dimensions": self.dimensions,
            "hypotheses": self.hypotheses,
            "report_outline": self.report_outline,
            "search_strategy": self.search_strategy,
        }


@dataclass
class ResearchGap:
    competitor: str
    dimension: str
    reason: str = ""
    followup_queries: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "competitor": self.competitor,
            "dimension": self.dimension,
            "reason": self.reason,
            "followup_queries": self.followup_queries,
        }


@dataclass
class Synthesis:
    executive_summary: str = ""
    key_insights: list[str] = field(default_factory=list)
    strategic_recommendations: list[str] = field(default_factory=list)
    competitive_landscape: str = ""
    risk_assessment: str = ""
    narrative_sections: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executive_summary": self.executive_summary,
            "key_insights": self.key_insights,
            "strategic_recommendations": self.strategic_recommendations,
            "competitive_landscape": self.competitive_landscape,
            "risk_assessment": self.risk_assessment,
            "narrative_sections": self.narrative_sections,
        }
