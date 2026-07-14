# retrieve.py — RETRIEVE 단계 (결정론, LLM 아님). M5 §3.
# 새 원문과 관련될 수 있는 기존 엔티티를 찾아, EXTRACT LLM 입력으로 붙일 압축 카드를 만든다.
# 조인 키 우선순위: 버전좌표(context) > 어휘 겹침. stdlib only.
import re

import store
import llm

_TOKEN = re.compile(r"[A-Za-z0-9]+|[가-힣]{2,}")


def _tokens(*strs):
    out = set()
    for s in strs:
        if not s:
            continue
        for m in _TOKEN.findall(str(s)):
            out.add(m.lower())
    return out


def _entity_context_map(rels, ctx_ids):
    """entity id -> set(연결된 context id). 관계 양방향 모두 반영."""
    m = {}
    for r in rels:
        a, b = r.get("from"), r.get("to")
        if b in ctx_ids:
            m.setdefault(a, set()).add(b)
        if a in ctx_ids:
            m.setdefault(b, set()).add(a)
    return m


def retrieve_related(text, vocab, k=8):
    """(list of card). 카드 = {id,type,state,title,coordinate[],occurred_at,deadline,summary,score}."""
    ents = store.list_entities()
    if not ents:
        return []
    rels = store.list_relations()
    ctx_ids = {e["id"] for e in ents if e.get("type") == "context"}
    ent_ctx = _entity_context_map(rels, ctx_ids)

    # 새 원문의 버전좌표 (matched/ambiguous 후보 모두 포함)
    matched_ctx = set()
    for lk in llm._match_contexts(text, vocab):
        matched_ctx.update(lk.get("candidates", []))
    ttoks = _tokens(text)

    # recency: occurred_at 우선, 없으면 captured_at. 값들을 정렬해 순위 정규화.
    def when(e):
        return e.get("occurred_at") or e.get("captured_at") or ""
    order = sorted({when(e) for e in ents if when(e)})

    def recency(e):
        w = when(e)
        return (order.index(w) + 1) / len(order) if (w and order) else 0.0

    scored = []
    for e in ents:
        if e.get("type") == "context":
            continue   # context 자체는 카드로 안 붙임 (좌표는 별도 표기)
        ctx = ent_ctx.get(e["id"], set())
        coord = len(ctx & matched_ctx)
        etoks = _tokens(e.get("title"), e.get("summary"), " ".join(e.get("tags") or []))
        lex = (len(etoks & ttoks) / len(etoks | ttoks)) if (etoks and ttoks) else 0.0
        active = 1.0 if e.get("state") == "active" else 0.0
        score = 3.0 * coord + 2.0 * lex + 0.5 * recency(e) + 0.3 * active
        if coord == 0 and lex < 0.08:
            continue   # 좌표도 안 겹치고 어휘도 거의 안 겹치면 제외 (과잉 검색 방지)
        scored.append((score, e, sorted(ctx)))
    scored.sort(key=lambda x: -x[0])

    cards = []
    for score, e, ctx in scored[:k]:
        coord_titles = []
        for cid in ctx:
            ce = store.get_entity(cid)
            coord_titles.append(ce.get("title") if ce else cid)
        cards.append({
            "id": e["id"], "type": e["type"], "state": e.get("state"),
            "title": e.get("title"), "coordinate": coord_titles,
            "occurred_at": e.get("occurred_at"), "deadline": e.get("deadline"),
            "summary": (e.get("summary") or "")[:80],
            "score": round(score, 2),
        })
    return cards
