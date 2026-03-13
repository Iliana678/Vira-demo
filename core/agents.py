"""
core/agents.py
四个专家 Agent 类定义。

Agent 1 · 视觉拆解师    — 多模态 Hook 特征提取
Agent 2 · 转化精算师    — RAG 增强商业重构脚本
Agent 3 · 合规排雷兵    — TikTok/抖音风控红线扫描（内置品牌风控字典）
Agent 4 · 策略执行官    — 汇总三路输出，生成 A/B Test 实验设计与成功置信度
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from prompts import (
    VISUAL_ANALYST,
    COMMERCE_OPTIMIZER,
    COMPLIANCE_AUDITOR,
    STRATEGY_OPTIMIZER,
)
from services.openai_client import OpenAIClient
from services.rag import RAGService

logger = logging.getLogger(__name__)


# ── TikTok 品牌风控红线字典 ──────────────────────────────────────────────────
# 用于 Agent 3 精准识别商业化禁忌词，演示时可直接向面试官展示此字典。
# 生产环境建议从数据库/配置文件动态加载，支持按行业/平台分类。

TIKTOK_RISK_DICT: Dict[str, List[str]] = {
    "绝对化词汇（必须避免）": [
        "第一", "最好", "最强", "最大", "最小", "最快", "最慢",
        "最安全", "最权威", "最专业", "最低价", "最高效",
        "唯一", "绝对", "完全", "100%", "无与伦比", "史无前例",
        "全国第一", "行业第一", "世界领先",
    ],
    "医疗效果暗示（高风险）": [
        "治疗", "治愈", "根治", "消除疾病", "药到病除",
        "医学证明", "临床验证", "临床实验", "诊断", "处方",
        "疗效显著", "康复", "抗癌", "降血糖", "降血压",
        "改善记忆力", "提高免疫力", "排毒", "解毒",
    ],
    "金融收益承诺（严禁）": [
        "保证回报", "稳赚不赔", "无风险投资", "100%盈利",
        "保本保息", "日赚万元", "月入十万", "躺赚",
        "被动收入保证", "财务自由", "轻松月入",
    ],
    "虚假宣传（需核实）": [
        "限时特价", "最后X件", "秒杀价",  # 无截止日期/真实库存限制时违规
        "真实案例", "用户反馈", "亲测有效",  # 无法核实时违规
        "专利技术", "独家配方", "秘制",  # 无证书时违规
    ],
    "身份冒充（严禁）": [
        "官方认证", "国家级", "政府推荐", "国家专利",
        "院士推荐", "诺贝尔奖", "专家强烈推荐",
        "某某医院专用", "明星同款（未授权）",
    ],
    "TikTok 平台专项禁忌": [
        "点击链接购买", "加微信", "扫码联系",  # 导流违规
        "刷礼物", "给我打钱",  # 诱导充值
        "关注涨粉", "互粉", "刷量",  # 虚假互动
    ],
}


def _format_risk_dict_for_prompt() -> str:
    """将风控字典格式化为 Prompt 注入格式，便于模型精准对照检查。"""
    lines = ["【TikTok 品牌风控红线字典（内置规则库 v1.0）】"]
    for category, terms in TIKTOK_RISK_DICT.items():
        lines.append(f"\n▸ {category}")
        lines.append("  禁用词/短语：" + "、".join(f'"{t}"' for t in terms))
    return "\n".join(lines)


# ── 通用数据结构 ──────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    """Agent 执行结果的标准化封装"""
    agent_name: str
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    raw_response: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _extract_json(text: str) -> dict:
    """
    从 LLM 输出中鲁棒地提取 JSON。
    兼容：纯 JSON、Markdown code block、JSON 前后有多余文字。
    """
    if not text:
        raise ValueError("空响应")

    # 1. 去除 Markdown code block 包装
    md_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if md_match:
        text = md_match.group(1).strip()

    # 2. 提取第一个完整 JSON 对象（贪婪匹配，兼容嵌套）
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        return json.loads(brace_match.group(0))

    # 3. 直接尝试解析
    return json.loads(text.strip())


# ── Agent 1：视觉拆解师 ───────────────────────────────────────────────────────

class VisualAnalystAgent:
    """
    Agent 1 · 视觉拆解师
    职责：多模态视觉特征提取，评估 Hook 质量与情绪基调。
    """

    NAME = "视觉拆解师"

    def __init__(self, client: OpenAIClient):
        self.client = client

    def run(self, image_data: bytes, extra_context: str = "") -> AgentResult:
        user_msg = "请分析这些视频帧图像，重点评估前3秒的视觉 Hook 设计与情绪基调。"
        if extra_context:
            user_msg += f"\n\n额外背景：{extra_context}"

        try:
            raw = self.client.chat(
                system_prompt=VISUAL_ANALYST,
                user_message=user_msg,
                image_data=image_data,
                max_tokens=800,
            )
            data = _extract_json(raw)
            logger.info("VisualAnalyst OK | hook_type=%s score=%s", data.get("hook_type"), data.get("hook_score"))
            return AgentResult(self.NAME, True, data, raw, usage=self.client.last_usage)
        except Exception as e:
            logger.error("VisualAnalyst FAILED: %s", e)
            return AgentResult(self.NAME, False, error=str(e), raw_response=str(e))


# ── Agent 2：转化精算师 ───────────────────────────────────────────────────────

class CommerceOptimizerAgent:
    """
    Agent 2 · 转化精算师
    职责：RAG 增强的商业重构脚本生成，依赖 Agent 1 的视觉分析结果。
    """

    NAME = "转化精算师"

    def __init__(self, client: OpenAIClient, rag: RAGService):
        self.client = client
        self.rag = rag

    def run(
        self,
        image_data: bytes,
        visual_result: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        rag_context = self.rag.build_context("爆款视频商业化转化脚本结构策略")
        visual_summary = json.dumps(visual_result or {}, ensure_ascii=False, indent=2)
        user_msg = (
            f"请基于以下视觉分析结果和品牌知识库，生成 3 套商业重构脚本：\n\n"
            f"【视觉分析摘要】\n{visual_summary}\n\n{rag_context}"
        )
        try:
            raw = self.client.chat(
                system_prompt=COMMERCE_OPTIMIZER,
                user_message=user_msg,
                image_data=image_data,
                max_tokens=1500,
            )
            data = _extract_json(raw)
            logger.info(
                "CommerceOptimizer OK | virality=%s scripts=%d",
                data.get("virality_score"), len(data.get("scripts", [])),
            )
            return AgentResult(self.NAME, True, data, raw, usage=self.client.last_usage)
        except Exception as e:
            logger.error("CommerceOptimizer FAILED: %s", e)
            return AgentResult(self.NAME, False, error=str(e), raw_response=str(e))


# ── Agent 3：合规排雷兵 ───────────────────────────────────────────────────────

class ComplianceAuditorAgent:
    """
    Agent 3 · 合规排雷兵
    职责：对照内置 TikTok 品牌风控红线字典 + 平台规范，进行精准合规扫描。

    核心差异：
    - 将 TIKTOK_RISK_DICT 完整注入 Prompt，模型可逐类对照检查
    - 演示效果：面试官可看到 Agent 如何"精准命中"字典中的禁用词
    """

    NAME = "合规排雷兵"

    def __init__(self, client: OpenAIClient):
        self.client = client
        # 一次性格式化，避免每次调用重复计算
        self._risk_dict_prompt = _format_risk_dict_for_prompt()

    def run(self, image_data: bytes) -> AgentResult:
        # 将风控字典注入 User Message，让模型"手持字典"逐条对照
        user_msg = (
            "请使用以下品牌风控红线字典，对视频帧中所有可见文字和画面内容进行逐类扫描，"
            "精准标记命中的禁用词或违规图案，给出风险等级和具体修改建议。\n\n"
            f"{self._risk_dict_prompt}"
        )
        try:
            raw = self.client.chat(
                system_prompt=COMPLIANCE_AUDITOR,
                user_message=user_msg,
                image_data=image_data,
                max_tokens=1000,
            )
            data = _extract_json(raw)

            # 将风控字典摘要附加到结果中，UI 可单独展示
            data["_risk_dict_categories"] = list(TIKTOK_RISK_DICT.keys())
            data["_total_rules"] = sum(len(v) for v in TIKTOK_RISK_DICT.values())

            logger.info(
                "ComplianceAuditor OK | risk=%s score=%s violations=%d",
                data.get("risk_level"), data.get("compliance_score"), len(data.get("violations", [])),
            )
            return AgentResult(self.NAME, True, data, raw, usage=self.client.last_usage)
        except Exception as e:
            logger.error("ComplianceAuditor FAILED: %s", e)
            return AgentResult(self.NAME, False, error=str(e), raw_response=str(e))


# ── Agent 4：策略执行官 ───────────────────────────────────────────────────────

class StrategyOptimizerAgent:
    """
    Agent 4 · 策略执行官 (Strategy Optimizer)
    职责：整合前三个 Agent 的全部输出，作为流水线的最终决策节点。

    核心产出：
    - 成功置信度 (Confidence Score 0-100)
    - A/B Test 实验设计（Control Group vs Test Group）
    - 关键战略洞察（3条）
    - 最终裁决 Executive Summary
    """

    NAME = "策略执行官"

    def __init__(self, client: OpenAIClient):
        self.client = client

    def run(
        self,
        image_data: bytes,
        visual_result: Optional[Dict[str, Any]] = None,
        commerce_result: Optional[Dict[str, Any]] = None,
        compliance_result: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """
        Args:
            image_data:         图片字节（可选，提供视觉上下文）
            visual_result:      Agent 1 的 data 字典
            commerce_result:    Agent 2 的 data 字典
            compliance_result:  Agent 3 的 data 字典
        """
        # 组装三路 Agent 的完整上下文
        all_results = {
            "Agent1_视觉拆解": visual_result or {},
            "Agent2_转化精算": commerce_result or {},
            "Agent3_合规排雷": compliance_result or {},
        }
        user_msg = (
            "以下是 VIRA 三位专家 Agent 对同一竞品视频的完整分析结果，"
            "请综合所有信息，输出最终的战略决策报告：\n\n"
            + json.dumps(all_results, ensure_ascii=False, indent=2)
        )

        try:
            raw = self.client.chat(
                system_prompt=STRATEGY_OPTIMIZER,
                user_message=user_msg,
                image_data=image_data,  # 附上原图，给模型最直接的视觉参考
                max_tokens=1200,
            )
            data = _extract_json(raw)
            logger.info(
                "StrategyOptimizer OK | confidence=%s verdict=%s",
                data.get("confidence_score"), data.get("verdict"),
            )
            return AgentResult(self.NAME, True, data, raw, usage=self.client.last_usage)
        except Exception as e:
            logger.error("StrategyOptimizer FAILED: %s", e)
            return AgentResult(self.NAME, False, error=str(e), raw_response=str(e))
