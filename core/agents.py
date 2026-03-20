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

JSON 契约（每个 Agent 的核心输出字段）：
  Agent 1 → hook_score(0-10), hook_type, key_visual_elements, emotion_tone, weakness
  Agent 2 → conversion_score(0-10), scripts(list), best_angle
  Agent 3 → risk_level(低/中/高), violation_keywords(list), suggestion
  Agent 4 → success_confidence(0-100), final_verdict, ab_test_plan(dict), top3_improvements(list)

Self-Reflection 机制（Agent 1 & Agent 4）：
  首次输出后，由同一模型扮演 Critic 角色检查输出质量。
  Critic 不通过时，将反馈注入原 prompt 重写一次（最多 1 次，避免循环）。
  结果通过 AgentResult.reflected / .critic_issues 透出，供 UI 展示。
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.openai_client import OpenAIClient
from services.rag import RAGService
import prompts

logger = logging.getLogger(__name__)


# ── 各 Agent 的默认兜底结构 ───────────────────────────────────────────────────
# JSON 解析失败时返回此结构，保证下游不崩溃

_DEFAULT_VISUAL: Dict[str, Any] = {
    "hook_score": 0,
    "hook_type": "未知",
    "key_visual_elements": [],
    "emotion_tone": "未知",
    "weakness": "分析失败，请重试",
}

_DEFAULT_COMMERCE: Dict[str, Any] = {
    "conversion_score": 0,
    "best_angle": "分析失败，请重试",
    "scripts": [],
}

_DEFAULT_COMPLIANCE: Dict[str, Any] = {
    "risk_level": "中",
    "violation_keywords": [],
    "suggestion": "分析失败，请人工审核",
}

_DEFAULT_STRATEGY: Dict[str, Any] = {
    "success_confidence": 0,
    "final_verdict": "分析失败，请重试",
    "ab_test_plan": {
        "control_group": "",
        "test_group": "",
        "success_metric": "",
        "duration": "",
    },
    "top3_improvements": [],
}

# 按 agent_name 快速索引默认结构
_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "Agent1·视觉拆解师": _DEFAULT_VISUAL,
    "Agent2·转化精算师": _DEFAULT_COMMERCE,
    "Agent3·合规排雷兵": _DEFAULT_COMPLIANCE,
    "Agent4·策略执行官": _DEFAULT_STRATEGY,
}


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

    # Self-Reflection 字段
    reflected: bool = False           # 是否经过 Critic 审核后被修正
    critic_issues: List[str] = field(default_factory=list)  # Critic 发现的原始问题列表

    # 执行耗时（ms），由 _call 负责填写
    elapsed_ms: int = 0

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

    Returns:
        解析成功时返回 dict；失败时返回空 dict {}
    """
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

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
    """所有 Agent 的基类，提供统一的调用框架、重试机制和错误处理"""

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
        """
        统一 API 调用入口，内置一次自动重试。

        流程：
          1. 首次调用 → 尝试解析 JSON
          2. 如解析失败，立即重试一次（相同参数）
          3. 重试仍失败 → 返回对应 Agent 的默认兜底结构，success=False
          4. 任何异常 → 返回默认兜底结构，success=False，不抛出
        """
        result = AgentResult(agent_name=self.name)
        default = _DEFAULTS.get(self.name, {})
        _t_start = time.perf_counter()

        for attempt in range(2):
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
                parsed = _parse_json(raw, self.name)

                if parsed:
                    result.data = parsed
                    result.success = True
                    result.error = ""
                    result.elapsed_ms = round((time.perf_counter() - _t_start) * 1000)
                    return result

                # 解析失败，准备重试
                if attempt == 0:
                    logger.warning(
                        "[%s] JSON parse failed on attempt 1, retrying...", self.name
                    )
                else:
                    logger.error(
                        "[%s] JSON parse failed after retry. Using default fallback.", self.name
                    )
                    result.data = dict(default)
                    result.success = False
                    result.error = "JSON 解析失败（已重试一次），已返回默认兜底结构"

            except Exception as e:
                logger.error(
                    "[%s] API call failed (attempt %d): %s", self.name, attempt + 1, e,
                    exc_info=True,
                )
                result.error = str(e)
                result.success = False
                if attempt == 1:
                    result.data = dict(default)

        result.elapsed_ms = round((time.perf_counter() - _t_start) * 1000)
        return result

    def _call_with_reflection(
        self,
        system_prompt: str,
        user_message: str,
        critic_system_prompt: str,
        image_data: Optional[bytes] = None,
        max_tokens: int = 1500,
        temperature: float = 0.3,
    ) -> AgentResult:
        """
        带 Self-Reflection 的调用入口（Agent 1 / Agent 4 专用）。

        执行流程：
          Step 1 — 首次调用（内含 JSON 解析重试，同 _call）
          Step 2 — 将输出交给 Critic（同一模型，不同 system prompt）做质量审核
                   Critic 只需一次调用，失败则跳过反思（安全降级）
          Step 3 — 若 Critic 判定不通过，把问题列表注入原 user_message 末尾，
                   触发一次修正重写（仍走 _call，内含重试）
          Step 4 — 修正成功：result.reflected=True，result.critic_issues 记录原始问题
                   修正失败：返回 Step 1 的原始结果，不崩溃

        约束：最多 1 次反思，Critic 自身不再反思（防止递归）。
        """
        # ── Step 1: 首次输出 ─────────────────────────────────────────────────
        result = self._call(
            system_prompt, user_message, image_data, max_tokens, temperature
        )
        if not result.success:
            return result

        # ── Step 2: Critic 质量审核（纯文本，不传图片）─────────────────────
        critic_input = (
            "以下是待审核的 JSON 输出，请严格按标准逐条审核：\n\n"
            + json.dumps(result.data, ensure_ascii=False, indent=2)
        )
        try:
            critic_raw = self.client.chat(
                system_prompt=critic_system_prompt,
                user_message=critic_input,
                image_data=None,
                max_tokens=400,
                temperature=0.1,
            )
            critic_data = _parse_json(critic_raw, self.name + "·Critic")
        except Exception as exc:
            logger.warning(
                "[%s] Critic call failed (%s), skipping reflection", self.name, exc
            )
            return result

        if not critic_data or critic_data.get("pass", True):
            logger.info("[%s] Critic: PASS — no reflection needed", self.name)
            return result

        # ── Step 3: 反思修正 ─────────────────────────────────────────────────
        issues      = critic_data.get("issues", [])
        suggestions = critic_data.get("suggestions", [])
        result.critic_issues = issues  # 保存 Critic 发现的问题（即使修正版本被采用）

        feedback = (
            "\n\n【⚠️ 质量审核反馈 — 请修正以下问题后重新输出完整 JSON】\n"
            "Critic 发现的问题：\n"
            + "\n".join(f"  · {i}" for i in issues)
            + "\nCritic 修正建议：\n"
            + "\n".join(f"  · {s}" for s in suggestions)
            + "\n\n请严格基于上述反馈重新分析，确保每个字段有具体依据、无空字符串、"
            "weakness/improvements 描述精确可执行。输出完整 JSON，不要只输出修改部分。"
        )

        logger.info(
            "[%s] Critic: FAIL (%d issues) → triggering self-reflection",
            self.name, len(issues),
        )

        revised = self._call(
            system_prompt=system_prompt,
            user_message=user_message + feedback,
            image_data=image_data,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        if revised.success:
            revised.reflected     = True
            revised.critic_issues = issues
            logger.info("[%s] Self-reflection complete | reflected=True", self.name)
            return revised

        # 修正失败 → 安全降级，返回首次原始结果
        logger.warning(
            "[%s] Revision call failed — returning original first-pass output", self.name
        )
        return result


# ── Agent 1：视觉拆解师 ───────────────────────────────────────────────────────

class VisualAnalystAgent(BaseAgent):
    """
    Agent 1 · 视觉拆解师 (Visual Analyst)

    职责：多模态 Vision 分析，提取视频帧的 Hook 特征与视觉质量。

    输出 JSON 核心字段：
      hook_score          — Hook 吸引力评分（0-10）
      hook_type           — Hook 类型（悬念式/痛点式/结果式等）
      key_visual_elements — 关键视觉元素列表
      emotion_tone        — 情绪基调
      weakness            — 当前视频最大短板（≤50字）
    """

    name = "Agent1·视觉拆解师"

    def run(self, image_data: bytes) -> AgentResult:
        """
        分析图片的 Hook 特征与视觉质量。

        Args:
            image_data: 图片字节（JPEG/PNG，来自 Streamlit file_uploader）

        Returns:
            AgentResult，data 包含 hook_score / hook_type / key_visual_elements 等
        """
        logger.info("[%s] start visual analysis | size=%d bytes", self.name, len(image_data))

        user_message = (
            "请分析这张视频帧截图，重点提取前3秒 Hook 特征、视觉质量和情绪基调。"
            "你的回答必须是且仅是一个合法 JSON 对象，不得包含任何其他文字。"
        )

        result = self._call_with_reflection(
            system_prompt=prompts.VISUAL_ANALYST,
            user_message=user_message,
            critic_system_prompt=prompts.VISUAL_CRITIC,
            image_data=image_data,
            max_tokens=600,
            temperature=0.2,
        )

        if result.success:
            logger.info(
                "[%s] done | hook_type=%s hook_score=%s reflected=%s",
                self.name,
                result.data.get("hook_type", "?"),
                result.data.get("hook_score", "?"),
                result.reflected,
            )
        return result


# ── Agent 2：转化精算师 ───────────────────────────────────────────────────────

class CommerceOptimizerAgent(BaseAgent):
    """
    Agent 2 · 转化精算师 (Commerce Optimizer)

    职责：基于 Agent 1 视觉结果 + RAG 品牌知识库，生成 3 套高转化商业脚本。
    必须在 Agent 1 完成后串行执行（Phase 2）。

    输出 JSON 核心字段：
      conversion_score — 综合转化潜力评分（0-10）
      best_angle       — 最具爆发潜力的切入角度说明（≤40字）
      scripts          — 3套脚本列表（title/hook/scenes/cta/influencer_type）
    """

    name = "Agent2·转化精算师"

    def __init__(self, client: OpenAIClient, rag: RAGService):
        super().__init__(client)
        self.rag = rag

    def run(
        self,
        image_data: bytes,
        visual_result: Optional[Dict[str, Any]] = None,
        brand_context: str = "",
    ) -> AgentResult:
        """
        生成品牌专属商业脚本。

        Args:
            image_data:     图片字节（用于 Vision 补充分析）
            visual_result:  Agent 1 输出的 data 字典（可为 None，降级处理）
            brand_context:  品牌知识库格式化文本（来自 services.brand_kb.format_brand_context）

        Returns:
            AgentResult，data 包含 conversion_score / scripts / best_angle
        """
        logger.info("[%s] start commerce optimization | has_brand=%s", self.name, bool(brand_context))

        if visual_result:
            visual_summary = (
                f"【Agent 1 视觉分析结果】\n"
                f"  Hook 类型：{visual_result.get('hook_type', '未知')}\n"
                f"  Hook 评分：{visual_result.get('hook_score', '?')}/10\n"
                f"  情绪基调：{visual_result.get('emotion_tone', '未知')}\n"
                f"  关键元素：{', '.join(visual_result.get('key_visual_elements', []))}\n"
                f"  短板：{visual_result.get('weakness', '无')}"
            )
        else:
            visual_summary = "【Agent 1 视觉分析结果】暂不可用，请基于图片自行进行视觉判断。"

        rag_query = (
            visual_result.get("hook_type", "爆款视频转化脚本")
            if visual_result
            else "爆款视频转化脚本"
        )
        rag_context = self.rag.build_context(rag_query)

        user_message = (
            f"{visual_summary}\n\n"
            f"{rag_context}\n\n"
            "请根据以上视觉分析和品牌知识库，生成3套专属拍摄脚本。"
            "你的回答必须是且仅是一个合法 JSON 对象，不得包含任何其他文字。"
        )

        brand_section = brand_context if brand_context else "【品牌知识库】未提供，使用通用模式分析，脚本不含特定品牌信息。"
        system_prompt = prompts.COMMERCE_OPTIMIZER.format(brand_context=brand_section)

        result = self._call(
            system_prompt=system_prompt,
            user_message=user_message,
            image_data=image_data,
            max_tokens=2500,
            temperature=0.5,
        )

        if result.success:
            logger.info(
                "[%s] done | conversion_score=%s scripts=%d best_angle=%s",
                self.name,
                result.data.get("conversion_score", "?"),
                len(result.data.get("scripts", [])),
                result.data.get("best_angle", "?")[:30] if result.data.get("best_angle") else "?",
            )
        return result


# ── Agent 3：合规排雷兵 ───────────────────────────────────────────────────────

class ComplianceAuditorAgent(BaseAgent):
    """
    Agent 3 · 合规排雷兵 (Compliance Auditor)

    职责：多模态 Vision 扫描，检测 TikTok / 抖音合规风险。
    与 Agent 1 在 Phase 1 中 asyncio.gather 真正并发执行。

    检测范围：极限词 / 医疗声称 / 金融承诺 / 虚假宣传 / 身份冒充
    风险等级：低（可发布）/ 中（需修改）/ 高（不可发布）

    输出 JSON 核心字段：
      risk_level         — 整体风险等级（低/中/高）
      violation_keywords — 被标记的违规词或短语列表
      suggestion         — 具体整改建议（≤100字）
    """

    name = "Agent3·合规排雷兵"

    def run(self, image_data: bytes) -> AgentResult:
        """
        扫描图片中的合规风险。

        Args:
            image_data: 图片字节

        Returns:
            AgentResult，data 包含 risk_level / violation_keywords / suggestion
        """
        logger.info("[%s] start compliance audit | size=%d bytes", self.name, len(image_data))

        user_message = (
            "请仔细扫描这张视频帧截图中可见的所有文字和视觉元素，"
            "检查是否存在 TikTok / 抖音社区规范或广告法违规风险。"
            "若画面中无可见文字，重点分析视觉元素是否涉及虚假宣传或身份冒充。"
            "你的回答必须是且仅是一个合法 JSON 对象，不得包含任何其他文字。"
        )

        result = self._call(
            system_prompt=prompts.COMPLIANCE_AUDITOR,
            user_message=user_message,
            image_data=image_data,
            max_tokens=600,
            temperature=0.1,
        )

        if result.success:
            logger.info(
                "[%s] done | risk=%s violation_keywords=%d",
                self.name,
                result.data.get("risk_level", "?"),
                len(result.data.get("violation_keywords", [])),
            )
        return result


# ── Agent 4：策略执行官 ───────────────────────────────────────────────────────

class StrategyOptimizerAgent(BaseAgent):
    """
    Agent 4 · 策略执行官 (Strategy Optimizer)

    职责：汇总 Agent 1/2/3 的全部输出，输出最终可执行战略裁决。
    必须在三个 Agent 全部完成后串行执行（Phase 3）。

    核心产出：
      success_confidence — 综合成功置信度（0-100）
      final_verdict      — 最终裁决（建议复刻/谨慎复刻/不建议复刻 + 理由）
      ab_test_plan       — A/B 测试方案（对照组/实验组/指标/周期）
      top3_improvements  — 3条高密度可执行改进建议

    输入：Agent 1/2/3 的 data 字典（可为 None，降级处理）
    """

    name = "Agent4·策略执行官"

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
            AgentResult，data 包含 success_confidence / final_verdict / ab_test_plan / top3_improvements
        """
        logger.info("[%s] start strategy synthesis", self.name)

        sections: List[str] = []

        if visual_result:
            sections.append(
                f"【Agent 1 · 视觉拆解师 输出】\n"
                f"  Hook 类型：{visual_result.get('hook_type', '未知')}\n"
                f"  Hook 评分：{visual_result.get('hook_score', '?')}/10\n"
                f"  情绪基调：{visual_result.get('emotion_tone', '未知')}\n"
                f"  关键元素：{', '.join(visual_result.get('key_visual_elements', []))}\n"
                f"  短板：{visual_result.get('weakness', '无')}"
            )
        else:
            sections.append("【Agent 1 · 视觉拆解师 输出】不可用")

        if commerce_result:
            scripts_preview = ""
            for i, s in enumerate(commerce_result.get("scripts", [])[:3], 1):
                scripts_preview += (
                    f"\n  脚本{i}「{s.get('title', '')}」"
                    f" Hook：{s.get('hook', '')} | CTA：{s.get('cta', '')}"
                )
            sections.append(
                f"【Agent 2 · 转化精算师 输出】\n"
                f"  转化评分：{commerce_result.get('conversion_score', '?')}/10\n"
                f"  最佳切入：{commerce_result.get('best_angle', '无')}"
                f"{scripts_preview}"
            )
        else:
            sections.append("【Agent 2 · 转化精算师 输出】不可用")

        if compliance_result:
            kws = compliance_result.get("violation_keywords", [])
            kw_summary = "、".join(kws[:5]) if kws else "无"
            sections.append(
                f"【Agent 3 · 合规排雷兵 输出】\n"
                f"  风险等级：{compliance_result.get('risk_level', '?')}\n"
                f"  违规关键词：{kw_summary}\n"
                f"  整改建议：{compliance_result.get('suggestion', '无')}"
            )
        else:
            sections.append("【Agent 3 · 合规排雷兵 输出】不可用")

        user_message = (
            "\n\n".join(sections)
            + "\n\n请基于以上三路 Agent 分析，综合输出最终战略裁决。"
            "你的回答必须是且仅是一个合法 JSON 对象，不得包含任何其他文字。"
        )

        result = self._call_with_reflection(
            system_prompt=prompts.STRATEGY_OPTIMIZER,
            user_message=user_message,
            critic_system_prompt=prompts.STRATEGY_CRITIC,
            image_data=image_data,
            max_tokens=1200,
            temperature=0.3,
        )

        if result.success:
            logger.info(
                "[%s] done | success_confidence=%s final_verdict=%s reflected=%s",
                self.name,
                result.data.get("success_confidence", "?"),
                str(result.data.get("final_verdict", "?"))[:40],
                result.reflected,
            )
        return result
