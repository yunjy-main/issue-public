# links.py — 원문/입력에서 URL을 뽑아 분류하고 Jira 이슈키를 추출한다. 결정론, LLM 없음.
# 엔티티의 urls 속성 = [{url, kind, issue_key?}]. Jira·Confluence·EDM 등 외부 시스템 연계용.
import re

_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.I)
# Jira 이슈키: 대문자 프로젝트키(2~10) + '-' + 숫자.  예: ABC-123, GPIO-4567
_KEY = r"[A-Z][A-Z0-9]{1,9}-\d+"
_JIRA_BROWSE = re.compile(r"/browse/(" + _KEY + r")", re.I)
_JIRA_QUERY = re.compile(r"[?&](?:selectedIssue|issueKey|issue|jql[^=]*)=(" + _KEY + r")", re.I)
_JIRA_PATH = re.compile(r"/(" + _KEY + r")(?:[/?#]|$)")   # cloud: /jira/.../ABC-123
_TRAIL = ".,;:)]}>\"'"   # 문장 끝 문장부호가 URL에 붙는 경우 제거


def classify(url):
    u = (url or "").lower()
    if "atlassian.net" in u or "jira" in u or "/browse/" in u or "issuekey=" in u or "selectedissue=" in u:
        return "jira"
    if "confluence" in u or "/wiki/" in u or "/display/" in u or "/pages/" in u or "/spaces/" in u:
        return "confluence"
    if "edm" in u:
        return "edm"
    return "other"


def jira_key(url):
    """Jira URL에서 이슈키 추출(대문자 강제). 못 찾으면 None."""
    for pat in (_JIRA_BROWSE, _JIRA_QUERY, _JIRA_PATH):
        m = pat.search(url or "")
        if m:
            return m.group(1).upper()
    return None


def _clean(url):
    url = url.strip()
    while url and url[-1] in _TRAIL:
        url = url[:-1]
    return url


def extract_links(text):
    """text에서 http(s) URL을 뽑아 [{url, kind, issue_key?}] 로. 중복 URL은 1회. Jira면 이슈키 부착."""
    out, seen = [], set()
    for m in _URL.finditer(text or ""):
        url = _clean(m.group(0))
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        kind = classify(url)
        item = {"url": url, "kind": kind}
        if kind == "jira":
            k = jira_key(url)
            if k:
                item["issue_key"] = k
        out.append(item)
    return out


def normalize(value):
    """폼/스텝 입력을 urls 리스트로 정규화 — 문자열(여러 줄/공백 구분) 또는 [str] 또는 이미 구조화된
    [{url,...}] 모두 받아 [{url,kind,issue_key?}]로. 결정론."""
    if not value:
        return []
    if isinstance(value, str):
        return extract_links(value)
    text_parts = []
    for v in value:
        if isinstance(v, dict) and v.get("url"):
            text_parts.append(str(v["url"]))
        elif isinstance(v, str):
            text_parts.append(v)
    return extract_links("\n".join(text_parts))
