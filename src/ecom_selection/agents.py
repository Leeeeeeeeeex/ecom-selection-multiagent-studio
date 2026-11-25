"""多 Agent 选品调研编排引擎：竞品筛选 → 双路并行取证 → 运营决策草稿 → 证据审核重写 → HITL 人工审批。"""

import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI

from ecom_selection.config import Settings
from ecom_selection.observability import SelectionObservability
from ecom_selection.screening import ScreeningResult, screen_comparable_competitors
from ecom_selection.store import SelectionStore, compute_topic_hash

_EVIDENCE_REF_RE = re.compile(r"\[(M\d+|R[A-Z0-9]+)\]")
_USD_PRICE_RE = re.compile(r"\$(\d+(?:\.\d+)?)")


def build_market_evidence(screening: ScreeningResult) -> list[dict[str, Any]]:
    """结构化统计取证（与主观评论物理隔离，防止模型凭评论猜测价格或销量）。"""
    comps = screening.comparable_items
    if not comps:
        return []
    prices = sorted(float(c.raw["price_usd"]) for c in comps if c.raw.get("price_usd") is not None)
    sales = [int(c.raw.get("monthly_sales_est") or 0) for c in comps]
    ratings = [float(c.raw.get("rating_avg") or 0.0) for c in comps]

    ev = [
        {
            "id": "M1",
            "fact": (
                f"可比竞品组共 {len(comps)} 款主商品（已排除 {len(screening.excluded_items)} 款配件/脏条目），"
                f"价格区间 ${min(prices):.2f} ~ ${max(prices):.2f}，中位数 ${statistics.median(prices):.2f}，均值 ${statistics.mean(prices):.2f}。"
            ),
            "min_price": min(prices),
            "max_price": max(prices),
            "median_price": round(statistics.median(prices), 2),
        },
        {
            "id": "M2",
            "fact": (
                f"竞品预估月销量合计 {sum(sales)} 件（单品最高 {max(sales)} 件，最低 {min(sales)} 件），"
                f"评分分布 {min(ratings):.1f} ~ {max(ratings):.1f} 星。"
            ),
        },
    ]
    for idx, c in enumerate(comps, start=3):
        ev.append(
            {
                "id": f"M{idx}",
                "fact": (
                    f"{c.item_id}《{c.title}》：售价 ${c.raw['price_usd']}，月销约 {c.raw.get('monthly_sales_est')} 件，"
                    f"评分 {c.raw.get('rating_avg')}，核心规格 {json.dumps(c.raw.get('specs', {}), ensure_ascii=False)}。"
                ),
            }
        )
    return ev


def build_user_review_evidence(screening: ScreeningResult) -> list[dict[str, Any]]:
    """限定在可比竞品组内提取带 [Rxxxx] 编号的买家评价证据。"""
    ev: list[dict[str, Any]] = []
    for c in screening.comparable_items:
        for rev in c.raw.get("reviews", []):
            ev.append(
                {
                    "id": rev["review_id"],
                    "item_id": c.item_id,
                    "rating": rev.get("rating"),
                    "sentiment": rev.get("sentiment"),
                    "verified": rev.get("verified", True),
                    "text": rev["text"],
                }
            )
    return ev


def audit_report_evidence(
    report_md: str,
    market_evidence: list[dict[str, Any]],
    user_evidence: list[dict[str, Any]],
    coverage_gaps: list[str],
) -> tuple[bool, list[str], list[str]]:
    """证据审核 Agent（确定性硬核验）：检查引用合法性、价格区间合理性、结构完整性与缺口披露。"""
    issues: list[str] = []
    valid_m = {e["id"] for e in market_evidence}
    valid_r = {e["id"] for e in user_evidence}
    valid_all = valid_m | valid_r

    cited = list(dict.fromkeys(_EVIDENCE_REF_RE.findall(report_md or "")))
    invalid_refs = [c for c in cited if c not in valid_all]
    if invalid_refs:
        issues.append(f"存在未在本轮证据池中的非法/伪造引用编号：{invalid_refs}")

    cited_m = [c for c in cited if c in valid_m]
    cited_r = [c for c in cited if c in valid_r]
    if market_evidence and not cited_m:
        issues.append("缺少市场结构化统计证据引用（至少应引用 [M1]/[M2] 等价格与销量依据）。")
    if user_evidence and not cited_r:
        issues.append("缺少买家评论原始证据引用（至少应引用 [Rxxxx] 评论编号支撑痛点）。")

    # 检查必需的 4 个评审章节
    required_keywords = ("定位", "定价", "改进", "风险")
    missing_sec = [k for k in required_keywords if k not in (report_md or "")]
    if missing_sec:
        issues.append(f"报告缺少必需评审章节关键词：{missing_sec}")

    # 检查若存在样本覆盖缺口，报告是否明确披露
    if coverage_gaps:
        if not any(kw in (report_md or "") for kw in ("缺口", "覆盖不足", "样本", "人工复核")):
            issues.append("本轮存在样本覆盖缺口，但报告未在风险章节明确提示覆盖不足。")

    # 检查报告中出现的美元价格是否严重偏离竞品统计价格带（防数字幻觉）
    if market_evidence and "min_price" in market_evidence[0]:
        lo = float(market_evidence[0]["min_price"]) * 0.4
        hi = float(market_evidence[0]["max_price"]) * 1.6
        # 允许引用配件/单品耗材小额价格（如 $2.5 滤芯），重点防极端离谱虚高报价
        for p_str in _USD_PRICE_RE.findall(report_md or ""):
            val = float(p_str)
            if val > hi and val > 10.0:
                issues.append(f"检测到偏离竞品价格带的异常高报价 ${val:.2f}（竞品最高 ${market_evidence[0]['max_price']:.2f}）。")
            elif val > 15.0 and val < lo:
                issues.append(f"检测到显著低于竞品价格带的异常报价 ${val:.2f}（竞品最低 ${market_evidence[0]['min_price']:.2f}）。")

    return len(issues) == 0, issues, cited


class SelectionState(TypedDict, total=False):
    task_id: str
    topic: str
    target_pet: str
    require_human_gate: bool
    research_mode: Literal["parallel", "serial"]
    force_bad_draft_once: bool  # 用于评测验证“首次审核打回 -> 自动重写修复”闭环

    screening_dict: dict[str, Any]
    coverage_gaps: list[str]
    market_evidence: list[dict[str, Any]]
    user_evidence: list[dict[str, Any]]
    market_summary: str
    user_insight_summary: str

    draft_markdown: str
    versions_buffer: list[dict[str, Any]]
    rewrite_count: int
    audit_passed: bool
    audit_issues: list[str]
    human_note: str | None
    status: Literal["running", "awaiting_human_review", "approved", "rejected"]
    current_node: str
    node_timings: dict[str, int]
    prompt_tokens: int
    completion_tokens: int


class SelectionWorkflowService:
    """多 Agent 选品调研工作台核心编排引擎。"""

    def __init__(
        self,
        settings: Settings,
        store: SelectionStore | None = None,
        obs: SelectionObservability | None = None,
        llm_client: OpenAI | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._obs = obs
        self._llm = llm_client
        self._graph = self._compile_graph()

    def _get_llm(self) -> OpenAI:
        if self._llm is not None:
            return self._llm
        if not self._settings.llm_api_key:
            raise ValueError("请先配置 DEEPSEEK_API_KEY。")
        self._llm = OpenAI(api_key=self._settings.llm_api_key, base_url=self._settings.llm_base_url)
        return self._llm

    def _call_llm(self, system: str, user: str) -> tuple[str, int, int]:
        resp = self._get_llm().chat.completions.create(
            model=self._settings.llm_model,
            temperature=0.1,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
        u = resp.usage
        return text, getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0

    def _node_screen(self, state: SelectionState) -> SelectionState:
        t0 = time.perf_counter()
        scr = screen_comparable_competitors(
            topic=state["topic"],
            target_pet=state.get("target_pet", "猫"),
            min_competitors=self._settings.min_comparable_competitors,
            min_reviews=self._settings.min_independent_reviews,
        )
        market_ev = build_market_evidence(scr)
        user_ev = build_user_review_evidence(scr)
        timings = dict(state.get("node_timings", {}))
        timings["screen_ms"] = int((time.perf_counter() - t0) * 1000)

        return {
            "current_node": "screen",
            "coverage_gaps": scr.coverage_gaps,
            "market_evidence": market_ev,
            "user_evidence": user_ev,
            "screening_dict": {
                "target_category": scr.target_category,
                "comparable_count": len(scr.comparable_items),
                "excluded_count": len(scr.excluded_items),
                "total_independent_reviews": scr.total_independent_reviews,
                "pos_reviews": scr.pos_review_count,
                "neg_reviews": scr.neg_review_count,
                "comparable": [
                    {"item_id": c.item_id, "title": c.title, "score": c.combined_score, "reason": c.reason}
                    for c in scr.comparable_items
                ],
                "excluded": [
                    {"item_id": c.item_id, "title": c.title, "reason": c.reason}
                    for c in scr.excluded_items
                ],
            },
            "node_timings": timings,
            "versions_buffer": [],
            "rewrite_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    def _run_market_agent(self, topic: str, market_ev: list[dict[str, Any]]) -> tuple[str, int, int, int]:
        t0 = time.perf_counter()
        sys_p = (
            "你是跨境电商市场分析 Agent。严格根据提供的结构化统计证据 [M1..Mn] 总结价格带、销量格局与主流规格，"
            "每条关键判断必须附带 [M编号]，严禁臆测未给出的数字，字数控制在 220 字以内。"
        )
        usr_p = f"调研主题：{topic}\n【结构化市场证据】\n{json.dumps(market_ev, ensure_ascii=False, indent=2)}"
        txt, pt, ct = self._call_llm(sys_p, usr_p)
        return txt, pt, ct, int((time.perf_counter() - t0) * 1000)

    def _run_user_insight_agent(self, topic: str, user_ev: list[dict[str, Any]]) -> tuple[str, int, int, int]:
        t0 = time.perf_counter()
        sys_p = (
            "你是跨境电商用户洞察 Agent。严格根据提供的已核验买家评论 [Rxxxx] 提炼高频好评卖点与核心差评痛点，"
            "每条结论句末必须标注对应评论编号如 [RF101]，严禁编造评论编号或过度外推，字数控制在 220 字以内。"
        )
        usr_p = f"调研主题：{topic}\n【买家评论证据】\n{json.dumps(user_ev, ensure_ascii=False, indent=2)}"
        txt, pt, ct = self._call_llm(sys_p, usr_p)
        return txt, pt, ct, int((time.perf_counter() - t0) * 1000)

    def _node_parallel_research(self, state: SelectionState) -> SelectionState:
        t0 = time.perf_counter()
        mode = state.get("research_mode", "parallel")
        topic = state["topic"]
        m_ev = state.get("market_evidence", [])
        u_ev = state.get("user_evidence", [])

        if mode == "parallel":
            with ThreadPoolExecutor(max_workers=2) as pool:
                fm = pool.submit(self._run_market_agent, topic, m_ev)
                fu = pool.submit(self._run_user_insight_agent, topic, u_ev)
                m_txt, m_pt, m_ct, m_ms = fm.result()
                u_txt, u_pt, u_ct, u_ms = fu.result()
        else:
            m_txt, m_pt, m_ct, m_ms = self._run_market_agent(topic, m_ev)
            u_txt, u_pt, u_ct, u_ms = self._run_user_insight_agent(topic, u_ev)

        timings = dict(state.get("node_timings", {}))
        timings["market_agent_ms"] = m_ms
        timings["user_insight_agent_ms"] = u_ms
        timings["research_wall_ms"] = int((time.perf_counter() - t0) * 1000)

        return {
            "current_node": "parallel_research",
            "market_summary": m_txt,
            "user_insight_summary": u_txt,
            "node_timings": timings,
            "prompt_tokens": state.get("prompt_tokens", 0) + m_pt + u_pt,
            "completion_tokens": state.get("completion_tokens", 0) + m_ct + u_ct,
        }

    def _node_strategy_draft(self, state: SelectionState) -> SelectionState:
        t0 = time.perf_counter()
        rewrite_cnt = state.get("rewrite_count", 0)
        timings = dict(state.get("node_timings", {}))

        # 仅用于评测自动验证“审核打回 -> 重写修复”路径
        if state.get("force_bad_draft_once") and rewrite_cnt == 0:
            bad_md = (
                "## 1. 目标人群与差异化定位\n主打高端养宠群体。\n\n"
                "## 2. 定价区间建议\n建议定价 $999.00 [M999]。\n\n"
                "## 3. 核心产品改进建议\n解决噪音问题 [RFAKE01]。\n\n"
                "## 4. 上架前待验证风险\n暂无明显风险。"
            )
            timings["strategy_draft_v1_ms"] = int((time.perf_counter() - t0) * 1000)
            return {"current_node": "strategy_draft", "draft_markdown": bad_md, "node_timings": timings}

        sys_p = """你是资深跨境电商选品运营决策 Agent。请综合【市场分析 Agent 结论】【用户洞察 Agent 结论】与【样本覆盖缺口】，输出结构化 Markdown 选品评审报告。

强制要求：
1. 必须包含 4 个二级标题：
   - `## 1. 目标人群与差异化定位`
   - `## 2. 定价区间与竞争格局`
   - `## 3. 核心产品改进建议`
   - `## 4. 上架前待验证风险与样本缺口`
2. 报告中必须同时引用市场证据（如 [M1]、[M2]）和买家评论证据（如 [RF101]），严禁编造证据池外的编号！
3. 定价建议必须落在竞品真实价格带区间内，严禁虚高或乱写美元数字。
4. 若【样本覆盖缺口】非空，必须在第 4 节明确提示“样本覆盖不足，建议人工复核”。"""

        usr_p = (
            f"调研主题：{state['topic']}（目标宠物：{state.get('target_pet', '猫')}）\n\n"
            f"【市场分析 Agent 结论】\n{state.get('market_summary')}\n\n"
            f"【用户洞察 Agent 结论】\n{state.get('user_insight_summary')}\n\n"
            f"【可用证据编号】市场: {[e['id'] for e in state.get('market_evidence', [])]}；评论: {[e['id'] for e in state.get('user_evidence', [])]}\n"
            f"【样本覆盖缺口】{state.get('coverage_gaps') or '无'}\n"
        )
        if state.get("audit_issues"):
            usr_p += f"\n【证据审核退回意见，请逐条修正并重写】\n{json.dumps(state['audit_issues'], ensure_ascii=False)}"
        if state.get("human_note"):
            usr_p += f"\n【运营人工批注修改要求，请重点吸收】\n{state['human_note']}"

        md, pt, ct = self._call_llm(sys_p, usr_p)
        timings[f"strategy_draft_v{rewrite_cnt + 1}_ms"] = int((time.perf_counter() - t0) * 1000)
        return {
            "current_node": "strategy_draft",
            "draft_markdown": md,
            "node_timings": timings,
            "prompt_tokens": state.get("prompt_tokens", 0) + pt,
            "completion_tokens": state.get("completion_tokens", 0) + ct,
        }

    def _node_evidence_audit(self, state: SelectionState) -> SelectionState:
        t0 = time.perf_counter()
        passed, issues, _cited = audit_report_evidence(
            report_md=state.get("draft_markdown", ""),
            market_evidence=state.get("market_evidence", []),
            user_evidence=state.get("user_evidence", []),
            coverage_gaps=state.get("coverage_gaps", []),
        )
        rewrite_cnt = state.get("rewrite_count", 0)
        stage_label = "v1_initial_draft" if rewrite_cnt == 0 else f"v{rewrite_cnt + 1}_audit_rewrite"
        versions = list(state.get("versions_buffer", []))
        versions.append(
            {
                "stage_label": stage_label,
                "markdown_content": state.get("draft_markdown", ""),
                "audit_passed": passed,
                "audit_issues": issues,
            }
        )
        timings = dict(state.get("node_timings", {}))
        timings["evidence_audit_ms"] = timings.get("evidence_audit_ms", 0) + int((time.perf_counter() - t0) * 1000)

        next_rewrite = rewrite_cnt + (0 if passed else 1)
        return {
            "current_node": "evidence_audit",
            "audit_passed": passed,
            "audit_issues": issues,
            "rewrite_count": next_rewrite,
            "versions_buffer": versions,
            "node_timings": timings,
        }

    def _route_after_audit(self, state: SelectionState) -> Literal["rewrite", "human_gate", "finalize"]:
        if not state.get("audit_passed", False):
            # 首次审核失败自动打回重写一次
            if state.get("rewrite_count", 0) <= self._settings.max_audit_rewrites:
                return "rewrite"
            # 二次仍不通过 → 强制挂起转人工复核
            return "human_gate"
        # 审核通过，但存在样本覆盖缺口或开启了人工审批门禁 → 挂起等待运营审批
        if state.get("coverage_gaps") or state.get("require_human_gate", False):
            return "human_gate"
        return "finalize"

    def _node_human_gate(self, state: SelectionState) -> SelectionState:
        return {"current_node": "awaiting_human_review", "status": "awaiting_human_review"}

    def _node_finalize(self, state: SelectionState) -> SelectionState:
        return {"current_node": "completed", "status": "approved"}

    def _compile_graph(self):
        builder = StateGraph(SelectionState)
        builder.add_node("screen", self._node_screen)
        builder.add_node("parallel_research", self._node_parallel_research)
        builder.add_node("strategy_draft", self._node_strategy_draft)
        builder.add_node("evidence_audit", self._node_evidence_audit)
        builder.add_node("human_gate", self._node_human_gate)
        builder.add_node("finalize", self._node_finalize)

        builder.add_edge(START, "screen")
        builder.add_edge("screen", "parallel_research")
        builder.add_edge("parallel_research", "strategy_draft")
        builder.add_edge("strategy_draft", "evidence_audit")
        builder.add_conditional_edges(
            "evidence_audit",
            self._route_after_audit,
            {"rewrite": "strategy_draft", "human_gate": "human_gate", "finalize": "finalize"},
        )
        builder.add_edge("human_gate", END)
        builder.add_edge("finalize", END)
        return builder.compile()

    def run_research(
        self,
        topic: str,
        target_pet: str = "猫",
        require_human_gate: bool = True,
        research_mode: Literal["parallel", "serial"] = "parallel",
        force_bad_draft_once: bool = False,
        persist: bool = True,
    ) -> dict[str, Any]:
        """发起选品调研任务（含 Redis 防重/并发限流 + PG 多版本持久化 + Langfuse 追踪）。"""
        topic_hash = compute_topic_hash(topic, target_pet)
        task_id = f"SEL-LOCAL-{topic_hash[:8].upper()}"

        if persist and self._store is not None:
            ok, err_msg, allocated_id = self._store.acquire_task_slot(topic_hash)
            if not ok:
                return {"accepted": False, "error": err_msg, "existing_task_id": allocated_id}
            assert allocated_id is not None
            task_id = allocated_id

        try:
            state = self._graph.invoke(
                {
                    "task_id": task_id,
                    "topic": topic,
                    "target_pet": target_pet,
                    "require_human_gate": require_human_gate,
                    "research_mode": research_mode,
                    "force_bad_draft_once": force_bad_draft_once,
                }
            )
            if persist and self._store is not None:
                self._store.save_task(
                    {
                        "task_id": task_id,
                        "topic_hash": topic_hash,
                        "topic": topic,
                        "target_pet": target_pet,
                        "status": state["status"],
                        "require_human_gate": require_human_gate,
                        "current_node": state["current_node"],
                        "rewrite_count": state.get("rewrite_count", 0),
                        "coverage_gaps": state.get("coverage_gaps", []),
                        "screening": state.get("screening_dict", {}),
                        "market_evidence": state.get("market_evidence", []),
                        "user_evidence": state.get("user_evidence", []),
                        "audit_issues": state.get("audit_issues", []),
                        "node_timings": state.get("node_timings", {}),
                        "human_note": None,
                        "total_prompt_tokens": state.get("prompt_tokens", 0),
                        "total_completion_tokens": state.get("completion_tokens", 0),
                    }
                )
                for ver in state.get("versions_buffer", []):
                    self._store.add_report_version(
                        task_id=task_id,
                        stage_label=ver["stage_label"],
                        markdown_content=ver["markdown_content"],
                        audit_passed=ver["audit_passed"],
                        audit_issues=ver["audit_issues"],
                    )
                if self._obs is not None:
                    self._obs.record_task_trace(
                        task_id=task_id,
                        topic=topic,
                        status=state["status"],
                        node_timings=state.get("node_timings", {}),
                        rewrite_count=state.get("rewrite_count", 0),
                        coverage_gaps=state.get("coverage_gaps", []),
                        prompt_tokens=state.get("prompt_tokens", 0),
                        completion_tokens=state.get("completion_tokens", 0),
                        audit_passed=bool(state.get("audit_passed", False)),
                    )
                detail = self._store.get_task_detail(task_id)
                return {"accepted": True, "task": detail}

            return {"accepted": True, "state": state}
        finally:
            if persist and self._store is not None:
                self._store.release_task_slot(topic_hash, task_id)

    def resume_human_review(
        self,
        task_id: str,
        decision: Literal["approve", "revise", "reject"],
        human_note: str = "",
    ) -> dict[str, Any]:
        """恢复处于 awaiting_human_review 的挂起任务（通过 / 附人工批注重写 / 驳回）。"""
        assert self._store is not None
        detail = self._store.get_task_detail(task_id)
        if not detail:
            raise KeyError(f"未找到任务 {task_id}")
        if detail["status"] != "awaiting_human_review":
            raise ValueError(f"任务 {task_id} 当前状态为 {detail['status']}，不在待人工审批状态。")

        latest_md = detail["versions"][-1]["markdown_content"] if detail["versions"] else ""
        pt_add = ct_add = 0
        new_status = "approved"
        audit_issues = detail.get("audit_issues", [])

        if decision == "approve":
            note_suffix = f"\n\n---\n> **人工审批通过**：{human_note or '运营复核无误，准予上架立项。'}"
            self._store.add_report_version(
                task_id=task_id,
                stage_label="human_approved",
                markdown_content=latest_md + note_suffix,
                audit_passed=True,
                audit_issues=[],
            )
            audit_issues = []
            new_status = "approved"
        elif decision == "reject":
            new_status = "rejected"
        elif decision == "revise":
            # 带入运营人工批注触发决策 Agent 修订并重新走证据审核
            t0 = time.perf_counter()
            revise_sys = (
                "你是跨境电商选品运营决策 Agent。请严格依据运营人工批注修订现有选品报告，"
                "保持 4 个二级标题结构，保留所有合法证据引用 [M*]/[R*] 且不编造新编号。"
            )
            revise_usr = (
                f"【当前报告草稿】\n{latest_md}\n\n"
                f"【运营人工批注要求】\n{human_note}\n\n"
                f"【合法证据池】市场: {[e['id'] for e in detail['market_evidence']]}；"
                f"评论: {[e['id'] for e in detail['user_evidence']]}"
            )
            revised_md, pt_add, ct_add = self._call_llm(revise_sys, revise_usr)
            passed, issues, _ = audit_report_evidence(
                revised_md, detail["market_evidence"], detail["user_evidence"], detail["coverage_gaps"]
            )
            self._store.add_report_version(
                task_id=task_id,
                stage_label="human_revised_final",
                markdown_content=revised_md,
                audit_passed=passed,
                audit_issues=issues,
            )
            detail["node_timings"]["human_revise_ms"] = int((time.perf_counter() - t0) * 1000)
            audit_issues = issues
            new_status = "approved" if passed else "awaiting_human_review"

        self._store.save_task(
            {
                "task_id": detail["task_id"],
                "topic_hash": detail["topic_hash"],
                "topic": detail["topic"],
                "target_pet": detail["target_pet"],
                "status": new_status,
                "require_human_gate": detail["require_human_gate"],
                "current_node": "completed" if new_status in ("approved", "rejected") else "awaiting_human_review",
                "rewrite_count": detail["rewrite_count"] + (1 if decision == "revise" else 0),
                "coverage_gaps": detail["coverage_gaps"],
                "screening": detail["screening"],
                "market_evidence": detail["market_evidence"],
                "user_evidence": detail["user_evidence"],
                "audit_issues": audit_issues,
                "node_timings": detail["node_timings"],
                "human_note": human_note,
                "total_prompt_tokens": detail["total_prompt_tokens"] + pt_add,
                "total_completion_tokens": detail["total_completion_tokens"] + ct_add,
            }
        )
        if self._obs is not None:
            self._obs.record_human_decision(task_id, decision, human_note)
        return self._store.get_task_detail(task_id)  # type: ignore[return-value]
