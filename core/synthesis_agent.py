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

_SYNTHESIS_PROMPT = """你是 VIRA 平台的 Agent 5「爆款公式提炼师」。

任务：分析多个竞品样本的 AI 分析报告，提炼共性爆款规律，输出一套可复用的内容方法论。

分析维度：
1. 爆款 Hook 通用公式（多个样本中反复出现的开场类型与逻辑）
2. 视觉规律（色彩、构图、文字布局的共性特征）
3. 商业转化共性（CTA 模式、痛点激活方式、价值主张表达）
4. 合规风险规律（哪些词/表达被多个样本标记为风险）
5. 策略建议（综合来看，值得优先复刻的关键要素）

严格输出以下 JSON，不得有任何额外文字：
{
  "sample_count": <int 分析的样本数>,
  "executive_summary": "string（≤120字的整体爆款规律总结）",
  "viral_formula": "string（一句话爆款公式，如：痛点式开场+信任背书+限时CTA）",
  "hook_patterns": [
    {"pattern": "string（Hook类型）", "frequency": <int 出现次数>, "example": "string（举例说明）"}
  ],
  "visual_rules": ["string（视觉规律要点）"],
  "conversion_insights": ["string（转化洞察）"],
  "compliance_watch": ["string（合规注意事项）"],
  "top_recommendations": [
    {"priority": <int 1-5，1最高>, "action": "string（具体可执行建议）", "reason": "string（原因）"}
  ],
  "methodology_doc": "string（300字内的完整方法论文档，Markdown格式，可直接分享给团队）"
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
                f"- Hook类型：{d.get('hook_type','—')} · Hook分：{d.get('hook_score','—')} · "
                f"视觉分：{d.get('visual_score','—')}\n"
                f"- 情绪基调：{d.get('emotional_tone','—')}\n"
                f"- 前3秒：{d.get('first_3s_analysis','—')}"
            )

        if wf.commerce and wf.commerce.success:
            d = wf.commerce.data
            parts.append(
                f"- 病毒指数：{d.get('virality_score','—')} · 转化潜力：{d.get('conversion_potential','—')}\n"
                f"- 优化逻辑：{d.get('optimization_summary','—')}"
            )

        if wf.compliance and wf.compliance.success:
            d = wf.compliance.data
            viols = d.get("violations", [])
            viol_summary = "、".join(
                f"{v.get('type','?')}[{v.get('severity','?')}]"
                for v in viols
            ) or "无"
            parts.append(
                f"- 合规风险：{d.get('risk_level','—')} · 违规项：{viol_summary}"
            )

        if wf.strategy and wf.strategy.success:
            d = wf.strategy.data
            parts.append(
                f"- 置信度：{d.get('confidence_score','—')} · 裁决：{d.get('verdict','—')}\n"
                f"- 摘要：{d.get('executive_summary','—')}"
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
                "SynthesisAgent OK | samples=%d formula=%s",
                len(valid),
                data.get("viral_formula", "—"),
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
