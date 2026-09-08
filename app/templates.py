"""分析模版（默认 + 自定义）管理。

- 默认模版：内置不可修改，包含一组通用分析维度与关键词；
- 自定义模版：用户可增减维度/关键词，持久化到 SQLite（storage.templates 表）。
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from . import storage

# 默认模版：维度名 + 关键词，作为研究规划的基础；可被 LLM 进一步扩展。
DEFAULT_TEMPLATE: dict[str, Any] = {
    "id": "default",
    "name": "默认模版",
    "is_custom": False,
    "dimensions": [
        {"name": "产品功能", "keywords": ["核心功能", "功能对比", "功能列表", "产品能力"]},
        {"name": "定价策略", "keywords": ["定价模式", "价格", "付费", "免费版", "价格对比"]},
        {"name": "目标用户", "keywords": ["目标用户", "客户群体", "适用行业", "用户画像"]},
        {"name": "产品定位", "keywords": ["产品定位", "市场定位", "差异化", "核心卖点"]},
        {"name": "技术能力", "keywords": ["技术架构", "API", "集成", "开放平台", "生态"]},
        {"name": "用户口碑", "keywords": ["用户评价", "口碑", "优缺点", "用户反馈", "评分"]},
    ],
}


def all_templates() -> list[dict[str, Any]]:
    """返回默认模版 + 全部自定义模版（默认模版置顶）。"""
    return [dict(DEFAULT_TEMPLATE), *storage.list_templates()]


async def aall_templates() -> list[dict[str, Any]]:
    return [dict(DEFAULT_TEMPLATE), *await storage.alist_templates()]


def normalize_dimensions(raw: Any) -> list[dict[str, Any]]:
    """清洗前端提交的维度/关键词，保证结构一致。"""
    if not isinstance(raw, list):
        return []
    dims: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        keywords = [str(k).strip() for k in item.get("keywords", []) if str(k).strip()]
        key_questions = [str(k).strip() for k in item.get("key_questions", []) if str(k).strip()]
        dims.append({"name": name, "keywords": keywords, "key_questions": key_questions})
    return dims


async def create_custom_template(name: str, dimensions: list[dict[str, Any]]) -> dict[str, Any]:
    tid = f"tpl-{uuid.uuid4().hex[:12]}"
    return await storage.acreate_template(
        tid,
        name.strip()[:60] or "自定义模版",
        normalize_dimensions(dimensions),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        is_custom=True,
    )


async def update_custom_template(template_id: str, name: str, dimensions: list[dict[str, Any]]) -> bool:
    return await storage.aupdate_template(
        template_id,
        name.strip()[:60] or "自定义模版",
        normalize_dimensions(dimensions),
    )
