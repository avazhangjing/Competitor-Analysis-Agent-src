"""标准测试集（Benchmark）。

预设标准赛道和预期竞品，用于可重复的 Agent 性能测试。
面试时可展示：如何设计评估基准、如何量化 Agent 表现。
"""

from dataclasses import dataclass, field


@dataclass
class BenchmarkCase:
    """单个基准测试用例。"""

    track: str
    expected_competitors: list[str]
    depth: str = "broad"
    description: str = ""
    # 预期至少应覆盖的研究维度
    expected_dimensions: list[str] = field(default_factory=list)


# 标准测试集：覆盖不同领域和复杂度
BENCHMARK_SUITE: list[BenchmarkCase] = [
    BenchmarkCase(
        track="AI编程助手",
        expected_competitors=["GitHub Copilot", "Cursor", "Codeium", "Tabnine"],
        depth="broad",
        description="AI代码补全与编程辅助工具赛道，竞争激烈，信息丰富",
        expected_dimensions=["功能特性", "定价策略", "目标用户", "技术架构"],
    ),
    BenchmarkCase(
        track="项目管理工具",
        expected_competitors=["Jira", "Notion", "Linear", "飞书项目"],
        depth="broad",
        description="企业级项目管理SaaS赛道，国内外竞品并存",
        expected_dimensions=["核心功能", "协作能力", "集成生态", "定价"],
    ),
    BenchmarkCase(
        track="AI Agent开发框架",
        expected_competitors=["LangChain", "CrewAI", "AutoGen", "Dify"],
        depth="deep",
        description="AI Agent开发框架赛道，技术性强，需要深度分析",
        expected_dimensions=["架构设计", "多Agent协作", "工具集成", "社区生态", "企业级特性"],
    ),
    BenchmarkCase(
        track="在线文档协作",
        expected_competitors=["飞书文档", "腾讯文档", "石墨文档", "Notion"],
        depth="broad",
        description="国内在线协作文档赛道，产品形态相近",
        expected_dimensions=["协作功能", "AI能力", "生态集成", "免费版限制"],
    ),
    BenchmarkCase(
        track="低代码平台",
        expected_competitors=["OutSystems", "Mendix", "PowerApps", "简道云"],
        depth="deep",
        description="企业级低代码/无代码开发平台，需要深度对比",
        expected_dimensions=["开发能力", "部署方式", "扩展性", "行业方案", "定价模式"],
    ),
]


def get_benchmark_cases() -> list[BenchmarkCase]:
    """获取所有基准测试用例。"""
    return BENCHMARK_SUITE


def get_benchmark_by_track(track: str) -> BenchmarkCase | None:
    """根据赛道名称查找对应的基准测试用例。"""
    track_lower = track.lower()
    for case in BENCHMARK_SUITE:
        if case.track.lower() == track_lower:
            return case
    return None


def evaluate_against_benchmark(
    case: BenchmarkCase,
    actual_competitors: list[str],
    research_data: dict,
) -> dict:
    """将实际分析结果与基准预期对比。

    Returns:
        包含匹配度、遗漏竞品等对比结果的字典
    """
    expected_set = {c.lower() for c in case.expected_competitors}
    actual_set = {c.lower() for c in actual_competitors}

    matched = expected_set & actual_set
    missed = expected_set - actual_set
    extra = actual_set - expected_set

    match_rate = len(matched) / len(expected_set) if expected_set else 0.0

    return {
        "benchmark_track": case.track,
        "match_rate": round(match_rate, 3),
        "matched_competitors": sorted(matched),
        "missed_competitors": sorted(missed),
        "extra_competitors": sorted(extra),
        "expected_count": len(case.expected_competitors),
        "actual_count": len(actual_competitors),
    }
