"""
services/report_generator.py
分析报告生成器

输出格式：
  · Markdown  — 始终可用，0 依赖
  · PDF       — 可选，需要 fpdf2；不可用时自动降级为 Markdown

报告内容：
  封面 → 四 Agent 分析详情 → 爆款公式（如有）→ 口播文案（如有）→ 附录
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Markdown 生成 ─────────────────────────────────────────────────────────────

def build_markdown(
    wf,
    image_name:    str  = "",
    synthesis      = None,   # SynthesisResult | None
    transcript:    str  = "",
    user_email:    str  = "",
) -> str:
    """
    根据 WorkflowResult 生成完整 Markdown 报告。

    Args:
        wf            : WorkflowResult
        image_name    : 分析的图片文件名
        synthesis     : SynthesisResult（批量爆款公式，可选）
        transcript    : 视频口播文字（可选）
        user_email    : 当前用户邮箱（写入封面）

    Returns:
        Markdown 字符串
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []

    def h(level: int, text: str):
        lines.append(f"\n{'#' * level} {text}\n")

    def p(text: str):
        lines.append(f"{text}\n")

    def kv(key: str, val: Any):
        lines.append(f"- **{key}：** {val}")

    def hr():
        lines.append("\n---\n")

    # ── 封面 ─────────────────────────────────────────────────────────────────
    lines.append(f"# VIRA · 爆款侦察兵 分析报告\n")
    lines.append(f"> 生成时间：{now}  \n")
    if image_name:
        lines.append(f"> 分析对象：`{image_name}`  \n")
    if user_email:
        lines.append(f"> 分析人：{user_email}  \n")
    hr()

    # ── Agent 1：视觉拆解师 ────────────────────────────────────────────────
    h(2, "Agent 1 · 视觉拆解师")
    if wf.visual and wf.visual.success:
        d = wf.visual.data
        kv("Hook 类型",  d.get("hook_type", "—"))
        kv("Hook 评分",  f"{d.get('hook_score', '—')}/100")
        kv("视觉质量",   f"{d.get('visual_score', '—')}/100")
        kv("情绪基调",   d.get("emotional_tone", "—"))
        p("")
        h(3, "前 3 秒分析")
        p(d.get("first_3s_analysis", "—"))
        if d.get("key_visual_elements"):
            h(3, "关键视觉元素")
            for el in d["key_visual_elements"]:
                lines.append(f"- {el}")
    else:
        p("*Agent 1 未运行或失败*")
    hr()

    # ── Agent 3：合规排雷兵 ────────────────────────────────────────────────
    h(2, "Agent 3 · 合规排雷兵")
    if wf.compliance and wf.compliance.success:
        d = wf.compliance.data
        kv("风险等级",   d.get("risk_level", "—"))
        kv("合规评分",   f"{d.get('compliance_score', '—')}/100")
        viols = d.get("violations", [])
        if viols:
            h(3, "命中风险项")
            for v in viols:
                lines.append(
                    f"- [{v.get('type','?')}] **{v.get('severity','?')}** "
                    f"— {v.get('text','')}  \n  建议：{v.get('suggestion','—')}"
                )
        else:
            p("✅ 未命中任何风控规则，可安全发布")
    else:
        p("*Agent 3 未运行或失败*")
    hr()

    # ── Agent 2：转化精算师 ────────────────────────────────────────────────
    h(2, "Agent 2 · 转化精算师")
    if wf.commerce and wf.commerce.success:
        d = wf.commerce.data
        kv("病毒传播潜力", f"{d.get('virality_score','—')}/100")
        kv("商业转化潜力", f"{d.get('conversion_potential','—')}/100")
        p("")
        p(d.get("optimization_summary", ""))
        scripts = d.get("scripts", [])
        if scripts:
            h(3, "三套重构脚本")
            for i, s in enumerate(scripts, 1):
                h(4, f"方案 {i}：{s.get('title', f'脚本{i}')}")
                kv("🎬 前3秒 Hook",  s.get("hook", "—"))
                kv("📖 正文内容",    s.get("body", "—"))
                kv("🎯 CTA",        s.get("cta", "—"))
    else:
        p("*Agent 2 未运行或失败*")
    hr()

    # ── Agent 4：策略执行官 ────────────────────────────────────────────────
    h(2, "Agent 4 · 策略执行官")
    if wf.strategy and wf.strategy.success:
        d = wf.strategy.data
        kv("成功置信度", f"{d.get('confidence_score','—')}/100")
        kv("裁决结论",   d.get("verdict", "—"))
        p("")
        p(d.get("executive_summary", ""))
        ab = d.get("ab_test", {})
        if ab:
            h(3, "A/B Test 实验方案")
            ctrl = ab.get("control_group", {})
            test = ab.get("test_group", {})
            kv("对照组", ctrl.get("description", "—"))
            kv("实验组", test.get("description", "—"))
            kv("成功指标", ab.get("success_metric", "—"))
            kv("测试周期", ab.get("test_duration", "—"))
        insights = d.get("key_insights", [])
        if insights:
            h(3, "关键战略洞察")
            for ins in insights:
                lines.append(f"- {ins}")
        if d.get("risk_warning"):
            p(f"\n⚠️ **风险提示：** {d['risk_warning']}")
    else:
        p("*Agent 4 未运行或失败*")
    hr()

    # ── 爆款公式（批量时附加）──────────────────────────────────────────────
    if synthesis and synthesis.success:
        sd = synthesis.data
        h(2, "Agent 5 · 爆款公式提炼师")
        kv("爆款公式", sd.get("viral_formula", "—"))
        p("")
        p(sd.get("executive_summary", ""))
        md_doc = sd.get("methodology_doc", "")
        if md_doc:
            h(3, "方法论文档")
            p(md_doc)
        hr()

    # ── 视频口播（如有）────────────────────────────────────────────────────
    if transcript:
        h(2, "视频口播文案（Whisper 转录）")
        p(transcript)
        hr()

    # ── 执行摘要 ──────────────────────────────────────────────────────────
    lines.append(f"\n---\n*本报告由 VIRA · 爆款侦察兵 自动生成 · {now}*\n")

    return "\n".join(lines)


# ── PDF 生成（可选，依赖 fpdf2）──────────────────────────────────────────────

def build_pdf(markdown_text: str) -> Optional[bytes]:
    """
    将 Markdown 文本转为 PDF bytes。
    如果 fpdf2 未安装，返回 None（调用方降级为 Markdown 下载）。
    """
    try:
        from fpdf import FPDF  # type: ignore
    except ImportError:
        logger.info("fpdf2 not installed, PDF generation skipped")
        return None

    class _PDF(FPDF):
        def header(self):
            self.set_font("Helvetica", "B", 9)
            self.set_text_color(100, 100, 120)
            self.cell(0, 8, "VIRA · Viral Scout Report", align="R")
            self.ln(4)

        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(160, 160, 180)
            self.cell(0, 8, f"Page {self.page_no()}", align="C")

    pdf = _PDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margins(18, 18, 18)

    for line in markdown_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            pdf.set_font("Helvetica", "B", 16)
            pdf.set_text_color(99, 102, 241)
            pdf.multi_cell(0, 9, stripped[2:])
        elif stripped.startswith("## "):
            pdf.set_font("Helvetica", "B", 12)
            pdf.set_text_color(99, 102, 241)
            pdf.ln(4)
            pdf.multi_cell(0, 8, stripped[3:])
            pdf.ln(1)
        elif stripped.startswith("### "):
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(168, 85, 247)
            pdf.multi_cell(0, 7, stripped[4:])
        elif stripped.startswith("- "):
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(60, 60, 80)
            pdf.multi_cell(0, 6, "  • " + stripped[2:])
        elif stripped.startswith("> "):
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(120, 120, 140)
            pdf.multi_cell(0, 6, stripped[2:])
        elif stripped.startswith("---"):
            pdf.set_draw_color(200, 200, 220)
            pdf.line(18, pdf.get_y(), 192, pdf.get_y())
            pdf.ln(3)
        elif stripped == "":
            pdf.ln(2)
        else:
            # 处理 **bold** 标记（简单处理：去除星号）
            clean = stripped.replace("**", "")
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(40, 40, 60)
            pdf.multi_cell(0, 6, clean)

    try:
        return bytes(pdf.output())
    except Exception as e:
        logger.error("PDF output failed: %s", e)
        return None
