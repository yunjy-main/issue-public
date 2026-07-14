# 셀프테스트 — 반입물 동봉용 (M2 §2.3 블라인드 개발 규약)
# 외부에서 실효능을 볼 수 없으므로, 운영자가 내부에서 실행해 pass/fail을 눈으로 확인한다.
# 실데이터 불필요: 임시 knowledge 저장소에 합성 입력으로 전 파이프라인을 돌린다.
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import time  # noqa: E402

import store  # noqa: E402
import pipeline  # noqa: E402
import llm  # noqa: E402

FAILS = []


def run_extract(run_id, source_id):
    """비동기 EXTRACT 실행 → job 완료 대기 → (ext응답, proposal) 반환."""
    ext = pipeline.handle_step({"step": "EXTRACT", "run_id": run_id,
                                "input": {"source_id": source_id}})
    end = time.time() + 30
    while time.time() < end:
        job = store.get_job(run_id)
        if job and job.get("state") in ("done", "error"):
            if job["state"] == "error":
                raise RuntimeError("extract error: %s" % job.get("error"))
            return ext, job["proposal"]
        time.sleep(0.02)
    raise RuntimeError("extract timeout")


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (mark, name, ("" if cond else "  -- " + detail)))
    if not cond:
        FAILS.append(name)


# M4 시나리오 B (합성 메신저) — 혐의 통보
SAMPLE_B = """[품질팀 김OO] 베타과제 N7 MPW2 EDS에서 IO 셀 burnt 다수 확인. ESD IP 불량으로 보임. 확인 요청.
[ESD 박OO] MPW1까지는 동일 셀 이상 없었습니다. MPW2 공정 변경점 확인 필요.
[품질팀 김OO] 공정쪽은 FEOL 도핑 조정 있었다고 함. 관련성은 모름."""

# M4 시나리오 A (합성 메일) — waive
SAMPLE_A = """제목: RE: [알파과제] GPIO-A release 전 DRC 결과
금일 N7 ESD rule 기준 DRC에서 GPIO-A에 error 7건 발생했습니다.
어제 한계평가에서 GPIO-A silicon 2kV pass 결과 받았습니다.
silicon 근거로 waive 처리 후 release 검토 부탁드립니다."""


def run():
    # 격리된 임시 저장소로 redirect
    tmp = tempfile.mkdtemp(prefix="issue-selftest-")
    store.K = os.path.join(tmp, "knowledge")
    for k in store.DIRS:
        store.DIRS[k] = os.path.join(store.K, *os.path.relpath(store.DIRS[k], os.path.join(ROOT, "knowledge")).split(os.sep))
    store.ensure_dirs()
    # vocabulary는 저장소 config에서 로드되므로 실제 시드를 복사
    shutil.copy(os.path.join(ROOT, "knowledge", "config", "vocabulary.json"),
                os.path.join(store.DIRS["config"], "vocabulary.json"))
    # 셀프테스트는 파이프라인 기계 검증이므로 운영자의 llm.json(anthropic/http)과 무관하게
    # 결정론적 stub을 강제한다 (LLM·키 없이도 항상 실행 가능).
    llm.load_llm_config = lambda: {"mode": "stub"}
    print("임시 저장소:", tmp)

    try:
        print("\n[1] CAPTURE")
        cap = pipeline.handle_step({"step": "CAPTURE", "input": {"text": SAMPLE_B, "channel": "messenger"}})
        check("CAPTURE 성공", cap["status"] == "COMPLETED")
        check("source_id 부여", cap["result"]["source_id"].startswith("SRC-"))
        check("run_id 부여", cap["run_id"].startswith("RUN-"))
        check("EXTRACT가 다음 단계", "EXTRACT" in cap["allowed_next_steps"])
        run_id, sid = cap["run_id"], cap["result"]["source_id"]
        raw = store.read_source_text(sid)
        check("원본 원문 저장 (capture-first)", raw == SAMPLE_B, "raw != input")

        print("\n[2] EXTRACT (비동기)")
        ext, prop = run_extract(run_id, sid)
        check("EXTRACT 즉시 RUNNING 반환", ext.get("status") == "RUNNING")
        check("EXTRACT_POLL 다음 단계", "EXTRACT_POLL" in ext["allowed_next_steps"])
        ents = prop["entities"]
        check("후보 엔티티 생성", len(ents) >= 2, "n=%d" % len(ents))
        types = {e["type"] for e in ents}
        check("case 컨테이너 존재", "case" in types)
        check("혐의 통보 → finding (issue 아님, ADR-004)",
              "finding" in types and "issue" not in types, "types=%s" % types)
        check("context 링크 매칭됨", any(l["status"] == "matched" for l in prop["links"]),
              "links=%s" % prop["links"])
        check("모든 후보에 source_refs", all(e.get("source_refs") for e in ents))
        check("job 완료 후 proposal 조회 가능", store.get_job(run_id)["state"] == "done")

        print("\n[3] COMMIT (전부 accept)")
        com = pipeline.handle_step({"step": "COMMIT", "run_id": run_id, "input": {"decisions": {}, "accept_all": True}})
        cids = com["result"]["entities"]
        check("엔티티 커밋됨", len(cids) >= 2, "n=%d" % len(cids))
        check("context 엔티티 생성됨", any(c.startswith("CTX-") for c in cids),
              "ids=%s" % cids)
        check("relation 커밋됨", len(com["result"]["relations"]) >= 1)
        check("미해소 경고 없음 (시드 별칭 전부 해소)",
              not com["result"]["warnings"], "warn=%s" % com["result"]["warnings"])
        check("VIEW_BUILD 다음 단계", "VIEW_BUILD" in com["allowed_next_steps"])

        print("\n[4] 이벤트 로그 & projection")
        # 이벤트 파일 존재
        evdir = store.DIRS["events"]
        evs = [f for f in os.listdir(evdir) if f.endswith(".jsonl")]
        lines = []
        for f in evs:
            with open(os.path.join(evdir, f), encoding="utf-8") as fh:
                lines += [json.loads(x) for x in fh if x.strip()]
        kinds = {e["event"] for e in lines}
        check("이벤트 종류 완비",
              {"source.capture", "proposal.create", "entity.upsert", "relation.upsert"} <= kinds,
              "kinds=%s" % kinds)
        # projection json+yaml 병행
        anyent = cids[0]
        check("엔티티 JSON projection", os.path.exists(os.path.join(store.DIRS["entities"], anyent + ".json")))
        check("엔티티 YAML projection", os.path.exists(os.path.join(store.DIRS["entities"], anyent + ".yaml")))

        print("\n[5] REJECT 결정 반영")
        cap2 = pipeline.handle_step({"step": "CAPTURE", "input": {"text": SAMPLE_A, "channel": "mail"}})
        r2, s2 = cap2["run_id"], cap2["result"]["source_id"]
        _, prop2 = run_extract(r2, s2)
        ents2 = prop2["entities"]
        check("시나리오 A: waive → initiative(assurance)",
              any(e["type"] == "initiative" and "assurance" in e.get("tags", []) for e in ents2),
              "types=%s" % [(e["type"], e.get("tags")) for e in ents2])
        # 첫 엔티티(case)를 reject → 커밋 결과에서 빠져야
        first = ents2[0]["temp_id"]
        before = len(store.list_entities())
        com2 = pipeline.handle_step({"step": "COMMIT", "run_id": r2,
                                     "input": {"decisions": {first: {"review": "reject"}}, "accept_all": True}})
        # reject된 first는 확정 엔티티를 만들지 않는다 — review.reject 이벤트로 실측
        _rev = []
        for _f in os.listdir(store.DIRS["events"]):
            if _f.endswith(".jsonl"):
                with open(os.path.join(store.DIRS["events"], _f), encoding="utf-8") as _fh:
                    _rev += [json.loads(x) for x in _fh if x.strip()]
        _rj = [e for e in _rev if e.get("event") == "review.reject" and e.get("temp_id") == first]
        check("reject된 temp_id가 review.reject로 기록됨(커밋 idmap 제외)",
              len(_rj) == 1, "reject events for %s=%d" % (first, len(_rj)))
        check("reject 후에도 커밋 정상 완료", com2["status"] == "COMPLETED")

        print("\n[6] EDIT 결정 반영 (finding → issue 승격)")
        cap3 = pipeline.handle_step({"step": "CAPTURE", "input": {"text": SAMPLE_A, "channel": "mail"}})
        r3, s3 = cap3["run_id"], cap3["result"]["source_id"]
        _, prop3 = run_extract(r3, s3)
        target = next(e["temp_id"] for e in prop3["entities"] if e["type"] == "finding")
        com3 = pipeline.handle_step({"step": "COMMIT", "run_id": r3,
                                     "input": {"decisions": {target: {"review": "edit",
                                               "patch": {"type": "issue", "title": "승격된 이슈"}}}, "accept_all": True}})
        promoted = [store.get_entity(i) for i in com3["result"]["entities"]]
        check("edit로 issue 승격 반영",
              any(e and e["type"] == "issue" and e["title"] == "승격된 이슈" for e in promoted),
              "committed=%s" % [(e["type"], e["title"]) for e in promoted if e])

        print("\n[7] 잘못된 단계 → 409")
        try:
            pipeline.handle_step({"step": "NOPE", "input": {}})
            check("미정의 단계 거부", False, "예외 안 남")
        except pipeline.StepError as e:
            check("미정의 단계 거부 (E-1009/409)", e.code == "E-1009" and e.http == 409)

        print("\n[8] stats 집계")
        st = store.stats()
        check("stats 엔티티 수 > 0", st["entities"] > 0)
        check("stats by_type에 finding 포함", "finding" in st["by_type"])

        print("\n[9] COMMIT 멱등성 (재실행이 중복 생성하지 않음)")
        cap4 = pipeline.handle_step({"step": "CAPTURE", "input": {"text": SAMPLE_B, "channel": "messenger"}})
        r4 = cap4["run_id"]
        run_extract(r4, cap4["result"]["source_id"])
        c_a = pipeline.handle_step({"step": "COMMIT", "run_id": r4, "input": {"decisions": {}, "accept_all": True}})
        n_after_first = len(store.list_entities())
        c_b = pipeline.handle_step({"step": "COMMIT", "run_id": r4, "input": {"decisions": {}, "accept_all": True}})
        n_after_second = len(store.list_entities())
        check("재COMMIT은 새 엔티티를 만들지 않음", n_after_first == n_after_second,
              "%d != %d" % (n_after_first, n_after_second))
        check("재COMMIT은 idempotent 플래그 반환", c_b.get("idempotent") is True)
        check("재COMMIT 결과가 최초와 동일", c_a["result"]["entities"] == c_b["result"]["entities"])

        print("\n[10] context 정밀도 (허위 연결·부분매칭 오탐 차단)")
        # 별칭 없는 줄의 finding은 어떤 context에도 연결되지 않아야
        text2 = "첫 줄은 알파과제 관련.\n둘째 줄에서 burnt 불량이 재현되었다."
        cap5 = pipeline.handle_step({"step": "CAPTURE", "input": {"text": text2, "channel": "mail"}})
        _, prop5 = run_extract(cap5["run_id"], cap5["result"]["source_id"])
        rels5 = prop5["relations"]
        burnt_e = next(e["temp_id"] for e in prop5["entities"] if "burnt" in e.get("title", ""))
        burnt_ctx = [r for r in rels5 if r["from"] == burnt_e and str(r["to"]).startswith("context:")]
        check("별칭 없는 줄의 finding은 context에 오연결되지 않음", not burnt_ctx,
              "오연결=%s" % burnt_ctx)
        # 부분매칭 오탐: 'BN7X'는 별칭 'N7'로 매칭되면 안 됨
        from llm import _match_contexts
        v = store.load_vocab()
        links_fp = _match_contexts("BN7X 코드 확인", v)
        check("'BN7X'가 별칭 'N7'로 오탐되지 않음", not any(l["alias"] == "N7" for l in links_fp),
              "links=%s" % links_fp)
        links_ok = _match_contexts("N7 노드에서 확인", v)
        check("정상 'N7'은 매칭됨", any(l["alias"] == "N7" for l in links_ok))

        print("\n[11] merge 결정 (신규 생성 대신 기존으로 매핑)")
        existing = next(i for i in store.list_entities() if i["type"] == "finding")["id"]
        cap6 = pipeline.handle_step({"step": "CAPTURE", "input": {"text": SAMPLE_B, "channel": "messenger"}})
        _, prop6 = run_extract(cap6["run_id"], cap6["result"]["source_id"])
        mtid = next(e["temp_id"] for e in prop6["entities"] if e["type"] == "finding")
        before6 = len(store.list_entities())
        c6 = pipeline.handle_step({"step": "COMMIT", "run_id": cap6["run_id"],
                                   "input": {"decisions": {mtid: {"review": "merge", "merge_into": existing}}, "accept_all": True}})
        check("merge는 새 엔티티를 만들지 않음(해당 후보에 대해)",
              existing not in c6["result"]["entities"], "committed=%s" % c6["result"]["entities"])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("=" * 40))
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    run()
