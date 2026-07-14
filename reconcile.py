# reconcile.py — RECONCILE 단계 (결정론 Tool, 재실행 가능). M5 §6.
# 전 엔티티를 공유 좌표로 묶어 '미연결 쌍'에 링크를 제안한다. 자동 커밋 안 함(사람 확정).
# idempotent: 이미 있는 관계는 스킵 → 몇 번 돌려도 같은 제안 → 무작위 전송 순서에 견고.
import store

# (from_type, to_type) -> (관계 방향, 타입). 방향은 (a,b) 순서 기준.
_RULES = {
    ("issue", "case"): ("a_to_b", "part_of"),      # issue part_of case
    ("case", "issue"): ("b_to_a", "part_of"),
    ("action", "issue"): ("a_to_b", "addresses"),  # action addresses issue
    ("issue", "action"): ("b_to_a", "addresses"),
    ("action", "case"): ("a_to_b", "addresses"),
    ("case", "action"): ("b_to_a", "addresses"),
    ("issue", "finding"): ("a_to_b", "derived_from"),  # issue derived_from finding
    ("finding", "issue"): ("b_to_a", "derived_from"),
    ("initiative", "issue"): ("a_to_b", "addresses"),
    ("issue", "initiative"): ("b_to_a", "addresses"),
}


def _card(e):
    return {"id": e["id"], "type": e.get("type"), "title": e.get("title"),
            "state": e.get("state")}


def _context_titles(ids):
    out = []
    for cid in sorted(ids):
        ce = store.get_entity(cid)
        out.append(ce.get("title") if ce else cid)
    return out


def _graph():
    ents = store.list_entities()
    rels = store.list_relations()
    ctx_ids = {e["id"] for e in ents if e.get("type") == "context"}
    ent_ctx, pairs = {}, set()
    for r in rels:
        a, b = r.get("from"), r.get("to")
        pairs.add((a, b))
        pairs.add((b, a))
        if b in ctx_ids:
            ent_ctx.setdefault(a, set()).add(b)
        if a in ctx_ids:
            ent_ctx.setdefault(b, set()).add(a)
    return ents, ctx_ids, ent_ctx, pairs


def propose(limit=60):
    """공유 좌표를 가진 미연결 쌍에 대한 링크 제안 목록."""
    ents, ctx_ids, ent_ctx, pairs = _graph()
    non_ctx = [e for e in ents if e.get("type") != "context"]
    out = []
    for i in range(len(non_ctx)):
        for j in range(i + 1, len(non_ctx)):
            a, b = non_ctx[i], non_ctx[j]
            shared = ent_ctx.get(a["id"], set()) & ent_ctx.get(b["id"], set())
            if not shared:
                continue
            rule = _RULES.get((a.get("type"), b.get("type")))
            if not rule:
                continue
            direction, rtype = rule
            frm, to = (a, b) if direction == "a_to_b" else (b, a)
            if (frm["id"], to["id"]) in pairs:
                continue   # 이미 연결됨 → 스킵 (idempotent)
            out.append({
                "from": frm["id"], "to": to["id"], "type": rtype,
                "reason": "공유 좌표: " + ", ".join(_context_titles(shared)),
                "a": _card(frm), "b": _card(to),
            })
            if len(out) >= limit:
                return out
    return out


def apply(items, actor="human"):
    """선택된 제안(from/to/type)을 관계로 커밋. 이미 있으면 스킵."""
    _, _, _, pairs = _graph()
    created = []
    for it in items or []:
        f, t, ty = it.get("from"), it.get("to"), it.get("type", "related_to")
        if not (f and t):
            continue
        fe, te = store.get_entity(f), store.get_entity(t)
        if not (fe and te and fe.get("state") == "active" and te.get("state") == "active"):
            continue   # trashed/merged 대상으로 active 관계 생성 방지 (BG-8)
        if (f, t) in pairs:
            continue
        rid = store.next_id("REL")
        store.upsert_relation(
            {"id": rid, "from": f, "type": ty, "to": t, "source_refs": [],
             "confidence": "low", "state": "active",
             "produced_by": {"type": "tool", "name": "reconcile", "version": "0.1"}}, actor)
        created.append(rid)
        pairs.add((f, t))
        pairs.add((t, f))
    return {"created": created, "n": len(created)}
