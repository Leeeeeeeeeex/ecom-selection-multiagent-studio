"""可比竞品筛选与样本覆盖缺口检测引擎（规则排除配件 + 完整性校验 + 双门槛评分）。"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "data" / "catalog.json"

_ACCESSORY_TITLE_KEYWORDS = ("滤芯", "替换", "垃圾袋", "水泵", "干燥剂", "配件", "装")


@dataclass
class ScreenedItem:
    item_id: str
    title: str
    included: bool
    rule_score: float
    lexical_score: float
    combined_score: float
    reason: str
    raw: dict[str, Any]


@dataclass
class ScreeningResult:
    query_topic: str
    target_category: str
    comparable_items: list[ScreenedItem] = field(default_factory=list)
    excluded_items: list[ScreenedItem] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    total_independent_reviews: int = 0
    pos_review_count: int = 0
    neg_review_count: int = 0


def load_catalog(path: Path = _DEFAULT_CATALOG) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def infer_category(topic: str) -> str:
    if any(k in topic for k in ("饮水机", "喝水", "水泵", "不锈钢")):
        return "宠物饮水机"
    if any(k in topic for k in ("猫砂盆", "铲屎", "猫砂")):
        return "智能猫砂盆"
    if any(k in topic for k in ("烘干", "吹风")):
        return "宠物烘干箱"
    return "宠物饮水机"


def _char_bigram_overlap(a: str, b: str) -> float:
    sa = {a[i : i + 2] for i in range(max(0, len(a) - 1))}
    sb = {b[i : i + 2] for i in range(max(0, len(b) - 1))}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def screen_comparable_competitors(
    topic: str,
    target_pet: str = "猫",
    catalog: list[dict[str, Any]] | None = None,
    min_competitors: int = 3,
    min_reviews: int = 4,
    min_combined_score: float = 0.45,
) -> ScreeningResult:
    """按规格、用途与目标宠物筛选可比竞品，剔除配件与脏数据，记录入选/排除依据与缺口。"""
    items = catalog if catalog is not None else load_catalog()
    category = infer_category(topic)
    res = ScreeningResult(query_topic=topic, target_category=category)

    for item in items:
        item_id = item["item_id"]
        title = item["title"]

        # 1. 类目不匹配直接跳过（不属于本轮候选池）
        if item.get("category") != category:
            continue

        # 2. 规则硬剔除配件（即使标题包含大量主商品关键词）
        if item.get("is_accessory") or any(kw in title for kw in _ACCESSORY_TITLE_KEYWORDS):
            res.excluded_items.append(
                ScreenedItem(
                    item_id=item_id,
                    title=title,
                    included=False,
                    rule_score=0.0,
                    lexical_score=0.0,
                    combined_score=0.0,
                    reason="规则排除：识别为耗材/替换配件（is_accessory=True 或标题含配件词），避免污染主商品价格与评价统计。",
                    raw=item,
                )
            )
            continue

        # 3. 规则硬剔除信息不完整脏条目（无价格、无规格）
        if item.get("price_usd") is None or not item.get("specs"):
            res.excluded_items.append(
                ScreenedItem(
                    item_id=item_id,
                    title=title,
                    included=False,
                    rule_score=0.0,
                    lexical_score=0.0,
                    combined_score=0.0,
                    reason="规则排除：商品价格或核心规格字段缺失，不具备可比性。",
                    raw=item,
                )
            )
            continue

        # 4. 计算规则分（类目一致 + 目标宠物匹配 + 规格完整）与标题词法相似度
        pets = item.get("target_pets") or []
        pet_match = 1.0 if (not target_pet or target_pet in pets) else 0.4
        spec_bonus = min(1.0, len(item.get("specs", {})) / 4.0)
        rule_score = round(0.60 * pet_match + 0.40 * spec_bonus, 3)

        spec_text = title + " " + " ".join(str(v) for v in item.get("specs", {}).values())
        lex_score = round(_char_bigram_overlap(topic, spec_text), 3)
        combined = round(0.75 * rule_score + 0.25 * min(1.0, lex_score * 3.0), 3)

        if combined < min_combined_score:
            res.excluded_items.append(
                ScreenedItem(
                    item_id=item_id,
                    title=title,
                    included=False,
                    rule_score=rule_score,
                    lexical_score=lex_score,
                    combined_score=combined,
                    reason=f"综合匹配分 {combined} 低于门槛 {min_combined_score}。",
                    raw=item,
                )
            )
            continue

        res.comparable_items.append(
            ScreenedItem(
                item_id=item_id,
                title=title,
                included=True,
                rule_score=rule_score,
                lexical_score=lex_score,
                combined_score=combined,
                reason=f"类目={category}、适用宠物匹配({','.join(pets)})、规格完整({len(item.get('specs', {}))}项)，综合分 {combined} 达标入选。",
                raw=item,
            )
        )

    res.comparable_items.sort(key=lambda x: x.combined_score, reverse=True)

    # 5. 统计独立评论覆盖度与正负样本缺口
    seen_reviews: set[str] = set()
    for comp in res.comparable_items:
        for rev in comp.raw.get("reviews", []):
            rid = rev["review_id"]
            if rid in seen_reviews:
                continue
            seen_reviews.add(rid)
            if rev.get("sentiment") == "pos":
                res.pos_review_count += 1
            elif rev.get("sentiment") == "neg":
                res.neg_review_count += 1
    res.total_independent_reviews = len(seen_reviews)

    if len(res.comparable_items) < min_competitors:
        res.coverage_gaps.append(
            f"可比竞品覆盖不足：仅匹配到 {len(res.comparable_items)} 款主商品（最低门槛 {min_competitors} 款），结论外推风险较高，建议人工复核。"
        )
    if res.total_independent_reviews < min_reviews:
        res.coverage_gaps.append(
            f"独立买家评论样本偏少：仅 {res.total_independent_reviews} 条（最低建议 {min_reviews} 条），痛点代表性有限。"
        )
    if res.neg_review_count == 0 or res.pos_review_count == 0:
        res.coverage_gaps.append("正负面评论覆盖失衡（缺少正面或负面样本），需补充差评或好评对比。")

    return res
