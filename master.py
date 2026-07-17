# master.py — 기준정보 그래프의 결정론 리졸버 + 병합계획. LLM 없음.
#
# 저장은 store가 일반 entity{kind}+relation 그래프로 한다(개방형 kind·주입식 관계·규칙 기반 cardinality).
# 이 모듈은 그 그래프 위에서 (1) 텍스트→좌표 해소(resolve), (2) 미등록 후보 탐지, (3) LLM/폼이 낸
# items를 현재 그래프와 대조한 병합계획(seed_plan)만 담당한다 — **해석은 LLM, 대조·계획은 결정론**.
#
# graph = get_master() 결과: {"entities":[{id,kind,aliases,label}], "relations":[{from,rel,to}],
#                             "kinds":[...], "rules":{rel:{from_kind,to_kind,max_per_from,promote}}}
import re

MASTER_KINDS = ("node", "process", "product", "project")

# 별칭 매칭 경계 — 'N7'이 'N7P' 안에서 안 잡히게(영숫자 인접 금지). 한글은 영숫자가 아니라
# '7 나노'가 '7 나노미터' 안에서 잡히는 건 허용(더 긴 별칭이 이김).
_LEFT, _RIGHT = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"


def _ents(graph):
    return graph.get("entities", []) or []


def _rules(graph):
    return graph.get("rules", {}) or {}


def _kinds(graph):
    return graph.get("kinds") or list(MASTER_KINDS)


def find(graph, mid, kind=None):
    for e in _ents(graph):
        if str(e.get("id", "")).lower() == str(mid).lower() and (kind is None or e.get("kind") == kind):
            return e
    return None


def parent_of(graph, mid, rel="narrower_of"):
    """graph에서 mid의 부모(to) id — active 관계만."""
    for r in graph.get("relations", []) or []:
        if (r.get("state", "active") == "active" and r.get("rel") == rel
                and str(r.get("from", "")).lower() == str(mid).lower()):
            return r.get("to")
    return None


# ---------- 별칭 매처 (결정론) ----------
def _pat(alias):
    toks = [t for t in re.split(r"\s+", str(alias).strip()) if t]
    if not toks:
        return None
    return _LEFT + r"\s*".join(re.escape(t) for t in toks) + _RIGHT


def build_matcher(graph):
    """(정규식, kind, id, alias) 목록. **긴 별칭 우선** = 가장 구체적인 것이 이긴다(N7P > N7)."""
    out = []
    for e in _ents(graph):
        if e.get("state", "active") != "active":
            continue
        for a in [e.get("id")] + list(e.get("aliases") or []):
            if not a:
                continue
            p = _pat(a)
            if p:
                out.append((p, e.get("kind"), e.get("id"), str(a)))
    out.sort(key=lambda e: (-len(e[3]), e[3]))   # 길이 내림차순, 동률은 사전순(결정론)
    return out


def _overlaps(span, taken):
    return any(not (span[1] <= s or span[0] >= e) for s, e in taken)


def _one(ids):
    u = sorted(set(x for x in ids if x))
    return u[0] if len(u) == 1 else None


def resolve(text, graph):
    """텍스트에서 기준정보를 찾아 좌표로 해소. 완전 결정론.
    - 긴 별칭 우선 + 겹침 방지 → 'N7P'가 'N7'을 이긴다.
    - 규칙(promote=True)인 관계로 **부모 좌표 자동 승격**: process를 잡으면 narrower_of의 node를 채움.
    - 같은 kind에서 서로 다른 id가 여럿이면 좌표 미확정 + conflicts(추측 금지).
    반환 {found[], coordinates{kind:id|None}, conflicts[], spans[]}."""
    text = text or ""
    found, taken = [], []
    for pat, kind, mid, alias in build_matcher(graph):
        for m in re.finditer(pat, text, re.I):
            span = (m.start(), m.end())
            if _overlaps(span, taken):
                continue
            taken.append(span)
            found.append({"surface": m.group(0), "kind": kind, "id": mid,
                          "alias": alias, "span": [span[0], span[1]]})
    found.sort(key=lambda f: f["span"][0])

    by = {}
    for f in found:
        by.setdefault(f["kind"], []).append(f["id"])
    # 규칙 기반 승격: promote=True인 관계의 from_kind를 잡으면 to_kind 부모를 좌표에 추가
    for rel, rule in _rules(graph).items():
        if not rule.get("promote"):
            continue
        fk, tk = rule.get("from_kind"), rule.get("to_kind")
        for cid in list(by.get(fk, [])):
            p = parent_of(graph, cid, rel)
            if p:
                by.setdefault(tk, []).append(p)

    coords, conflicts = {}, []
    for k in list(_kinds(graph)) + [k for k in by if k not in _kinds(graph)]:
        ids = by.get(k, [])
        coords[k] = _one(ids)
        u = sorted(set(ids))
        if len(u) > 1:
            conflicts.append({"kind": k, "ids": u})
    return {"found": found, "coordinates": coords, "conflicts": conflicts, "spans": sorted(taken)}


# 마스터에 없는 '기준정보처럼 보이는' 토큰 — 사람/LLM에게 신규 제안 후보로 넘긴다(결정론 힌트).
# 꼬리는 영숫자 혼합 허용(N7X9·LN07LPP·N7GAE 처럼 숫자로 끝나거나 섞이는 실제 표기).
_CAND = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{1,4}\d{1,3}[A-Za-z0-9]{0,8}|\d{1,2}\s*(?:nm|나노))(?![A-Za-z0-9])", re.I)
_VER = re.compile(r"^[vV]\d")   # 'v2.1' 같은 버전 표기는 후보 아님(흔한 오탐)


def find_unknown_candidates(text, graph):
    """마스터로 해소 안 된 구간에서 node/process 형태 토큰을 후보로. 확정이 아니라 제안(최종은 사람)."""
    text = text or ""
    r = resolve(text, graph)
    taken = [tuple(s) for s in r["spans"]]
    out, seen = [], set()
    for m in _CAND.finditer(text):
        span = (m.start(), m.end())
        if _overlaps(span, taken):
            continue
        s = m.group(0).strip()
        if _VER.match(s):
            continue
        k = re.sub(r"\s+", "", s).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append({"surface": s, "span": [span[0], span[1]]})
    return out


# ---------- items(LLM/폼) → 병합 계획 (결정론, 적용 안 함) ----------
def _has_alias(entity, a):
    k = re.sub(r"\s+", "", str(a)).lower()
    pool = [entity.get("id")] + list(entity.get("aliases") or [])
    return any(re.sub(r"\s+", "", str(x)).lower() == k for x in pool if x)


def seed_plan(graph, items):
    """items(LLM 해석 또는 폼)를 현재 그래프와 대조해 create/update/skip 계획을 만든다. **적용 안 함**
    — 사람이 diff로 검토한 뒤 apply(전통 CRUD와 같은 저장 경로)한다. items 각 항목:
    {type|kind, id, node?, label?, aliases[]}."""
    plan = []
    incoming_ids = {str(it.get("id")).lower() for it in (items or []) if it.get("id")}
    for it in items or []:
        kind = it.get("kind") or it.get("type")
        mid = str(it.get("id") or "").strip()
        if kind not in MASTER_KINDS or not mid:
            continue
        cur = find(graph, mid)
        node = it.get("node")
        if cur is None:
            row = {"op": "create", "type": kind, "id": mid, "aliases": it.get("aliases") or []}
            if node:
                row["node"] = node
            if it.get("label"):
                row["label"] = it["label"]
            if kind == "process" and not node:
                row["warn"] = "process인데 소속 node 없음 — 확인 필요"
            elif node and not find(graph, node, "node") and node.lower() not in incoming_ids:
                row["warn"] = "소속 node '%s'가 마스터에 없음" % node
            plan.append(row)
            continue
        if cur.get("kind") and cur.get("kind") != kind:
            plan.append({"op": "skip", "type": kind, "id": mid,
                         "why": "id가 이미 다른 kind(%s)로 존재 — 무시" % cur.get("kind")})
            continue
        changes = {}
        add = [a for a in (it.get("aliases") or []) if not _has_alias(cur, a)]
        if add:
            changes["aliases_add"] = add
        if it.get("label") and it["label"] != cur.get("label"):
            changes["label"] = {"from": cur.get("label"), "to": it["label"]}
        if node:
            cur_node = parent_of(graph, mid)
            if (cur_node or "").lower() != node.lower():
                changes["node"] = {"from": cur_node, "to": node}
        if changes:
            plan.append({"op": "update", "type": kind, "id": mid, "changes": changes})
        else:
            plan.append({"op": "skip", "type": kind, "id": mid, "why": "동일 — 변경 없음"})
    return plan
