# 워크플로 단계 디스패처 (M3 §6). 상태 머신은 클라이언트에 있고,
# 서버는 한 단계만 실행하고 allowed_next_steps로 가능한 전이만 광고한다.
import hashlib
import json
import os
import threading
import time

import store
import llm

CORE_STEPS = ["CAPTURE", "STRUCTURE", "EXTRACT", "LINK", "REVIEW", "COMMIT", "VIEW_BUILD"]
ENTITY_TYPES = {"case", "issue", "finding", "initiative", "action", "pattern", "context"}
# status = 생애주기 8(순서형) + 특례 2(비순서: 보류/재발). M3 2축 결정(IMPROVEMENTS P1-6).
_VALID_STATUS = set(store.STATUS_ORDER) | {"보류", "재발"}
_VALID_SEVERITY = {"S1", "S2", "S3", "S4"}
# 관계 유형 vocabulary (prompts/extract.md와 일치). HIER_TYPES = 이슈 트리에서 부모 계층을
# 만드는 유형(자식 from → 부모 to). index.html의 HIER_REL과 동일 집합 — 함께 유지.
REL_TYPES = {"part_of", "related_to", "derived_from", "addresses", "affects", "applies_to",
             "similar_to", "duplicate_of", "recurrence_of", "instance_of", "supports",
             "contradicts", "produces"}
HIER_TYPES = {"recurrence_of", "duplicate_of", "part_of", "instance_of", "derived_from"}
# 기준정보(마스터) 오류 — 사람이 읽고 고칠 수 있게 사유를 명시한다.
_MASTER_ERR = {
    "E-4301": "미등록 기준정보 유형 (node/process/product/project 중)",
    "E-4302": "id 필요",
    "E-4303": "process는 소속 node가 필요합니다",
    "E-4304": "소속 node가 마스터에 없습니다 (node를 먼저 등록)",
    "E-4305": "대상 없음/이미 삭제됨",
    "E-4306": "하위 항목(narrower_of)이 있어 삭제할 수 없습니다 (하위를 먼저 정리)",
    "E-4307": "이 좌표를 쓰는 항목이 있어 삭제할 수 없습니다 (재지정 후 삭제)",
    "E-4308": "관계 양끝이 모두 active 기준정보여야 합니다",
    "E-4309": "관계 규칙의 kind와 맞지 않습니다",
    "E-4310": "같은 id가 이미 다른 kind로 존재합니다",
}


def _coord_missing(coords):
    """이슈 좌표 {node,process,product,project} 중 **마스터에 없는** 것 목록(빈 값 무시).
    좌표는 마스터가 진실 — 이슈는 마스터에 있는 좌표에만 붙는다(없으면 기준정보 먼저)."""
    g = store.get_master()
    miss = []
    for kind in ("node", "process", "product", "project"):
        v = (coords or {}).get(kind)
        if v and not store.master_find(g, v, kind):
            miss.append({"kind": kind, "id": v})
    return miss


def _miss_msg(miss):
    return "마스터에 없는 좌표: " + ", ".join("%s=%s" % (m["kind"], m["id"]) for m in miss)


def _run_master_crud_plan(text, guide):
    """기준정보 자연어 CRUD 계획(원문+가이드 → LLM 해석 → 결정론 대조). 비동기 worker에서 호출.
    현실 시드는 정형이 아니므로 해석은 LLM, 대조·계획은 결정론(seed_plan/store)."""
    import master as master_mod
    cur = store.get_master()
    res = llm.master_crud_plan(text, guide, cur)   # llm.LLMError 가능
    plan = master_mod.seed_plan(cur, res["items"])
    for d in res.get("deletes") or []:
        row = {"op": "delete", "type": d["type"], "id": d["id"], "evidence": d.get("evidence")}
        imp = store.master_impact(d["type"], d["id"])
        if not store.master_find(store.get_master(include_deleted=True), d["id"], d["type"]):
            row["warn"] = "마스터에 없는 항목 — 삭제 대상 아님"
        elif imp["refs"] or imp["children"]:
            row["warn"] = "참조 %d건%s — 강제 삭제만 가능" % (
                imp["refs"], (" · 하위 %d" % len(imp["children"])) if imp["children"] else "")
        plan.append(row)
    by = {(it["type"], it["id"]): it for it in res["items"]}
    for p in plan:
        src = by.get((p.get("type"), p.get("id")))
        if src:
            p["evidence"] = src.get("evidence"); p["confidence"] = src.get("confidence")
    return {"format": "llm", "errors": [], "items": res["items"],
            "unresolved": res.get("unresolved") or [], "plan": plan}


def _run_issue_crud_plan(text, guide):
    """이슈 자연어 CRUD 계획. LLM은 '무엇이 이슈인가'만, 좌표해소·needs_master는 결정론. 비동기 worker용."""
    import master as master_mod
    import links
    issues = [e for e in store.list_entities() if e.get("type") == "issue"]
    res = llm.issue_crud_plan(text, guide, issues, store.get_master())   # llm.LLMError 가능
    mg = store.get_master()
    text_coords = master_mod.resolve(text, mg)["coordinates"]
    text_urls = links.extract_links(text)
    by_id = {e["id"].lower(): e for e in issues}
    plan = []
    for it in res["items"]:
        op = it.get("op")
        if op == "create":
            coords = _resolve_coords_det(it)
            for k in ("node", "process", "product", "project"):
                if not coords.get(k):
                    coords[k] = text_coords.get(k)
            row = {"op": "create", "title": it.get("title"), "summary": it.get("summary"),
                   "coordinates": coords, "status": it.get("status"), "severity": it.get("severity"),
                   "start_date": it.get("start_date"), "deadline": it.get("deadline"),
                   "urls": text_urls, "evidence": it.get("evidence"), "confidence": it.get("confidence")}
            miss = _coord_missing(coords)
            if not (it.get("title") or "").strip():
                row["warn"] = "제목 없음"
            elif miss:
                row["warn"] = _miss_msg(miss)
            plan.append(row)
        elif op in ("update", "delete"):
            tgt = by_id.get(str(it.get("id", "")).lower())
            row = {"op": op, "id": it.get("id"), "evidence": it.get("evidence"),
                   "confidence": it.get("confidence")}
            if not tgt:
                row["op"] = "skip"; row["warn"] = "등록된 이슈 아님 — 건너뜀"
            elif op == "update":
                patch = {}
                for k in ("title", "summary", "status", "severity", "start_date", "deadline"):
                    if it.get(k) and it.get(k) != tgt.get(k):
                        patch[k] = it[k]
                rc = _resolve_coords_det(it)
                for k in ("node", "process", "product", "project"):
                    if rc.get(k) and rc.get(k) != tgt.get(k):
                        patch[k] = rc[k]
                row["patch"] = patch; row["title"] = tgt.get("title")
                miss = _coord_missing({k: patch.get(k) for k in ("node", "process", "product", "project")})
                if not patch:
                    row["op"] = "skip"; row["warn"] = "변경 없음"
                elif miss:
                    row["warn"] = _miss_msg(miss)
            else:
                row["title"] = tgt.get("title")
            plan.append(row)
    nm = [{"surface": c["surface"], "why": "마스터에 없음 — 기준정보 먼저 등록"}
          for c in master_mod.find_unknown_candidates(text, mg)]
    return {"plan": plan, "needs_master": nm, "unresolved": res.get("unresolved") or []}


def _resolve_coords_det(item):
    """LLM이 준 좌표 표기(별칭일 수 있음)+evidence+title을 **결정론 리졸버로 정준화**한다.
    LLM은 '무엇이 이슈인가'만 해석하고, 좌표 표면형→정준 id 해소는 master.resolve가 한다
    (예: LLM이 'N7+'라 해도 → process=N7P + node 승격). 추출 좌표와 동일 규칙이라 일관적."""
    import master as mm
    g = store.get_master()
    c = item.get("coordinates") or {}
    txt = " ".join(str(x) for x in [c.get("node"), c.get("process"), c.get("product"),
                                    c.get("project"), item.get("evidence"), item.get("title")] if x)
    return mm.resolve(txt, g)["coordinates"]


def _coerce_enums(obj, tid, warnings):
    """status/severity가 유효 enum이 아니면 조용히 이월하지 않고 제거+경고(E-3004). 엔티티는 유지.
    accept/edit(obj)와 merge(후보 e) 양 경로에 동일 적용(게이트 비대칭 방지)."""
    if obj.get("status") and obj["status"] not in _VALID_STATUS:
        warnings.append({"code": "E-3004", "temp_id": tid, "field": "status", "value": obj.pop("status")})
    if obj.get("severity") and obj["severity"] not in _VALID_SEVERITY:
        warnings.append({"code": "E-3004", "temp_id": tid, "field": "severity", "value": obj.pop("severity")})


def _active(eid):
    """존재하고 state=='active'인 엔티티만 id 반환(BG-8: trashed/merged로의 관계 커밋 방지)."""
    e = store.get_entity(eid)
    return eid if (e and e.get("state") == "active") else None


def _mark_existing(proposal, retrieved):
    """결정론적 후처리: RAE 검색된 기존 엔티티와 강하게 일치하는 후보에 merge_into를 설정한다.
    LLM이 merge_into를 빠뜨려도 '이미 있음'이 항상 후보로 보이고 사람이 병합/수락을 고르게 한다
    (숨기지 않음). 같은 타입 + 제목 어휘 Jaccard≥0.5(dedup과 동일 기준)."""
    import retrieve
    for e in proposal.get("entities", []) or []:
        if e.get("merge_into"):
            continue   # LLM이 이미 지정
        etoks, etype = retrieve._tokens(e.get("title") or ""), e.get("type")
        if not etoks:
            continue
        best, bestsim = None, 0.5
        for c in retrieved or []:
            if c.get("type") and etype and c.get("type") != etype:
                continue
            ctoks = retrieve._tokens(c.get("title") or "")
            if not ctoks:
                continue
            sim = len(etoks & ctoks) / len(etoks | ctoks)
            if sim >= bestsim:
                bestsim, best = sim, c.get("id")
        if best:
            e["merge_into"] = best


def _sha1(obj):
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _prompt_text(name):
    try:
        with open(os.path.join(llm.ROOT, "prompts", name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _effective_revision(cfg, vocab):
    """지금 이 순간 실효 설정·프롬프트·온톨로지의 sha1. manifest 기록과 실제 실행 해시 비교에 공용."""
    return {
        "config_revision": _sha1(cfg),
        "prompt_revisions": {"extract": _sha1(_prompt_text("extract.md"))},
        "ontology_revision": _sha1({"entity_types": sorted(ENTITY_TYPES),
                                    "status_order": store.STATUS_ORDER,
                                    "contexts": sorted(c.get("id", "") for c in vocab.get("contexts", []))}),
    }


def _build_run_manifest(run_id, sid, src_meta, vocab):
    """RUN 시작 시 **ref_time만 실제로 pin**한다(P0-1). config/prompt/ontology sha1은 'RUN 생성 시점의
    revision 기록'일 뿐 — 실제 추출은 아직 현재 파일을 재로딩한다(완전 pin은 BG-13/step2). 재시도 시
    실행 해시와 이 기록을 비교해 drift를 감지한다. ref_time은 캡처 시각이며 없으면 None(now 강등 금지)."""
    cfg = llm.load_llm_config()
    captured = src_meta.get("captured_at")
    m = {
        "run_id": run_id, "source_id": sid,
        "ref_time": captured,
        "ref_time_source": "captured_at" if captured else "absent",
        "provider": {"mode": cfg.get("mode"), "model": cfg.get("model")},
        "created_at": store.now_iso(),
    }
    m.update(_effective_revision(cfg, vocab))
    return m


class StepError(Exception):
    def __init__(self, code, msg, http=422):
        super().__init__(msg)
        self.code = code
        self.http = http


def _llm_http(code):
    # M3 §6.4 에러 대역 → HTTP
    if code == "E-2007":   # 인증 없음/만료
        return 401
    if code == "E-2001":   # 호출 실패/타임아웃
        return 504
    if code and code.startswith("E-3"):
        return 422
    return 502


def _resolve_context(alias, vocab):
    """context:<별칭> → (CTX-id, meta, status). status: found | none | ambiguous."""
    hits = []
    for c in vocab.get("contexts", []):
        if alias == c["id"] or alias in c.get("aliases", []) or alias == c.get("title"):
            hits.append(c)
    if len(hits) == 1:
        return hits[0]["id"], hits[0], "found"
    if not hits:
        return None, None, "none"
    return None, None, "ambiguous"


_MERGE_FILL = ("summary", "process", "product", "project", "start_date", "deadline",
               "occurred_at", "date_confidence", "date_note")


def _consolidate(target_id, e, actor, captured_at=None, first_captured_at=None):
    """병합: 기존 엔티티의 '빈' PM 필드를 새 후보 값으로 채우고 출처·태그를 합집합. revision++.
    기존 값을 덮어쓰지 않는다(사람이 확정한 값 보존) — 빈 칸만 채운다.
    전송시각은 first_captured_at=min(최초), captured_at=max(최종)로 병합(오프셋 정규화, BG-12)."""
    tgt = store.get_entity(target_id)
    if not tgt:
        return
    changed = dict(tgt)
    dirty = False
    for k in _MERGE_FILL:
        if not changed.get(k) and e.get(k):
            changed[k] = e[k]
            dirty = True
    srefs = list(changed.get("source_refs") or [])
    for r in (e.get("source_refs") or []):
        if r not in srefs:
            srefs.append(r)
            dirty = True
    changed["source_refs"] = srefs
    if e.get("tags"):
        tags = list(changed.get("tags") or [])
        for tg in e["tags"]:
            if tg not in tags:
                tags.append(tg)
                dirty = True
        changed["tags"] = tags
    # issue 상태·중요도는 '더 진행된 것' 채택 (빈칸 채우기 아님)
    ns = store.merge_status(changed.get("status"), e.get("status"))
    if ns and ns != changed.get("status"):
        changed["status"] = ns; dirty = True
    nv = store.merge_severity(changed.get("severity"), e.get("severity"))
    if nv and nv != changed.get("severity"):
        changed["severity"] = nv; dirty = True
    if not changed.get("closed_at") and e.get("closed_at"):
        changed["closed_at"] = e["closed_at"]; dirty = True
    # 전송시각: 최종=max, 최초=min
    if captured_at:
        tca = changed.get("captured_at")
        nca = store.ts_max(tca, captured_at) if tca else captured_at
        if nca != changed.get("captured_at"):
            changed["captured_at"] = nca; dirty = True
    fca = first_captured_at or captured_at
    if fca:
        tfa = changed.get("first_captured_at") or changed.get("captured_at")
        nfa = store.ts_min(tfa, fca) if tfa else fca
        if nfa != changed.get("first_captured_at"):
            changed["first_captured_at"] = nfa; dirty = True
    if dirty:
        store.upsert_entity(changed, actor)   # revision++ (upsert가 bump)


def handle_step(body):
    step = (body.get("step") or "").upper()
    run_id = body.get("run_id")
    inp = body.get("input") or {}
    req_id = body.get("request_id")

    def ok(result, nexts, extra=None):
        out = {"run_id": run_id, "step": step, "request_id": req_id,
               "status": "COMPLETED", "result": result, "allowed_next_steps": nexts}
        if extra:
            out.update(extra)
        return out

    if step == "CAPTURE":
        text = (inp.get("text") or "").strip()
        if not text:
            raise StepError("E-1001", "빈 입력", 400)
        run_id = store.next_id("RUN")
        src = store.capture_source(text, inp.get("channel"),
                                   first_captured_at=inp.get("first_captured_at"),
                                   guide=inp.get("guide"))   # 가이드는 본문과 분리 보관(해석 힌트)
        return ok({"source_id": src["id"], "source": src},
                  ["STRUCTURE", "EXTRACT", "STOP"], {"run_id": run_id, "result_id": src["id"]})

    if step in ("EXTRACT", "EXTRACT_RETRY"):
        step = "EXTRACT"
        sid = inp.get("source_id")
        text = store.read_source_text(sid) if sid else None
        if text is None:
            raise StepError("E-1002", "source_id 없음", 404)
        if not run_id:
            run_id = store.next_id("RUN")
        # BG-2: 이미 확정된 run을 재추출하면 proposal.json이 덮여 committed.json과 desync된다 → 차단.
        # (정상 재추출은 새 run으로: redoEntity가 새 CAPTURE→새 run_id를 만든다.)
        if store.get_committed(run_id):
            raise StepError("E-1012", "이미 확정된 run은 재추출 불가(새 원문 전송으로): %s" % run_id, 409)
        # 비동기: 즉시 RUNNING 반환하고 추출은 백그라운드에서. LLM이 수 분 걸려도
        # 클라이언트가 긴 연결을 붙들지 않으므로 프록시·라우터 타임아웃에 걸리지 않는다.
        started = store.now_iso()
        src_meta = store.get_source(sid) or {}
        # RUN vocab pin (BG-4): 재시도(같은 run_id)면 최초 pin을 **워커·COMMIT 모두** 재사용해
        # EXTRACT와 COMMIT이 동일 어휘로 해소되게 한다(추출은 live·커밋만 pin이던 불일치 제거).
        _pinned = store.get_inputs(run_id)
        if _pinned and _pinned.get("vocab"):
            vocab = _pinned["vocab"]
        else:
            vocab = store.load_vocab()
            store.save_inputs(run_id, {"vocab": vocab})
        # RUN revision 매니페스트: ref_time·설정·프롬프트·온톨로지를 RUN 시작 시 고정(재시도면 재사용).
        manifest = store.get_manifest(run_id) or _build_run_manifest(run_id, sid, src_meta, vocab)
        store.save_manifest(run_id, manifest)
        ref_time = manifest.get("ref_time")   # captured_at 또는 None(불명) — now로 강등하지 않음
        # RETRIEVE(결정론): 관련 기존 지식을 찾아 EXTRACT LLM 입력으로 첨부 (M5 §3)
        try:
            import retrieve
            retrieved = retrieve.retrieve_related(text, vocab)
        except Exception:  # noqa — 검색 실패는 치명적 아님 (증강 없이 진행)
            retrieved = []
        # generation 가드(BG-7): 같은 run_id로 EXTRACT가 겹치면 최신만 유효, stale 워커 쓰기는 폐기.
        generation = store.begin_extract(run_id, sid, started)
        rid = run_id

        def _worker():
            t0 = time.time()
            try:
                proposal, producer = llm.extract(text, vocab, sid, ref_time, retrieved)
                # 자연어 삭제/숨김 명령: 마커 있을 때만 전용 focused 감지(약한 LLM에도 견고),
                # 대상은 더 넓게 검색(top-15)해 목록에 포함되게 한다.
                if llm.has_command_marker(text):
                    ops = llm.detect_operations(text, retrieve.retrieve_related(text, vocab, k=15))
                    if ops:
                        seen = {(o["op"], o["target"]) for o in (proposal.get("operations") or [])}
                        merged = list(proposal.get("operations") or [])
                        for o in ops:
                            if (o["op"], o["target"]) not in seen:
                                merged.append(o); seen.add((o["op"], o["target"]))
                        proposal["operations"] = merged
                # 결정론적 후처리: 기존 항목과 일치하는 후보에 merge_into 표시(숨기지 않고 '이미 있음'으로)
                _mark_existing(proposal, retrieved)
                # 실제 실행에 쓰인 현재 해시(거짓 provenance 방지). manifest 기록과 다르면
                # 재시도 사이 config/prompt/vocab이 편집된 것 → drift 플래그. 완전 pin은 BG-13/step2.
                actual_rev = _effective_revision(llm.load_llm_config(), vocab)
                # provenance를 job.json에만 두지 않고 감사 정본 proposal.json에 함께 영속(BG-2)
                provenance = {
                    "links": proposal.get("links", []),
                    "fallback": proposal.get("fallback"),
                    "retrieved": retrieved,
                    "operations": proposal.get("operations", []),   # 자연어 삭제/숨김 제안
                    "ref_time": ref_time,                            # RUN에 고정된 기준시각(불명이면 None)
                    "revision": actual_rev,
                    "revision_drift": any(actual_rev[k] != manifest.get(k) for k in actual_rev),
                }
                # generation 게이트 하에 원자적 커밋 — superseded면 rec=None으로 폐기(로그 오염 방지)
                rec = store.finalize_extract(rid, generation, sid, proposal, producer, provenance,
                                             started, int((time.time() - t0) * 1000))
                if rec is None:
                    return   # 더 최신 EXTRACT가 이 run을 이어받음 → 이 워커 결과 폐기
                # 승자 generation만 출처 프로파일 스탬프 (수동 채널 입력 대체)
                store.set_source_profile(sid, proposal.get("source_profile"),
                                         producer.get("name", "llm"))
            except llm.LLMError as e:
                store.fail_extract(rid, generation, {
                    "state": "error", "source_id": sid, "started_at": started,
                    "extract_ms": int((time.time() - t0) * 1000),
                    "code": e.code, "error": str(e), "http": _llm_http(e.code)})
            except Exception as e:  # noqa
                store.fail_extract(rid, generation, {
                    "state": "error", "source_id": sid, "started_at": started,
                    "extract_ms": int((time.time() - t0) * 1000), "code": "E-5000",
                    "error": type(e).__name__ + ": " + str(e), "http": 500})

        threading.Thread(target=_worker, daemon=True).start()
        return ok({"run_id": run_id}, ["EXTRACT_POLL", "STOP"],
                  {"result_id": run_id, "status": "RUNNING"})

    if step == "COMMIT":
        if not run_id:
            raise StepError("E-1003", "run_id 필요", 400)
        # 멱등성: 이미 커밋된 run은 재실행하지 않고 이전 결과를 그대로 반환 (재시도·중복 POST 방지)
        prior = store.get_committed(run_id)
        if prior is not None:
            return ok(prior, ["VIEW_BUILD", "CAPTURE", "STOP"],
                      {"result_id": run_id, "idempotent": True})
        prop = store.get_proposal(run_id)
        if not prop:
            raise StepError("E-1004", "proposal 없음", 404)
        decisions = inp.get("decisions") or {}
        # BG-5: 미검토 후보를 조용히 accept로 커밋하지 않는다. 각 후보에 결정이 있거나
        # accept_all=true(명시적 일괄 수락 — UI의 'COMMIT 클릭'이 이에 해당)여야 커밋 허용.
        # decisions:{} 맹목 커밋(사람검토 0)을 서버에서 차단.
        _undecided = [e.get("temp_id") for e in prop.get("entities", [])
                      if e.get("temp_id") and e.get("temp_id") not in decisions]
        if _undecided and not inp.get("accept_all"):
            raise StepError("E-1010", "미검토 후보 %d개 — 각 후보를 검토하거나 accept_all로 명시" % len(_undecided), 422)
        # RUN에 pin된 vocab로 해소(BG-4) — EXTRACT와 동일 어휘라 핫리로드 사이 관계 조용소실 방지.
        # 옛 run(inputs 없음)은 live vocab로 폴백.
        vocab = (store.get_inputs(run_id) or {}).get("vocab") or store.load_vocab()
        actor = inp.get("actor", "human")

        idmap = {}       # temp_id -> 확정 id (accept/edit=신규, merge=기존)
        dropped = set()  # 의도적으로 제외된 temp_id (reject/merge실패) — 관계 드롭 시 경고 안 함
        committed_e, committed_r = [], []
        warnings = []
        # 프롬프트 전송 시점(캡처 시각) — 시계열 분석용 1급 메타로 엔티티에 스탬프
        _src = store.get_source(prop.get("source_id")) or {}
        captured_at = _src.get("captured_at")
        first_captured_at = _src.get("first_captured_at") or captured_at

        # 1) 엔티티 결정 반영
        for e in prop.get("entities", []):
            tid = e.get("temp_id")
            d = decisions.get(tid, {"review": "accept"})
            rev = d.get("review", "accept")
            if rev == "reject":
                dropped.add(tid)
                store.append_event({"event": "review.reject", "run_id": run_id,
                                    "temp_id": tid, "decision": "reject", "actor": actor})
                continue
            if rev == "merge":
                target = d.get("merge_into")
                if target and _active(target):   # BG-8: trashed/merged로 병합 금지
                    idmap[tid] = target   # 관계가 기존 엔티티를 가리키도록
                    _coerce_enums(e, tid, warnings)   # merge도 accept/edit와 동일 enum 게이트
                    _consolidate(target, e, actor, captured_at, first_captured_at)   # 빈 필드·출처+시각 통합
                    store.append_event({"event": "review.merge", "run_id": run_id, "temp_id": tid,
                                        "entity_id": target, "decision": "merge", "actor": actor})
                else:
                    warnings.append({"code": "E-1005", "temp_id": tid,
                                     "msg": "merge_into 대상 없음"})
                    dropped.add(tid)
                continue
            # PM 정형 필드 + 시간 필드 + issue 상태/중요도도 확정 엔티티로 이월
            carry = ("summary", "tags", "confidence",
                     "process", "product", "project", "start_date", "deadline",
                     "occurred_at", "date_confidence", "date_note",
                     "status", "severity", "closed_at")
            obj = {k: e[k] for k in carry if k in e}
            etype = e.get("type", "finding")
            title = e.get("title", "")
            src_refs = e.get("source_refs", [])
            if rev == "edit":
                patch = d.get("patch", {})
                etype = patch.get("type", etype)
                title = patch.get("title", title)
                if "source_refs" in patch:
                    src_refs = patch["source_refs"]
                for k in carry:
                    if k in patch:
                        obj[k] = patch[k]
            # 타입 검증 (E-3003): 미등록 유형은 finding으로 강등하고 경고
            if etype not in ENTITY_TYPES:
                warnings.append({"code": "E-3003", "temp_id": tid, "type": etype})
                etype = "finding"
            _coerce_enums(obj, tid, warnings)   # 상태/중요도 enum 게이트 (accept/edit)
            eid = store.next_id(etype.upper())
            obj.update({"id": eid, "type": etype, "title": title, "state": "active",
                        "source_refs": src_refs, "produced_by": prop.get("producer")})
            if captured_at:
                obj["captured_at"] = captured_at              # 최종(현재) 전송 시점
                obj["first_captured_at"] = first_captured_at   # 최초 전송 시점(재추출 계보 보존)
            store.upsert_entity(obj, actor)
            idmap[tid] = eid
            committed_e.append(eid)
            store.append_event({"event": "review." + rev, "run_id": run_id, "temp_id": tid,
                                "entity_id": eid, "decision": rev,
                                "changed_fields": list((d.get("patch") or {}).keys()) if rev == "edit" else [],
                                "actor": actor})

        # 2) 참조 해소 — 부작용 없는 lookup. context는 '보류'로 표시만.
        pending_ctx = {}   # alias -> meta (관계가 실제 커밋될 때 생성)

        def lookup(ref):
            """returns (id or None, warning or None)."""
            if not isinstance(ref, str):
                return None, {"code": "E-4001", "ref": ref}
            if ref in idmap:
                return idmap[ref], None
            if ref in dropped:
                return None, None   # 사용자가 의도적으로 제외 — 조용히 관계 드롭
            if ":" in ref:
                typ, alias = ref.split(":", 1)
                if typ == "context":
                    cid, meta, st = _resolve_context(alias, vocab)
                    if st == "found":
                        pending_ctx[alias] = meta
                        return cid, None
                    if st == "ambiguous":
                        return None, {"code": "E-4002", "ref": ref}
                    return None, {"code": "E-4001", "ref": ref}
                # 그 외 <type>:<별칭> — 기존 엔티티 id/제목과 대조 (pattern 등)
                if _active(alias):   # BG-8: active만
                    return alias, None
                for ent in store.list_entities():
                    if ent.get("title") == alias and ent.get("type") == typ:
                        return ent["id"], None
                return None, {"code": "E-4001", "ref": ref}
            # 접두사 없는 값이 기존 엔티티 id면 허용 (BG-8: active만)
            if _active(ref):
                return ref, None
            return None, {"code": "E-4001", "ref": ref}

        materialized = {}   # alias -> CTX id (한 번만 생성)

        def materialize_pending():
            for alias, meta in list(pending_ctx.items()):
                cid = meta["id"]
                if alias in materialized:
                    continue
                _cx = store.get_entity(cid)
                if _cx is None or _cx.get("state") != "active":   # BG-8: 없거나 trashed/merged면 active로 (재)생성
                    store.upsert_entity(
                        {"id": cid, "type": "context",
                         "context_type": meta.get("context_type"),
                         "title": meta.get("title", alias), "state": "active",
                         "source_refs": [], "aliases": meta.get("aliases", []),
                         "produced_by": {"type": "tool", "name": "linker", "version": "0.1"}},
                        "tool")
                    committed_e.append(cid)
                materialized[alias] = cid

        # 3) 양 끝이 모두 해소된 relation만 커밋. 아니면 경고(조용한 소실 방지).
        for r in prop.get("relations", []):
            pending_ctx.clear()
            f, wf = lookup(r.get("from"))
            t, wt = lookup(r.get("to"))
            if not f or not t:
                for w in (wf, wt):
                    if w:
                        warnings.append(dict(w, relation="%s->%s" % (r.get("from"), r.get("to"))))
                continue
            materialize_pending()   # 관계가 확정될 때만 context 엔티티 생성
            rid = store.next_id("REL")
            store.upsert_relation(
                {"id": rid, "from": f, "type": r.get("type", "related_to"), "to": t,
                 "source_refs": r.get("source_refs", []),
                 "confidence": r.get("confidence", "low"), "state": "active",
                 "produced_by": prop.get("producer")}, actor)
            committed_r.append(rid)

        # 4) 자연어 작업 명령 적용 — 사람이 확정한 것만 input.operations로 전달됨
        applied_ops = []
        for opd in inp.get("operations") or []:
            op, tgt = opd.get("op"), opd.get("target")
            if not tgt or not store.get_entity(tgt):
                warnings.append({"code": "E-4003", "op": op, "target": tgt, "msg": "대상 없음"})
                continue
            if op == "delete":
                res = store.trash_entity(tgt, actor)   # BG-9: state=trashed(복구가능)
                if res is None:
                    warnings.append({"code": "E-4003", "op": op, "target": tgt, "msg": "삭제 대상이 active 아님"})
                    continue
                store.append_event({"event": "review.delete", "entity_id": tgt, "op_id": res[1],
                                    "run_id": run_id, "actor": actor, "via": "nl"})
                applied_ops.append({"op": "delete", "target": tgt})
            elif op == "hide":
                store.set_hidden(tgt, True, actor)
                store.append_event({"event": "review.hide", "entity_id": tgt,
                                    "run_id": run_id, "actor": actor, "via": "nl"})
                applied_ops.append({"op": "hide", "target": tgt})

        committed = {"entities": committed_e, "relations": committed_r, "warnings": warnings,
                     "decisions": decisions, "operations": applied_ops}   # 복기용 스냅샷
        store.mark_committed(run_id, committed)
        return ok(committed, ["VIEW_BUILD", "CAPTURE", "STOP"], {"result_id": run_id})

    if step == "ENTITY_EDIT":   # 후처리 편집 (편집가능 필드만) — M5
        eid = inp.get("id")
        patch = dict(inp.get("patch") or {})
        if patch.get("urls") is not None:   # URL은 {url,kind,issue_key}로 정규화(Jira키 추출 포함)
            import links
            patch["urls"] = links.normalize(patch["urls"])
        ent, rejected = store.edit_entity(eid, patch, inp.get("actor", "human"))
        if ent is None:
            raise StepError("E-1002", "엔티티 없음: %s" % eid, 404)
        return ok({"entity": ent, "rejected": rejected}, ["ENTITY_EDIT", "STOP"])

    if step == "ENTITY_RETIRE":   # 재추출용 회수 — 엔티티+관계 소프트삭제 (M5)
        eid = inp.get("id")
        e = store.retire_entity(eid, inp.get("actor", "human"))
        if e is None:
            raise StepError("E-1002", "엔티티 없음/이미 회수됨: %s" % eid, 404)
        return ok({"id": eid}, ["CAPTURE", "STOP"])

    if step == "ENTITY_HIDE":   # 숨기기/숨김해제 (표시 전용, 그래프 유지)
        eid = inp.get("id")
        hidden = bool(inp.get("hidden", True))
        e = store.set_hidden(eid, hidden, inp.get("actor", "human"))
        if e is None:
            raise StepError("E-1002", "엔티티 없음: %s" % eid, 404)
        store.append_event({"event": "review." + ("hide" if hidden else "unhide"),
                            "entity_id": eid, "actor": inp.get("actor", "human")})
        return ok({"id": eid, "hidden": hidden}, ["ENTITY_HIDE", "STOP"])

    if step == "ENTITY_DELETE":   # 삭제 → state=trashed (복구가능·병합과 구분) BG-9
        eid = inp.get("id")
        res = store.trash_entity(eid, inp.get("actor", "human"))
        if res is None:
            raise StepError("E-1002", "엔티티 없음/이미 삭제됨: %s" % eid, 404)
        return ok({"id": eid, "op_id": res[1]}, ["ENTITY_RESTORE", "STOP"])

    if step == "REBUILD_PROJECTION":   # BG-10: 이벤트로부터 projection 재생성(복구·정합성 점검)
        res = store.rebuild_projection(strict=not inp.get("force"))
        return ok(res, ["REBUILD_PROJECTION", "STOP"])

    if step == "ENTITY_RESTORE":   # 휴지통 복원 → trashed를 active로, op 태그 관계 되살림 BG-9
        eid = inp.get("id")
        res = store.restore_entity(eid, inp.get("actor", "human"))
        if res is None:
            raise StepError("E-1002", "복원 대상 아님(휴지통 상태 아님): %s" % eid, 404)
        return ok(res, ["ENTITY_RESTORE", "STOP"])

    if step == "RELATION_ADD":   # 사람이 직접 관계(이슈 계층 포함) 추가 — 상세페이지 관계 편집기
        frm, to = inp.get("from"), inp.get("to")
        ty = inp.get("type", "related_to")
        actor = inp.get("actor", "human")
        if not (frm and to):
            raise StepError("E-4101", "from·to 모두 필요", 422)
        if frm == to:
            raise StepError("E-4102", "자기 자신과는 관계를 만들 수 없음", 422)
        if ty not in REL_TYPES:
            raise StepError("E-4103", "미등록 관계 유형: %s" % ty, 422)
        # 가드(중복·순환·양끝 active)+커밋을 store에서 단일 _LOCK으로 원자 실행 — 동시요청 TOCTOU 방지
        rel, err = store.add_relation(frm, to, ty, HIER_TYPES, actor)
        if err == "E-4104":
            raise StepError("E-4104", "관계 양끝이 모두 active 엔티티여야 함", 404)
        if err == "E-4105":
            raise StepError("E-4105", "이미 있는 관계: %s -%s-> %s" % (frm, ty, to), 409)
        if err == "E-4106":
            raise StepError("E-4106", "계층 순환이 생김(대상이 이미 이 항목의 하위)", 409)
        for eid in {frm, to}:   # 양끝 엔티티 검토 이력에 모두 남기도록 entity_id 태그
            store.append_event({"event": "review.relation_add", "id": rel["id"], "entity_id": eid,
                                "from": frm, "to": to, "rtype": ty, "actor": actor})
        return ok({"relation": rel}, ["RELATION_ADD", "RELATION_REMOVE", "STOP"])

    if step == "RELATION_REMOVE":   # 잘못 만든 관계를 소프트삭제(해제, active만) — 이력 보존
        rid = inp.get("id")
        actor = inp.get("actor", "human")
        r = store.get_relation(rid)   # 엔드포인트 캡처(감사 이벤트용) — retire 전에
        res = store.retire_relation(rid, actor)
        if res is None:
            raise StepError("E-4107", "관계 없음/active 아님(이미 해제/trashed): %s" % rid, 404)
        for eid in {r.get("from"), r.get("to")} - {None}:
            store.append_event({"event": "review.relation_remove", "id": rid, "entity_id": eid,
                                "from": r.get("from"), "to": r.get("to"), "rtype": r.get("type"),
                                "actor": actor})
        return ok({"id": rid}, ["RELATION_ADD", "STOP"])

    if step == "RECLASSIFY":   # 유형 재분류 — 새 접두사 ID 발급 + 관계 이관 (M5)
        eid = inp.get("id")
        nt = inp.get("type")
        if nt not in ENTITY_TYPES:
            raise StepError("E-3003", "미등록 유형: %s" % nt, 422)
        ne = store.reclassify_entity(eid, nt, inp.get("actor", "human"))
        if ne is None:
            raise StepError("E-1002", "엔티티 없음: %s" % eid, 404)
        return ok({"entity": ne, "old_id": eid, "new_id": ne["id"]}, ["ENTITY_EDIT", "STOP"])

    # ---------- STRUCTURE: 전처리(분해·중복접기) — 결정론, LLM 없음 ----------
    if step == "STRUCTURE":
        sid = inp.get("source_id")
        text = store.read_source_text(sid)   # 원문 정본은 raw/*.txt (sources/*.json은 메타만)
        if text is None:
            raise StepError("E-1003", "원문 없음: %s" % sid, 404)
        import preprocess
        res = preprocess.structure(text, sid)
        store.save_struct_docs(sid, res, inp.get("actor", "human"))
        return ok(res, ["MASTER_RESOLVE", "EXTRACT", "STOP"])

    if step == "STRUCT_GET":
        res = store.get_struct_docs(inp.get("source_id"))
        if res is None:
            raise StepError("E-1003", "구조화 결과 없음(STRUCTURE 먼저)", 404)
        return ok(res, ["MASTER_RESOLVE", "STOP"])

    # ---------- 기준정보(마스터) — 사람이 만드는 좌표축 ----------
    if step == "MASTER_GET":
        full = store.get_master(include_deleted=True)
        return ok({"master": store.get_master(),
                   "deleted": [e for e in full["entities"] if e.get("state") == "deleted"]},
                  ["MASTER_UPSERT", "STOP"])

    if step == "MASTER_UPSERT":   # 전통 CRUD (생성·수정) — 자연어 경로도 결국 여기로
        typ = inp.get("mtype")
        row, err = store.master_upsert(typ, inp.get("item") or {}, inp.get("actor", "human"))
        if err:
            raise StepError(err, _MASTER_ERR.get(err, "기준정보 저장 실패"), 422)
        return ok({"item": row}, ["MASTER_GET", "MASTER_UPSERT", "STOP"])

    if step == "MASTER_DELETE":   # 소프트 삭제 — 참조 중이면 차단(force로만 강행)
        typ, mid = inp.get("mtype"), inp.get("id")
        row, err = store.master_delete(typ, mid, inp.get("actor", "human"), bool(inp.get("force")))
        if err:
            imp = store.master_impact(typ, mid) if err in ("E-4306", "E-4307") else {}
            raise StepError(err, "%s %s" % (_MASTER_ERR.get(err, "삭제 실패"),
                                            json.dumps(imp, ensure_ascii=False) if imp else ""), 409)
        return ok({"id": mid}, ["MASTER_GET", "MASTER_RESTORE", "STOP"])

    if step == "MASTER_RESTORE":
        row, err = store.master_restore(inp.get("mtype"), inp.get("id"), inp.get("actor", "human"))
        if err:
            raise StepError(err, _MASTER_ERR.get(err, "복원 대상 아님"), 404)
        return ok({"item": row}, ["MASTER_GET", "STOP"])

    if step == "MASTER_IMPACT":   # 삭제 전 영향도 미리보기
        return ok(store.master_impact(inp.get("mtype"), inp.get("id")), ["MASTER_DELETE", "STOP"])

    if step == "MASTER_SEED_PLAN":   # 원문+가이드 → LLM 해석 → 결정론 계획. **비동기**(프록시 타임아웃·새로고침 대응)
        text = inp.get("text") or ""
        if not text.strip():
            raise StepError("E-1001", "원문이 비었습니다", 400)
        guide = inp.get("guide")
        jid = store.job_start("master_crud")

        def _w():
            try:
                store.job_finish(jid, _run_master_crud_plan(text, guide))
            except (StepError, llm.LLMError) as e:
                store.job_error(jid, getattr(e, "code", "E-2001"), str(e))
            except Exception as e:  # noqa
                store.job_error(jid, "E-5000", type(e).__name__ + ": " + str(e))
        threading.Thread(target=_w, daemon=True).start()
        return ok({"job_id": jid, "status": "RUNNING"}, ["JOB_GET", "STOP"])

    if step == "JOB_GET":   # 범용 비동기 작업 폴링 (자연어 CRUD 등)
        j = store.job_get(inp.get("id"))
        if j is None:
            raise StepError("E-1002", "작업 없음: %s" % inp.get("id"), 404)
        return ok(j, ["JOB_GET", "STOP"])

    if step == "MASTER_SEED_APPLY":   # 사람이 고른 계획만 적용 → 전통 CRUD와 동일 저장 경로
        actor = inp.get("actor", "human")
        applied, failed = [], []
        for row in (inp.get("apply") or []):
            op = row.get("op")
            if op == "skip":
                continue
            typ = row.get("type")
            if op == "delete":   # 삭제 가드(참조·하위)는 store가 유지 — force는 사람이 명시할 때만
                r, err = store.master_delete(typ, row.get("id"), actor, bool(row.get("force")))
                (failed if err else applied).append(
                    {"type": typ, "id": row.get("id"), "op": op, "error": err} if err
                    else {"type": typ, "id": row.get("id"), "op": op})
                continue
            item = {"id": row.get("id")}
            if op == "create":
                for k in ("node", "label"):
                    if row.get(k):
                        item[k] = row[k]
                item["aliases"] = row.get("aliases") or []
            elif op == "update":
                ch = row.get("changes") or {}
                if ch.get("aliases_add"):
                    item["aliases_add"] = ch["aliases_add"]
                if isinstance(ch.get("label"), dict):
                    item["label"] = ch["label"].get("to")
                if isinstance(ch.get("node"), dict):
                    item["node"] = ch["node"].get("to")
            else:
                continue
            r, err = store.master_upsert(typ, item, actor)
            (applied if not err else failed).append(
                {"type": typ, "id": row.get("id"), "op": op, "error": err} if err
                else {"type": typ, "id": r["id"], "op": op})
        return ok({"applied": applied, "failed": failed, "n": len(applied)},
                  ["MASTER_GET", "STOP"])

    if step == "MASTER_RESOLVE":   # 문서+가이드 → 결정론 좌표 해소 + 미등록 후보 (게이트1 입력)
        import master as master_mod
        sid = inp.get("source_id")
        docs = (store.get_struct_docs(sid) or {}).get("docs") if sid else None
        if docs is None:
            txt = inp.get("text")
            if not txt:
                raise StepError("E-1003", "source_id(STRUCTURE 완료) 또는 text 필요", 422)
            docs = [{"doc_id": "adhoc", "body_clean": txt}]
        cur = store.get_master()
        # 가이드는 인자로 받거나, 없으면 CAPTURE 때 저장한 원문의 가이드를 쓴다(재실행에도 동일 조건)
        guide = (inp.get("guide") or "").strip()
        if not guide and sid:
            guide = ((store.get_source(sid) or {}).get("guide") or "").strip()
        out, cands = [], []
        for d in docs:
            body = d.get("body_clean") or ""
            r = master_mod.resolve(body, cur)
            g = master_mod.resolve(guide, cur) if guide else None
            merged, gconf = dict(r["coordinates"]), []
            if g:   # 가이드는 **본문이 모호할 때만** 채우는 기본값. 충돌하면 덮지 않고 보고한다.
                for k, gv in g["coordinates"].items():
                    bv = merged.get(k)
                    if gv and not bv:
                        merged[k] = gv
                    elif gv and bv and gv != bv:
                        gconf.append({"field": k, "body": bv, "guide": gv})
            out.append({"doc_id": d.get("doc_id"), "coordinates": merged,
                        "found": r["found"], "conflicts": r["conflicts"],
                        "guide_conflicts": gconf})
            for c in master_mod.find_unknown_candidates(body, cur):
                if not any(x["surface"].lower() == c["surface"].lower() for x in cands):
                    cands.append({"surface": c["surface"], "doc_id": d.get("doc_id")})
        blocked = any(x["conflicts"] or x["guide_conflicts"] for x in out) or bool(cands)
        return ok({"docs": out, "unknown_candidates": cands, "needs_review": blocked},
                  ["MASTER_UPSERT", "MASTER_SEED_PLAN", "EXTRACT", "STOP"])

    # ---------- 이슈 CRUD — 사람이 만드는 주제 컨테이너 (전통 폼 + 자연어) ----------
    if step == "ISSUE_CREATE":   # 전통 폼: 사람이 이슈 등록 (좌표는 마스터에서만)
        import links
        f = dict(inp.get("fields") or {})
        if f.get("urls") is not None:   # 폼이 준 URL(문자열/리스트) → {url,kind,issue_key} 정규화
            f["urls"] = links.normalize(f["urls"])
        if not (f.get("title") or "").strip():
            raise StepError("E-4401", "제목(title)은 필수입니다", 422)
        miss = _coord_missing({k: f.get(k) for k in ("node", "process", "product", "project")})
        if miss and not inp.get("allow_missing"):
            raise StepError("E-4402", _miss_msg(miss) + " — 기준정보 탭에서 먼저 등록하세요", 409)
        e = store.create_entity("issue", f, inp.get("actor", "human"))
        return ok({"entity": e}, ["ISSUE_CREATE", "ENTITY_EDIT", "STOP"])

    if step == "ISSUE_CRUD_PLAN":   # 자연어: 원문+가이드 → LLM 해석 → 결정론 계획. **비동기**(프록시·새로고침 대응)
        text = inp.get("text") or ""
        if not text.strip():
            raise StepError("E-1001", "원문이 비었습니다", 400)
        guide = inp.get("guide")
        jid = store.job_start("issue_crud")

        def _w():
            try:
                store.job_finish(jid, _run_issue_crud_plan(text, guide))
            except (StepError, llm.LLMError) as e:
                store.job_error(jid, getattr(e, "code", "E-2001"), str(e))
            except Exception as e:  # noqa
                store.job_error(jid, "E-5000", type(e).__name__ + ": " + str(e))
        threading.Thread(target=_w, daemon=True).start()
        return ok({"job_id": jid, "status": "RUNNING"}, ["JOB_GET", "STOP"])

    if step == "ISSUE_CRUD_APPLY":   # 사람이 고른 계획만 적용 → 전통 create/edit/trash 저장 경로
        actor = inp.get("actor", "human")
        applied, failed = [], []
        for row in (inp.get("apply") or []):
            op = row.get("op")
            if op == "skip":
                continue
            try:
                if op == "create":
                    coords = row.get("coordinates") or {}
                    if _coord_missing(coords):
                        failed.append({"op": op, "title": row.get("title"), "error": "E-4402"}); continue
                    f = dict(coords)
                    for k in ("title", "summary", "status", "severity", "start_date", "deadline"):
                        if row.get(k):
                            f[k] = row[k]
                    if row.get("urls"):
                        f["urls"] = row["urls"]
                    e = store.create_entity("issue", f, actor)
                    applied.append({"op": op, "id": e["id"]})
                elif op == "update":
                    ent, _rej = store.edit_entity(row.get("id"), row.get("patch") or {}, actor)
                    (applied if ent else failed).append(
                        {"op": op, "id": row.get("id")} if ent else {"op": op, "id": row.get("id"), "error": "E-1002"})
                elif op == "delete":
                    res = store.trash_entity(row.get("id"), actor)
                    (applied if res else failed).append(
                        {"op": op, "id": row.get("id")} if res else {"op": op, "id": row.get("id"), "error": "E-1002"})
            except Exception as ex:   # noqa
                failed.append({"op": op, "id": row.get("id"), "error": type(ex).__name__})
        return ok({"applied": applied, "failed": failed, "n": len(applied)}, ["VIEW_BUILD", "STOP"])

    # ---------- 사건 추출 → 이슈 매핑 (기계가 사건을 뽑아 사람 이슈에 붙임) ----------
    if step == "EVENT_EXTRACT":   # 확정 좌표 하에서 사건만 추출(이슈 안 만듦)
        import master as master_mod
        import links
        sid = inp.get("source_id")
        docs = (store.get_struct_docs(sid) or {}).get("docs") if sid else None
        if docs is None:
            txt = inp.get("text")
            if not txt:
                raise StepError("E-1003", "source_id(STRUCTURE 완료) 또는 text 필요", 422)
            docs = [{"doc_id": "adhoc", "body_clean": txt}]
        src = store.get_source(sid) if sid else {}
        guide = inp.get("guide") or (src or {}).get("guide") or ""
        ref = (src or {}).get("captured_at")
        mg = store.get_master()
        events = []
        for d in docs:
            body = d.get("body_clean") or ""
            coords = master_mod.resolve(body, mg)["coordinates"]
            doc_urls = links.extract_links(body)   # 이 문서(메시지)의 Jira·Confluence·EDM 링크(결정론)
            try:
                evs = llm.extract_events(body, coords, ref, guide)
            except llm.LLMError as e:
                raise StepError(e.code, str(e), 502)
            for ev in evs:
                ev = dict(ev)
                for k in ("node", "process", "product", "project"):
                    ev[k] = coords.get(k)
                ev["source_refs"] = ["%s#%s" % (sid, d.get("doc_id", ""))] if sid else []
                ev["title"] = ev.get("what")
                if doc_urls:
                    ev["urls"] = doc_urls
                events.append(ev)
        for i, ev in enumerate(events, 1):   # 문서 넘어 temp_id 유일화
            ev["temp_id"] = "ev%d" % i
        return ok({"events": events, "source_id": sid}, ["ISSUE_MAP", "EVENT_COMMIT", "STOP"])

    if step == "ISSUE_MAP":   # 사건 → 사람이 등록한 이슈 연결 제안 (좌표로 후보 사전필터)
        events = inp.get("events") or []
        issues = [e for e in store.list_entities() if e.get("type") == "issue"]

        def _coset(o):
            return {(k, o.get(k)) for k in ("node", "process", "product", "project") if o.get(k)}
        evc = set()
        for ev in events:
            evc |= _coset(ev)
        cand = [e for e in issues if _coset(e) & evc] or issues   # 겹침 없으면 전체 후보
        try:
            res = llm.map_events(events, cand)
        except llm.LLMError as e:
            raise StepError(e.code, str(e), 502)
        res["candidates"] = [e.get("id") for e in cand]
        return ok(res, ["EVENT_COMMIT", "STOP"])

    if step == "EVENT_COMMIT":   # 게이트2 통과분: 사건 저장 + 이슈 연결(event part_of issue)
        actor = inp.get("actor", "human")
        events = inp.get("events") or []
        reviews = inp.get("reviews") or {}   # temp_id → accept|reject (기본 accept)
        links = {l.get("event"): l.get("issue") for l in (inp.get("links") or []) if l.get("event")}
        committed_ev, committed_links, warnings = [], [], []
        tmp2id = {}
        for ev in events:
            tid = ev.get("temp_id")
            if reviews.get(tid) == "reject":
                continue
            e = store.create_event(ev, actor)
            tmp2id[tid] = e["id"]
            committed_ev.append(e["id"])
        for tid, issue_id in links.items():   # 사건을 이슈에 붙임 → 트리에서 이슈 밑에 사건 나열
            eid = tmp2id.get(tid)
            if not eid:
                continue
            if reviews.get("link:" + tid) == "reject":
                continue
            rel, err = store.add_relation(eid, issue_id, "part_of", HIER_TYPES, actor)
            if err:
                warnings.append({"event": eid, "issue": issue_id, "code": err})
            else:
                committed_links.append(rel["id"])
        return ok({"events": committed_ev, "links": committed_links, "warnings": warnings},
                  ["VIEW_BUILD", "STOP"])

    if step == "VIEW_BUILD":
        return ok({"stats": store.stats()}, ["CAPTURE", "STOP"])

    if step == "RECONCILE":   # 재조정 제안 (결정론, 자동 커밋 안 함) — M5 §6
        import reconcile
        return ok({"proposals": reconcile.propose()}, ["RECONCILE_APPLY", "STOP"])

    if step == "RECONCILE_APPLY":   # 사람이 고른 제안만 관계로 커밋
        import reconcile
        res = reconcile.apply(inp.get("apply") or [], inp.get("actor", "human"))
        return ok(res, ["RECONCILE", "VIEW_BUILD", "STOP"])

    if step == "DEDUP":   # 중복 검출 (같은 타입 유사 군집) — 자동 병합 안 함
        import dedup
        return ok({"clusters": dedup.find_clusters()}, ["DEDUP_APPLY", "STOP"])

    if step == "DEDUP_APPLY":   # 사람이 고른 군집만 병합 (대표로 흡수 → 개수 감소)
        import dedup
        actor = inp.get("actor", "human")
        results = [dedup.merge(m.get("survivor"), m.get("absorbed") or [], actor)
                   for m in (inp.get("merges") or []) if m.get("survivor")]
        n = sum(len(r.get("absorbed", [])) for r in results)
        return ok({"merged": results, "n": n}, ["DEDUP", "VIEW_BUILD", "STOP"])

    raise StepError("E-1009", "미정의 단계: %s" % step, 409)
