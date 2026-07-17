# preprocess.py — STRUCTURE 단계의 결정론 코어 (LLM 없음).
# 거대·비정형 원문 덤프(30번 루프 돈 메일 스레드 등)를 → 원자 메시지로 분해 →
# 인용 반복을 중복제거 → 구조화 문서 N개로 만든다. EXTRACT는 이 위에서 작고 깨끗한 입력을 받는다.
#
# 설계 원칙:
# - **완전 결정론**: 정규식 경계 감지 + 내용 해시. 같은 입력 → 항상 같은 출력.
# - **원문 보존**: 아무것도 버리지 않는다. 각 문서는 provenance(원본 줄 범위)를 들고 있어
#   원문으로 되짚을 수 있다. 중복은 '폐기'가 아니라 'dup_of'로 접힌다(감사 가능).
# - 채널·헤더 감지 실패는 치명적이지 않다 — 최소한 한 덩어리 문서로 나온다.
import hashlib
import re

# ---------- 경계 감지 패턴 ----------
# 메일 헤더 (한/영, 아웃룩·지메일·사내 메일 공통 표기)
_H_FROM = re.compile(r"^\s*(보낸사람|보낸이|발신|발신자|From)\s*[:：]\s*(.+?)\s*$", re.I)
_H_DATE = re.compile(r"^\s*(보낸날짜|날짜|보낸시각|Sent|Date)\s*[:：]\s*(.+?)\s*$", re.I)
_H_SUBJ = re.compile(r"^\s*(제목|Subject)\s*[:：]\s*(.+?)\s*$", re.I)
_H_TO = re.compile(r"^\s*(받는사람|수신|To)\s*[:：]\s*(.+?)\s*$", re.I)
_H_ANY = (_H_FROM, _H_DATE, _H_SUBJ, _H_TO)

# 인용 구분자 (새 메시지의 시작을 알리는 줄)
_SEP = re.compile(
    r"^\s*(-{2,}\s*(Original Message|원본 메시지|Forwarded message|전달된 메시지)\s*-{2,}"
    r"|_{10,}"
    r"|-{10,}"
    r"|(On|On)\s+.{4,60}\s+wrote\s*[:：]"
    r"|.{1,40}\s*(님이|님께서)\s*(작성|작성했습니다|씀)\s*[:：]?)\s*$", re.I)

# 메신저 라인:  [10:23] 김QA:   /  김QA [10:23]:  /  오전 10:23 김QA
_MSGR = re.compile(
    r"^\s*(?:\[(?P<t1>\d{1,2}:\d{2})\]\s*(?P<n1>[^:：\[\]]{1,20})\s*[:：]"
    r"|(?P<n2>[^:：\[\]]{1,20})\s*\[(?P<t2>\d{1,2}:\d{2})\]\s*[:：]?"
    r"|(?P<ampm>오전|오후)\s*(?P<t3>\d{1,2}:\d{2})\s+(?P<n3>[^:：]{1,20})\s*[:：])\s*(?P<rest>.*)$")

_QUOTE = re.compile(r"^(\s*>)+\s?")


def _quote_depth(line):
    """앞머리 '>' 개수 = 인용 깊이. '> > >' 처럼 띄어쓴 것도 센다."""
    d, i, n = 0, 0, len(line)
    while i < n:
        if line[i] in " \t":
            i += 1
        elif line[i] == ">":
            d += 1
            i += 1
        else:
            break
    return d


def _strip_quote(line):
    return _QUOTE.sub("", line)


def _norm_for_hash(text):
    """해시용 정규화 — 인용마커·공백·대소문자 차이를 흡수해 '같은 메시지'를 같게 본다."""
    t = _QUOTE.sub("", text or "")
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


def _h(text):
    return hashlib.sha1(_norm_for_hash(text).encode("utf-8")).hexdigest()[:16]


def _is_header_line(s):
    return any(p.match(s) for p in _H_ANY)


def split_messages(text):
    """덤프 → 원자 메시지 세그먼트. 결정론.
    경계: (1) From/보낸사람 헤더 시작, (2) 인용 구분자(-----Original-----, 'On … wrote:', '님이 작성:'),
    (3) 메신저 라인. 아무 경계도 못 찾으면 전체를 문서 1개로 낸다(손실 없음)."""
    lines = (text or "").split("\n")
    segs, cur = [], None

    def flush():
        if cur and any(l.strip() for l in cur["lines"]):
            segs.append(cur)

    def start(i, channel, depth):
        return {"channel": channel, "quote_depth": depth, "line_start": i + 1,
                "lines": [], "sender": None, "timestamp": None, "subject": None}

    for i, raw in enumerate(lines):
        depth = _quote_depth(raw)
        s = _strip_quote(raw).rstrip()
        boundary, channel = None, None
        if _H_FROM.match(s):
            boundary, channel = "mail", "mail"
        elif _SEP.match(s):
            boundary, channel = "sep", "mail"
        else:
            m = _MSGR.match(s)
            if m:
                boundary, channel = "messenger", "messenger"

        if boundary:
            flush()
            cur = start(i, channel, depth)
            cur["line_end"] = i + 1
            if boundary == "mail":
                cur["sender"] = _H_FROM.match(s).group(2).strip()
            elif boundary == "messenger":
                m = _MSGR.match(s)
                cur["sender"] = (m.group("n1") or m.group("n2") or m.group("n3") or "").strip()
                cur["timestamp"] = (m.group("t1") or m.group("t2") or m.group("t3") or "").strip()
                if m.group("ampm"):
                    cur["timestamp"] = m.group("ampm") + " " + cur["timestamp"]
                if (m.group("rest") or "").strip():
                    cur["lines"].append(m.group("rest").strip())
            continue

        if cur is None:   # 첫 경계 이전의 서두 — 버리지 않고 문서로 담는다
            cur = start(i, "unknown", depth)
        # 헤더 줄이면 메타로 흡수, 아니면 본문
        if _H_DATE.match(s) and not cur["timestamp"]:
            cur["timestamp"] = _H_DATE.match(s).group(2).strip()
        elif _H_SUBJ.match(s) and not cur["subject"]:
            cur["subject"] = _H_SUBJ.match(s).group(2).strip()
        elif _H_TO.match(s):
            pass   # 수신자는 현재 쓰지 않음(필요해지면 메타로)
        elif _is_header_line(s):
            pass
        else:
            cur["lines"].append(s)
        cur["line_end"] = i + 1
        # quote_depth는 **경계 줄의 깊이**로 고정한다. 본문 줄로 갱신하면 인용 블록 중간의
        # 빈 줄(깊이 0)이 깊이를 0으로 오염시켜 dedup의 '가장 얕은 것=원본' 판정이 깨진다.
    flush()

    for sg in segs:
        body = "\n".join(sg.pop("lines")).strip()
        sg["body_clean"] = body
        sg["hash"] = _h(body)
    return [s for s in segs if s["body_clean"]]


def dedup_messages(segs):
    """인용 반복 접기. 같은 본문(해시 동일)이 여러 번 나오면 **인용 깊이가 가장 얕은**(=원본,
    가장 온전한) 것을 남기고 나머지는 dup_of로 접는다. 30루프 스레드 → 고유 k건.
    폐기가 아니라 접기 — 접힌 것도 dups에 남아 감사 가능."""
    best = {}
    for sg in segs:
        h = sg["hash"]
        prev = best.get(h)
        if prev is None or sg["quote_depth"] < prev["quote_depth"]:
            best[h] = sg
    kept, dups = [], []
    seen = set()
    for sg in segs:
        h = sg["hash"]
        if best[h] is sg and h not in seen:
            seen.add(h)
            kept.append(sg)
        else:
            dups.append({"hash": h, "line_start": sg["line_start"], "line_end": sg["line_end"],
                         "quote_depth": sg["quote_depth"], "dup_of": best[h]["line_start"]})
    # 메타 보강: 접힌 인용본에만 sender/timestamp가 있으면 대표로 올린다
    for sg in kept:
        if sg.get("sender") and sg.get("timestamp"):
            continue
        for o in segs:
            if o["hash"] == sg["hash"] and o is not sg:
                sg["sender"] = sg.get("sender") or o.get("sender")
                sg["timestamp"] = sg.get("timestamp") or o.get("timestamp")
                sg["subject"] = sg.get("subject") or o.get("subject")
    return kept, dups


def structure(text, source_id="SRC-?"):
    """덤프 → 구조화 문서 N개 (+ 접힌 중복 통계). STRUCTURE 단계의 결정론 산출물."""
    segs = split_messages(text)
    kept, dups = dedup_messages(segs)
    docs = []
    for i, sg in enumerate(kept, start=1):
        docs.append({
            "doc_id": "%s#m%02d" % (source_id, i),
            "source_id": source_id,
            "seq": i,
            "channel": sg["channel"],
            "sender": sg.get("sender"),
            "timestamp": sg.get("timestamp"),
            "subject": sg.get("subject"),
            "quote_depth": sg["quote_depth"],
            "body_clean": sg["body_clean"],
            "hash": sg["hash"],
            "provenance": {"source_id": source_id,
                           "line_start": sg["line_start"], "line_end": sg["line_end"]},
        })
    return {"docs": docs,
            "stats": {"segments": len(segs), "docs": len(docs), "folded_dups": len(dups),
                      "chars_in": len(text or ""),
                      "chars_out": sum(len(d["body_clean"]) for d in docs)},
            "dups": dups}
