"""
services/template_store.py
分析模板库：保存 / 加载 / 列举 / 删除分析模板

模板结构：
  {
    "id":           str  (uuid8)
    "name":         str  用户起的模板名
    "description":  str  简介
    "rag_text":     str  品牌知识库内容
    "tags":         list[str]  标签（如：美妆、服装、食品）
    "example_summary": str  来自某次 workflow_result 的简要摘要（可选）
    "viral_formula":   str  爆款公式（来自 synthesis_result，可选）
    "created_at":   str  ISO8601
    "created_by":   str  用户邮箱
  }

存储：JSON 文件（data/templates/<id>.json），零依赖，随时可迁移到数据库。
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

TEMPLATE_DIR = Path(__file__).parent.parent / "data" / "templates"
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)


# ── 公开 API ──────────────────────────────────────────────────────────────────

def save_template(
    name:             str,
    rag_text:         str,
    description:      str       = "",
    tags:             list[str] = None,
    example_summary:  str       = "",
    viral_formula:    str       = "",
    created_by:       str       = "",
) -> dict:
    """
    保存新模板。返回保存后的模板字典。
    """
    tpl = {
        "id":              str(uuid.uuid4())[:8],
        "name":            name.strip() or "未命名模板",
        "description":     description.strip(),
        "rag_text":        rag_text,
        "tags":            [t.strip() for t in (tags or []) if t.strip()],
        "example_summary": example_summary,
        "viral_formula":   viral_formula,
        "created_at":      datetime.now().isoformat(),
        "created_by":      created_by,
    }
    _write(tpl)
    return tpl


def list_templates() -> list[dict]:
    """返回所有模板，按 created_at 倒序"""
    tpls = []
    for fp in TEMPLATE_DIR.glob("*.json"):
        try:
            tpls.append(json.loads(fp.read_text("utf-8")))
        except Exception:
            pass
    return sorted(tpls, key=lambda x: x.get("created_at", ""), reverse=True)


def get_template(tid: str) -> Optional[dict]:
    fp = TEMPLATE_DIR / f"{tid}.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text("utf-8"))
    except Exception:
        return None


def delete_template(tid: str) -> bool:
    fp = TEMPLATE_DIR / f"{tid}.json"
    if fp.exists():
        fp.unlink()
        return True
    return False


def update_template(tid: str, **kwargs) -> Optional[dict]:
    """修改模板某些字段（name / description / tags / rag_text）"""
    tpl = get_template(tid)
    if tpl is None:
        return None
    allowed = {"name", "description", "tags", "rag_text", "viral_formula", "example_summary"}
    for k, v in kwargs.items():
        if k in allowed:
            tpl[k] = v
    _write(tpl)
    return tpl


# ── 内部工具 ──────────────────────────────────────────────────────────────────

def _write(tpl: dict) -> None:
    fp = TEMPLATE_DIR / f"{tpl['id']}.json"
    fp.write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
