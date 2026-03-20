"""
core/synthesis_agent.py
Agent 5 · 爆款公式提炼师

职责：接收多个样本的 WorkflowResult，归纳通用爆款规律、
      输出可复用的内容公式与方法论。

设计决策：
  · 不接收图片（纯文本聚合），调用一次 chat（不含 Vision），成本低
  · 输入：所有样本的 visual/commerce/compliance/strategy 数据 JSON 摘要
  · 输出：JSON 结构化爆款公式报告
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.openai_client import OpenAIClient

logger = logging.getLogger(__name__)

# ── 结果容器（轻量，不复用 AgentResult 以减少依赖）────────────────────────────

@dataclass
class SynthesisResult:
    success:      bool
    data:         Dict[str, Any]  = field(default_factory=dict)
    raw_response: str             = ""
    usage:        Dict[str, Any]  = field(default_factory=dict)
    error:        str             = ""


# ── System Prompt ─────────────────────────────────────────────────────────────
# 与 prompts/__init__.py 中的 ATTRIBUTION_ANALYST 保持字段一致
# 核心输出字段：formula_name, core_pattern, reusable_elements, applicable_scenarios

_SYNTHESIS_PROMPT = """你是 VIRA 平台的 Agent 5「爆款公式提炼师」。

任务：分析多个竞品样本的 AI 分析报告，提炼共性爆款规律，输出一套可复用的内容方法论。

分析维度：
1. 爆款 Hook 通用公式（多个样本中反复出现的开场类型与逻辑）
2. 视觉规律（色彩、构图、文字布局的共性特征）
3. 商业转化共性（CTA 模式、痛点激活方式、价值主张表达）
4. 合规风险规律（哪些词/表达被多个样本标记为风险）
5. 可复用元素与适用场景

你的回答必须是且仅是一个合法 JSON 对象，不得包含任何 JSON 以外的文字、解释或 markdown 代码块。

输出格式：
{
  "formula_name": "string（公式名称，如：痛点闪击+权威背书+限时CTA式）",
  "core_pattern": "string（≤80字，核心创作模式，格式：开场→主体→结尾，概括多个样本的共性规律）",
  "reusable_elements": [
    "string（可直接复用的元素，如：前3秒痛点疑问句式）",
    "string（可直接复用的元素）",
    "string（可直接复用的元素）"
  ],
  "applicable_scenarios": [
    "string（适用的内容场景或品类，如：快消品种草/知识付费/美妆测评）",
    "string（适用场景）"
  ],
  "sample_count": <int 分析的样本数>,
  "compliance_watch": ["string（需注意的合规风险词，来自多个样本的共性警告）"]
}"""


# ── Agent ─────────────────────────────────────────────────────────────────────

class SynthesisAgent:
    """
    Agent 5 · 爆款公式提炼师

    用法：
        agent = SynthesisAgent(client)
        result = agent.run(batch_items)
        # batch_items: list of {"name": str, "result": WorkflowResult | None}
    """

    NAME = "爆款公式提炼师"

    def __init__(self, client: OpenAIClient):
        self.client = client

    # ── 将 WorkflowResult 转为 LLM 可读摘要 ────────────────────────────────
    @staticmethod
    def _summarize_item(name: str, wf) -> str:
        """把单个 WorkflowResult 压缩成结构化文字（节省 Token）"""
        if wf is None:
            return f"【{name}】分析失败，跳过。\n"

        parts = [f"## 样本：{name}"]

        if wf.visual and wf.visual.success:
            d = wf.visual.data
            parts.append(
                f"- Hook类型：{d.get('hook_type','—')} · Hook评分：{d.get('hook_score','—')}/10\n"
                f"- 情绪基调：{d.get('emotion_tone','—')}\n"
                f"- 关键元素：{', '.join(d.get('key_visual_elements', []))}\n"
                f"- 短板：{d.get('weakness','—')}"
            )

        if wf.commerce and wf.commerce.success:
            d = wf.commerce.data
            scripts = d.get("scripts", [])
            hooks = "、".join(s.get("hook", "") for s in scripts[:3] if s.get("hook"))
            parts.append(
                f"- 转化评分：{d.get('conversion_score','—')}/10\n"
                f"- 最佳切入：{d.get('best_angle','—')}\n"
                f"- 脚本Hook示例：{hooks or '—'}"
            )

        if wf.compliance and wf.compliance.success:
            d = wf.compliance.data
            kws = d.get("violation_keywords", [])
            kw_summary = "、".join(kws[:5]) if kws else "无"
            parts.append(
                f"- 合规风险：{d.get('risk_level','—')} · 违规词：{kw_summary}\n"
                f"- 整改建议：{d.get('suggestion','—')}"
            )

        if wf.strategy and wf.strategy.success:
            d = wf.strategy.data
            improvements = d.get("top3_improvements", [])
            imp_summary = "；".join(improvements[:3]) if improvements else "—"
            parts.append(
                f"- 置信度：{d.get('success_confidence','—')} · 裁决：{d.get('final_verdict','—')}\n"
                f"- 改进建议：{imp_summary}"
            )

        return "\n".join(parts) + "\n"

    def run(self, batch_items: List[Dict]) -> SynthesisResult:
        """
        Args:
            batch_items: list of {"name": str, "result": WorkflowResult | None}
        Returns:
            SynthesisResult
        """
        valid = [b for b in batch_items if b.get("result") is not None]
        if len(valid) < 2:
            return SynthesisResult(
                success=False,
                error="至少需要 2 个成功分析的样本才能提炼公式",
            )

        # ── 构建摘要文本 ────────────────────────────────────────────────────
        summaries = "\n".join(
            self._summarize_item(b["name"], b["result"]) for b in valid
        )
        user_msg = (
            f"以下是 {len(valid)} 个竞品样本的分析报告，请提炼爆款公式与方法论：\n\n"
            f"{summaries}"
        )

        try:
            raw = self.client.chat(
                system_prompt=_SYNTHESIS_PROMPT,
                user_message=user_msg,
                image_data=None,
                max_tokens=1800,
                temperature=0.4,
            )
            data = _extract_json(raw)
            data.setdefault("sample_count", len(valid))
            logger.info(
                "SynthesisAgent OK | samples=%d formula_name=%s",
                len(valid),
                data.get("formula_name", "—"),
            )
            return SynthesisResult(
                success=True, data=data, raw_response=raw, usage=self.client.last_usage
            )
        except Exception as e:
            logger.error("SynthesisAgent FAILED: %s", e)
            return SynthesisResult(success=False, error=str(e))


# ── JSON 提取工具（与 agents.py 同款，避免循环导入）──────────────────────────

def _extract_json(text: str) -> dict:
    if not text:
        raise ValueError("空响应")
    md_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if md_match:
        text = md_match.group(1).strip()
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        return json.loads(brace_match.group(0))
    return json.loads(text.strip())
