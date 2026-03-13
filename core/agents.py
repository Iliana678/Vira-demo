"""
core/agents.py
VIRA 四个专家 Agent 的完整实现。

架构说明：
  每个 Agent 继承自 BaseAgent，run() 方法：
    1. 构建专属 system prompt（来自 prompts/__init__.py）
    2. 调用 OpenAIClient.chat()（支持 Vision / 纯文本）
    3. 解析 JSON 响应，返回 AgentResult

  Agent 1 VisualAnalystAgent     — 多模态 Vision，提取 Hook 特征与视觉质量
  Agent 2 CommerceOptimizerAgent — RAG 增强，生成 3 套高转化商业脚本
  Agent 3 ComplianceAuditorAgent — 多模态 Vision，TikTok/抖音合规风险扫描
  Agent 4 StrategyOptimizerAgent — 纯文本，汇总三路输出，输出最终战略裁决

并发说明（由 workflow.py 管理）：
  Agent 1 & 3 在 Phase 1 中 asyncio.gather 真正并发
  Agent 2 在 Phase 2 中串行（依赖 Agent 1 的视觉分析结果）
  Agent 4 在 Phase 3 中串行（汇总 1/2/3 的全部输出）
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.openai_client import OpenAIClient
from services.rag import RAGService
import prompts

logger = logging.getLogger(__name__)


# ── 通用结果容器 ──────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    """单个 Agent 的执行结果"""

    agent_name: str
    success: bool = False
    data: Dict[str, Any] = field(default_factory=dict)
    raw_response: str = ""
    error: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default=None):
        """快捷访问 data 字段"""
        return self.data.get(key, default)


# ── JSON 解析工具函数 ─────────────────────────────────────────────────────────

def _parse_json(text: str, agent_name: str) -> Dict[str, Any]:
    """
    从模型响应中鲁棒地提取 JSON。

    按优先级尝试：
      1. 直接 json.loads（响应完全合规时）
      2. 提取首个 ```json ... ``` 代码块
      3. 提取首个 { ... } 匹配（允许多行）
    """
    text = text.strip()

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 提取 ```json 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 提取最外层 { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("[%s] JSON parse failed, raw: %s...", agent_name, text[:200])
    return {}


# ── 基类 ─────────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，提供统一的调用框架和错误处理"""

    name: str = "BaseAgent"

    def __init__(self, client: OpenAIClient):
        self.client = client

    def _call(
        self,
        system_prompt: str,
        user_message: str,
        image_data: Optional[bytes] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> AgentResult:
        """统一 API 调用入口，自动封装成功/失败的 AgentResult"""
        result = AgentResult(agent_name=self.name)
        try:
            raw = self.client.chat(
                system_prompt=system_prompt,
                user_message=user_message,
                image_data=image_data,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            result.raw_response = raw
            result.usage = dict(self.client.last_usage)
            result.data = _parse_json(raw, self.name)
            result.success = bool(result.data)
            if not result.success:
                result.error = "JSON 解析失败：模型未返回有效 JSON"
        except Exception as e:
            result.error = str(e)
            result.success = False
            logger.error("[%s] call failed: %s", self.name, e, exc_info=True)
        return result


# ── Agent 1：视觉拆解师 ───────────────────────────────────────────────────────

class VisualAnalystAgent(BaseAgent):
    """
    Agent 1 · 视觉拆解师 (Visual Analyst)

    职责：多模态 Vision 分析，提取视频帧的 Hook 特征与视觉质量。

    输出 JSON 关键字段：
      hook_type         — Hook 类型（悬念式/痛点式/结果式等）
      hook_score        — Hook 质量评分（0-100）
      visual_score      — 视觉质量评分（0-100）
      first_3s_analysis — 前3秒画面描述
      emotional_tone    — 情绪基调
      key_visual_elements — 关键视觉元素列表
      hook_summary      — ≤60字一句话总结
    """

    name = "Agent1·视觉拆解师"

    def __init__(self, client: OpenAIClient):
        super().__init__(client)

    def run(self, image_data: bytes) -> AgentResult:
        """
        分析图片的 Hook 特征与视觉质量。

        Args:
            image_data: 图片字节（JPEG/PNG，来自 Streamlit file_uploader）

        Returns:
            AgentResult，data 包含 hook_type / hook_score / visual_score 等
        """
        logger.info("[%s] start visual analysis | size=%d bytes", self.name, len(image_data))

        user_message = (
            "请分析这张视频帧截图，重点提取前3秒 Hook 特征、视觉质量和情绪基调。"
            "严格按 System Prompt 要求输出 JSON，不要有任何额外文字。"
        )

        result = self._call(
            system_prompt=prompts.VISUAL_ANALYST,
            user_message=user_message,
            image_data=image_data,
            max_tokens=800,
            temperature=0.2,
        )

        if result.success:
            logger.info(
                "[%s] done | hook_type=%s hook_score=%s visual_score=%s",
                self.name,
                result.data.get("hook_type", "?"),
                result.data.get("hook_score", "?"),
                result.data.get("visual_score", "?"),
            )
        return result


# ── Agent 2：转化精算师 ───────────────────────────────────────────────────────

class CommerceOptimizerAgent(BaseAgent):
    """
    Agent 2 · 转化精算师 (Commerce Optimizer)

    职责：基于 Agent 1 视觉结果 + RAG 品牌知识库，生成 3 套高转化商业脚本。
    必须在 Agent 1 完成后串行执行（Phase 2）。

    输出 JSON 关键字段：
      virality_score      — 病毒潜力评分（0-100）
      conversion_potential — 转化潜力评分（0-100）
      scripts             — 3套脚本（hook / body / cta）
      rag_references      — 引用的知识库片段
      optimization_summary — ≤80字优化逻辑说明
    """

    name = "Agent2·转化精算师"

    def __init__(self, client: OpenAIClient, rag: RAGService):
        super().__init__(client)
        self.rag = rag

    def run(
        self,
        image_data: bytes,
        visual_result: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """
        生成品牌专属商业脚本。

        Args:
            image_data:    图片字节（用于 Vision 补充分析）
            visual_result: Agent 1 输出的 data 字典（可为 None，降级处理）

        Returns:
            AgentResult，data 包含 scripts 列表等
        """
        logger.info("[%s] start commerce optimization", self.name)

        # 构建 Agent 1 摘要（若 Agent 1 成功则注入，否则提示模型独立分析）
        if visual_result:
            visual_summary = (
                f"【Agent 1 视觉分析结果】\n"
                f"  Hook 类型：{visual_result.get('hook_type', '未知')}\n"
                f"  Hook 评分：{visual_result.get('hook_score', '?')}/100\n"
                f"  视觉质量：{visual_result.get('visual_score', '?')}/100\n"
                f"  情绪基调：{visual_result.get('emotional_tone', '未知')}\n"
                f"  一句话总结：{visual_result.get('hook_summary', '无')}\n"
                f"  关键元素：{', '.join(visual_result.get('key_visual_elements', []))}"
            )
        else:
            visual_summary = "【Agent 1 视觉分析结果】暂不可用，请基于图片自行进行视觉判断。"

        # RAG 检索品牌知识库
        rag_query = (
            visual_result.get("hook_summary", "爆款视频转化脚本")
            if visual_result
            else "爆款视频转化脚本"
        )
        rag_context = self.rag.build_context(rag_query)

        user_message = (
            f"{visual_summary}\n\n"
            f"{rag_context}\n\n"
            "请根据以上视觉分析和品牌知识库，生成3套高转化商业脚本。"
            "严格按 System Prompt 要求输出 JSON，不要有任何额外文字。"
        )

        result = self._call(
            system_prompt=prompts.COMMERCE_OPTIMIZER,
            user_message=user_message,
            image_data=image_data,
            max_tokens=2000,
            temperature=0.5,  # 脚本创作需要更多创意
        )

        if result.success:
            logger.info(
                "[%s] done | virality=%s conversion=%s scripts=%d",
                self.name,
                result.data.get("virality_score", "?"),
                result.data.get("conversion_potential", "?"),
                len(result.data.get("scripts", [])),
            )
        return result


# ── Agent 3：合规排雷兵 ───────────────────────────────────────────────────────

class ComplianceAuditorAgent(BaseAgent):
    """
    Agent 3 · 合规排雷兵 (Compliance Auditor)

    职责：多模态 Vision 扫描，检测 TikTok / 抖音合规风险。
    与 Agent 1 在 Phase 1 中 asyncio.gather 真正并发执行。

    检测范围：极限词 / 医疗声称 / 金融承诺 / 虚假宣传 / 身份冒充
    风险等级：LOW（绿色）/ MEDIUM（黄色）/ HIGH（红色）

    输出 JSON 关键字段：
      risk_level       — 整体风险等级（LOW/MEDIUM/HIGH）
      compliance_score — 合规评分（0-100，越高越合规）
      violations       — 违规项列表（text/type/severity/suggestion）
      platform_notes   — TikTok 和抖音分平台说明
      audit_summary    — ≤60字审计结论
    """

    name = "Agent3·合规排雷兵"

    def __init__(self, client: OpenAIClient):
        super().__init__(client)

    def run(self, image_data: bytes) -> AgentResult:
        """
        扫描图片中的合规风险。

        Args:
            image_data: 图片字节

        Returns:
            AgentResult，data 包含 risk_level / violations 等
        """
        logger.info("[%s] start compliance audit | size=%d bytes", self.name, len(image_data))

        user_message = (
            "请仔细扫描这张视频帧截图中可见的所有文字和视觉元素，"
            "检查是否存在 TikTok / 抖音社区规范或广告法违规风险。"
            "若画面中无可见文字，重点分析视觉元素是否涉及虚假宣传或身份冒充。"
            "严格按 System Prompt 要求输出 JSON，不要有任何额外文字。"
        )

        result = self._call(
            system_prompt=prompts.COMPLIANCE_AUDITOR,
            user_message=user_message,
            image_data=image_data,
            max_tokens=1000,
            temperature=0.1,  # 合规判断要求高一致性，低温度
        )

        if result.success:
            risk = result.data.get("risk_level", "?")
            violations = result.data.get("violations", [])
            logger.info(
                "[%s] done | risk=%s violations=%d compliance_score=%s",
                self.name,
                risk,
                len(violations),
                result.data.get("compliance_score", "?"),
            )
        return result


# ── Agent 4：策略执行官 ───────────────────────────────────────────────────────

class StrategyOptimizerAgent(BaseAgent):
    """
    Agent 4 · 策略执行官 (Strategy Optimizer)

    职责：汇总 Agent 1/2/3 的全部输出，输出最终可执行战略裁决。
    必须在三个 Agent 全部完成后串行执行（Phase 3）。

    核心产出：
      confidence_score — 综合成功置信度（0-100）
      verdict          — 最终裁决（建议复刻/谨慎复刻/不建议复刻）
      ab_test          — A/B 测试方案（对照组 vs 实验组）
      key_insights     — 3条高密度可执行结论
      executive_summary — ≤100字战略结论

    输入：Agent 1/2/3 的 data 字典（可为 None，降级处理）
    """

    name = "Agent4·策略执行官"

    def __init__(self, client: OpenAIClient):
        super().__init__(client)

    def run(
        self,
        image_data: bytes,
        visual_result: Optional[Dict[str, Any]] = None,
        commerce_result: Optional[Dict[str, Any]] = None,
        compliance_result: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """
        基于三路 Agent 输出，生成最终战略决策。

        Args:
            image_data:        图片字节（用于 Vision 辅助判断）
            visual_result:     Agent 1 data 字典
            commerce_result:   Agent 2 data 字典
            compliance_result: Agent 3 data 字典

        Returns:
            AgentResult，data 包含 confidence_score / ab_test / executive_summary 等
        """
        logger.info("[%s] start strategy synthesis", self.name)

        # 构建上游三路输出摘要
        sections: List[str] = []

        if visual_result:
            sections.append(
                f"【Agent 1 · 视觉拆解师 输出】\n"
                f"  Hook 类型：{visual_result.get('hook_type', '未知')}\n"
                f"  Hook 评分：{visual_result.get('hook_score', '?')}/100\n"
                f"  视觉质量：{visual_result.get('visual_score', '?')}/100\n"
                f"  前3秒分析：{visual_result.get('first_3s_analysis', '无')}\n"
                f"  情绪基调：{visual_result.get('emotional_tone', '未知')}\n"
                f"  关键元素：{', '.join(visual_result.get('key_visual_elements', []))}\n"
                f"  一句话总结：{visual_result.get('hook_summary', '无')}"
            )
        else:
            sections.append("【Agent 1 · 视觉拆解师 输出】不可用")

        if commerce_result:
            scripts_preview = ""
            for i, s in enumerate(commerce_result.get("scripts", [])[:3], 1):
                scripts_preview += (
                    f"\n  脚本{i}「{s.get('title', '')}」\n"
                    f"    Hook：{s.get('hook', '')}\n"
                    f"    CTA：{s.get('cta', '')}"
                )
            sections.append(
                f"【Agent 2 · 转化精算师 输出】\n"
                f"  病毒潜力：{commerce_result.get('virality_score', '?')}/100\n"
                f"  转化潜力：{commerce_result.get('conversion_potential', '?')}/100\n"
                f"  优化逻辑：{commerce_result.get('optimization_summary', '无')}"
                f"{scripts_preview}"
            )
        else:
            sections.append("【Agent 2 · 转化精算师 输出】不可用")

        if compliance_result:
            violations = compliance_result.get("violations", [])
            v_summary = (
                "、".join(
                    f"{v.get('type','?')}({v.get('severity','?')})"
                    for v in violations[:3]
                )
                if violations
                else "无违规项"
            )
            sections.append(
                f"【Agent 3 · 合规排雷兵 输出】\n"
                f"  风险等级：{compliance_result.get('risk_level', '?')}\n"
                f"  合规评分：{compliance_result.get('compliance_score', '?')}/100\n"
                f"  违规项（前3条）：{v_summary}\n"
                f"  TikTok 说明：{compliance_result.get('platform_notes', {}).get('tiktok', '无')}\n"
                f"  审计结论：{compliance_result.get('audit_summary', '无')}"
            )
        else:
            sections.append("【Agent 3 · 合规排雷兵 输出】不可用")

        user_message = (
            "\n\n".join(sections)
            + "\n\n请基于以上三路 Agent 分析，综合输出最终战略裁决。"
            "严格按 System Prompt 要求输出 JSON，不要有任何额外文字。"
        )

        result = self._call(
            system_prompt=prompts.STRATEGY_OPTIMIZER,
            user_message=user_message,
            image_data=image_data,
            max_tokens=1800,
            temperature=0.3,
        )

        if result.success:
            logger.info(
                "[%s] done | confidence=%s verdict=%s",
                self.name,
                result.data.get("confidence_score", "?"),
                result.data.get("verdict", "?"),
            )
        return result
