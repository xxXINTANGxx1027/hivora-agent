"""条款知识库：M1 内置示例条款 + 关键词检索（下一步换 pgvector 向量检索）。"""

POLICY_CHUNKS = [
    dict(insurer="示例人寿 (Demo Life)", product="MediShield Plus", page=3,
         text="MediShield Plus 医疗保险的一般等待期为首次投保生效日起 30 天。"
              "在等待期内因疾病住院不获赔偿，意外受伤不受等待期限制。"),
    dict(insurer="示例人寿 (Demo Life)", product="MediShield Plus", page=4,
         text="特定疾病等待期：投保生效日起 120 天，适用于白内障、胆结石、"
              "疝气、痔疮、扁桃体切除等特定疾病或手术。"),
    dict(insurer="示例人寿 (Demo Life)", product="MediShield Plus", page=7,
         text="MediShield Plus 年度保障限额为 RM 1,500,000，终身无限额。"
              "住院病房每日限额 RM 250（R&B 250 计划）。"),
    dict(insurer="示例人寿 (Demo Life)", product="MediShield Plus", page=12,
         text="除外责任：投保前已存在的疾病 (pre-existing conditions) 在首 24 个月内不保；"
              "美容整形、牙科（意外除外）、生育相关费用不在保障范围内。"),
    dict(insurer="示例人寿 (Demo Life)", product="MediShield Plus", page=15,
         text="保费宽限期 (grace period) 为保费到期日起 31 天。宽限期内保单继续有效；"
              "宽限期届满仍未缴费，保单将失效 (lapse)。"),
    dict(insurer="示例人寿 (Demo Life)", product="CarePlus 360", page=2,
         text="CarePlus 360 一般等待期为 60 天，特定疾病等待期为 180 天。"
              "意外导致的医疗费用自生效日起即受保障。"),
    dict(insurer="示例人寿 (Demo Life)", product="CarePlus 360", page=5,
         text="CarePlus 360 年度保障限额 RM 2,000,000，附带每年一次的健康体检津贴 RM 500。"
              "住院病房每日限额 RM 400。"),
    dict(insurer="示例人寿 (Demo Life)", product="CarePlus 360", page=9,
         text="CarePlus 360 除外责任：先天性疾病、自残、危险运动（潜水、攀岩、赛车）"
              "导致的伤害不在保障范围。投保前已存在疾病首 12 个月不保。"),
    dict(insurer="示例保险 (Demo Assurance)", product="FamilyGuard Term Life", page=2,
         text="FamilyGuard 定期寿险保额可选 RM 100,000 至 RM 2,000,000，"
              "保障期限 10/20/30 年可选。身故或完全永久残废 (TPD) 赔付全额保额。"),
    dict(insurer="示例保险 (Demo Assurance)", product="FamilyGuard Term Life", page=6,
         text="FamilyGuard 自杀条款：保单生效首 12 个月内自杀不获赔偿，仅退还已缴保费。"
              "TPD 保障至 65 岁。"),
]

_KWS = ["等待期", "限额", "除外", "宽限", "自杀", "残废", "保额", "体检",
        "病房", "已存在", "先天", "意外", "续保", "失效"]
_PRODS = ["medishield", "careplus", "familyguard"]


def _score(query: str, product: str, text: str) -> int:
    q_terms = set(query.lower().replace("？", " ").replace("?", " ").split())
    t = (product + " " + text).lower()
    score = sum(1 for x in q_terms if x and x in t)
    score += sum(2 for kw in _KWS if kw in query and kw in text)
    score += sum(3 for p in _PRODS if p in query.lower() and p in product.lower())
    return score


def search_policy_chunks(query: str, agent_id: str = "", top_k: int = 4) -> list[dict]:
    """内置示例条款 + 该代理人自己上传的文档，合并按相关度排序。"""
    import db
    cands = list(POLICY_CHUNKS)
    if agent_id:
        s = db.SessionLocal()
        try:
            for c in s.query(db.Chunk).filter_by(agent_id=agent_id).all():
                cands.append(dict(insurer=c.insurer or "上传文档",
                                  product=c.product or (c.document.filename if c.document else ""),
                                  page=c.page, text=c.text))
        finally:
            s.close()
    scored = [(sc, c) for c in cands if (sc := _score(query, c["product"], c["text"])) > 0]
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]


def _old_search(query: str, top_k: int = 4) -> list[dict]:
    q_terms = set(query.lower().replace("？", " ").replace("?", " ").split())
    scored = []
    for c in POLICY_CHUNKS:
        text = (c["product"] + " " + c["text"]).lower()
        score = sum(1 for t in q_terms if t and t in text)
        score += sum(2 for kw in _KWS if kw in query and kw in c["text"])
        score += sum(3 for p in _PRODS if p in query.lower() and p in c["product"].lower())
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]
