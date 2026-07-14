# dedup.py — 중복 검출·병합 (재조정과 분리된 배치 도구). M5.
# 재조정=잇기(링크 추가), 중복병합=합치기(엔티티 흡수→개수 감소). 같은 타입끼리만 병합.
# 검출은 결정론(좌표+PM필드+제목 어휘). 병합은 사람이 군집별 확정.
from functools import reduce

import store
import retrieve

_FILL = ("summary", "process", "product", "project", "start_date", "deadline",
         "occurred_at", "date_confidence", "date_note")
# 계층(hier) 관계 — 이들이 순환하면 트리 렌더가 무한/오동작. 병합 후 순환 방지(BG-14).
_HIER = {"recurrence_of", "duplicate_of", "part_of", "instance_of", "derived_from"}


def _break_hier_cycles(node, actor):
    """병합 재지정이 node를 지나는 hier 순환을 만들면 순환을 닫는 엣지를 폐기한다(BG-14).
    재지정은 흡수 대상에 닿던 엣지만 node로 바꾸므로 새 순환은 반드시 node를 지난다 → node 기점 DFS로 충분."""
    def hier_out(x):
        return [r for r in store.list_relations() if r.get("type") in _HIER and r.get("from") == x]
    for _ in range(64):   # 순환을 하나씩 끊으며 반복(안전 상한)
        stack, seen, closed = [(node, None)], set(), None
        while stack:
            cur, _p = stack.pop()
            for r in hier_out(cur):
                nxt = r.get("to")
                if nxt == node:      # node로 되돌아옴 → 순환을 닫는 엣지
                    closed = r; break
                if nxt not in seen:
                    seen.add(nxt); stack.append((nxt, r))
            if closed:
                break
        if not closed:
            return
        store._retire_relation(closed, actor)


def _graph():
    ents = store.list_entities()
    rels = store.list_relations()
    ctx_ids = {e["id"] for e in ents if e.get("type") == "context"}
    ent_ctx, deg = {}, {}
    for r in rels:
        a, b = r.get("from"), r.get("to")
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
        if b in ctx_ids:
            ent_ctx.setdefault(a, set()).add(b)
        if a in ctx_ids:
            ent_ctx.setdefault(b, set()).add(a)
    return ents, ctx_ids, ent_ctx, deg


def _lex(a, b):
    # 제목만 사용 — 요약은 boilerplate가 비슷해 서로 다른 항목(가설 3종 등)을 오검출한다.
    # 병합은 파괴적이므로 판별력 높은 제목으로만 유사도를 잰다.
    ta = retrieve._tokens(a.get("title"))
    tb = retrieve._tokens(b.get("title"))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _similar(a, b, ent_ctx):
    """같은 실물(중복)인가 — 보수적(정밀도 우선). 좌표 공유는 '같은 대상에 대한 것'일 뿐
    중복이 아니다(한 이슈의 여러 action/finding은 서로 다른 항목). 진짜 중복은 제목·요약이
    강하게 겹쳐야 한다. 병합은 파괴적이므로 놓치는(recall) 것보다 잘못 합치는(precision) 걸 피한다."""
    return _lex(a, b) >= 0.5


def _ctitles(ids):
    out = []
    for cid in sorted(ids):
        ce = store.get_entity(cid)
        out.append(ce.get("title") if ce else cid)
    return out


def _card(e, deg):
    return {"id": e["id"], "type": e.get("type"), "title": e.get("title"),
            "state": e.get("state"), "deg": deg.get(e["id"], 0),
            "pm": {k: e.get(k) for k in ("process", "product", "project", "occurred_at", "deadline")
                   if e.get(k)}}


def find_clusters():
    """같은 타입·유사 엔티티를 union-find로 묶어 병합 후보 군집(멤버 2+)을 낸다."""
    ents, ctx_ids, ent_ctx, deg = _graph()
    cand = [e for e in ents if e.get("type") != "context"]
    parent = {e["id"]: e["id"] for e in cand}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    by_type = {}
    for e in cand:
        by_type.setdefault(e["type"], []).append(e)
    for group in by_type.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if _similar(group[i], group[j], ent_ctx):
                    union(group[i]["id"], group[j]["id"])

    clusters = {}
    for e in cand:
        clusters.setdefault(find(e["id"]), []).append(e)

    def pmcount(e):
        return sum(1 for k in _FILL if e.get(k))

    out = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        # 대표 추천: 관계 많고(deg) PM 풍부하고, 동률이면 오래된(작은) id
        members = sorted(members, key=lambda e: (-deg.get(e["id"], 0), -pmcount(e), e["id"]))
        surv = members[0]["id"]
        coord = set()
        for e in members:
            coord |= ent_ctx.get(e["id"], set())
        reason = "%s 유사 %d건" % (members[0]["type"], len(members))
        if coord:
            reason += " · 좌표 " + ", ".join(_ctitles(coord))
        out.append({"type": members[0]["type"], "survivor": surv,
                    "members": [_card(m, deg) for m in members], "reason": reason})
    out.sort(key=lambda c: -len(c["members"]))
    return out


def merge(survivor_id, absorbed_ids, actor="human"):
    """absorbed들을 survivor로 흡수: 빈 필드 통합 + 관계 재지정 + 소프트삭제."""
    surv = store.get_entity(survivor_id)
    if not surv:
        return {"error": "survivor 없음: %s" % survivor_id}
    absorbed = [a for a in absorbed_ids if a != survivor_id and store.get_entity(a)]
    if not absorbed:
        return {"survivor": survivor_id, "absorbed": [], "redirected": 0}
    # 1) 빈 필드·출처·태그 통합
    changed = dict(surv)
    srefs = list(changed.get("source_refs") or [])
    tags = list(changed.get("tags") or [])
    for aid in absorbed:
        a = store.get_entity(aid)
        for k in _FILL:
            if not changed.get(k) and a.get(k):
                changed[k] = a[k]
        for r in (a.get("source_refs") or []):
            if r not in srefs:
                srefs.append(r)
        for tg in (a.get("tags") or []):
            if tg not in tags:
                tags.append(tg)
    changed["source_refs"] = srefs
    if tags:
        changed["tags"] = tags
    # issue 상태·중요도·만료 + 전송시각(최초 min·최종 max) 통합
    firsts = [changed.get("first_captured_at") or changed.get("captured_at")]
    lasts = [changed.get("captured_at")]
    for aid in absorbed:
        a = store.get_entity(aid)
        changed["status"] = store.merge_status(changed.get("status"), a.get("status")) or changed.get("status")
        changed["severity"] = store.merge_severity(changed.get("severity"), a.get("severity")) or changed.get("severity")
        if not changed.get("closed_at") and a.get("closed_at"):
            changed["closed_at"] = a["closed_at"]
        firsts.append(a.get("first_captured_at") or a.get("captured_at"))
        lasts.append(a.get("captured_at"))
    firsts = [f for f in firsts if f]
    lasts = [c for c in lasts if c]
    if firsts:
        changed["first_captured_at"] = reduce(store.ts_min, firsts)   # 오프셋 정규화(BG-12)
    if lasts:
        changed["captured_at"] = reduce(store.ts_max, lasts)
    changed = {k: v for k, v in changed.items() if v not in (None, "")}
    store.upsert_entity(changed, actor)
    # 2) 관계 재지정 → hier 순환 방지(BG-14) → 3) 소프트삭제
    redirected = store.redirect_relations(absorbed, survivor_id, actor)
    _break_hier_cycles(survivor_id, actor)
    for aid in absorbed:
        store.soft_delete_entity(aid, survivor_id, actor)
    return {"survivor": survivor_id, "absorbed": absorbed, "redirected": redirected}
