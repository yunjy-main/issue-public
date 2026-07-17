# issue 지식 저장소 — 파일 기반, stdlib only (ADR-001)
# JSONL 이벤트(불변 이력, source of truth) + JSON/YAML projection(재생성 가능한 현재 상태)
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
K = os.path.join(ROOT, "knowledge")
DIRS = {
    "sources": os.path.join(K, "sources"),
    "raw": os.path.join(K, "sources", "raw"),
    "proposals": os.path.join(K, "proposals"),
    "events": os.path.join(K, "events"),
    "entities": os.path.join(K, "entities"),
    "relations": os.path.join(K, "relations"),
    "config": os.path.join(K, "config"),
    "state": os.path.join(K, "state"),
    "master": os.path.join(K, "master"),   # 기준정보(사람이 만드는 좌표축)
    "docs": os.path.join(K, "docs_struct"),   # STRUCTURE 산출물(구조화 문서)
}
KST = timezone(timedelta(hours=9))
_LOCK = threading.RLock()   # 쓰기/읽기 모두 이 락 하에서 파일 접근 (win32 os.replace 경합 방지)


def ensure_dirs():
    for d in DIRS.values():
        os.makedirs(d, exist_ok=True)


def now_iso():
    return datetime.now(KST).replace(microsecond=0).isoformat()


# ---------- 시각 비교 (오프셋 정규화, BG-12) ----------
def _ts_key(s):
    """ISO 시각 → 비교용 UTC instant(float). tz 없음(date-only 등)·파싱불가는 문자열 그대로.
    'Z' 접미사도 파싱(Python<3.11 fromisoformat 대비)."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).timestamp() if d.tzinfo else str(s)
    except (ValueError, TypeError):
        return str(s)


def ts_min(a, b):
    """이른 시각. 둘 다 tz-aware면 instant 비교, 아니면 **문자열 사전식** 폴백(타입 안전 — int×str crash 방지)."""
    ka, kb = _ts_key(a), _ts_key(b)
    if isinstance(ka, float) and isinstance(kb, float):
        return a if ka <= kb else b
    return a if str(a) <= str(b) else b


def ts_max(a, b):
    ka, kb = _ts_key(a), _ts_key(b)
    if isinstance(ka, float) and isinstance(kb, float):
        return a if ka >= kb else b
    return a if str(a) >= str(b) else b


# ---------- 이슈 상태 생애주기 · 중요도 (M5) ----------
STATUS_ORDER = ["정의", "원인탐색", "원인분석", "원인발견", "해결책탐색", "재발방지", "해결중", "종결"]
_SEVERITY_RANK = {"S1": 4, "S2": 3, "S3": 2, "S4": 1}


def status_rank(s):
    return STATUS_ORDER.index(s) if s in STATUS_ORDER else -1


def merge_status(a, b):
    """병합 시 더 진행된 상태 채택. 재발(재오픈)은 우선. 보류는 진행상태에 양보."""
    if not a:
        return b
    if not b:
        return a
    if "재발" in (a, b):
        return "재발"
    ra, rb = status_rank(a), status_rank(b)
    if ra < 0 and rb < 0:
        return a
    return a if ra >= rb else b


def merge_severity(a, b):
    if not a:
        return b
    if not b:
        return a
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


# 후처리 편집 허용 필드 (그 외는 보호: id·state·revision·source_refs·captured_at·produced_by·merged_into).
# type은 경고 후 허용(프론트 경고) — 여기 포함하되 UI에서 확인.
# type은 일반 편집이 아니라 재분류(reclassify_entity)로 — ID 접두사 교정 + 관계 이관.
EDITABLE_FIELDS = ("title", "summary", "process", "product", "project",
                   "start_date", "deadline", "occurred_at", "date_confidence",
                   "date_note", "tags", "status", "severity", "closed_at")


def reclassify_entity(old_id, new_type, actor="human"):
    """유형 재분류 — 새 접두사 ID를 발급해 필드 이관, 관계를 새 ID로 재지정, 옛것 소프트삭제.
    ID가 유형을 거짓말하지 않게 한다(FINDING-이 사실은 issue인 문제 해결). 반환: 새 엔티티 or None."""
    with _LOCK:
        old = get_entity(old_id)
        if old is None or old.get("state") == "merged":
            return None
        new_id = next_id(new_type.upper())
        obj = dict(old)
        obj["id"] = new_id
        obj["type"] = new_type
        obj["state"] = "active"
        obj["reclassified_from"] = old_id
        obj.pop("revision", None)      # 새 계보 → revision 1부터
        obj.pop("merged_into", None)
        new_ent = upsert_entity(obj, actor)
        redirect_relations([old_id], new_id, actor)   # 관계를 새 ID로 이관
        soft_delete_entity(old_id, new_id, actor)      # 옛것 숨김(감사·복구)
        return new_ent


# ---------- 최소 YAML 덤퍼 (사람 열람용 projection 전용, 파서 아님) ----------
_YAML_SPECIAL = re.compile(r"[:#\[\]{}&*!|>'\"%@`,]|^[\s-]|[\s]$")


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # 개행/제어문자·특수문자·앞뒤 공백이 있으면 이중따옴표로 감싸고 이스케이프
    if s == "" or "\n" in s or "\t" in s or "\r" in s or _YAML_SPECIAL.search(s):
        s = (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r"))
        return '"' + s + '"'
    return s


def yaml_dump(obj, indent=0):
    pad = "  " * indent
    out = []
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n"
        for k, v in obj.items():
            key = _yaml_scalar(k)   # 키도 이스케이프
            if isinstance(v, (dict, list)) and v:
                out.append(pad + key + ":\n")
                out.append(yaml_dump(v, indent + 1))
            else:
                out.append(pad + key + ": " + (
                    "{}" if v == {} else "[]" if v == [] else _yaml_scalar(v)) + "\n")
    elif isinstance(obj, list):
        if not obj:
            return pad + "[]\n"
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                inner = yaml_dump(item, indent + 1)
                out.append(pad + "- " + inner[len(pad) + 2:])  # 첫 줄만 '- '로 당겨 붙임
            else:
                out.append(pad + "- " + _yaml_scalar(item) + "\n")
    else:
        return pad + _yaml_scalar(obj) + "\n"
    return "".join(out)


# ---------- 원자적 쓰기 (스레드별 tmp + win32 재시도) ----------
def _atomic_write(path, text):
    tmp = "%s.%d.tmp" % (path, threading.get_ident())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.02 * (attempt + 1))


def _read_json(path):
    """존재하지 않거나 손상된 파일은 None (호출부에서 격리)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json_yaml(base_no_ext, obj):
    _atomic_write(base_no_ext + ".json", json.dumps(obj, ensure_ascii=False, indent=2))
    _atomic_write(base_no_ext + ".yaml", yaml_dump(obj))


# ---------- 카운터 / ID ----------
def _counters_path():
    return os.path.join(DIRS["state"], "counters.json")


def _rebuild_counters():
    """카운터 소실 시 기존 파일명에서 최대값을 복원 (리셋→중복 ID 발급 방지)."""
    c = {}
    scan = [(DIRS["entities"], ".json"), (DIRS["relations"], ".json"),
            (DIRS["raw"], ".txt"), (DIRS["sources"], ".json")]
    for d, ext in scan:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = re.match(r"([A-Z]+)-(\d+)\." + ext.lstrip(".") + "$", name)
            if m:
                pfx, n = m.group(1), int(m.group(2))
                c[pfx] = max(c.get(pfx, 0), n)
    for d in [DIRS["proposals"]]:
        if os.path.isdir(d):
            for name in os.listdir(d):
                m = re.match(r"(RUN)-(\d+)$", name)
                if m:
                    c["RUN"] = max(c.get("RUN", 0), int(m.group(2)))
    # OP-id는 파일명이 아니라 trashed_by 필드에만 있으므로 엔티티·관계 JSON에서 최댓값 복원 (BG-9)
    for d in [DIRS["entities"], DIRS["relations"]]:
        if os.path.isdir(d):
            for name in os.listdir(d):
                if not name.endswith(".json"):
                    continue
                tb = (_read_json(os.path.join(d, name)) or {}).get("trashed_by")
                m = re.match(r"OP-(\d+)$", tb) if tb else None
                if m:
                    c["OP"] = max(c.get("OP", 0), int(m.group(1)))
    return c


def next_id(prefix):
    with _LOCK:
        p = _counters_path()
        c = _read_json(p)
        if c is None:
            c = _rebuild_counters()
        c[prefix] = c.get(prefix, 0) + 1
        _atomic_write(p, json.dumps(c, ensure_ascii=False, indent=2))
        return "%s-%06d" % (prefix, c[prefix])


# ---------- 이벤트 (append-only, source of truth) ----------
def append_event(evt):
    with _LOCK:
        evt = dict(evt)
        evt.setdefault("ts", now_iso())
        month = datetime.now(KST).strftime("%Y-%m")
        path = os.path.join(DIRS["events"], month + ".jsonl")
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
            f.flush()
        return evt


# ---------- 이벤트 읽기 · 엔티티 검토 이력 (M5, codex review_history 벤치마킹) ----------
def _read_events():
    with _LOCK:
        d = DIRS["events"]
        if not os.path.isdir(d):
            return []
        evs = []
        for name in sorted(os.listdir(d)):
            if not name.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                evs.append(json.loads(line))
                            except ValueError:
                                pass
            except OSError:
                pass
        evs.sort(key=lambda e: e.get("ts", ""))
        return evs


def entity_history(eid):
    """한 엔티티의 검토·변경 이력을 시간순으로. review.* 결정 + entity.upsert 리비전(변경필드 diff)."""
    out, prev = [], None
    for ev in _read_events():
        et = ev.get("event", "")
        if et.startswith("review.") and ev.get("entity_id") == eid:
            row = {"ts": ev.get("ts"), "kind": et, "decision": ev.get("decision"),
                   "changed": ev.get("changed_fields") or [], "actor": ev.get("actor", "?")}
            if et in ("review.relation_add", "review.relation_remove"):
                row["rel"] = {"from": ev.get("from"), "to": ev.get("to"), "type": ev.get("rtype")}
            out.append(row)
        elif et == "entity.upsert" and ev.get("id") == eid:
            data = ev.get("data") or {}
            changed = []
            if prev is not None:
                for k in set(list(prev.keys()) + list(data.keys())):
                    if k in ("revision", "updated_at"):
                        continue
                    if prev.get(k) != data.get(k):
                        changed.append(k)
            state = data.get("state")
            if state == "merged":
                kind = "entity.retire" if data.get("retired") else "entity.merge"
            elif ev.get("revision") == 1:
                kind = "entity.reclassify" if data.get("reclassified_from") else "entity.create"
            else:
                kind = "entity.update"
            out.append({"ts": ev.get("ts"), "kind": kind, "revision": ev.get("revision"),
                        "actor": ev.get("actor", "?"), "changed": sorted(changed),
                        "merged_into": data.get("merged_into"),
                        "reclassified_from": data.get("reclassified_from")})
            prev = data
    return out


# ---------- vocabulary ----------
def load_vocab():
    p = os.path.join(DIRS["config"], "vocabulary.json")
    v = _read_json(p)
    return v if v is not None else {"contexts": [], "keywords": {}}


def save_vocab_projection(vocab):
    with _LOCK:
        _atomic_write(os.path.join(DIRS["config"], "vocabulary.yaml"), yaml_dump(vocab))


# ---------- 원본 (capture-first). 이벤트 먼저, 그다음 projection ----------
def capture_source(text, channel=None, actor="human", first_captured_at=None, guide=None):
    """channel은 하위호환 인자 — 사용자가 지정하지 않으며(UI에서 제거됨),
    채널 판정은 EXTRACT 단계에서 LLM이 source_profile로 수행해 set_source_profile로 스탬프한다.
    first_captured_at: 재추출 시 원본의 '최초 전송 시각'을 이어받아 계보에 보존.
    guide: 사용자가 준 선택적 가이드('이 본문은 어느 공정/제품/과제 건인지'). 본문과 **분리 보관**한다 —
    가이드는 사실이 아니라 해석 힌트라서 원문에 섞으면 출처가 오염된다(본문이 모호할 때만 좌표를 채우고,
    본문이 명시하면 본문이 이긴다: MASTER_RESOLVE)."""
    with _LOCK:
        sid = next_id("SRC")
        now = now_iso()
        # first_captured_at은 ISO 문자열만 허용 — 숫자·비ISO(외부 클라이언트 오입력)면 now로 강등(BG-12 crash 방어)
        fca = first_captured_at if isinstance(first_captured_at, str) and first_captured_at else now
        meta = {
            "id": sid, "channel": channel or "unspecified",
            "chars": len(text), "lines": text.count("\n") + 1,
            "captured_at": now,
            "first_captured_at": fca,
        }
        g = (guide or "").strip()
        if g:
            meta["guide"] = g
        # 원문 저장(불변) → 이벤트 → projection
        _atomic_write(os.path.join(DIRS["raw"], sid + ".txt"), text)
        append_event({"event": "source.capture", "id": sid, "actor": actor, "data": meta})
        _write_json_yaml(os.path.join(DIRS["sources"], sid), meta)
        return meta


def set_source_profile(sid, profile, actor="llm"):
    """EXTRACT가 판정한 출처 프로파일(channels[] 다중 채널·category·note)을 source 메타에 스탬프."""
    if not profile:
        return None
    with _LOCK:
        meta = _read_json(os.path.join(DIRS["sources"], sid + ".json"))
        if not meta:
            return None
        meta["source_profile"] = profile
        append_event({"event": "source.profile", "id": sid, "actor": actor, "data": profile})
        _write_json_yaml(os.path.join(DIRS["sources"], sid), meta)
        return meta


def read_source_text(sid):
    with _LOCK:
        p = os.path.join(DIRS["raw"], sid + ".txt")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return f.read()


def get_source(sid):
    with _LOCK:
        return _read_json(os.path.join(DIRS["sources"], sid + ".json"))


# ---------- proposals (RUN 단위) ----------
def save_proposal(run_id, source_id, proposal, producer, extra=None):
    """proposal.json = 그 RUN 후보의 감사 정본. extra로 provenance(retrieved·fallback·operations·
    links·ref_time·revision·revision_drift)를 함께 영속한다(BG-2: job.json에만 있던 비대칭 해소).
    /api/run(get_proposal)이 이 파일 전체를 돌려주므로 provenance가 감사 정본에 노출된다."""
    with _LOCK:
        rdir = os.path.join(DIRS["proposals"], run_id)
        os.makedirs(rdir, exist_ok=True)
        rec = {
            "run_id": run_id, "source_id": source_id,
            "producer": producer, "created_at": now_iso(),
            "entities": proposal.get("entities", []),
            "relations": proposal.get("relations", []),
        }
        # LLM이 판정한 출처 프로파일(채널 다중분류·내용 분류) — 수동 채널 입력 대체
        if proposal.get("source_profile"):
            rec["source_profile"] = proposal["source_profile"]
        if extra:
            rec.update(extra)   # provenance를 감사 정본(proposal.json)에 영속
        ev_data = {"source_id": source_id,
                   "n_entities": len(rec["entities"]),
                   "n_relations": len(rec["relations"])}
        if extra:   # append-only 로그에도 provenance 신호를 남긴다(§7 감사)
            ev_data.update({
                "n_retrieved": len(extra.get("retrieved") or []),
                "n_operations": len(extra.get("operations") or []),
                "fallback": bool(extra.get("fallback")),
                "revision": (extra.get("revision") or {}).get("config_revision"),
                "revision_drift": bool(extra.get("revision_drift")),
            })
        append_event({"event": "proposal.create", "run_id": run_id,
                      "actor": producer.get("name", "?"), "data": ev_data})
        _atomic_write(os.path.join(rdir, "proposal.json"),
                      json.dumps(rec, ensure_ascii=False, indent=2))
        _atomic_write(os.path.join(rdir, "review.yaml"), yaml_dump(rec))
        return rec


def get_proposal(run_id):
    with _LOCK:
        return _read_json(os.path.join(DIRS["proposals"], run_id, "proposal.json"))


# ---------- 비동기 EXTRACT 작업 상태 (job.json) ----------
def set_job(run_id, obj):
    with _LOCK:
        rdir = os.path.join(DIRS["proposals"], run_id)
        os.makedirs(rdir, exist_ok=True)
        _atomic_write(os.path.join(rdir, "job.json"), json.dumps(obj, ensure_ascii=False))


def get_job(run_id):
    with _LOCK:
        return _read_json(os.path.join(DIRS["proposals"], run_id, "job.json"))


def get_committed(run_id):
    with _LOCK:
        return _read_json(os.path.join(DIRS["proposals"], run_id, "committed.json"))


# ---------- RUN revision 매니페스트 (P0-1: ref_time·설정·프롬프트·온톨로지 고정) ----------
def save_manifest(run_id, obj):
    with _LOCK:
        rdir = os.path.join(DIRS["proposals"], run_id)
        os.makedirs(rdir, exist_ok=True)
        _atomic_write(os.path.join(rdir, "manifest.json"), json.dumps(obj, ensure_ascii=False))


def get_manifest(run_id):
    with _LOCK:
        return _read_json(os.path.join(DIRS["proposals"], run_id, "manifest.json"))


def save_inputs(run_id, obj):
    """RUN이 실제로 쓴 입력(현재: vocab 스냅샷)을 고정한다. COMMIT이 EXTRACT와 동일 vocab으로
    해소해 핫리로드 사이 관계 조용소실을 막는다(BG-4). 프롬프트·설정 완전 pin은 BG-13에서 확장."""
    with _LOCK:
        rdir = os.path.join(DIRS["proposals"], run_id)
        os.makedirs(rdir, exist_ok=True)
        _atomic_write(os.path.join(rdir, "inputs.json"), json.dumps(obj, ensure_ascii=False))


def get_inputs(run_id):
    with _LOCK:
        return _read_json(os.path.join(DIRS["proposals"], run_id, "inputs.json"))


# ---------- EXTRACT 워커 생성(generation) 가드 (BG-7) ----------
def begin_extract(run_id, source_id, started_at):
    """새 EXTRACT 시도 시작 — generation을 원자적으로 +1 하고 running으로 표시. 워커는 이 generation을
    들고 있다가 완료 시 현재값과 대조한다(같은 run_id 재시도로 뜬 stale 워커의 쓰기·이벤트 폐기)."""
    with _LOCK:
        gen = ((get_job(run_id) or {}).get("generation") or 0) + 1
        set_job(run_id, {"state": "running", "source_id": source_id,
                         "started_at": started_at, "generation": gen})
        return gen


def finalize_extract(run_id, generation, source_id, proposal, producer, extra, started_at, extract_ms):
    """워커 결과를 원자적으로 커밋. 현재 generation과 다르면(재시도로 superseded) 폐기하고 None 반환 —
    save_proposal의 proposal.create 이벤트 append도 이 게이트 안에서만 일어나 stale 워커가
    불변 로그를 오염시키지 않는다(_LOCK은 재진입 가능이라 save_proposal 중첩 획득 OK)."""
    with _LOCK:
        cur = (get_job(run_id) or {}).get("generation")
        if cur is not None and generation != cur:
            return None
        rec = save_proposal(run_id, source_id, proposal, producer, extra=extra)
        set_job(run_id, {"state": "done", "source_id": source_id, "started_at": started_at,
                         "extract_ms": extract_ms, "generation": generation, "proposal": rec})
        return rec


def fail_extract(run_id, generation, err):
    """워커 실패를 generation 게이트 하에 기록. superseded면 무시(최신 running/done을 덮지 않음)."""
    with _LOCK:
        cur = (get_job(run_id) or {}).get("generation")
        if cur is not None and generation != cur:
            return False
        set_job(run_id, dict(err, generation=generation))
        return True


def reap_running(reason="서버 재기동으로 EXTRACT 워커 중단", code="E-2008"):
    """BG-11: EXTRACT 워커는 daemon 스레드라 서버 재기동에서 살아남지 못한다. 따라서 시작 시
    state==running인 job은 전부 stale — error로 전이해 '영구 running 고착'을 막는다(UI에 재시도 노출).
    서버 부팅 시 1회 호출. 전이된 run_id 목록 반환."""
    with _LOCK:
        d = DIRS["proposals"]
        if not os.path.isdir(d):
            return []
        reaped = []
        for name in sorted(os.listdir(d)):
            rdir = os.path.join(d, name)
            if not os.path.isdir(rdir):
                continue
            job = _read_json(os.path.join(rdir, "job.json"))
            if job and job.get("state") == "running":
                set_job(name, dict(job, state="error", code=code, error=reason, http=503))
                append_event({"event": "extract.reaped", "run_id": name, "actor": "reaper",
                              "data": {"reason": reason}})
                reaped.append(name)
        return reaped


def list_runs():
    with _LOCK:
        d = DIRS["proposals"]
        if not os.path.isdir(d):
            return []
        runs = []
        for name in sorted(os.listdir(d), reverse=True):
            rdir = os.path.join(d, name)
            if not os.path.isdir(rdir):
                continue
            job = _read_json(os.path.join(rdir, "job.json"))
            prop = _read_json(os.path.join(rdir, "proposal.json"))
            if job is None and prop is None:
                continue
            # running run은 proposal이 아직 없으므로 job으로 노출 (새로고침 재연결용)
            entry = {"run_id": name,
                     "state": (job or {}).get("state", "done" if prop else "unknown"),
                     "committed": os.path.exists(os.path.join(rdir, "committed.json")),
                     "created_at": (prop or {}).get("created_at"),
                     "source_id": (prop or {}).get("source_id") or (job or {}).get("source_id")}
            if prop:
                entry["n_entities"] = len(prop.get("entities", []))
                entry["n_relations"] = len(prop.get("relations", []))
            runs.append(entry)
        return runs


def mark_committed(run_id, committed):
    with _LOCK:
        _atomic_write(os.path.join(DIRS["proposals"], run_id, "committed.json"),
                      json.dumps(committed, ensure_ascii=False, indent=2))


# ---------- entity / relation projection. 이벤트 먼저, 그다음 projection ----------
def upsert_entity(obj, actor="human"):
    with _LOCK:
        obj = dict(obj)
        prev = get_entity(obj["id"])
        obj["revision"] = (prev.get("revision", 0) + 1) if prev else 1
        obj["updated_at"] = now_iso()
        append_event({"event": "entity.upsert", "id": obj["id"],
                      "revision": obj["revision"], "actor": actor, "data": obj})
        _write_json_yaml(os.path.join(DIRS["entities"], obj["id"]), obj)
        return obj


def edit_entity(eid, patch, actor="human"):
    """후처리 편집 — EDITABLE_FIELDS만 반영(보호 필드는 거부). upsert로 revision++·이벤트 감사.
    반환: (updated_entity or None, rejected_keys[])."""
    with _LOCK:
        e = get_entity(eid)
        if e is None:
            return None, []
        e = dict(e)
        rejected, changed = [], False
        for k, v in (patch or {}).items():
            if k not in EDITABLE_FIELDS:
                rejected.append(k)
                continue
            if e.get(k) == v:
                continue
            if v in ("", None):
                e.pop(k, None)
            else:
                e[k] = v
            changed = True
        if changed:
            e = upsert_entity(e, actor)
        return e, rejected


def upsert_relation(obj, actor="human"):
    with _LOCK:
        obj = dict(obj)
        prev = get_relation(obj["id"])
        obj["revision"] = (prev.get("revision", 0) + 1) if prev else 1
        obj["updated_at"] = now_iso()
        append_event({"event": "relation.upsert", "id": obj["id"],
                      "revision": obj["revision"], "actor": actor, "data": obj})
        _write_json_yaml(os.path.join(DIRS["relations"], obj["id"]), obj)
        return obj


def _read_all(dirkey):
    with _LOCK:
        d = DIRS[dirkey]
        if not os.path.isdir(d):
            return []
        out = []
        for name in sorted(os.listdir(d)):
            if name.endswith(".json") and not name.endswith(".tmp"):
                o = _read_json(os.path.join(d, name))
                if o is not None:
                    out.append(o)
        return out


def rebuild_projection(strict=True):
    """이벤트 로그로부터 projection(엔티티·관계·source-메타)을 재생성한다 (BG-10, ARCH §2.4).
    각 id의 *.upsert/capture 이벤트 중 **최신(최대 revision) data 스냅샷**을 projection에 직접 쓴다 —
    append_event를 거치지 않으므로 revision 재증가·이력 이중화가 없다(idempotent). 재생 범위는
    이벤트-정본인 **엔티티·관계·source-메타뿐**(raw 원문·proposal·provenance는 대상 아님, BG-1).
    rebuild는 STATE를 정하므로 history 표시(_read_events, 관대)와 달리 **fail-loud**: 손상 이벤트 라인이
    있으면 projection을 건드리지 않고 보류한다(strict=True). force(strict=False)면 손상은 건너뛰고 강행."""
    with _LOCK:
        latest_ent, latest_rel, latest_src, latest_prof = {}, {}, {}, {}
        bad = []
        d = DIRS["events"]
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if not name.endswith(".jsonl"):
                    continue
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError as ex:
                            bad.append({"file": name, "line": lineno, "error": str(ex)})
                            continue
                        ev, eid, data = e.get("event"), e.get("id"), e.get("data")
                        if not eid or data is None:
                            continue
                        rev = e.get("revision", 0)
                        if ev == "entity.upsert" and (eid not in latest_ent or rev >= latest_ent[eid][0]):
                            latest_ent[eid] = (rev, data)
                        elif ev == "relation.upsert" and (eid not in latest_rel or rev >= latest_rel[eid][0]):
                            latest_rel[eid] = (rev, data)
                        elif ev == "source.capture":
                            latest_src[eid] = data
                        elif ev == "source.profile":
                            latest_prof[eid] = data
        if strict and bad:   # fail-loud: 손상 로그면 projection을 건드리지 않고 보류
            return {"ok": False, "corrupt_events": bad, "entities": 0, "relations": 0, "sources": 0,
                    "msg": "이벤트 손상 %d줄 — rebuild 보류(force로 강행 가능)" % len(bad)}
        for eid, (rev, data) in latest_ent.items():
            _write_json_yaml(os.path.join(DIRS["entities"], eid), data)
        for rid, (rev, data) in latest_rel.items():
            _write_json_yaml(os.path.join(DIRS["relations"], rid), data)
        for sid, meta in latest_src.items():
            m = dict(meta)
            if sid in latest_prof:
                m["source_profile"] = latest_prof[sid]   # source.profile 이벤트 병합
            _write_json_yaml(os.path.join(DIRS["sources"], sid), m)
        return {"ok": True, "entities": len(latest_ent), "relations": len(latest_rel),
                "sources": len(latest_src), "corrupt_events": bad}


def get_entity(eid):
    with _LOCK:
        return _read_json(os.path.join(DIRS["entities"], eid + ".json"))


def get_relation(rid):
    with _LOCK:
        return _read_json(os.path.join(DIRS["relations"], rid + ".json"))


def list_entities(include_merged=False, include_trashed=False):
    ents = _read_all("entities")
    if not include_merged:
        ents = [e for e in ents if e.get("state") != "merged"]
    if not include_trashed:
        ents = [e for e in ents if e.get("state") != "trashed"]
    return ents


def list_relations(include_merged=False, include_trashed=False):
    rels = _read_all("relations")
    if not include_merged:
        rels = [r for r in rels if r.get("state") != "merged"]
    if not include_trashed:
        rels = [r for r in rels if r.get("state") != "trashed"]
    return rels


# ---------- 중복 병합 (소프트삭제 + 관계 재지정, M5) ----------
def soft_delete_entity(eid, merged_into, actor="human"):
    """엔티티를 state=merged로 소프트삭제(목록·통계서 제외). 감사 위해 파일·이벤트는 남긴다."""
    with _LOCK:
        e = get_entity(eid)
        if not e:
            return None
        e = dict(e)
        e["state"] = "merged"
        e["merged_into"] = merged_into
        return upsert_entity(e, actor)   # revision++


def set_hidden(eid, hidden, actor="human"):
    """숨기기(표시 전용) — 그래프엔 그대로 두고 hidden 플래그만. UI 리스트서만 제외."""
    with _LOCK:
        e = get_entity(eid)
        if e is None:
            return None
        e = dict(e)
        if hidden:
            e["hidden"] = True
        else:
            e.pop("hidden", None)
        return upsert_entity(e, actor)


def trash_entity(eid, actor="human"):
    """사용자 삭제(BG-9) — state=trashed로 병합과 구분하고 복구 가능하게 한다. 함께 비활성화되는 관계에
    trashed_by=op_id를 달아, 복원 시 '이 삭제로 없어진 관계'만 정확히 되살린다. 하드삭제 아님.
    반환: (엔티티, op_id) 또는 None(대상 없음/이미 active 아님)."""
    with _LOCK:
        e = get_entity(eid)
        if e is None or e.get("state") != "active":
            return None
        op_id = next_id("OP")
        affected = []
        for r in _read_all("relations"):
            if r.get("state") == "active" and (r.get("from") == eid or r.get("to") == eid):
                r2 = dict(r); r2["state"] = "trashed"; r2["trashed_by"] = op_id
                upsert_relation(r2, actor)
                affected.append(r["id"])
        e = dict(e); e["state"] = "trashed"; e["trashed_by"] = op_id; e["trashed_at"] = now_iso()
        ent = upsert_entity(e, actor)
        append_event({"event": "entity.trash", "entity_id": eid, "op_id": op_id,
                      "relations": affected, "actor": actor})
        return ent, op_id


def restore_entity(eid, actor="human"):
    """휴지통 복원(BG-9) — trashed 엔티티를 active로 되돌리고, 이 엔티티에 닿는 trashed 관계 중
    **양 끝이 현재 active인 것**을 되살린다. 관계는 엔티티 삭제의 부수효과로만 trashed되므로
    '양끝 active ⇒ 관계 active'는 불변식이다. op_id 태그에만 의존하면 인접 두 엔티티를 각각 삭제 후
    둘 다 복원할 때 연결 관계가 영구 유실된다(적대검증 결함1). 반환: 요약 dict 또는 None."""
    with _LOCK:
        e = get_entity(eid)
        if e is None or e.get("state") != "trashed":
            return None
        e = dict(e); e["state"] = "active"; e.pop("trashed_by", None); e.pop("trashed_at", None)
        ent = upsert_entity(e, actor)
        active_ids = {x["id"] for x in _read_all("entities") if x.get("state") == "active"}
        restored, skipped = [], []
        for r in _read_all("relations"):
            if r.get("state") != "trashed" or (r.get("from") != eid and r.get("to") != eid):
                continue
            if r.get("from") in active_ids and r.get("to") in active_ids:
                r2 = dict(r); r2["state"] = "active"; r2.pop("trashed_by", None)
                upsert_relation(r2, actor); restored.append(r["id"])
            else:
                skipped.append(r["id"])   # 상대가 아직 trashed/merged → 그 엔티티 복원 시 회수됨
        append_event({"event": "entity.restore", "entity_id": eid,
                      "relations_restored": restored, "relations_skipped": skipped, "actor": actor})
        return {"entity": ent, "restored": restored, "skipped": skipped}


def retire_entity(eid, actor="human"):
    """항목 회수 — 엔티티와 그 관계를 소프트삭제(재추출 전 정리). 하드삭제 아님(감사·복구)."""
    with _LOCK:
        e = get_entity(eid)
        if e is None or e.get("state") == "merged":
            return None
        for r in _read_all("relations"):
            if r.get("state") != "merged" and (r.get("from") == eid or r.get("to") == eid):
                _retire_relation(r, actor)
        e = dict(e)
        e["state"] = "merged"
        e["retired"] = True
        return upsert_entity(e, actor)


def _retire_relation(r, actor):
    r = dict(r)
    r["state"] = "merged"
    upsert_relation(r, actor)


def add_relation(frm, to, rtype, hier_types, actor="human"):
    """관계를 **원자적으로** 추가(가드+커밋 전체를 단일 _LOCK 하에). 반환:
    (relation, None) 성공 | (None, error_code) 위반. 가드 — 양끝 active, (from,type,to)
    활성 중복 없음, 계층(hier_types 유형)이면 순환 없음. read-modify-write를 한 임계구역으로
    묶어 ThreadingHTTPServer 동시요청의 TOCTOU 경합(중복·순환 가드 우회)을 막는다.
    순환 판정·중복·부모 그래프는 **active 관계만**(list_relations 기본) 사용."""
    with _LOCK:
        fe, te = get_entity(frm), get_entity(to)
        if not (fe and fe.get("state") == "active" and te and te.get("state") == "active"):
            return None, "E-4104"
        rels = list_relations()   # active만 (merged/trashed 제외)
        for r in rels:
            if r.get("from") == frm and r.get("to") == to and r.get("type") == rtype:
                return None, "E-4105"
        if rtype in hier_types:   # 계층 순환: to에서 부모(자식→부모) 방향으로 올라가 frm에 닿으면 순환
            parents = {}
            for r in rels:
                if r.get("type") in hier_types:
                    parents.setdefault(r.get("from"), set()).add(r.get("to"))
            seen, stack = set(), [to]
            while stack:
                cur = stack.pop()
                for p in parents.get(cur, ()):
                    if p == frm:
                        return None, "E-4106"
                    if p not in seen:
                        seen.add(p)
                        stack.append(p)
        rid = next_id("REL")
        rel = upsert_relation(
            {"id": rid, "from": frm, "type": rtype, "to": to, "source_refs": [],
             "confidence": "medium", "state": "active",
             "produced_by": {"type": "human", "name": actor, "version": "manual"}}, actor)
        return rel, None


def retire_relation(rid, actor="human"):
    """관계 하나를 소프트삭제(state=merged). 상세페이지 관계 편집기의 '관계 해제'용.
    **active 관계만** 해제한다 — trashed 관계(인접 엔티티 삭제의 부수효과)를 merged로 뒤집으면
    restore_entity가 그 관계를 복원 못 해 영구 소실되므로, active가 아니면 None(변경 없음)."""
    with _LOCK:
        r = get_relation(rid)
        if not r or r.get("state") != "active":
            return None
        _retire_relation(r, actor)
        return rid


def redirect_relations(absorbed_ids, survivor_id, actor="human"):
    """흡수 대상을 가리키던 관계를 대표(survivor)로 재지정. 자기고리·중복은 폐기(state=merged)."""
    absorbed = set(absorbed_ids)
    with _LOCK:
        rels = [r for r in _read_all("relations") if r.get("state") != "merged"]
        seen = set((r.get("from"), r.get("type"), r.get("to")) for r in rels)
        n = 0
        for r in rels:
            f, t = r.get("from"), r.get("to")
            nf = survivor_id if f in absorbed else f
            nt = survivor_id if t in absorbed else t
            if (nf, nt) == (f, t):
                continue   # 이 관계는 흡수 대상과 무관
            if nf == nt:                       # 자기고리 → 폐기
                _retire_relation(r, actor); n += 1; continue
            key = (nf, r.get("type"), nt)
            if key in seen:                    # 중복 → 폐기
                _retire_relation(r, actor); n += 1; continue
            seen.add(key)
            r2 = dict(r); r2["from"] = nf; r2["to"] = nt
            upsert_relation(r2, actor); n += 1
        return n


# ---------- 기준정보(마스터) — 사람이 만드는 좌표축 (node→process, product, project) ----------
# **전통 CRUD와 자연어 CRUD가 공유하는 유일한 저장 경로**. 자연어는 입력 방식일 뿐이고
# 검증·저장·이력은 전부 여기를 지난다(두 경로가 갈라지면 버그가 난다).
# projection은 단일 파일(master.json/.yaml) — 항목이 수백 규모라 파일당 1개는 과하고,
# id에 슬래시 등 파일명 불가 문자가 올 수 있어 안전하다. 이력은 이벤트 로그가 정본.
_MASTER_TYPES = ("node", "process", "product", "project")
_MKEY = {"node": "nodes", "process": "processes", "product": "products", "project": "projects"}
_MREF_FIELD = {"node": "node", "process": "process", "product": "product", "project": "project"}


def _master_path():
    return os.path.join(DIRS["master"], "master")


def _akey(a):
    return re.sub(r"\s+", "", str(a)).lower()


def _dedup_aliases(aliases):
    out, seen = [], set()
    for a in aliases or []:
        a = str(a).strip()
        if not a or _akey(a) in seen:
            continue
        seen.add(_akey(a))
        out.append(a)
    return out


def get_master(include_deleted=False):
    """마스터 projection. 없으면 빈 마스터(첫 실행)."""
    with _LOCK:
        m = _read_json(_master_path() + ".json") or {}
        out = {}
        for k in ("nodes", "processes", "products", "projects"):
            lst = m.get(k) or []
            out[k] = lst if include_deleted else [x for x in lst if x.get("state", "active") == "active"]
        return out


def master_find(m, typ, mid):
    for it in (m.get(_MKEY[typ]) or []):
        if str(it.get("id", "")).lower() == str(mid).lower():
            return it
    return None


def master_refs(typ, mid):
    """이 마스터 항목을 좌표로 쓰는 active 엔티티 수 — 삭제 영향도(좌표 파괴 방지)."""
    f = _MREF_FIELD[typ]
    return sum(1 for e in list_entities() if str(e.get(f) or "").lower() == str(mid).lower())


def master_upsert(typ, item, actor="human"):
    """마스터 항목 생성/수정. 반환 (row, None) | (None, error_code).
    aliases는 전체교체(폼) 또는 aliases_add/aliases_remove(계획) 둘 다 받는다."""
    if typ not in _MASTER_TYPES:
        return None, "E-4301"
    mid = str(item.get("id") or "").strip()
    if not mid:
        return None, "E-4302"
    with _LOCK:
        m = get_master(include_deleted=True)
        cur = master_find(m, typ, mid)
        if typ == "process":   # process는 소속 node가 반드시 있고, 그 node가 마스터에 있어야 한다
            node = item.get("node") or (cur or {}).get("node")
            if not node:
                return None, "E-4303"
            nd = master_find(m, "node", node)
            if not nd or nd.get("state", "active") != "active":
                return None, "E-4304"
        row = dict(cur or {})
        row.update({"id": mid, "type": typ})
        for k in ("node", "label"):
            if item.get(k) is not None:
                row[k] = item[k]
        if item.get("aliases") is not None:
            row["aliases"] = _dedup_aliases(item["aliases"])
        if item.get("aliases_add"):
            row["aliases"] = _dedup_aliases(list(row.get("aliases") or []) + list(item["aliases_add"]))
        if item.get("aliases_remove"):
            rm = {_akey(a) for a in item["aliases_remove"]}
            row["aliases"] = [a for a in (row.get("aliases") or []) if _akey(a) not in rm]
        row.setdefault("aliases", [])
        row["state"] = "active"
        row["revision"] = (cur.get("revision", 0) + 1) if cur else 1
        row["updated_at"] = now_iso()
        lst = m[_MKEY[typ]]
        if cur is None:
            lst.append(row)
        else:
            for i, x in enumerate(lst):
                if str(x.get("id", "")).lower() == mid.lower():
                    lst[i] = row
                    break
        append_event({"event": "master.upsert", "id": mid, "mtype": typ,
                      "revision": row["revision"], "actor": actor, "data": row})
        _write_json_yaml(_master_path(), m)
        return row, None


def master_delete(typ, mid, actor="human", force=False):
    """소프트 삭제(state=deleted). **참조 중이거나 하위 process가 있으면 차단** — 좌표 파괴 방지.
    force로만 강행(사람이 영향을 보고 결정). 반환 (row, None) | (None, code)."""
    if typ not in _MASTER_TYPES:
        return None, "E-4301"
    with _LOCK:
        m = get_master(include_deleted=True)
        cur = master_find(m, typ, mid)
        if not cur or cur.get("state", "active") != "active":
            return None, "E-4305"
        if typ == "node":
            kids = [p for p in (m.get("processes") or [])
                    if p.get("state", "active") == "active"
                    and str(p.get("node", "")).lower() == str(mid).lower()]
            if kids and not force:
                return None, "E-4306"
        if master_refs(typ, mid) and not force:
            return None, "E-4307"
        cur["state"] = "deleted"
        cur["revision"] = cur.get("revision", 1) + 1
        cur["updated_at"] = now_iso()
        append_event({"event": "master.delete", "id": mid, "mtype": typ, "actor": actor, "data": cur})
        _write_json_yaml(_master_path(), m)
        return cur, None


def master_restore(typ, mid, actor="human"):
    with _LOCK:
        m = get_master(include_deleted=True)
        cur = master_find(m, typ, mid)
        if not cur or cur.get("state") != "deleted":
            return None, "E-4305"
        cur["state"] = "active"
        cur["revision"] = cur.get("revision", 1) + 1
        cur["updated_at"] = now_iso()
        append_event({"event": "master.restore", "id": mid, "mtype": typ, "actor": actor, "data": cur})
        _write_json_yaml(_master_path(), m)
        return cur, None


def master_impact(typ, mid):
    """삭제 전 영향도 미리보기 — 참조 엔티티 수 + (node면) 하위 process 목록."""
    m = get_master(include_deleted=True)
    kids = []
    if typ == "node":
        kids = [p["id"] for p in (m.get("processes") or [])
                if p.get("state", "active") == "active"
                and str(p.get("node", "")).lower() == str(mid).lower()]
    return {"refs": master_refs(typ, mid), "child_processes": kids}


# ---------- STRUCTURE 산출물(구조화 문서) ----------
def save_struct_docs(source_id, result, actor="human"):
    """preprocess.structure() 결과를 저장(원본 source_id 기준). 재실행하면 덮어쓴다(결정론)."""
    with _LOCK:
        _write_json_yaml(os.path.join(DIRS["docs"], source_id), result)
        append_event({"event": "source.structure", "id": source_id, "actor": actor,
                      "data": {"stats": result.get("stats")}})
        return result


def get_struct_docs(source_id):
    with _LOCK:
        return _read_json(os.path.join(DIRS["docs"], source_id + ".json"))


def list_sources():
    return _read_all("sources")


def stats():
    ents = list_entities()
    rels = list_relations()
    by_type, by_state = {}, {}
    for e in ents:
        by_type[e.get("type", "?")] = by_type.get(e.get("type", "?"), 0) + 1
        by_state[e.get("state", "?")] = by_state.get(e.get("state", "?"), 0) + 1
    return {
        "entities": len(ents), "relations": len(rels),
        "sources": len(list_sources()), "runs": len(list_runs()),
        "by_type": by_type, "by_state": by_state,
    }
