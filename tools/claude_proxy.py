# claude_proxy.py — Claude Code CLI(host 인증)를 OpenAI 호환 /v1/chat/completions API로 래핑.
# API 키 없이 실제 Claude를 쓰게 하고, 소비자(issue 등)는 사내 LLM과 동일한 mode:http로 붙는다.
# stdlib only. alice-agnt/backends/claude_cli.py와 같은 CLI 호출 방식.
import argparse
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# claude CLI 호출 직렬화: 동시에 여러 claude 서브프로세스를 띄우면 부하·세션 충돌로 실패한다.
_CLI_LOCK = threading.Lock()

HOME = os.path.expanduser("~")
DEFAULT_CMD = os.path.join(HOME, "AppData", "Roaming", "npm", "claude.cmd")
DEFAULT_NODE = "C:/Program Files/nodejs"
DISALLOWED = "Bash,Edit,Write,MultiEdit,NotebookEdit,WebSearch,WebFetch,Task"
CFG = {"cmd": DEFAULT_CMD, "node_dir": DEFAULT_NODE, "default_model": "haiku", "timeout": 300}


def _text(content):
    """OpenAI content(문자열 또는 [{type:text,text}]) → 문자열."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def _run_claude(model, system, user, schema):
    if not os.path.exists(CFG["cmd"]):
        raise RuntimeError("claude CLI 없음: %s" % CFG["cmd"])
    env = os.environ.copy()
    env["PATH"] = str(CFG["node_dir"]) + os.pathsep + env.get("PATH", "")
    argv = [CFG["cmd"], "-p", "--output-format", "json", "--model", model or CFG["default_model"],
            "--no-session-persistence", "--dangerously-skip-permissions",
            "--disallowed-tools", DISALLOWED]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema)]
    # system은 --system-prompt로 넘기지 않고 stdin에 합친다:
    # --system-prompt는 CLI의 StructuredOutput 지시를 대체해 --json-schema를 무력화한다.
    prompt = (system + "\n\n" + user) if system else user
    # 동시 호출 직렬화(_CLI_LOCK) + 1회 재시도(부하 시 일시 실패 흡수)
    last = "unknown"
    with _CLI_LOCK:
        for attempt in range(2):
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=CFG["timeout"], env=env)
            if proc.returncode != 0:
                last = "exit %s: %s" % (proc.returncode, (proc.stderr or "")[:200])
                continue
            try:
                env_json = json.loads((proc.stdout or "").strip())
            except ValueError:
                last = "stdout parse fail"
                continue
            if env_json.get("is_error"):
                last = "claude error: %s" % str(env_json.get("result"))[:200]
                continue
            so = env_json.get("structured_output")
            content = (json.dumps(so, ensure_ascii=False) if isinstance(so, dict)
                       else str(env_json.get("result", "")))
            return content, env_json.get("model") or model, env_json.get("usage") or {}
    raise RuntimeError(last)


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-proxy/0.1"

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "anthropic-cli"}
                for m in ("haiku", "sonnet", "opus")]})
        else:
            self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": {"message": "invalid json", "type": "invalid_request_error"}})
            return
        msgs = body.get("messages") or []
        system = "\n\n".join(_text(m.get("content")) for m in msgs if m.get("role") == "system")
        user = "\n\n".join(_text(m.get("content")) for m in msgs if m.get("role") != "system")
        # OpenAI response_format → CLI --json-schema (있으면 structured_output 사용)
        schema = None
        rf = body.get("response_format") or {}
        if rf.get("type") == "json_schema":
            schema = (rf.get("json_schema") or {}).get("schema")
        try:
            content, model, usage = _run_claude(body.get("model"), system, user, schema)
        except subprocess.TimeoutExpired:
            self._send(504, {"error": {"message": "claude timeout", "type": "timeout_error"}})
            return
        except Exception as e:  # noqa
            self._send(502, {"error": {"message": str(e)[:300], "type": "api_error"}})
            return
        self._send(200, {
            "id": "chatcmpl-%d" % int(time.time() * 1000),
            "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                      "completion_tokens": usage.get("output_tokens", 0),
                      "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
        })

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="Claude CLI를 OpenAI 호환 API로 래핑")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8810)  # llm.json url과 정합(8806=issue-codex 회피)
    ap.add_argument("--command", default=DEFAULT_CMD)
    ap.add_argument("--node-dir", default=DEFAULT_NODE)
    ap.add_argument("--default-model", default="haiku")
    args = ap.parse_args()
    CFG.update({"cmd": args.command, "node_dir": args.node_dir, "default_model": args.default_model})
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("claude-proxy (OpenAI-compatible) on http://%s:%d/v1/chat/completions" % (args.host, args.port))
    print("  wraps:", CFG["cmd"], "| default model:", CFG["default_model"])
    httpd.serve_forever()


if __name__ == "__main__":
    main()
