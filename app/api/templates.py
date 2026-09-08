"""自定义分析模版 API：默认模版只读，自定义模版可增删改。"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import storage
from ..templates import aall_templates, create_custom_template, update_custom_template

router = APIRouter()

MAX_DIMENSIONS = 20
MAX_KEYWORDS = 30


class Dimension(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    keywords: list[str] = Field(default_factory=list)
    key_questions: list[str] = Field(default_factory=list)


class TemplateWrite(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    dimensions: list[Dimension] = Field(default_factory=list)


def _validate_payload(req: TemplateWrite) -> dict[str, Any]:
    if len(req.dimensions) == 0:
        raise HTTPException(status_code=400, detail="模版至少需要一个维度")
    if len(req.dimensions) > MAX_DIMENSIONS:
        raise HTTPException(status_code=400, detail=f"维度数量不能超过 {MAX_DIMENSIONS}")
    dims: list[dict[str, Any]] = []
    for dim in req.dimensions:
        if len(dim.keywords) > MAX_KEYWORDS:
            raise HTTPException(status_code=400, detail=f"维度「{dim.name}」关键词不能超过 {MAX_KEYWORDS} 个")
        dims.append({
            "name": dim.name.strip(),
            "keywords": [k.strip() for k in dim.keywords if k.strip()],
            "key_questions": [q.strip() for q in dim.key_questions if q.strip()],
        })
    if not dims:
        raise HTTPException(status_code=400, detail="模版至少需要一个有效维度")
    return {"name": req.name.strip()[:60] or "自定义模版", "dimensions": dims}


@router.get("/templates")
async def list_templates() -> dict[str, Any]:
    return {"templates": await aall_templates()}


@router.post("/templates")
async def create_template(req: TemplateWrite) -> dict[str, Any]:
    payload = _validate_payload(req)
    template = await create_custom_template(**payload)
    return {"template": template}


@router.put("/templates/{template_id}")
async def update_template(template_id: str, req: TemplateWrite) -> dict[str, Any]:
    if template_id == "default":
        raise HTTPException(status_code=400, detail="默认模版不可修改")
    payload = _validate_payload(req)
    ok = await update_custom_template(template_id, payload["name"], payload["dimensions"])
    if not ok:
        raise HTTPException(status_code=404, detail="模版不存在")
    updated = await storage.aget_template(template_id)
    return {"template": updated}


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str) -> dict[str, str]:
    if template_id == "default":
        raise HTTPException(status_code=400, detail="默认模版不可删除")
    ok = await storage.adelete_template(template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="模版不存在")
    return {"status": "deleted"}
