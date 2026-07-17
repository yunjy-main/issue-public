# master.py — 기준정보(마스터) 계층 · 결정론 리졸버 · 시드 파서. LLM 없음.
#
# 기준정보 4종: node(상위 노드) → process(소속 node를 가짐), product, project. 각각 별칭 다수.
# **기준정보와 이슈는 사람이 만든다**(전통 CRUD / 자연어 CRUD). 이 모듈은 자료구조·해소·시드파싱만
# 담당하고 저장은 store가 한다. 자연어 경로(LLM)는 여기 결정론 경로가 못 먹은 것만 처리한다.
#
# 왜 계층인가: 현장에서 'N7'은 **상위 node 명**이고 실제 공정은 N7·N7P·N7PP·LN07LPP·N7GAE 등
# 그 아래 변이다. 표면형도 7nm·7 나노·07 nm·N7+ 처럼 난립한다. flat 별칭으로 두면 N7P 이슈와
# N7PP 이슈가 "둘 다 N7"이라며 오병합된다 — 좌표는 node AND process 둘 다 맞아야 한다.
import json
import re

MASTER_TYPES = ("node", "process", "product", "project")
_KEY = {"node": "nodes", "process": "processes", "product": "products", "project": "projects"}

# 별칭 매칭 경계 — 'N7'이 'N7P' 안에서 잡히면 안 된다(영숫자 인접 금지).
# 한글은 [A-Za-z0-9]가 아니므로 '7 나노'가 '7 나노미터' 안에서 잡히는 건 허용(더 긴 별칭이 이김).
_LEFT, _RIGHT = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"


def empty_master():
    return {"nodes": [], "processes": [], "products": [], "projects": []}


def items_of(master, typ):
    return (master or {}).get(_KEY[typ], []) or []


def find(master, typ, mid):
    for it in items_of(master, typ):
        if str(it.get("id", "")).lower() == str(mid).lower():
            return it
    return None


def process_node(master, pid):
    p = find(master, "process", pid)
    return p.get("node") if p else None


# ---------- 별칭 매처 (결정론) ----------
def _pat(alias):
    """별칭 → 공백에 유연한 정규식. '7 나노'가 '7나노'도, 'N7 plus'가 'N7plus'도 잡게."""
    toks = [t for t in re.split(r"\s+", str(alias).strip()) if t]
    if not toks:
        return None
    return _LEFT + r"\s*".join(re.escape(t) for t in toks) + _RIGHT


def build_matcher(master):
    """(정규식, type, id, alias) 목록. **긴 별칭 우선** = 가장 구체적인 것이 이긴다(N7P > N7)."""
    out = []
    for typ in ("process", "product", "project", "node"):
        for it in items_of(master, typ):
            names = [it.get("id")] + list(it.get("aliases") or [])
            for a in names:
                if not a:
                    continue
                p = _pat(a)
                if p:
                    out.append((p, typ, it.get("id"), str(a)))
    out.sort(key=lambda e: (-len(e[3]), e[3]))   # 길이 내림차순, 동률은 사전순(결정론)
    return out


def _overlaps(span, taken):
    return any(not (span[1] <= s or span[0] >= e) for s, e in taken)


def _one(ids):
    u = sorted(set(x for x in ids if x))
    return u[0] if len(u) == 1 else None


def resolve(text, master):
    """텍스트에서 마스터 항목을 찾아 좌표로 해소한다. 완전 결정론.
    - 긴 별칭 우선 + 겹침 방지 → 'N7P'가 'N7'을 이긴다.
    - process가 잡히면 그 소속 node를 **자동 승격**(텍스트에 node가 없어도 좌표에 채움).
    - 같은 타입에서 서로 다른 id가 여러 개면 좌표를 정하지 않고 conflicts로 보고한다(추측 금지).
    반환: {found[], coordinates{}, conflicts[], spans[]}"""
    text = text or ""
    found, taken = [], []
    for pat, typ, mid, alias in build_matcher(master):
        for m in re.finditer(pat, text, re.I):
            span = (m.start(), m.end())
            if _overlaps(span, taken):
                continue
            taken.append(span)
            found.append({"surface": m.group(0), "type": typ, "id": mid,
                          "alias": alias, "span": [span[0], span[1]]})
    found.sort(key=lambda f: f["span"][0])

    by = {t: [] for t in MASTER_TYPES}
    for f in found:
        by[f["type"]].append(f["id"])
    nodes = list(by["node"])
    for pid in by["process"]:                      # process → node 승격
        n = process_node(master, pid)
        if n:
            nodes.append(n)
    coords = {"node": _one(nodes), "process": _one(by["process"]),
              "product": _one(by["product"]), "project": _one(by["project"])}
    conflicts = []
    for typ, ids in (("node", nodes), ("process", by["process"]),
                     ("product", by["product"]), ("project", by["project"])):
        u = sorted(set(ids))
        if len(u) > 1:
            conflicts.append({"type": typ, "ids": u})
    return {"found": found, "coordinates": coords, "conflicts": conflicts,
            "spans": sorted(taken)}


# 마스터에 없는 '기준정보처럼 보이는' 토큰 — 사람/LLM에게 신규 제안 후보로 넘긴다.
# 꼬리는 영숫자 혼합 허용(N7X9·LN07LPP·N7GAE 처럼 숫자로 끝나거나 섞이는 실제 표기를 잡아야 함).
_CAND = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{1,4}\d{1,3}[A-Za-z0-9]{0,8}|\d{1,2}\s*(?:nm|나노))(?![A-Za-z0-9])", re.I)
_VER = re.compile(r"^[vV]\d")   # 'v2.1' 같은 버전 표기는 기준정보 후보가 아님(흔한 오탐)


def find_unknown_candidates(text, master):
    """마스터로 해소되지 않은 구간에서 node/process 형태의 토큰을 찾아 후보로 낸다(결정론 힌트).
    확정이 아니라 '제안 후보' — 최종 판단은 사람(또는 LLM 제안 → 사람)."""
    text = text or ""
    r = resolve(text, master)
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


# ---------- 시드 파서: JSON / YAML(최소 서브셋) / 표(TSV·CSV) ----------
def _unq(v):
    v = str(v).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _scalar(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [_unq(x) for x in v[1:-1].split(",") if x.strip()]
    return _unq(v)


def parse_yaml_min(text):
    """마스터 시드 shape 전용 **최소 YAML 서브셋** 파서 (stdlib에 yaml이 없고 반입물은 무의존).
    지원: 최상위 `key:` → 아이템 리스트, `- k: v` 아이템, 필드 `k: v`, 인라인 `[a, b]`,
    블록 리스트(`k:` 다음 더 깊은 `- a`). 그 밖의 YAML 문법은 지원하지 않는다(자유형식은 LLM 경로)."""
    root, top, item = {}, None, None
    field, field_indent = None, None
    for raw in (text or "").split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        s = raw.strip()
        if indent == 0:
            if s.endswith(":"):
                top = s[:-1].strip()
                root[top] = []
                item, field, field_indent = None, None, None
            continue
        if s.startswith("-"):
            body = s[1:].strip()
            if field is not None and field_indent is not None and indent > field_indent:
                item[field].append(_unq(body))       # 필드의 블록 리스트 항목
                continue
            if top is None:
                continue
            item = {}
            root[top].append(item)
            field, field_indent = None, None
            if body and ":" in body:
                k, v = body.split(":", 1)
                item[k.strip()] = _scalar(v)
            continue
        if item is not None and ":" in s:
            k, v = s.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                item[k] = []
                field, field_indent = k, indent
            else:
                item[k] = _scalar(v)
                field, field_indent = None, None
    return root


def _items_from_obj(obj):
    """우리 마스터 shape({nodes:[…]}) 또는 flat 리스트([{type,id,…}]) → 표준 item 목록."""
    out, errors = [], []
    if isinstance(obj, dict):
        for typ, key in _KEY.items():
            for it in (obj.get(key) or []):
                if not isinstance(it, dict) or not it.get("id"):
                    errors.append("id 없는 %s 항목 건너뜀: %r" % (typ, it))
                    continue
                out.append({"type": typ, "id": str(it["id"]).strip(),
                            "node": (it.get("node") or None),
                            "label": it.get("label") or None,
                            "aliases": [str(a).strip() for a in (it.get("aliases") or []) if str(a).strip()]})
    elif isinstance(obj, list):
        for it in obj:
            if not isinstance(it, dict):
                errors.append("dict 아님: %r" % (it,))
                continue
            t = str(it.get("type", "")).strip().lower()
            if t not in MASTER_TYPES or not it.get("id"):
                errors.append("type/id 불명 항목 건너뜀: %r" % (it,))
                continue
            out.append({"type": t, "id": str(it["id"]).strip(),
                        "node": it.get("node") or None, "label": it.get("label") or None,
                        "aliases": [str(a).strip() for a in (it.get("aliases") or []) if str(a).strip()]})
    else:
        errors.append("지원하지 않는 최상위 형식")
    return out, errors


_HDR_ALIAS = {"type": "type", "유형": "type", "구분": "type",
              "id": "id", "아이디": "id", "이름": "id", "name": "id", "코드": "id",
              "node": "node", "노드": "node", "상위": "node", "parent": "node",
              "label": "label", "설명": "label", "라벨": "label",
              "aliases": "aliases", "별칭": "aliases", "alias": "aliases", "동의어": "aliases"}


def _split_row(line):
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    return [c.strip() for c in line.split(",")]


def _items_from_table(text):
    """헤더가 있는 TSV/CSV만 결정론 파싱한다. 헤더가 없으면 컬럼 의미를 추측해야 하므로
    **파싱하지 않고 LLM 경로로 넘긴다**(틀린 해석보다 미해석이 낫다)."""
    lines = [l for l in (text or "").split("\n") if l.strip()]
    if not lines:
        return [], ["빈 표"]
    hdr = [_HDR_ALIAS.get(c.strip().lower()) for c in _split_row(lines[0])]
    if "id" not in hdr:
        return [], ["헤더 없음/인식 불가 — 결정론 파싱 불가(자연어 경로로 처리). "
                    "인식 컬럼: type·id·node·label·aliases (한글 유형·아이디·노드·설명·별칭)"]
    out, errors = [], []
    for ln in lines[1:]:
        cells = _split_row(ln)
        row = {}
        for i, key in enumerate(hdr):
            if key and i < len(cells) and cells[i]:
                row[key] = cells[i]
        if not row.get("id"):
            continue
        t = str(row.get("type", "")).strip().lower()
        if t not in MASTER_TYPES:
            errors.append("행의 type 불명(%r) — 건너뜀: %s" % (row.get("type"), ln.strip()))
            continue
        aliases = [a.strip() for a in re.split(r"[,;|]", row.get("aliases", "")) if a.strip()]
        out.append({"type": t, "id": row["id"], "node": row.get("node") or None,
                    "label": row.get("label") or None, "aliases": aliases})
    return out, errors


def parse_seed(text):
    """시드 텍스트 → {format, items[], errors[]}. JSON/YAML/표를 **결정론**으로 처리하고,
    자유형식은 format='unknown'으로 돌려 자연어(LLM) 경로가 맡게 한다."""
    t = (text or "").strip()
    if not t:
        return {"format": "empty", "items": [], "errors": []}
    if t[:1] in "{[":
        try:
            items, errs = _items_from_obj(json.loads(t))
            return {"format": "json", "items": items, "errors": errs}
        except ValueError as e:
            return {"format": "json", "items": [], "errors": ["JSON 파싱 실패: %s" % e]}
    # YAML: 최상위에 'nodes:/processes:/products:/projects:' 가 보이면
    if re.search(r"^(nodes|processes|products|projects)\s*:\s*$", t, re.M):
        try:
            items, errs = _items_from_obj(parse_yaml_min(t))
            return {"format": "yaml", "items": items, "errors": errs}
        except Exception as e:  # noqa — 파서 한계는 LLM 경로로
            return {"format": "yaml", "items": [], "errors": ["YAML(서브셋) 파싱 실패: %s" % e]}
    first = t.split("\n", 1)[0]
    if "\t" in first or "," in first:
        items, errs = _items_from_table(t)
        if items or "헤더" not in " ".join(errs):
            return {"format": "table", "items": items, "errors": errs}
        return {"format": "unknown", "items": [], "errors": errs}
    return {"format": "unknown", "items": [],
            "errors": ["형식 감지 실패 — 자연어(LLM) 경로로 처리"]}


# ---------- 시드 → 병합 계획 (결정론, 적용 안 함) ----------
def _has_alias(item, a):
    k = re.sub(r"\s+", "", str(a)).lower()
    pool = [item.get("id")] + list(item.get("aliases") or [])
    return any(re.sub(r"\s+", "", str(x)).lower() == k for x in pool if x)


def seed_plan(master, items):
    """시드 items를 현재 마스터와 대조해 create/update/skip 계획을 만든다. **적용하지 않는다**
    — 사람이 diff로 검토한 뒤 apply(전통 CRUD와 같은 저장 경로)한다."""
    plan = []
    for it in items or []:
        cur = find(master, it["type"], it["id"])
        if cur is None:
            row = {"op": "create", "type": it["type"], "id": it["id"],
                   "aliases": it.get("aliases") or []}
            if it.get("node"):
                row["node"] = it["node"]
            if it.get("label"):
                row["label"] = it["label"]
            if it["type"] == "process" and not it.get("node"):
                row["warn"] = "process인데 소속 node 없음 — 확인 필요"
            elif it.get("node") and not find(master, "node", it["node"]) \
                    and not any(x["type"] == "node" and x["id"] == it["node"] for x in items):
                row["warn"] = "소속 node '%s'가 마스터에 없음" % it["node"]
            plan.append(row)
            continue
        changes = {}
        add = [a for a in (it.get("aliases") or []) if not _has_alias(cur, a)]
        if add:
            changes["aliases_add"] = add
        if it.get("label") and it["label"] != cur.get("label"):
            changes["label"] = {"from": cur.get("label"), "to": it["label"]}
        if it.get("node") and it["node"] != cur.get("node"):
            changes["node"] = {"from": cur.get("node"), "to": it["node"]}
        if changes:
            plan.append({"op": "update", "type": it["type"], "id": cur["id"], "changes": changes})
        else:
            plan.append({"op": "skip", "type": it["type"], "id": cur["id"], "why": "동일 — 변경 없음"})
    return plan
