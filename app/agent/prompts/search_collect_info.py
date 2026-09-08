COLLECT_PROMPT = """你是竞品信息采集专家。请基于你的知识和搜索摘要，输出竞品结构化信息。
赛道：{track}
竞品：{name}
分析深度：{depth}
搜索摘要：
{search_summary}

输出严格 JSON：
{{
  "features": ["功能1", "功能2"],
  "capabilities": ["可评估能力1", "可评估能力2"],
  "pricing": "定价模式",
  "pricing_freetier": "免费版限制",
  "pricing_paid_min": "最低付费版",
  "target_users": "目标用户",
  "positioning": "一句话定位",
  "key_selling_points": ["卖点1", "卖点2"],
  "differentiation": "差异化",
  "tech_stack": "技术能力或集成能力",
  "api_openness": "API开放程度",
  "strengths": ["优势1"],
  "weaknesses": ["劣势1"],
  "sentiment": "用户口碑摘要",
  "evidence_notes": ["基于搜索或网页正文可支撑的事实1"]
}}
"""
