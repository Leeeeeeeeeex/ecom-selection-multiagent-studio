"""可比竞品筛选、证据审核硬拦截与报告版本 Diff 离线单元测试。"""

from ecom_selection.agents import audit_report_evidence, build_market_evidence, build_user_review_evidence
from ecom_selection.screening import screen_comparable_competitors
from ecom_selection.store import compute_markdown_diff


def test_screening_excludes_accessories_and_dirty_records() -> None:
    res = screen_comparable_competitors("无线泵静音不锈钢宠物饮水机", target_pet="猫")
    inc_ids = {c.item_id for c in res.comparable_items}
    exc_ids = {c.item_id for c in res.excluded_items}

    assert {"COMP-F01", "COMP-F02", "COMP-F03", "COMP-F04"} == inc_ids
    assert {"COMP-F-ACC1", "COMP-F-ACC2", "COMP-F-BAD"} == exc_ids
    assert len(res.coverage_gaps) == 0


def test_screening_flags_coverage_gap_for_sparse_category() -> None:
    res = screen_comparable_competitors("宠物智能恒温烘干箱", target_pet="猫")
    assert len(res.comparable_items) == 1
    assert any("可比竞品覆盖不足" in g for g in res.coverage_gaps)
    assert any("独立买家评论样本偏少" in g for g in res.coverage_gaps)


def test_evidence_audit_catches_fake_citations_and_hallucinated_price() -> None:
    res = screen_comparable_competitors("宠物智能恒温烘干箱", target_pet="猫")
    m_ev = build_market_evidence(res)
    u_ev = build_user_review_evidence(res)

    bad_report = (
        "## 1. 目标人群与差异化定位\n主打高端养宠人群。\n"
        "## 2. 定价区间建议\n建议售价 $999.00 [M999]。\n"
        "## 3. 核心产品改进建议\n降低噪音 [RFAKE1]。\n"
        "## 4. 上架前待验证风险\n暂无特别风险。"
    )
    passed, issues, _ = audit_report_evidence(bad_report, m_ev, u_ev, res.coverage_gaps)
    assert passed is False
    assert any("伪造引用" in i for i in issues)
    assert any("样本覆盖缺口" in i for i in issues)
    assert any("异常高报价" in i for i in issues)


def test_markdown_diff_generates_unified_changes() -> None:
    diff = compute_markdown_diff("## 定价\n建议 $999", "## 定价\n建议 $49.99 [M1]")
    assert "-建议 $999" in diff
    assert "+建议 $49.99 [M1]" in diff


def test_selection_chinese_frontend_served() -> None:
    from pathlib import Path
    from ecom_selection.api import serve_frontend

    html = Path(serve_frontend().path).read_text(encoding="utf-8")
    assert "跨境电商多 Agent 选品调研与人工审批工作台" in html
    assert "Human-in-the-Loop" in html
