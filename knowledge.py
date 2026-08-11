"""条款知识库：代理人上传的真实条款 + 关键词检索（下一步换 pgvector 向量检索）。

⚠️ 下面的 POLICY_CHUNKS 是**虚构的示例条款**（示例人寿 / Demo Life 等），
只提供给演示账号（DEMO_AGENT）。真实付费用户永远看不到它们——否则 AI 会带着
"📄 第X页" 的出处一本正经地引用编造的条款，代理人再转发给客户，就是合规事故。
"""

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
_STOP = {"的", "了", "吗", "呢", "是", "在", "有", "和", "我", "你", "他", "她",
         "什么", "多少", "怎么", "可以", "请问", "一下", "the", "a", "an", "is",
         "are", "of", "to", "for", "and", "what", "how", "my", "i"}
_CJK = "一-鿿"
_SEP = str.maketrans({c: " " for c in "？?！!。，,、；;：:（）()【】[]「」\"'\n\r\t"})
_MAX_CANDS = 800     # 单次检索最多打分的 chunk 数，防止大 PDF 把内存吃穿


def _terms(query: str) -> list[str]:
    """中文按 2-gram 切，英文/数字按空格切。

    原来直接 query.split() 对中文无效——中文不按空格断词，整句会变成一个 term，
    命中率约等于 0，实际只有 _KWS 的加分在起作用。
    """
    import re
    q = query.lower().translate(_SEP)
    out = []
    for w in q.split():
        if not w or w in _STOP:
            continue
        if re.fullmatch(f"[{_CJK}]+", w):
            out.extend(w[i:i + 2] for i in range(max(1, len(w) - 1)))   # 2-gram
        else:
            for piece in re.findall(f"[^{_CJK}]+|[{_CJK}]+", w):
                if re.fullmatch(f"[{_CJK}]+", piece):
                    out.extend(piece[i:i + 2] for i in range(max(1, len(piece) - 1)))
                elif piece not in _STOP and len(piece) > 1:
                    out.append(piece)
    return [t for t in dict.fromkeys(out) if t not in _STOP]


def _score(query: str, product: str, text: str, terms: list[str] | None = None) -> int:
    terms = _terms(query) if terms is None else terms
    t = (product + " " + text).lower()
    score = sum(1 for x in terms if x in t)
    score += sum(2 for kw in _KWS if kw in query and kw in text)
    score += sum(3 for w in terms if len(w) > 2 and w in product.lower())   # 产品名命中加权
    return score


def search_policy_chunks(query: str, agent_id: str = "", top_k: int = 4) -> list[dict]:
    """该代理人自己上传的条款文档，按相关度排序。演示账号额外附带内置示例条款。"""
    import db
    from sqlalchemy import or_

    terms = _terms(query)
    cands: list[dict] = []
    if db.DEMO_DATA and agent_id == db.DEMO_AGENT:
        cands.extend(POLICY_CHUNKS)
    if agent_id:
        s = db.SessionLocal()
        try:
            q = (s.query(db.Chunk).join(db.Document)
                 .filter(db.Chunk.agent_id == agent_id)
                 .filter((db.Document.deleted == "") | (db.Document.deleted.is_(None))))
            # 先在 SQL 侧用关键词粗筛，筛不到再退回全量（上限 _MAX_CANDS）。
            likes = [db.Chunk.text.contains(w) for w in terms[:8]]
            rows = q.filter(or_(*likes)).limit(_MAX_CANDS).all() if likes else []
            if not rows:
                rows = q.limit(_MAX_CANDS).all()
            for c in rows:
                cands.append(dict(insurer=c.insurer or "上传文档",
                                  product=c.product or (c.document.filename if c.document else ""),
                                  page=c.page, text=c.text))
        finally:
            s.close()
    scored = [(sc, c) for c in cands
              if (sc := _score(query, c["product"], c["text"], terms)) > 0]
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]
