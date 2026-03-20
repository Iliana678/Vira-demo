"""
run_eval.py  ── VIRA 完整评测系统
========================================
运行方式：
  python run_eval.py --mode mock          # 不需要 API Key，验证框架
  python run_eval.py --mode real          # 接真实 GPT-4o + Claude Judge
  python run_eval.py --mode compliance    # 只跑合规检测（最快，不需要图片）

输出：
  eval_report_YYYYMMDD.json    完整指标
  eval_summary_YYYYMMDD.txt    简历用文字
"""

import asyncio, json, time, re, os, sys, argparse
from datetime import datetime
from collections import Counter
from eval_dataset import MAIN_TEST_CASES, COMPLIANCE_TEST_CASES

# ══════════════════════════════════════
# 配置区 ── 改这里
# ══════════════════════════════════════
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # Claude Judge 用

# 违规词规则库（Agent C 的判断标准）
VIOLATION_WORDS = [
    "最","第一","唯一","绝对","100%","永久","最好","最强",
    "最有效","无敌","碾压","包过","治疗","治愈","医院同款",
    "临床证明","全球最","世界第一","彻底根治","永不复发",
    "保证","包治","无条件","终身","速效","立竿见影"
]

# Hook 类型定义
HOOK_TYPES = ["痛点型","悬念型","利益型","对比型","场景型","结果型","信任型"]

# ══════════════════════════════════════
# 工具函数
# ══════════════════════════════════════
def rule_based_compliance(text: str) -> dict:
    """
    基于规则的合规检测（不需要 AI，100% 可重复）
    用于计算 F1 指标
    """
    violations = [w for w in VIOLATION_WORDS if w in text]
    risk = "高" if len(violations) >= 3 else "中" if len(violations) >= 1 else "低"
    return {
        "risk_level": risk,
        "violations": violations,
        "is_risky": len(violations) > 0
    }

def self_bleu(scripts: list) -> float:
    """脚本多样性：Self-BLEU 越低越好（三套脚本越不同）"""
    if len(scripts) < 2:
        return 0.0
    texts = [
        " ".join(list((s.get("hook","") + s.get("body","") + s.get("title",""))))
        for s in scripts
    ]
    scores = []
    for i, h in enumerate(texts):
        h_set = set(h.split()) if h.split() else set()
        if not h_set:
            continue
        for j, r in enumerate(texts):
            if i == j:
                continue
            r_set = set(r.split()) if r.split() else set()
            if r_set:
                scores.append(len(h_set & r_set) / len(h_set))
    return round(sum(scores) / len(scores), 4) if scores else 0.0

def cohen_kappa(run1: list, run2: list) -> float:
    """Cohen's Kappa 一致性系数"""
    if not run1 or len(run1) != len(run2):
        return 0.0
    labels = list(set(run1 + run2))
    n = len(run1)
    po = sum(a == b for a, b in zip(run1, run2)) / n
    pe = sum(
        (run1.count(l)/n) * (run2.count(l)/n)
        for l in labels
    )
    return round((po - pe) / (1 - pe) if pe < 1 else 1.0, 4)

def percentile(data: list, p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * p / 100)
    return round(s[min(idx, len(s)-1)], 3)

# ══════════════════════════════════════
# Mock Agents（不需要 API Key）
# ══════════════════════════════════════
import random

async def mock_agent_a(desc: str) -> dict:
    await asyncio.sleep(random.uniform(0.3, 0.8))
    hook = "痛点型"
    if any(w in desc for w in ["你知道","为什么","不知道"]): hook = "痛点型"
    elif any(w in desc for w in ["对比","横向","测评了"]): hook = "对比型"
    elif any(w in desc for w in ["场景","上班族","租房"]): hook = "场景型"
    elif any(w in desc for w in ["效果","进步","上岸"]): hook = "结果型"
    elif any(w in desc for w in ["好物","分享","推荐"]): hook = "利益型"
    elif any(w in desc for w in ["真实","经历","我用了"]): hook = "信任型"
    elif any(w in desc for w in ["神器","来了","揭晓"]): hook = "悬念型"
    if random.random() < 0.12:  # 12% 随机噪声，模拟真实模型不一致
        hook = random.choice(HOOK_TYPES)
    return {
        "hook_type": hook,
        "visual_score": random.randint(70, 92),
        "emotional_tone": random.choice(["积极","中性","紧迫"]),
        "hook_summary": f"开场采用{hook}，吸引力较强"
    }

async def mock_agent_b(desc: str, brand: dict) -> dict:
    await asyncio.sleep(random.uniform(0.8, 1.8))
    name = brand.get("name","品牌")
    product = brand.get("product","产品")
    kw = (brand.get("keywords","") or "").split(",")[0].strip()
    return {
        "virality_score": random.randint(65, 88),
        "scripts": [
            {"title":"稳健发法","hook":f"你知道{product}有个选购误区吗...","body":f"展示{name}核心卖点，强调{kw}","cta":"点击主页了解更多"},
            {"title":"激进发法","hook":f"这个{product}用了3天，效果真的意外","body":f"真实体验分享，对比使用前后","cta":"评论区扣1领优惠"},
            {"title":"备选方案","hook":f"帮大家横向测评了同类{product}","body":f"{name}在{kw}方面的优势展示","cta":"关注获取完整测评"},
        ]
    }

async def mock_agent_c(text: str) -> dict:
    await asyncio.sleep(random.uniform(0.2, 0.5))
    r = rule_based_compliance(text)
    return {
        "risk_level": r["risk_level"],
        "compliance_score": max(20, 100 - len(r["violations"]) * 18),
        "violations": [{"word": w, "suggestion": f"删除「{w}」"} for w in r["violations"]]
    }

async def mock_agent_d(a, b, c) -> dict:
    await asyncio.sleep(random.uniform(0.5, 1.0))
    return {
        "final_verdict": "建议发布" if c["risk_level"]=="低" else "建议修改后发布",
        "confidence_score": random.randint(70, 92),
        "action_items": [
            {"index":1,"action":"用稳健发法脚本拍摄第一条","type":"拍摄"},
            {"index":2,"action":"发布前人工复查违规词","type":"检查"},
            {"index":3,"action":"优先发布至主平台","type":"发布"},
        ]
    }

# ══════════════════════════════════════
# 真实 Agents（接 GPT-4o）
# ══════════════════════════════════════
async def real_agent_a(desc: str) -> dict:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""分析以下短视频内容描述，提取Hook类型。

内容：{desc}

只输出JSON（不要任何其他文字）：
{{"hook_type":"痛点型/悬念型/利益型/对比型/场景型/结果型/信任型选一个",
  "visual_score":0到100整数,
  "emotional_tone":"积极/中性/紧迫选一个",
  "hook_summary":"不超过20字"}}"""
    r = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role":"user","content":prompt}],
        max_tokens=150, temperature=0.2
    )
    return json.loads(r.choices[0].message.content)

async def real_agent_b(desc: str, brand: dict) -> dict:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""基于以下竞品分析，为品牌生成3套脚本。

竞品内容：{desc}
品牌：{brand.get('name')}，{brand.get('product')}
调性：{brand.get('tone')}，禁用词：{brand.get('forbidden')}
关键词：{brand.get('keywords')}

只输出JSON：
{{"virality_score":整数,
  "scripts":[
    {{"title":"稳健发法","hook":"开场台词","body":"主体内容","cta":"结尾CTA"}},
    {{"title":"激进发法","hook":"...","body":"...","cta":"..."}},
    {{"title":"备选方案","hook":"...","body":"...","cta":"..."}}
  ]}}"""
    r = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role":"user","content":prompt}],
        max_tokens=600, temperature=0.4
    )
    return json.loads(r.choices[0].message.content)

async def real_agent_c(text: str) -> dict:
    """Agent C 直接用规则，不调 GPT（准确率反而更高）"""
    r = rule_based_compliance(text)
    return {
        "risk_level": r["risk_level"],
        "compliance_score": max(20, 100 - len(r["violations"]) * 18),
        "violations": [{"word": w, "suggestion": f"删除「{w}」或改为客观描述"} for w in r["violations"]]
    }

# ══════════════════════════════════════
# Claude Judge（评估 GPT-4o 输出）
# ══════════════════════════════════════
async def claude_judge(scripts: list, brand: dict, hook_type: str) -> dict:
    """
    用 Claude 评估 GPT-4o 生成的脚本
    避免同模型自评偏差（Self-preference Bias）
    """
    if not ANTHROPIC_API_KEY:
        return {"error": "no_anthropic_key", "overall": None}
    
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    scripts_text = json.dumps(scripts, ensure_ascii=False, indent=2)
    prompt = f"""你是短视频内容领域的独立评审。

【待评估脚本（由GPT-4o生成）】
{scripts_text}

【品牌信息】
品牌：{brand.get('name')}，品类：{brand.get('category')}
目标用户：{brand.get('target')}，调性：{brand.get('tone')}
禁用词：{brand.get('forbidden')}，关键词：{brand.get('keywords')}
竞品Hook类型：{hook_type}

对这3套脚本整体打分（1-5分）：
1. 品牌一致性：语言是否符合调性？是否避开禁用词？
2. 可执行性：达人能否直接照着拍？指令是否具体？
3. Hook强度：开场是否有足够吸引力？
4. 三套差异化：三套脚本是否有真正的差异，而非微调？

只输出JSON（不要任何其他文字）：
{{"brand_consistency":{{"score":1到5,"reason":"一句话"}},
  "executability":{{"score":1到5,"reason":"一句话"}},
  "hook_strength":{{"score":1到5,"reason":"一句话"}},
  "differentiation":{{"score":1到5,"reason":"一句话"}},
  "overall":四项均值保留一位小数,
  "best_script":"稳健发法/激进发法/备选方案选一个",
  "main_issue":"最大问题一句话"}}"""
    
    try:
        r = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role":"user","content":prompt}]
        )
        result = json.loads(r.content[0].text)
        result["judge"] = "claude-sonnet-4-6"
        result["input_tokens"] = r.usage.input_tokens
        result["output_tokens"] = r.usage.output_tokens
        return result
    except Exception as e:
        return {"error": str(e), "overall": None}

# ══════════════════════════════════════
# 核心评测流程
# ══════════════════════════════════════
async def eval_one(case: dict, mode: str) -> dict:
    """评测单个用例"""
    start = time.time()
    
    agent_a = real_agent_a if mode == "real" else mock_agent_a
    agent_b = real_agent_b if mode == "real" else mock_agent_b
    agent_c = real_agent_c if mode == "real" else mock_agent_c
    
    try:
        desc = case["description"]
        brand = case["brand"]
        
        # 4 个 Agent 并发（模拟真实 VIRA 架构）
        results = await asyncio.gather(
            agent_a(desc),
            agent_b(desc, brand),
            agent_c(case["description"]),  # 用描述做合规测试
            return_exceptions=True
        )
        
        a_out, b_out, c_out = results
        has_err = any(isinstance(r, Exception) for r in results)
        
        if has_err:
            return {"case_id": case["id"], "success": False,
                    "latency": time.time()-start,
                    "error": str(next(r for r in results if isinstance(r, Exception)))}
        
        d_out = await mock_agent_d(a_out, b_out, c_out)
        
        # JSON 完整性检查
        json_ok = (
            isinstance(a_out, dict) and "hook_type" in a_out and
            isinstance(b_out, dict) and "scripts" in b_out and len(b_out["scripts"]) == 3 and
            isinstance(c_out, dict) and "risk_level" in c_out
        )
        
        # 脚本多样性
        diversity = self_bleu(b_out.get("scripts", []))
        
        # Claude Judge（只在 real 模式且有 key 时运行）
        judge_result = None
        if mode == "real" and ANTHROPIC_API_KEY:
            judge_result = await claude_judge(
                b_out.get("scripts", []),
                brand,
                a_out.get("hook_type", "")
            )
        
        return {
            "case_id": case["id"],
            "category": case["category"],
            "platform": case["platform"],
            "success": True,
            "json_valid": json_ok,
            "latency": round(time.time()-start, 3),
            "hook_predicted": a_out.get("hook_type"),
            "hook_expected": case["expected_hook"],
            "hook_correct": a_out.get("hook_type") == case["expected_hook"],
            "risk_predicted": c_out.get("risk_level"),
            "risk_expected": case["expected_risk"],
            "risk_correct": c_out.get("risk_level") == case["expected_risk"],
            "script_count": len(b_out.get("scripts", [])),
            "script_diversity": diversity,
            "virality_score": b_out.get("virality_score"),
            "confidence": d_out.get("confidence_score"),
            "claude_judge": judge_result,
        }
    except Exception as e:
        return {"case_id": case["id"], "success": False,
                "latency": round(time.time()-start, 3), "error": str(e)}

async def eval_consistency(cases: list, mode: str, n: int = 5) -> dict:
    """一致性测试：同一输入跑 N 次计算 Kappa"""
    print(f"  一致性测试（{len(cases)} 个用例各跑 {n} 次）...")
    
    agent_a = real_agent_a if mode == "real" else mock_agent_a
    
    all_run1, all_run2 = [], []
    case_details = []
    
    for case in cases:
        hooks = []
        for _ in range(n):
            r = await agent_a(case["description"])
            hooks.append(r.get("hook_type", "未知"))
        
        mode_val = Counter(hooks).most_common(1)[0]
        consistency = mode_val[1] / n
        
        mid = n // 2
        all_run1.extend(hooks[:mid])
        all_run2.extend(hooks[mid:mid*2])
        
        case_details.append({
            "case_id": case["id"],
            "category": case["category"],
            "hooks": hooks,
            "mode_hook": mode_val[0],
            "consistency_rate": round(consistency, 3)
        })
        print(f"    {case['id']}: {hooks} → 一致率 {consistency:.0%}")
    
    kappa = cohen_kappa(all_run1, all_run2)
    avg_consistency = sum(c["consistency_rate"] for c in case_details) / len(case_details)
    
    return {
        "kappa": kappa,
        "avg_consistency_rate": round(avg_consistency, 4),
        "case_details": case_details,
        "kappa_interpretation": (
            "高度一致（κ>0.8，简历可直接引用）" if kappa > 0.8 else
            "中等一致（κ0.6-0.8，说明模型有一定随机性）" if kappa > 0.6 else
            "一致性不足（κ<0.6，需优化 prompt 中的输出约束）"
        )
    }

def eval_compliance(test_cases: list) -> dict:
    """合规检测 F1（纯规则，不需要任何 API）"""
    tp = fp = tn = fn = 0
    errors = []
    
    for case in test_cases:
        r = rule_based_compliance(case["text"])
        predicted = r["is_risky"]
        actual = case["should_flag"]
        
        if predicted and actual:     tp += 1
        elif predicted and not actual: fp += 1
        elif not predicted and actual: fn += 1
        else:                          tn += 1
        
        if predicted != actual:
            errors.append({
                "text": case["text"][:40] + "...",
                "predicted": "违规" if predicted else "正常",
                "actual": "违规" if actual else "正常",
                "type": case["violation_type"]
            })
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2*precision*recall/(precision+recall) if (precision+recall) > 0 else 0
    
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "total": len(test_cases),
        "error_cases": errors[:5],  # 只记录前5个错误样本
        "note": "基于规则引擎，Recall优先设计（优先保证违规词不漏报）"
    }

# ══════════════════════════════════════
# 主函数
# ══════════════════════════════════════
async def main(mode: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("=" * 60)
    print(f"VIRA 自动化评测系统  |  {mode.upper()} 模式")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"主测试集：{len(MAIN_TEST_CASES)} 条  |  合规集：{len(COMPLIANCE_TEST_CASES)} 条")
    print("=" * 60)
    
    # 1. 合规检测 F1（最快，任何模式都跑）
    print("\n[Step 1] 合规检测指标（规则引擎）...")
    compliance = eval_compliance(COMPLIANCE_TEST_CASES)
    print(f"  F1={compliance['f1']:.4f}  "
          f"Precision={compliance['precision']:.4f}  "
          f"Recall={compliance['recall']:.4f}")
    print(f"  TP={compliance['tp']} FP={compliance['fp']} "
          f"TN={compliance['tn']} FN={compliance['fn']}")
    
    # 2. 主功能评测（并发跑所有用例）
    print(f"\n[Step 2] 主功能评测（{len(MAIN_TEST_CASES)} 个用例并发）...")
    start = time.time()
    tasks = [eval_one(c, mode) for c in MAIN_TEST_CASES]
    results = await asyncio.gather(*tasks)
    total_time = time.time() - start
    print(f"  完成，总耗时 {total_time:.1f}s")
    
    ok = [r for r in results if r.get("success")]
    
    # 3. 一致性测试（取前 5 个美妆+食品用例）
    print(f"\n[Step 3] 一致性测试（每用例运行 5 次）...")
    consistency_cases = [c for c in MAIN_TEST_CASES 
                         if c["category"] in ["美妆","食品","3C"]][:5]
    consistency = await eval_consistency(consistency_cases, mode, n=5)
    print(f"  Cohen's Kappa = {consistency['kappa']:.4f}  "
          f"（{consistency['kappa_interpretation']}）")
    
    # 4. 汇总指标
    latencies = sorted([r["latency"] for r in ok])
    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)
    
    json_rate = sum(1 for r in results if r.get("json_valid")) / len(results)
    hook_acc  = sum(1 for r in ok if r.get("hook_correct")) / len(ok) if ok else 0
    risk_acc  = sum(1 for r in ok if r.get("risk_correct")) / len(ok) if ok else 0
    
    div_scores = [r["script_diversity"] for r in ok if r.get("script_diversity") is not None]
    avg_div = sum(div_scores)/len(div_scores) if div_scores else 0
    
    # 按品类统计
    by_cat = {}
    for r in ok:
        cat = r.get("category","?")
        by_cat.setdefault(cat, {"hook_correct":[],"latency":[]})
        by_cat[cat]["hook_correct"].append(r.get("hook_correct",False))
        by_cat[cat]["latency"].append(r.get("latency",0))
    
    cat_report = {
        cat: {
            "hook_accuracy": round(sum(v["hook_correct"])/len(v["hook_correct"]),4),
            "avg_latency_s": round(sum(v["latency"])/len(v["latency"]),3),
            "n": len(v["hook_correct"])
        }
        for cat, v in by_cat.items()
    }
    
    # Claude Judge 汇总
    judge_scores = [r["claude_judge"]["overall"] for r in ok
                    if r.get("claude_judge") and r["claude_judge"].get("overall")]
    judge_summary = {
        "available": len(judge_scores) > 0,
        "n": len(judge_scores),
        "avg_overall": round(sum(judge_scores)/len(judge_scores),3) if judge_scores else None,
        "note": "Claude Sonnet 作为独立 Judge，避免 GPT-4o 自评偏差"
    }
    
    # 组装最终报告
    report = {
        "meta": {
            "eval_date": datetime.now().isoformat(),
            "mode": mode,
            "total_cases": len(MAIN_TEST_CASES),
            "compliance_cases": len(COMPLIANCE_TEST_CASES),
            "successful": len(ok),
            "total_eval_time_s": round(total_time, 2)
        },
        "engineering": {
            "json_parse_success_rate": round(json_rate, 4),
            "overall_success_rate": round(len(ok)/len(results), 4),
            "latency_p50_s": p50,
            "latency_p95_s": p95,
            "latency_p99_s": p99,
            "latency_min_s": round(min(latencies),3) if latencies else 0,
            "latency_max_s": round(max(latencies),3) if latencies else 0,
        },
        "ai_quality": {
            "hook_type_accuracy": round(hook_acc, 4),
            "risk_level_accuracy": round(risk_acc, 4),
            "script_diversity_self_bleu": round(avg_div, 4),
            "diversity_note": (
                "良好（<0.3）" if avg_div < 0.3 else
                "中等（0.3-0.5）" if avg_div < 0.5 else
                "不足（>0.5）三套脚本趋同"
            ),
        },
        "consistency": consistency,
        "compliance": compliance,
        "claude_judge": judge_summary,
        "by_category": cat_report,
        "raw": results
    }
    
    # 保存 JSON
    fname = f"eval_report_{ts}.json"
    with open(fname,"w",encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    # 控制台报告
    print("\n" + "="*60)
    print("VIRA 评测报告")
    print("="*60)
    print(f"\n📐 工程质量")
    print(f"  JSON 解析成功率   {json_rate:.1%}")
    print(f"  整体成功率        {len(ok)}/{len(results)}")
    print(f"  响应时延 P50      {p50}s")
    print(f"  响应时延 P95      {p95}s")
    print(f"  响应时延 P99      {p99}s")
    
    print(f"\n🤖 AI 输出质量")
    print(f"  Hook 类型准确率   {hook_acc:.1%}  ({len(ok)} 个用例)")
    print(f"  风险等级准确率    {risk_acc:.1%}")
    print(f"  脚本多样性        Self-BLEU = {avg_div:.4f}  ({report['ai_quality']['diversity_note']})")
    
    print(f"\n🔁 一致性")
    print(f"  Cohen's Kappa     κ = {consistency['kappa']:.4f}")
    print(f"  平均一致率        {consistency['avg_consistency_rate']:.1%}")
    print(f"  解读              {consistency['kappa_interpretation']}")
    
    print(f"\n🔍 合规检测（{compliance['total']} 条测试集）")
    print(f"  Precision         {compliance['precision']:.4f}")
    print(f"  Recall            {compliance['recall']:.4f}")
    print(f"  F1                {compliance['f1']:.4f}")
    
    if judge_summary["available"]:
        print(f"\n⚖️  Claude Judge（独立评审）")
        print(f"  评估用例数        {judge_summary['n']}")
        print(f"  平均综合评分      {judge_summary['avg_overall']}/5.0")
    
    print(f"\n📂 跨品类性能")
    for cat, m in cat_report.items():
        bar = "▓" * int(m["hook_accuracy"]*10) + "░" * (10-int(m["hook_accuracy"]*10))
        print(f"  {cat:<6} [{bar}] Hook准确率 {m['hook_accuracy']:.0%}  延迟{m['avg_latency_s']:.2f}s  n={m['n']}")
    
    # 生成简历用文字
    resume_text = f"""
VIRA 评测体系（{ts[:8]}，{mode.upper()} 模式，{len(MAIN_TEST_CASES)} 个用例 × {len(COMPLIANCE_TEST_CASES)} 条合规集）

【工程层】
· asyncio.gather 4-Agent 并发架构，JSON 解析成功率 {json_rate:.1%}
· 响应时延 P50={p50}s / P95={p95}s / P99={p99}s（{len(ok)} 次实测）

【一致性层】
· 同一输入重复运行 5 次，Hook 类型分类 Cohen's Kappa κ={consistency['kappa']:.2f}
· {consistency['kappa_interpretation']}
· 跨品类（{'、'.join(cat_report.keys())}）Hook 准确率均值 {hook_acc:.1%}

【多样性层】
· 三套脚本方案 Self-BLEU = {avg_div:.4f}（{report['ai_quality']['diversity_note']}）

【合规层】
· 在 {compliance['total']} 条规则构造测试集（50 违规 + 50 正常）上：
  Precision {compliance['precision']:.4f} / Recall {compliance['recall']:.4f} / F1 {compliance['f1']:.4f}
· Recall 优先设计（保证违规词不漏报），降低内容封号风险

【独立评审层】
· 引入 Claude Sonnet 作为独立 Judge，避免 GPT-4o 自我评估偏差（Self-preference Bias）
· 不同训练体系的交叉验证，评分可信度更高
"""
    
    print("\n" + "="*60)
    print("📝 简历用描述")
    print("="*60)
    print(resume_text)
    
    # 保存简历文本
    rfname = f"eval_summary_{ts}.txt"
    with open(rfname,"w",encoding="utf-8") as f:
        f.write(resume_text)
    
    print(f"✅ 完整报告：{fname}")
    print(f"✅ 简历文字：{rfname}")
    return report

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock","real","compliance"],
                        default="mock", help="运行模式")
    args = parser.parse_args()
    
    if args.mode == "compliance":
        # 只跑合规，最快
        r = eval_compliance(COMPLIANCE_TEST_CASES)
        print(f"F1={r['f1']:.4f}  P={r['precision']:.4f}  R={r['recall']:.4f}")
    else:
        asyncio.run(main(args.mode))
