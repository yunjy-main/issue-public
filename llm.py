# EXTRACT 단계 추출기 — 어댑터 (M3 §2, ADR-002)
# mode=stub : 오프라인 결정론적 휴리스틱 (외부 개발/셀프테스트용, LLM 없이 동작)
# mode=http : 사내 LLM API 호출 (프롬프트=데이터 파일, 운영자가 내부에서 config로 전환)
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


class LLMError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def load_llm_config():
    p = os.path.join(ROOT, "knowledge", "config", "llm.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"mode": "stub", "producer": {"type": "tool", "name": "stub-extractor", "version": "0.1"}}


# ---------- LLM 요청/응답 로깅 (사내 LLM 연동 디버깅) ----------
# 항상: 요청 요약(URL·바이트·response_format 여부)·응답 요약(status·경과·바이트)·실패 상세(실제 예외·경과·URL).
# ISSUE_LLM_DEBUG=1        → 요청/응답 '본문 전문'과 헤더까지 출력(토큰은 마스킹).
# ISSUE_LLM_DEBUG=secrets  → 위 + 헤더의 토큰/키까지 마스킹 없이 출력(신뢰된 로컬 디버깅에서만!).
# llm.json에 "debug": true 로도 본문 전문을 켤 수 있다.
def _dbg_level():
    return (os.environ.get("ISSUE_LLM_DEBUG", "") or "").strip().lower()


def _dbg_full(cfg=None):
    return _dbg_level() in ("1", "true", "yes", "on", "full", "secrets") or bool((cfg or {}).get("debug"))


def _llog(msg):
    # Windows 콘솔(cp949) 등에서 비인코딩 문자로 UnicodeEncodeError가 나면 로그가 사라지지 않도록
    # 스트림 인코딩으로 재인코딩(errors=replace)해서라도 반드시 출력한다. 마커는 ASCII만 쓴다.
    line = "[LLM] " + msg + "\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stderr, "encoding", None) or "utf-8"
            sys.stderr.buffer.write(line.encode(enc, "replace"))
            sys.stderr.buffer.flush()
        except Exception:  # noqa
            pass
    except Exception:  # noqa — 로깅이 절대 파이프라인을 깨지 않게
        pass


def _redact_headers(headers):
    """헤더의 토큰/키류를 마스킹(첫10·끝4·길이만). ISSUE_LLM_DEBUG=secrets면 원문 노출."""
    if _dbg_level() == "secrets":
        return dict(headers or {})
    out = {}
    for k, v in (headers or {}).items():
        kl = str(k).lower()
        if kl in ("authorization", "cookie") or any(t in kl for t in ("token", "key", "secret", "auth", "apikey")):
            s = str(v)
            out[k] = (s[:10] + "…[마스킹 %d자]…" % len(s) + s[-4:]) if len(s) > 18 else "…[마스킹 %d자]…" % len(s)
        else:
            out[k] = v
    return out


def _log_llm_request(tag, url, headers, body_bytes, cfg):
    """전송 '직전'에 요청 전문을 남긴다 — 타임아웃돼도 무엇을 어디로 보냈는지 로그에 남는다."""
    body = body_bytes.decode("utf-8", "replace") if isinstance(body_bytes, (bytes, bytearray)) else str(body_bytes)
    nbytes = len(body_bytes) if isinstance(body_bytes, (bytes, bytearray)) else len(body.encode("utf-8"))
    has_rf = '"response_format"' in body
    _llog("-> %s POST %s | body %d bytes | response_format=%s | timeout=%ss"
          % (tag, url, nbytes, "YES" if has_rf else "no", cfg.get("timeout", 60)))
    if has_rf:
        _llog("  [!] %s 요청에 response_format(json_schema)가 포함됨 — 사내 LLM이 미지원/지연하면 "
              "직접 호출은 빠른데 서버 경유만 timeout 날 수 있음. llm.json에서 \"response_schema\" 제거로 끌 수 있음." % tag)
    if _dbg_full(cfg):
        _llog("  headers: %s" % json.dumps(_redact_headers(headers), ensure_ascii=False))
        _llog("  body(전문):\n%s" % body)
    else:
        _llog("  body(head 800 - 전문은 ISSUE_LLM_DEBUG=1):\n%s" % body[:800])


def _log_llm_response(tag, status, elapsed, body_text, cfg):
    _llog("<- %s HTTP %s | %.2fs | %d bytes" % (tag, status, elapsed, len((body_text or "").encode("utf-8"))))
    if _dbg_full(cfg):
        _llog("  resp(전문):\n%s" % body_text)
    else:
        _llog("  resp(head 800):\n%s" % (body_text or "")[:800])


def _log_llm_error(tag, url, elapsed, exc, extra=""):
    """실패 시 실제 예외 메시지·경과·URL을 남긴다(기존엔 type명만 남겨 디버깅 불가였음)."""
    _llog("[X] %s 실패 | %.2fs | %s | %s: %s%s"
          % (tag, elapsed, url, type(exc).__name__, exc, ("\n  " + extra) if extra else ""))


# ---------- 문장/라인 분할 (source_refs용 위치 보존) ----------
def _segments(text):
    segs = []
    for i, line in enumerate(text.split("\n"), start=1):
        s = line.strip()
        if not s:
            continue
        # 메신저 '[이름] 발화' 형태는 발화만 남김
        m = re.match(r"^\[[^\]]{1,20}\]\s*(.+)$", s)
        body = m.group(1) if m else s
        # 메일 헤더/인용 접두 제거
        body = re.sub(r"^(RE:|Re:|>|제목:|title:)\s*", "", body).strip()
        for j, part in enumerate(re.split(r"(?<=[.!?。])\s+|(?<=다)\.\s*", body)):
            part = part.strip()
            if len(part) >= 2:
                segs.append({"loc": "L%d" % i if j == 0 else "L%d.%d" % (i, j), "text": part})
    return segs


def _alias_in(alias, text):
    """별칭 포함 여부. ASCII 별칭은 영숫자 경계를 요구해 부분매칭 오탐(N7⊂BN7X)을 막는다."""
    if not alias:
        return False
    if re.fullmatch(r"[A-Za-z0-9 ._-]+", alias):
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])", text) is not None
    return alias in text  # 한글 등은 경계 판정이 모호하므로 그대로


def _match_contexts(text, vocab):
    """텍스트에서 발견된 context 별칭 → (alias, CTX-id, status). LINK 단계 fold.
    같은 표면 언급이 서로 다른 context로 걸리면(예: 'N7 MPW2'가 NODE-N7과 NODE-N7-MPW2) ambiguous."""
    found = {}          # alias -> {CTX-id}
    span_hits = {}      # 표면 span(소문자) -> {CTX-id} : 표면 중의성 탐지
    for c in vocab.get("contexts", []):
        for alias in c.get("aliases", []):
            if _alias_in(alias, text):
                found.setdefault(alias, set()).add(c["id"])
    links = []
    for alias, ids in found.items():
        ids = sorted(ids)
        # 다른 alias가 이 alias의 부분/상위 문자열로 같은 위치를 다른 context로 가리키면 중의
        overlap = set(ids)
        for other, oids in found.items():
            if other != alias and (alias in other or other in alias):
                overlap |= oids
        status = "matched" if len(overlap) == 1 else "ambiguous"
        links.append({"alias": alias, "candidates": ids, "status": status})
    return links


def _has(seg_text, words):
    return any(w in seg_text for w in words)


def _stub_source_profile(text):
    """휴리스틱 채널 다중분류 (stub 전용 — LLM이 대체한다).
    한 원문에 여러 채널이 중첩될 수 있으므로(메일로 전달된 컨플 회의록 등) 리스트로 판정."""
    channels = []
    if re.search(r"(^|\n)\s*(제목|수신|발신|참조)\s*:|(^|\n)\s*(RE|Re|FW|Fw)\s*:|\[.*메일.*\]", text):
        channels.append("mail")
    if re.search(r"\[[^\]\n]{1,20}\]\s*\S|메신저", text):
        channels.append("messenger")
    if re.search(r"회의록|회의\b|참석\s*:|안건\s*:|결정사항", text):
        channels.append("meeting")
    if re.search(r"[Cc]onfluence|컨플루언스|위키", text):
        channels.append("confluence")
    if not channels:
        channels = ["other"]
    return {"channels": channels, "category": None,
            "note": "stub 휴리스틱 판정 — LLM 연결 시 대체"}


def stub_extract(text, vocab, source_id):
    """결정론적 휴리스틱 추출. 불완전함이 정상 — 사내 LLM이 대체/보강한다."""
    kw = vocab.get("keywords", {})
    segs = _segments(text)
    links = _match_contexts(text, vocab)
    src = source_id or "SRC"

    entities = []
    relations = []
    tid = [0]

    def new_tid():
        tid[0] += 1
        return "e%d" % tid[0]

    # 소스 전체를 감싸는 case
    case_id = new_tid()
    head = segs[0]["text"][:40] if segs else "빈 입력"
    entities.append({"record": "entity", "temp_id": case_id, "type": "case",
                     "title": head, "summary": "단일 입력에서 추출된 조사 묶음.",
                     "source_refs": [src], "confidence": "low"})

    seg_ctx = {}  # seg loc -> [alias...]
    for seg in segs:
        for lk in links:
            if _alias_in(lk["alias"], seg["text"]):
                seg_ctx.setdefault(seg["loc"], []).append(lk["alias"])

    for seg in segs:
        t = seg["text"]
        ref = "%s#%s" % (src, seg["loc"])
        etype = None
        tags = None
        conf = "low"
        if _has(t, kw.get("initiative", [])):
            etype = "initiative"
            tags = ["assurance"] if _has(t, ["waive", "웨이브", "보증"]) else ["corrective"]
            conf = "medium"
        elif _has(t, kw.get("finding", [])):
            etype = "finding"
            # 혐의/미확정 표지가 있으면 confidence 낮춤 (ADR-004: issue 아님, finding 유지)
            conf = "low" if _has(t, kw.get("hedge", [])) else "medium"
        elif _has(t, kw.get("question", [])):
            etype = "finding"
            conf = "low"
        if not etype:
            continue
        eid = new_tid()
        ent = {"record": "entity", "temp_id": eid, "type": etype,
               "title": t[:60], "source_refs": [ref], "confidence": conf}
        if tags:
            ent["tags"] = tags
        entities.append(ent)
        relations.append({"record": "relation", "from": eid, "type": "part_of",
                          "to": case_id, "source_refs": [ref], "confidence": "low"})
        # context 앵커링: 반드시 '같은 세그먼트'에 나타난 별칭만 연결한다.
        # (전역 폴백 금지 — 없는 위치를 provenance로 박는 허위 연결 방지)
        for alias in seg_ctx.get(seg["loc"], []):
            rtype = "applies_to" if etype in ("finding", "issue") else "addresses"
            relations.append({"record": "relation", "from": eid, "type": rtype,
                              "to": "context:%s" % alias, "source_refs": [ref],
                              "confidence": "low"})

    return {"entities": entities, "relations": relations, "links": links,
            "source_profile": _stub_source_profile(text)}


def _format_retrieved(retrieved):
    """RETRIEVE 카드 목록 → system에 붙일 '관련 기존 지식' 블록 (M5 §4). 비면 ''."""
    if not retrieved:
        return ""
    lines = ["", "관련 기존 지식 (아래는 이 원문과 관련될 수 있는 '이미 확정된' 지식이다):"]
    for c in retrieved:
        coord = " · ".join(c.get("coordinate") or []) or "-"
        when = c.get("occurred_at") or ""
        lines.append("- [%s] %s (%s) | 좌표: %s%s"
                     % (c.get("id"), c.get("title") or "", c.get("type"),
                        coord, (" | 발생 " + when) if when else ""))
    lines += [
        "지시:",
        "- 원문에서 발견한 엔티티는 **하나도 빠짐없이** 후보로 낸다. 위 '기존 지식'에 이미 있는 대상이라도"
        " **절대 생략(skip)하지 말고** 후보로 내되, 그 후보 entity에 merge_into:<기존ID>를 설정해 '이미 있음'을 표시한다."
        " 같은 원문은 항상 같은 개수의 후보를 내야 한다(무엇을 병합/수락/거절할지는 사람이 검토에서 결정한다).",
        "- 새 후보가 위의 한 항목과 '동일 대상'이면 반드시 merge_into:<기존ID>를 넣는다(사람이 확정).",
        "- 새 사건이 위 항목과 같거나 이어지는 사건이면, 그 '기존 ID'를 relation의 from/to에 그대로 써서 연결하라"
        " (recurrence_of/addresses/derived_from/duplicate_of/instance_of 등).",
        "- 위 목록에 없는 정식 ID를 지어내지 말라(그 외엔 temp_id/context:<별칭>만).",
        "- 기존 지식을 임의로 삭제·변조하지 말라(아래 명령 규칙 예외).",
        "",
        "작업 명령(operations) — 원문이 위 '기존 항목'을 지우거나 숨기라고 '지시'할 때만 뽑는다:",
        "- op=delete(삭제: '지워/삭제/잘못 등록됐으니 없애') 또는 op=hide(숨김: '숨겨/목록에서 빼/안 보이게'), target=위 목록의 기존 ID.",
        "- target은 반드시 위 목록의 ID에서 고른다. 어느 것인지 모호하거나 목록에 없으면 operations를 만들지 말라.",
        "- '불량이 제거됐다/해결됐다' 같은 사실 서술은 명령이 아니다(FACT로 finding). 확실한 '지시'만 명령으로.",
        "- 확실하지 않으면 operations를 비운다(파괴적 오작동 방지). 명령은 사람이 최종 확정한다.",
        "- evidence에 명령 근거 문구를 그대로 인용한다.",
    ]
    return "\n".join(lines)


def _build_prompt(text, vocab, source_id, ref_time=None, retrieved=None):
    """system(extract.md + 기준시각 + 관련 기존 지식 + SOURCE-ID + 별칭) / user(원문) — 공유."""
    ppath = os.path.join(ROOT, "prompts", "extract.md")
    try:
        with open(ppath, encoding="utf-8") as f:
            base = f.read()
    except OSError:
        raise LLMError("E-2003", "extract prompt file 없음")
    aliases = sorted({a for c in vocab.get("contexts", []) for a in c.get("aliases", [])})
    # 기준 시각(프롬프트 전송 시점) — 상대 일자("1일 후","일주일 전") 환산의 기준. RUN에서 pin되어 전달.
    # ref_time이 없으면(캡처시각 불명) now로 강등하지 않는다 — occurred_at의 조용한 오염 방지(BG-3).
    if ref_time:
        time_block = ("\n\n기준 시각(프롬프트 전송 시점): %s" % ref_time
                      + "\n- 원문에 명시된 날짜(예: 회의 채널의 날짜)가 있으면 그 문맥을 우선 기준으로 삼는다."
                      + "\n- '1일 후', '지난주', '일주일쯤 전' 같은 상대표현은 위 기준에서 절대일자(YYYY-MM-DD)로 환산한다."
                      + "\n- 환산이 근사·불확실하면 date_confidence(exact/approximate/uncertain)와 date_note로 반드시 표시한다.")
    else:
        time_block = ("\n\n기준 시각 불명 — 상대 일자 표현('1일 후','지난주' 등)은 절대일자로 환산하지 말고, "
                      "date_confidence=uncertain으로 두고 date_note에 원문 표현을 그대로 남긴다.")
    system = (base + time_block
              + _format_retrieved(retrieved)
              + "\n\nSOURCE-ID: %s  (source_refs는 %s#L<줄번호> 형식으로 쓴다)" % (source_id, source_id)
              + "\n알려진 context 별칭(가능하면 context:<별칭>으로 참조): "
              + ", ".join(aliases))
    # user 턴은 실제 추출 지시로 프레이밍 — 원문만 주면 모델이 대화형/임의 스키마로 답한다
    user = ("다음 원문에서 위 규칙에 따라 source_profile 1개와 Entity·Relation 후보를 생성하라. "
            "반드시 지정된 필드 구조(source_profile은 channels/category/note, "
            "entity는 temp_id/type/title/source_refs/confidence, "
            "relation은 from/type/to/source_refs/confidence)로만 출력하고, "
            "다른 스키마나 설명 문장을 덧붙이지 않는다.\n\n원문:\n" + text)
    return system, user


def _iter_records(content):
    """LLM 텍스트 응답에서 후보 레코드(dict)를 뽑는다. OSS/보통 LLM의 잡음에 관대:
    마크다운 코드펜스, JSON 배열, {entities,relations} 객체, 줄 단위 JSONL을 모두 처리.
    반환: (records, bad_line_count)."""
    if not isinstance(content, str):
        raise LLMError("E-2002", "LLM 응답 content가 문자열이 아님")
    # 1) 코드펜스(``` / ```json) 줄 제거
    lines = [ln for ln in content.splitlines() if not ln.strip().startswith("```")]
    body = "\n".join(lines).strip()
    # 2) 전체를 하나의 JSON으로 시도 (배열 / {entities,relations} / 단일 객체 — 여러 줄 허용)
    if body[:1] in ("[", "{"):
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)], 0
        if isinstance(parsed, dict):
            if "entities" in parsed or "relations" in parsed:
                recs = []
                sp = parsed.get("source_profile")
                if isinstance(sp, dict):
                    recs.append(dict(sp, record="source_profile"))
                for op in parsed.get("operations") or []:
                    if isinstance(op, dict):
                        recs.append(dict(op, record="operation"))
                for r in parsed.get("entities") or []:
                    if isinstance(r, dict):
                        r.setdefault("record", "entity"); recs.append(r)
                for r in parsed.get("relations") or []:
                    if isinstance(r, dict):
                        r.setdefault("record", "relation"); recs.append(r)
                return recs, 0
            if parsed.get("record") or ("from" in parsed and "to" in parsed) or "type" in parsed:
                return [parsed], 0
    # 3) 줄 단위 JSONL (관대 — 한 줄 실패가 배치를 무산시키지 않음)
    recs, bad = [], 0
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            recs.append(json.loads(ln))
        except ValueError:
            bad += 1
    return recs, bad


def _parse_candidates(content, text, vocab):
    """LLM 응답을 entity/relation 후보 + source_profile로 분류 — 공유."""
    recs, bad = _iter_records(content)
    entities, relations, operations = [], [], []
    profile = None
    for o in recs:
        if not isinstance(o, dict):
            continue
        rec = o.get("record")
        # 출처 프로파일 레코드 — 채널 다중분류(혼합 소스)와 내용 분류
        if rec == "source_profile" or (rec is None and "channels" in o):
            profile = {k: o[k] for k in ("channels", "category", "note") if k in o}
            continue
        # 작업 명령 레코드 (자연어 삭제/숨김) — 대상 있는 것만
        if rec == "operation" or (rec is None and o.get("op") in ("delete", "hide") and "target" in o):
            if o.get("op") in ("delete", "hide") and o.get("target"):
                operations.append({k: o[k] for k in ("op", "target", "evidence", "confidence") if k in o})
            continue
        if rec not in ("entity", "relation"):
            rec = "relation" if ("from" in o and "to" in o) else "entity"
        (entities if rec == "entity" else relations).append(o)
    if not entities and not relations and not operations and bad:
        raise LLMError("E-3001", "후보를 한 건도 파싱하지 못함 (%d줄 실패)" % bad)
    out = {"entities": entities, "relations": relations, "bad_lines": bad,
           "links": _match_contexts(text, vocab)}
    if profile:
        out["source_profile"] = profile
    if operations:
        out["operations"] = operations
    return out


def _http_extract(text, vocab, source_id, cfg, ref_time=None, retrieved=None):
    """사내 LLM(OpenAI 호환) 호출. 프롬프트는 데이터 파일."""
    import urllib.request
    system_prompt, user = _build_prompt(text, vocab, source_id, ref_time, retrieved)
    url = cfg.get("url")
    if not url:
        raise LLMError("E-2004", "llm.json에 url 미설정")
    payload = {"model": cfg.get("model", "internal"),
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": user}]}
    # response_format(json_schema) 전달 시 서버가 구조화 출력을 강제 — claude-proxy·호환 LLM에서 신뢰성↑
    if cfg.get("response_schema"):
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "candidates", "schema": _EXTRACT_SCHEMA}}
    # 사내 LLM이 sampling 파라미터를 요구하면 config로만 추가 (Anthropic엔 temperature 금지)
    payload.update(cfg.get("extra_payload", {}))
    headers = {"Content-Type": "application/json", **cfg.get("headers", {})}
    data = json.dumps(payload).encode("utf-8")
    timeout = cfg.get("timeout", 60)
    import urllib.error
    _log_llm_request("EXTRACT", url, headers, data, cfg)   # 전송 직전 — 타임아웃돼도 요청 전문이 남음
    req = urllib.request.Request(url, data=data, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            _log_llm_response("EXTRACT", getattr(r, "status", getattr(r, "code", "?")), time.time() - t0, raw, cfg)
            resp = json.loads(raw)
    except urllib.error.HTTPError as e:   # 프록시/LLM이 non-2xx — 본문을 남겨 진단 가능하게
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa
            pass
        _log_llm_error("EXTRACT", url, time.time() - t0, e, "HTTP %s · resp body: %s" % (e.code, body[:800]))
        raise LLMError("E-2001", "LLM HTTP %s: %s" % (e.code, body[:220]))
    except Exception as e:  # noqa — 실제 예외 메시지·경과시간을 남겨 디버깅 가능하게
        _log_llm_error("EXTRACT", url, time.time() - t0, e,
                       "timeout=%ss. 직접 호출은 빠른데 여기서만 느리면 위 요청 body의 response_format을 의심(끄려면 "
                       "llm.json에서 \"response_schema\" 제거). 전문 로그: ISSUE_LLM_DEBUG=1" % timeout)
        raise LLMError("E-2001", "LLM 호출 실패(%s, %.1fs, %s): %s"
                       % (type(e).__name__, time.time() - t0, url, e))
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        _llog("  [!] EXTRACT 응답에 choices[0].message.content 없음 — resp 최상위 키: %s" % list(resp)[:12]
              if isinstance(resp, dict) else "  [!] EXTRACT 응답이 dict 아님: %s" % type(resp).__name__)
        raise LLMError("E-2002", "LLM 응답 형식 예상 밖")
    return _parse_candidates(content, text, vocab)


def _anthropic_extract(text, vocab, source_id, cfg, ref_time=None, retrieved=None):
    """Claude를 프록시 LLM으로 사용 (외부 개발 전용, M2 §2.3).
    anthropic SDK는 지연 import — stub/http 경로와 오프라인 반입물엔 의존성 없음."""
    try:
        import anthropic   # 외부 개발 환경에만 설치
    except ImportError:
        raise LLMError("E-2006", "anthropic SDK 미설치 (pip install anthropic)")
    system_prompt, user = _build_prompt(text, vocab, source_id, ref_time, retrieved)
    client_kwargs = {}
    key_env = cfg.get("api_key_env")   # 지정 시 그 env에서 키를 읽어 명시 주입
    if key_env and os.environ.get(key_env):
        client_kwargs["api_key"] = os.environ[key_env]
    auth_hint = "Claude 인증 없음/만료 — ANTHROPIC_API_KEY 설정 또는 `ant auth login` 필요"
    try:
        client = anthropic.Anthropic(**client_kwargs)   # 미지정 시 env/프로필 자동 해소
        # temperature 금지(Opus 4.8), thinking 생략(단순 추출), 비스트리밍(max_tokens<16k)
        resp = client.messages.create(
            model=cfg.get("model", "claude-opus-4-8"),
            max_tokens=cfg.get("max_tokens", 8000),
            system=system_prompt,
            messages=[{"role": "user", "content": user}])
    except getattr(anthropic, "AuthenticationError", ()):   # 키 무효/만료(401)
        raise LLMError("E-2007", auth_hint)
    except anthropic.APIStatusError as e:
        raise LLMError("E-2001", "Claude 호출 실패 HTTP %s" % e.status_code)
    except Exception as e:  # noqa
        m = str(e).lower()
        if isinstance(e, TypeError) or "authentic" in m or "api_key" in m or "auth_token" in m:
            raise LLMError("E-2007", auth_hint)   # 자격증명 미해소
        raise LLMError("E-2001", "Claude 호출 실패: %s" % type(e).__name__)
    if getattr(resp, "stop_reason", None) == "refusal":
        raise LLMError("E-2005", "Claude가 요청을 거부함 (refusal)")
    content = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _parse_candidates(content, text, vocab)


_EXTRACT_SCHEMA = {
    "type": "object", "required": ["entities", "relations"],
    "properties": {
        # 출처 프로파일 — 사용자는 채널을 지정하지 않는다. 원문 자체에서 LLM이 판정한다.
        # 메일 안에 메신저 대화·컨플 회의록이 '중첩'될 수 있으므로 channels는 다중 리스트.
        "source_profile": {
            "type": "object",
            "properties": {
                "channels": {"type": "array",
                             "items": {"enum": ["mail", "messenger", "meeting",
                                                "confluence", "ticket", "other"]},
                             "description": "원문에 실제로 섞여 있는 채널 전부 (중첩 전달 포함)"},
                "category": {"type": "string",
                             "description": "내용 기준 분류 한 개 (예: 불량보고, 불량원인보고, "
                                            "일상보고, 회의결정, 질의, 공지)"},
                "note": {"type": "string", "description": "판정 근거 한 줄"},
            }},
        # 자연어 작업 명령 — 원문이 '기존 항목'의 삭제/숨김을 지시할 때만. 확실할 때만(불확실→비움).
        "operations": {"type": "array", "items": {
            "type": "object", "required": ["op", "target"],
            "properties": {
                "op": {"enum": ["delete", "hide"], "description": "delete=삭제, hide=숨김"},
                "target": {"type": "string",
                           "description": "대상 기존 엔티티 ID — 반드시 '관련 기존 지식' 목록에서만"},
                "evidence": {"type": "string", "description": "명령 근거 원문 인용"},
                "confidence": {"enum": ["low", "medium", "high"]},
            }}},
        "entities": {"type": "array", "items": {
            "type": "object", "required": ["temp_id", "type", "title", "source_refs", "confidence"],
            "properties": {
                "temp_id": {"type": "string"},
                "type": {"enum": ["case", "issue", "finding", "initiative", "action", "pattern", "context"]},
                "title": {"type": "string"}, "summary": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                # PM 정형 필드 — 원문에 있으면 채우고 없으면 생략. 날짜는 YYYY-MM-DD.
                "process": {"type": "string", "description": "공정명 (예: N7, N5)"},
                "product": {"type": "string", "description": "제품·디바이스명"},
                "project": {"type": "string", "description": "과제명"},
                "start_date": {"type": "string", "description": "시작일자 YYYY-MM-DD"},
                "deadline": {"type": "string", "description": "데드라인 YYYY-MM-DD"},
                # 이 후보가 '관련 기존 지식' 목록의 한 항목과 동일 대상이면 그 기존 ID (병합 제안, 사람 확정)
                "merge_into": {"type": "string",
                               "description": "동일 대상인 기존 엔티티 ID (제공된 목록에서만). 병합 제안."},
                # issue 전용 — 원문에서 판단되면. 생애주기 상태·중요도·만료일.
                "status": {"enum": ["정의", "원인탐색", "원인분석", "원인발견", "해결책탐색",
                                    "재발방지", "해결중", "종결", "보류", "재발"],
                           "description": "issue 생애주기 상태 (issue에만)"},
                "severity": {"enum": ["S1", "S2", "S3", "S4"],
                             "description": "issue 중요도 S1(차단)~S4(경미)"},
                "closed_at": {"type": "string", "description": "종결 시 만료일 YYYY-MM-DD"},
                # 시계열 분석용 시간 필드 — 상대표현은 기준 시각에서 절대일자로 환산
                "occurred_at": {"type": "string",
                                "description": "발생·사건 일시. YYYY-MM-DD 또는 YYYY-MM-DD HH:MM:SS"},
                "date_confidence": {"enum": ["exact", "approximate", "uncertain"],
                                    "description": "환산된 일자의 확실성 (근사/불확실 표기)"},
                "date_note": {"type": "string",
                              "description": "환산 근거·불확실 사유. 예: '지난주'를 기준 2026-07-13에서 환산"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"enum": ["low", "medium", "high"]},
            }}},
        "relations": {"type": "array", "items": {
            "type": "object", "required": ["from", "type", "to", "source_refs", "confidence"],
            "properties": {
                "from": {"type": "string"}, "type": {"type": "string"}, "to": {"type": "string"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
                "confidence": {"enum": ["low", "medium", "high"]},
            }}},
    },
}


def _cli_extract(text, vocab, source_id, cfg, ref_time=None, retrieved=None):
    """Claude Code CLI 서브프로세스로 호출 (host 인증 — API 키 불필요, 외부 개발용).
    alice-agnt/backends/claude_cli.py와 동일 방식. 오프라인 반입물엔 미사용."""
    import subprocess
    system_prompt, user = _build_prompt(text, vocab, source_id, ref_time, retrieved)
    full = system_prompt + "\n\n" + user  # user는 이미 추출 지시로 프레이밍됨
    home = os.path.expanduser("~")
    cmd = cfg.get("command", os.path.join(home, "AppData", "Roaming", "npm", "claude.cmd"))
    if not os.path.exists(cmd):
        raise LLMError("E-2008", "claude CLI 없음 (%s) — llm.json의 command 지정 필요" % cmd)
    env = os.environ.copy()
    node_dir = cfg.get("node_dir", "C:/Program Files/nodejs")
    env["PATH"] = str(node_dir) + os.pathsep + env.get("PATH", "")
    argv = [cmd, "-p", "--output-format", "json", "--model", cfg.get("model", "haiku"),
            "--json-schema", json.dumps(_EXTRACT_SCHEMA),
            "--no-session-persistence", "--dangerously-skip-permissions",
            "--disallowed-tools", "Bash,Edit,Write,MultiEdit,NotebookEdit,WebSearch,WebFetch,Task"]
    try:
        proc = subprocess.run(argv, input=full, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=cfg.get("timeout", 240), env=env)
    except subprocess.TimeoutExpired:
        raise LLMError("E-2001", "claude CLI 타임아웃")
    except Exception as e:  # noqa
        raise LLMError("E-2001", "claude CLI 실행 실패: %s" % type(e).__name__)
    if proc.returncode != 0:
        raise LLMError("E-2001", "claude CLI 종료코드 %s: %s" % (proc.returncode, (proc.stderr or "")[:160]))
    try:
        envelope = json.loads((proc.stdout or "").strip())
    except ValueError:
        raise LLMError("E-2002", "claude CLI 응답 파싱 실패")
    if envelope.get("is_error"):
        raise LLMError("E-2001", "claude CLI 오류: %s" % str(envelope.get("result"))[:160])
    so = envelope.get("structured_output")
    payload = json.dumps(so, ensure_ascii=False) if isinstance(so, dict) else str(envelope.get("result", ""))
    return _parse_candidates(payload, text, vocab)


_STUB_PRODUCER = {"type": "tool", "name": "stub-extractor", "version": "0.1"}
_PROVIDERS = {"http": _http_extract, "anthropic": _anthropic_extract, "cli": _cli_extract}


# LLM 세트 스펙 — 각 판단을 전문 LLM 하나로 분해. 실제 시스템 프롬프트는 prompts/*.md.
# stage(직렬 순서 A~E) + parallel(같은 stage 내 병렬 여부) 로 직·병렬 배치를 표현한다.
_ENGINE_SPECS = [
    {"id": "master_crud", "role": "MASTER_CRUD", "title": "기준정보 자연어 CRUD (원문+가이드 해석)",
     "stage": "독립", "parallel": False, "prompt": "master_crud.md", "status": "active",
     "context": ["현재 마스터 목록 (기존 참조는 이 id에서만)", "가이드 텍스트(해석 힌트)"],
     "output": ("items[](type·id·node·aliases·evidence·confidence) + deletes[] + unresolved[]. "
                "**해석만** — 대조·계획·적용은 결정론(seed_plan/store). 추출 파이프라인과 분리된 독립 flow."),
     "schema": True},
    {"id": "attr", "role": "ATTR", "title": "정형 속성·카테고리 추출",
     "stage": "A", "parallel": True, "prompt": "attr.md", "status": "planned",
     "context": ["기준정보 어휘 (공정·제품 정규화 앵커)"],
     "output": "process · product · project · event_name · occurred_at · category · keywords"},
    {"id": "provenance", "role": "SOURCE", "title": "출처·신뢰도 분석",
     "stage": "A", "parallel": True, "prompt": "provenance.md", "status": "planned",
     "context": ["채널 사전 (mail·messenger·meeting·confluence)"],
     "output": ("channels[](다중 — 메일 안에 메신저·컨플 회의록이 중첩될 수 있음) · reporter · "
                "reported_at · evidence_type · reliability. "
                "※ 채널 다중분류+내용 분류는 EXTRACT 세트의 source_profile로 1차 통합 운용 중 — "
                "사용자는 채널을 지정하지 않는다(capture-first)."),},
    {"id": "severity", "role": "SEVERITY", "title": "이슈 여부·심각도 판정",
     "stage": "A", "parallel": True, "prompt": "severity.md", "status": "planned",
     "context": ["심각도 S1–S4 기준", "실패유형 A/B 정의 (M1)"],
     "output": "is_issue · severity(S1–S4) · failure_type(A/B) · confidence"},
    {"id": "extract", "role": "EXTRACT", "title": "Entity·Relation 후보 추출",
     "stage": "B", "parallel": False, "prompt": "extract.md", "status": "active",
     "context": ["기준정보 어휘 (vocabulary)", "출력 스키마 (JSON Schema)",
                 "STAGE A 산출 (속성·심각도)"],
     "output": ("entities[] + relations[] 후보 (temp_id 기반) + source_profile"
                "(channels[] 다중 채널분류 · category 내용분류 — 수동 채널 선택 대체)"), "schema": True,
     "user_template": ("다음 원문에서 위 규칙에 따라 source_profile 1개와 Entity·Relation 후보를 "
                       "생성하라. 반드시 지정된 필드 구조로만 출력하고, 다른 스키마나 설명 문장을 "
                       "덧붙이지 않는다.\n\n원문:\n<CAPTURE된 원문 텍스트>"),
     "runtime": ("런타임에 system 끝에 '기준 시각(프롬프트 전송 시점)' + 'SOURCE-ID: <sid>' + "
                 "'알려진 context 별칭 …' 주입 — 상대 일자 환산의 기준")},
    {"id": "link", "role": "LINK", "title": "버전좌표 해소",
     "stage": "C", "parallel": False, "prompt": "link.md", "status": "planned",
     "context": ["기준정보 (PDK 버전·released IP·process·device·project)"],
     "output": "mention → canonical_id 매칭 (found / ambiguous / unknown)"},
    {"id": "dedup", "role": "DEDUP", "title": "중복·재발 판정",
     "stage": "D", "parallel": True, "prompt": "dedup.md", "status": "planned",
     "context": ["기존 확정 이슈 목록 (버전좌표)"],
     "output": "duplicate_of · recurrence_of · similar_to"},
    {"id": "action", "role": "ACTION", "title": "조치·오너·기한 추출",
     "stage": "D", "parallel": True, "prompt": "action.md", "status": "planned",
     "context": [],
     "output": "actions[ {title, owner, due, status} ]"},
    {"id": "brief", "role": "BRIEF", "title": "브리프·현황 생성",
     "stage": "E", "parallel": False, "prompt": "brief.md", "status": "planned",
     "context": ["확정 지식 그래프 · PM 필드"],
     "output": "headline · by_severity · at_risk[] · summary"},
]

_ENGINE_STAGES = [
    {"id": "A", "title": "원문 병렬 분석", "parallel": True,
     "desc": "원문에서 정형 속성·출처·심각도를 병렬로 추출·판정"},
    {"id": "B", "title": "온톨로지 후보 추출", "parallel": False,
     "desc": "엔티티·관계 후보 생성 (현재 활성 세트)"},
    {"id": "C", "title": "버전좌표 해소", "parallel": False,
     "desc": "기준정보 대조로 정식 좌표(process×device×rule_deck×IP×project) 매핑"},
    {"id": "D", "title": "관계·조치 판정", "parallel": True,
     "desc": "중복·재발과 조치를 병렬 판정"},
    {"id": "E", "title": "브리프 생성", "parallel": False,
     "desc": "확정 지식 → PM 현황 브리프"},
]


def describe_engines(vocab=None):
    """LLM '세트'(= API + 시스템프롬프트 + 함께 제공되는 컨텍스트)들을 그대로 노출한다.
    각 판단을 전문 LLM 하나로 분해하고, 실제 프롬프트 파일을 직접 읽어 보여준다(문서-실제 불일치 방지).
    status: active=현재 파이프라인에 연결됨, planned=프롬프트 완비·연결 예정."""
    vocab = vocab or {}
    aliases = sorted({a for c in vocab.get("contexts", []) for a in c.get("aliases", [])})
    try:
        cfg = load_llm_config()
    except Exception:  # noqa
        cfg = {}
    api = {"mode": cfg.get("mode"), "model": cfg.get("model"), "endpoint": cfg.get("url"),
           "via": "claude-proxy (사내 반입 시 url만 사내 LLM으로 교체 — 동일 mode:http)"}

    def load_prompt(fn):
        try:
            with open(os.path.join(ROOT, "prompts", fn), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return "(prompts/%s 없음)" % fn

    engines = []
    for sp in _ENGINE_SPECS:
        ctx = []
        for name in sp.get("context", []):
            entry = {"name": name}
            if "어휘" in name or "기준정보" in name:
                entry["detail"] = "현재 %d개 별칭" % len(aliases)
                entry["items"] = aliases
            ctx.append(entry)
        if sp.get("schema"):
            ctx.append({"name": "출력 스키마 (JSON Schema)",
                        "detail": "구조 강제 (response_format / --json-schema)",
                        "schema": _EXTRACT_SCHEMA})
        engines.append({
            "id": sp["id"], "role": sp["role"], "title": sp["title"], "kind": "llm",
            "status": sp["status"], "stage": sp["stage"], "parallel": sp["parallel"],
            "arrangement": ("병렬" if sp["parallel"] else "직렬") + " · STAGE " + sp["stage"],
            "api": api,
            "system_prompt": load_prompt(sp["prompt"]),
            "system_runtime_suffix": sp.get("runtime"),
            "user_template": sp.get("user_template"),
            "context": ctx,
            "output": sp["output"],
        })
    active = sum(1 for e in engines if e["status"] == "active")
    return {
        "engines": engines, "stages": _ENGINE_STAGES,
        "counts": {"total": len(engines), "active": active, "planned": len(engines) - active},
        "note": ("세트 = (LLM API + 시스템프롬프트 + 함께 제공되는 컨텍스트). "
                 "각 판단을 전문 LLM 하나로 분해하고, 이 세트들을 직렬·병렬로 배치해 온톨로지를 구성한다. "
                 "지금은 %d개 세트가 설계돼 있고 그중 EXTRACT %d개가 파이프라인에 연결(active)돼 있다. "
                 "나머지는 프롬프트가 완비된 상태로 사내 LLM 반입 후 순차 연결한다." % (len(engines), active)),
    }


def extract(text, vocab, source_id, ref_time=None, retrieved=None):
    """(result, producer). 설정된 LLM으로 추출한다.
    ref_time = 프롬프트 전송 시점(캡처 시각) — 상대 일자 환산의 기준으로 LLM에 전달.
    retrieved = RETRIEVE 카드 목록 — '관련 기존 지식'으로 프롬프트에 첨부(M5).

    **stub 폴백은 기본 OFF** (운영 결정): LLM 실패를 stub 결과로 조용히 덮으면 실제로는 실패인데
    성공처럼 보이므로 위험하다. LLM 실패는 그대로 raise 해 UI/로그에 드러낸다. stub 기능 자체는
    보존한다 — (1) `mode:"stub"`(오프라인·selftest·무설정 기본) 명시 사용, (2) `"fallback":"stub"`을
    llm.json에 **명시**하면 예전 폴백 동작을 opt-in으로 되살릴 수 있다."""
    cfg = load_llm_config()
    mode = cfg.get("mode", "stub")
    _llog("extract: mode=%s model=%s url=%s timeout=%ss response_schema=%s retrieved=%d src=%s"
          % (mode, cfg.get("model"), cfg.get("url"), cfg.get("timeout", 60),
             bool(cfg.get("response_schema")), len(retrieved or []), source_id))
    if mode == "stub":   # 명시적 stub(오프라인·selftest·무설정 기본) — 보존
        return stub_extract(text, vocab, source_id), dict(_STUB_PRODUCER)
    if mode not in _PROVIDERS:   # 오타/미지원 mode → 조용히 stub 하지 않고 명확히 실패
        _llog("  [X] 미지원 mode '%s' — http/anthropic/cli/stub 중이어야 함(stub 자동대체 안 함)" % mode)
        raise LLMError("E-2008", "미지원 LLM mode '%s' (http/anthropic/cli/stub 중 하나)" % mode)
    t0 = time.time()
    try:
        result = _PROVIDERS[mode](text, vocab, source_id, cfg, ref_time, retrieved)
        _llog("extract: %s 성공 %.2fs (엔티티 %d·관계 %d)"
              % (mode, time.time() - t0, len(result.get("entities", []) or []), len(result.get("relations", []) or [])))
        return result, cfg.get("producer", {"type": "llm", "name": mode, "version": "?"})
    except LLMError as e:
        # stub 폴백 기본 OFF — 실패를 조용히 stub으로 덮지 않는다. 되살리려면 "fallback":"stub" 명시.
        if cfg.get("fallback", "none") != "stub":
            _llog("extract: %s 실패(stub 폴백 없음) %.2fs %s: %s" % (mode, time.time() - t0, e.code, e))
            raise
        _llog("extract: %s 실패 -> stub 폴백(fallback:stub 명시적 opt-in) %.2fs %s: %s"
              % (mode, time.time() - t0, e.code, e))
        result = stub_extract(text, vocab, source_id)
        result["fallback"] = {"from": mode, "code": e.code, "reason": str(e)}
        producer = dict(_STUB_PRODUCER); producer["fallback_from"] = mode
        return result, producer


def _prompt_file(name):
    """시스템 프롬프트는 **데이터 파일**(prompts/*.md)이다 — 코드에 하드코딩하지 않는다.
    유저가 편집·교체할 수 있어야 하고, 프롬프트 탭이 실제 파일을 읽어 노출한다."""
    try:
        with open(os.path.join(ROOT, "prompts", name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        raise LLMError("E-2010", "프롬프트 파일 없음: prompts/%s" % name)


def _lenient_json_obj(content):
    """LLM 텍스트에서 JSON **객체 하나**를 관대하게 뽑는다 — 코드펜스·서두 설명을 흡수.
    사내 LLM은 response_schema(구조화 출력 강제)를 못 쓰는 경우가 많아 자유형식 응답에 대비한다."""
    if not isinstance(content, str):
        return None
    body = "\n".join(ln for ln in content.splitlines() if not ln.strip().startswith("```")).strip()
    try:
        o = json.loads(body)
        return o if isinstance(o, dict) else None
    except ValueError:
        pass
    i, j = body.find("{"), body.rfind("}")   # 서두 설명이 붙은 경우 첫 { ~ 마지막 }
    if i >= 0 and j > i:
        try:
            o = json.loads(body[i:j + 1])
            return o if isinstance(o, dict) else None
        except ValueError:
            return None
    return None


# ---------- 기준정보(마스터) CRUD — 원문+가이드 해석 (독립 flow, 추출과 분리) ----------
_MASTER_CRUD_SCHEMA = {
    "type": "object", "required": ["items"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "required": ["type", "id"],
            "properties": {
                "type": {"enum": ["node", "process", "product", "project"]},
                "id": {"type": "string"}, "node": {"type": "string"}, "label": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "string"},
                "confidence": {"enum": ["low", "medium", "high"]}}}},
        "deletes": {"type": "array", "items": {
            "type": "object", "required": ["type", "id"],
            "properties": {"type": {"enum": ["node", "process", "product", "project"]},
                           "id": {"type": "string"}, "evidence": {"type": "string"}}}},
        "unresolved": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}, "why": {"type": "string"}}}},
    }}


def _master_context(master):
    """현재 마스터를 프롬프트에 넣을 압축 목록으로. 기존 항목 참조는 이 id에서만(환각 차단)."""
    lines = []
    for typ, key in (("node", "nodes"), ("process", "processes"),
                     ("product", "products"), ("project", "projects")):
        for it in (master or {}).get(key, []) or []:
            al = ", ".join(it.get("aliases") or [])
            nd = " (node=%s)" % it["node"] if typ == "process" and it.get("node") else ""
            lines.append("- %s: %s%s%s" % (typ, it.get("id"), nd, (" | 별칭: " + al) if al else ""))
    return "\n".join(lines) if lines else "(등록된 기준정보 없음 — 전부 신규)"


def master_crud_plan(text, guide=None, master=None, cfg=None):
    """원문(아무 형식)+가이드 → 기준정보 항목 해석. **LLM이 해석만** 하고, 기존 마스터와의 대조·
    계획·적용은 결정론(master.seed_plan/store)이 한다. 반환 {items, deletes, unresolved}.
    추출 파이프라인과 **완전히 분리된 독립 flow** (프롬프트·스텝·저장 경로 공유 안 함)."""
    cfg = cfg or load_llm_config()
    mode = cfg.get("mode", "stub")
    if mode != "http":
        raise LLMError("E-2009", "기준정보 자연어 CRUD는 mode:http에서만 동작합니다 (현재 mode=%s)" % mode)
    system = _prompt_file("master_crud.md") + "\n\n[현재 마스터]\n" + _master_context(master)
    user = ("[가이드]\n" + ((guide or "").strip() or "없음")
            + "\n\n[원문]\n" + (text or ""))
    payload = {"model": cfg.get("model", "internal"),
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if cfg.get("response_schema"):
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "master_crud", "schema": _MASTER_CRUD_SCHEMA}}
    payload.update(cfg.get("extra_payload", {}))
    headers = {"Content-Type": "application/json", **cfg.get("headers", {})}
    data = json.dumps(payload).encode("utf-8")
    url, timeout = cfg.get("url"), cfg.get("timeout", 60)
    if not url:
        raise LLMError("E-2004", "llm.json에 url 미설정")
    import urllib.request
    import urllib.error
    _log_llm_request("MASTER_CRUD", url, headers, data, cfg)
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers),
                                    timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            _log_llm_response("MASTER_CRUD", getattr(r, "status", "?"), time.time() - t0, raw, cfg)
            resp = json.loads(raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa
            pass
        _log_llm_error("MASTER_CRUD", url, time.time() - t0, e, "HTTP %s · %s" % (e.code, body[:500]))
        raise LLMError("E-2001", "LLM HTTP %s: %s" % (e.code, body[:220]))
    except Exception as e:  # noqa
        _log_llm_error("MASTER_CRUD", url, time.time() - t0, e, "timeout=%ss" % timeout)
        raise LLMError("E-2001", "LLM 호출 실패(%s, %.1fs, %s): %s"
                       % (type(e).__name__, time.time() - t0, url, e))
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("E-2002", "LLM 응답 형식 예상 밖")
    obj = _lenient_json_obj(content)   # 코드펜스·서두 잡음 흡수 (자유형식 LLM 대비)
    if obj is None:
        raise LLMError("E-2002", "LLM 응답에서 JSON을 찾지 못함")
    out = {"items": [], "deletes": [], "unresolved": []}
    for it in (obj.get("items") or []):
        if not isinstance(it, dict) or it.get("type") not in ("node", "process", "product", "project"):
            continue
        mid = str(it.get("id") or "").strip()
        if not mid:
            continue
        out["items"].append({
            "type": it["type"], "id": mid,
            "node": (str(it["node"]).strip() if it.get("node") else None),
            "label": (str(it["label"]).strip() if it.get("label") else None),
            "aliases": [str(a).strip() for a in (it.get("aliases") or []) if str(a).strip()],
            "evidence": it.get("evidence"), "confidence": it.get("confidence")})
    for d in (obj.get("deletes") or []):
        if isinstance(d, dict) and d.get("type") in ("node", "process", "product", "project") and d.get("id"):
            out["deletes"].append({"type": d["type"], "id": str(d["id"]).strip(),
                                   "evidence": d.get("evidence")})
    for u in (obj.get("unresolved") or []):
        if isinstance(u, dict):
            out["unresolved"].append({"text": u.get("text"), "why": u.get("why")})
    return out


# ---------- 이슈 CRUD — 원문+가이드 해석 (독립 flow, 추출과 분리) ----------
_ISSUE_CRUD_SCHEMA = {
    "type": "object", "required": ["items"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "required": ["op"],
            "properties": {
                "op": {"enum": ["create", "update", "delete"]},
                "id": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"},
                "coordinates": {"type": "object", "properties": {
                    "node": {"type": "string"}, "process": {"type": "string"},
                    "product": {"type": "string"}, "project": {"type": "string"}}},
                "status": {"type": "string"}, "severity": {"type": "string"},
                "start_date": {"type": "string"}, "deadline": {"type": "string"},
                "evidence": {"type": "string"}, "confidence": {"enum": ["low", "medium", "high"]}}}},
        "needs_master": {"type": "array", "items": {
            "type": "object", "properties": {"surface": {"type": "string"}, "why": {"type": "string"}}}},
        "unresolved": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}, "why": {"type": "string"}}}},
    }}


def _issue_context(issues):
    """등록된 이슈를 프롬프트용 압축 목록으로 — update/delete 대상은 이 id에서만(환각 차단)."""
    lines = []
    for e in issues or []:
        coord = "/".join(x for x in (e.get("node"), e.get("process"), e.get("product"),
                                     e.get("project")) if x)
        lines.append("- %s | %s | %s%s" % (e.get("id"), (e.get("title") or "")[:50],
                     coord or "좌표없음", (" | " + e.get("status")) if e.get("status") else ""))
    return "\n".join(lines) if lines else "(등록된 이슈 없음)"


def issue_crud_plan(text, guide=None, issues=None, master=None, cfg=None):
    """원문+가이드 → 이슈 항목 해석. **LLM은 해석만**, 대조·계획·적용은 결정론(pipeline/store).
    반환 {items, needs_master, unresolved}. 추출 파이프라인과 분리된 독립 flow."""
    cfg = cfg or load_llm_config()
    if cfg.get("mode", "stub") != "http":
        raise LLMError("E-2009", "이슈 자연어 CRUD는 mode:http에서만 동작합니다 (현재 mode=%s)" % cfg.get("mode", "stub"))
    system = (_prompt_file("issue_crud.md")
              + "\n\n[등록된 마스터]\n" + _master_context(master)
              + "\n\n[등록된 이슈]\n" + _issue_context(issues))
    user = "[가이드]\n" + ((guide or "").strip() or "없음") + "\n\n[원문]\n" + (text or "")
    payload = {"model": cfg.get("model", "internal"),
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if cfg.get("response_schema"):
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "issue_crud", "schema": _ISSUE_CRUD_SCHEMA}}
    payload.update(cfg.get("extra_payload", {}))
    headers = {"Content-Type": "application/json", **cfg.get("headers", {})}
    data = json.dumps(payload).encode("utf-8")
    url, timeout = cfg.get("url"), cfg.get("timeout", 60)
    if not url:
        raise LLMError("E-2004", "llm.json에 url 미설정")
    import urllib.request
    import urllib.error
    _log_llm_request("ISSUE_CRUD", url, headers, data, cfg)
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers),
                                    timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            _log_llm_response("ISSUE_CRUD", getattr(r, "status", "?"), time.time() - t0, raw, cfg)
            resp = json.loads(raw)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa
            pass
        _log_llm_error("ISSUE_CRUD", url, time.time() - t0, e, "HTTP %s · %s" % (e.code, body[:500]))
        raise LLMError("E-2001", "LLM HTTP %s: %s" % (e.code, body[:220]))
    except Exception as e:  # noqa
        _log_llm_error("ISSUE_CRUD", url, time.time() - t0, e, "timeout=%ss" % timeout)
        raise LLMError("E-2001", "LLM 호출 실패(%s, %.1fs, %s): %s"
                       % (type(e).__name__, time.time() - t0, url, e))
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("E-2002", "LLM 응답 형식 예상 밖")
    obj = _lenient_json_obj(content)
    if obj is None:
        raise LLMError("E-2002", "LLM 응답에서 JSON을 찾지 못함")
    _S = {"정의", "원인탐색", "원인분석", "원인발견", "해결책탐색", "재발방지", "해결중", "종결", "보류", "재발"}
    out = {"items": [], "needs_master": [], "unresolved": []}
    for it in (obj.get("items") or []):
        if not isinstance(it, dict) or it.get("op") not in ("create", "update", "delete"):
            continue
        row = {"op": it["op"]}
        if it.get("id"):
            row["id"] = str(it["id"]).strip()
        for k in ("title", "summary", "start_date", "deadline", "evidence"):
            if it.get(k):
                row[k] = str(it[k]).strip()
        if it.get("status") in _S:
            row["status"] = it["status"]
        if it.get("severity") in ("S1", "S2", "S3", "S4"):
            row["severity"] = it["severity"]
        c = it.get("coordinates") or {}
        row["coordinates"] = {k: (str(c[k]).strip() if c.get(k) else None)
                              for k in ("node", "process", "product", "project")}
        if it.get("confidence"):
            row["confidence"] = it["confidence"]
        out["items"].append(row)
    for u in (obj.get("needs_master") or []):
        if isinstance(u, dict):
            out["needs_master"].append({"surface": u.get("surface"), "why": u.get("why")})
    for u in (obj.get("unresolved") or []):
        if isinstance(u, dict):
            out["unresolved"].append({"text": u.get("text"), "why": u.get("why")})
    return out


# ---------- 자연어 작업 명령 감지 (전용 focused 경로 — 약한 LLM에도 견고) ----------
_CMD_MARKERS = ("삭제", "지워", "지우", "없애", "없앤", "제거해", "제거하", "빼줘", "빼주",
                "숨겨", "숨김", "숨기", "안 보이", "목록에서 빼", "잘못 등록", "오입력", "삭제해")
_OP_SCHEMA = {"type": "object", "required": ["operations"], "properties": {
    "operations": {"type": "array", "items": {
        "type": "object", "required": ["op", "target"], "properties": {
            "op": {"enum": ["delete", "hide", "none"]},
            "target": {"type": "string"},
            "evidence": {"type": "string"},
            "confidence": {"enum": ["low", "medium", "high"]}}}}}}


def has_command_marker(text):
    """결정론적 사전 필터 — 명령 어휘가 있을 때만 op 감지 LLM을 호출(비용·오탐 절감)."""
    return any(m in (text or "") for m in _CMD_MARKERS)


def detect_operations(text, retrieved, cfg=None):
    """삭제/숨김 명령만 뽑는 전용 프롬프트. 대상은 retrieved 목록의 ID에서만(환각 차단), 보수적."""
    cfg = cfg or load_llm_config()
    if cfg.get("mode") != "http" or not retrieved:
        return []
    cand = "\n".join("- %s | %s" % (c.get("id"), (c.get("title") or "")) for c in retrieved)
    system = ("당신은 QA 원문에서 '기존 항목 조작 명령'만 감지하는 분류기다. "
              "op=delete(삭제 지시) 또는 hide(숨김 지시)만 뽑는다.\n"
              "규칙: (1) '지워/삭제/잘못 등록/없애'=delete, '숨겨/목록에서 빼/안 보이게'=hide. "
              "(2) target은 아래 '기존 항목' id에서만 고른다. 모호하거나 목록에 없으면 그 명령을 만들지 않는다. "
              "(3) '불량이 제거됐다/해결됐다' 같은 사실 서술은 명령이 아니다(무시). "
              "(4) 확실하지 않으면 operations를 비운다. evidence에 근거 문구를 인용한다. 설명 없이 JSON만.\n\n"
              "기존 항목:\n" + cand)
    user = "다음 원문에서 삭제/숨김 명령만 뽑아라:\n\n" + text
    payload = {"model": cfg.get("model", "internal"),
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "response_format": {"type": "json_schema", "json_schema": {"name": "ops", "schema": _OP_SCHEMA}}}
    import urllib.request
    url = cfg.get("url")
    headers = {"Content-Type": "application/json", **cfg.get("headers", {})}
    body = json.dumps(payload).encode("utf-8")
    timeout = cfg.get("timeout", 60)
    # 부하로 프록시가 CLI를 직렬화하면 간헐 502가 날 수 있어 1회 재시도(명령 유실 방지).
    data = None
    for attempt in range(2):
        _log_llm_request("DETECT_OPS(시도 %d)" % (attempt + 1), url, headers, body, cfg)
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                _log_llm_response("DETECT_OPS", getattr(r, "status", "?"), time.time() - t0, raw, cfg)
                resp = json.loads(raw)
            content = resp["choices"][0]["message"]["content"]
            data = json.loads(content) if content.strip()[:1] == "{" else {}
            break
        except Exception as e:  # noqa — 명령 감지 실패는 치명적 아님 (상세 화면 삭제 버튼이 확정 경로)
            _log_llm_error("DETECT_OPS(시도 %d)" % (attempt + 1), url, time.time() - t0, e)
            data = None
    if data is None:
        return []
    valid = {c.get("id") for c in retrieved}
    out = []
    for o in (data.get("operations") or []):
        if isinstance(o, dict) and o.get("op") in ("delete", "hide") and o.get("target") in valid:
            out.append({k: o[k] for k in ("op", "target", "evidence", "confidence") if k in o})
    return out
