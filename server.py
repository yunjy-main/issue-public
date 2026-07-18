# issue — 이슈 지식 시스템 서버 (stdlib only, 오프라인 반입 전제)
# GET  /                     -> web/index.html
# GET  /api/docs             -> docs/*.md 스캔 매니페스트
# GET  /api/doc?id=          -> 문서 본문 + 메타
# POST /api/workflow/step    -> 파이프라인 단계 실행 (M3 §6)
# GET  /api/runs|entities|relations|sources|stats|vocabulary -> 지식 저장소 조회
import argparse
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store
import pipeline
import llm

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(ROOT, "docs")
WEB_DIR = os.path.join(ROOT, "web")

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
# frontmatter 한 줄은 'key:' 또는 들여쓴 연속줄만 허용 (본문 '---' 수평선 오인 방지)
KEYLINE_RE = re.compile(r"^(?:\s+\S|[A-Za-z_][\w-]*\s*:)")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def parse_doc(path):
    # utf-8-sig: BOM이 있어도 frontmatter 매칭이 깨지지 않음
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    meta = {}
    body = raw
    m = FM_RE.match(raw)
    block = m.group(1).splitlines() if m else []
    if m and all(not ln.strip() or KEYLINE_RE.match(ln) for ln in block):
        body = raw[m.end():]
        for line in block:
            if ":" in line and not line[:1].isspace():
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    st = os.stat(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    # id가 비었거나 안전 문자 규칙을 벗어나면 파일명 스템으로 폴백
    # (유령 문서 방지 + 매니페스트 경유 XSS 원천 차단)
    if not ID_RE.fullmatch(meta.get("id", "")):
        meta["id"] = stem
    meta["file"] = os.path.basename(path)
    meta["size"] = st.st_size
    meta["lines"] = raw.count("\n") + 1
    try:
        meta["order"] = int(meta.get("order", 999))
    except ValueError:
        meta["order"] = 999
    return meta, body


def scan_docs():
    # 손상·비UTF8 파일 하나가 매니페스트 전체를 죽이지 않도록 파일 단위로 격리 (E-1003)
    items, skipped = [], []
    if os.path.isdir(DOCS_DIR):
        for name in sorted(os.listdir(DOCS_DIR)):
            if name.lower().endswith(".md"):
                try:
                    meta, _ = parse_doc(os.path.join(DOCS_DIR, name))
                    items.append(meta)
                except (OSError, UnicodeDecodeError) as e:
                    skipped.append({"file": name, "code": "E-1003",
                                    "error": type(e).__name__})
    items.sort(key=lambda x: (x.get("order", 999), x.get("id", "")))
    # 중복 id 경고 (find_doc이 비결정적으로 아무거나 반환하는 상황을 가시화)
    seen = {}
    for it in items:
        seen.setdefault(it["id"], []).append(it["file"])
    for did, files in seen.items():
        if len(files) > 1:
            skipped.append({"file": ", ".join(files), "code": "E-1004",
                            "error": "DuplicateId:" + did})
    return items, skipped


def find_doc(doc_id):
    if not os.path.isdir(DOCS_DIR):
        return None, None
    # scan_docs와 동일하게 정렬해 중복 id 시에도 결정적으로 같은 파일 반환
    for name in sorted(os.listdir(DOCS_DIR)):
        if not name.lower().endswith(".md"):
            continue
        try:
            meta, body = parse_doc(os.path.join(DOCS_DIR, name))
        except (OSError, UnicodeDecodeError):
            continue
        if meta.get("id") == doc_id:
            return meta, body
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "issue/0.1"

    def _send(self, code, ctype, payload):
        data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except OSError:
                self._json({"error": "index.html not found", "code": "E-6001"}, 500)
        elif path == "/api/docs":
            docs, skipped = scan_docs()
            self._json({"docs": docs, "skipped": skipped})
        elif path == "/api/doc":
            q = urllib.parse.parse_qs(parsed.query)
            doc_id = (q.get("id") or [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", doc_id):
                self._json({"error": "bad id", "code": "E-1001"}, 400)
                return
            meta, body = find_doc(doc_id)
            if meta is None:
                self._json({"error": "doc not found", "code": "E-1002"}, 404)
            else:
                self._json({"meta": meta, "content": body})
        elif path in ("/api/runs", "/api/run", "/api/run_status", "/api/source_text",
                      "/api/entities", "/api/relations", "/api/sources", "/api/stats",
                      "/api/vocabulary", "/api/master", "/api/job", "/api/engines", "/api/history"):
            # 저장소 조회 예외를 격리 — 손상 파일 하나가 커넥션을 끊지 않도록 JSON 500
            try:
                if path == "/api/run_status":
                    q = urllib.parse.parse_qs(parsed.query)
                    rid = (q.get("id") or [""])[0]
                    if not re.fullmatch(r"RUN-\d{6,}", rid):
                        self._json({"error": "bad id", "code": "E-1001"}, 400)
                    else:
                        job = store.get_job(rid)
                        self._json(job if job is not None else {"state": "unknown"},
                                   200 if job is not None else 404)
                elif path == "/api/runs":
                    self._json({"runs": store.list_runs()})
                elif path == "/api/run":
                    q = urllib.parse.parse_qs(parsed.query)
                    rid = (q.get("id") or [""])[0]
                    prop = store.get_proposal(rid) if re.fullmatch(r"RUN-\d{6,}", rid) else None
                    if prop is None:
                        self._json({"error": "run not found", "code": "E-1002"}, 404)
                    else:
                        out = {"run": prop}
                        comm = store.get_committed(rid)   # 복기: 확정 결정 스냅샷
                        if comm is not None:
                            out["committed"] = comm
                        self._json(out)
                elif path == "/api/source_text":
                    q = urllib.parse.parse_qs(parsed.query)
                    sid = (q.get("id") or [""])[0]
                    txt = store.read_source_text(sid) if re.fullmatch(r"SRC-\d{6,}", sid) else None
                    if txt is None:
                        self._json({"error": "source not found", "code": "E-1002"}, 404)
                    else:   # 수집 메타(수집 날짜·채널·프로파일)를 함께 — 원문 뷰가 수집 단위로 구분·표시
                        self._json({"source_id": sid, "text": txt, "meta": store.get_source(sid) or {}})
                elif path == "/api/entities":
                    q = urllib.parse.parse_qs(parsed.query)
                    st = (q.get("state") or [""])[0]
                    if st == "trashed":   # 휴지통 뷰 (BG-9)
                        ents = [e for e in store.list_entities(include_trashed=True)
                                if e.get("state") == "trashed"]
                    elif st == "merged":  # 병합됨 뷰
                        ents = [e for e in store.list_entities(include_merged=True)
                                if e.get("state") == "merged"]
                    else:
                        ents = store.list_entities()   # 기본 = active
                    self._json({"entities": ents})
                elif path == "/api/relations":
                    self._json({"relations": store.list_relations()})
                elif path == "/api/sources":
                    self._json({"sources": store.list_sources()})
                elif path == "/api/stats":
                    self._json({"stats": store.stats()})
                elif path == "/api/vocabulary":
                    self._json({"vocabulary": store.load_vocab()})
                elif path == "/api/job":   # 범용 비동기 작업 폴링(자연어 CRUD 등)
                    q = urllib.parse.parse_qs(parsed.query)
                    jid = (q.get("id") or [""])[0]
                    j = store.job_get(jid) if re.fullmatch(r"JOB-\d{4,}", jid) else None
                    self._json(j if j is not None else {"error": "job not found", "code": "E-1002"},
                               200 if j is not None else 404)
                elif path == "/api/master":   # 기준정보 — 일반 entity{kind}+relation 그래프
                    full = store.get_master(include_deleted=True)
                    self._json({"master": store.get_master(),
                                "deleted": [e for e in full["entities"] if e.get("state") == "deleted"]})
                elif path == "/api/engines":
                    self._json(llm.describe_engines(store.load_vocab()))
                elif path == "/api/history":
                    q = urllib.parse.parse_qs(parsed.query)
                    eid = (q.get("entity") or [""])[0]
                    if not re.fullmatch(r"(?:[A-Z]+-\d{4,}|CTX-[A-Z0-9-]+)", eid):
                        self._json({"error": "bad id", "code": "E-1001"}, 400)
                    else:
                        self._json({"history": store.entity_history(eid)})
            except Exception as e:  # noqa
                self._json({"error": type(e).__name__ + ": " + str(e), "code": "E-5001"}, 500)
        else:
            self._json({"error": "not found", "code": "E-1000"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/workflow/step":
            self._json({"error": "not found", "code": "E-1000"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > 5_000_000:
            self._json({"error": "bad body length", "code": "E-1001"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "invalid json", "code": "E-1001"}, 400)
            return
        if not isinstance(body, dict):
            self._json({"error": "body must be a JSON object", "code": "E-1001"}, 400)
            return
        try:
            self._json(pipeline.handle_step(body))
        except pipeline.StepError as e:
            self._json({"error": str(e), "code": e.code,
                        "step": body.get("step"), "status": "FAILED"}, e.http)
        except Exception as e:  # noqa — 어떤 단계 오류도 500 JSON으로 (커넥션 유지)
            self._json({"error": type(e).__name__ + ": " + str(e),
                        "code": "E-5000", "status": "FAILED"}, 500)

    def log_message(self, fmt, *args):
        pass  # 조용히. 필요 시 여기서 파일 로그로 전환


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8805)
    args = ap.parse_args()
    store.ensure_dirs()
    reaped = store.reap_running()   # BG-11: 재기동으로 죽은 워커의 running job을 error로 전이
    if reaped:
        print("reaped %d stuck 'running' job(s): %s" % (len(reaped), ", ".join(reaped)))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"issue server on http://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
