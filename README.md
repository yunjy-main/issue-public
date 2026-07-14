# issue — 이슈·지식 온톨로지 시스템

난잡한 채널(메일·메신저·회의록 등)의 원문을 단일 텍스트로 받아 → LLM 후보 추출 → 사람 검토 →
버전 좌표 지식 그래프 → 현황·통계 뷰로 만드는 **개인 운영 이슈·지식 관리 시스템**.

핵심 루프: **capture-first(무조건 저장) → EXTRACT(LLM 후보) → 사람 검토 → COMMIT(지식 그래프)**.

## 특징

- **의존성 없음** — Python 표준 라이브러리만 (오프라인·폐쇄망 배포 가능)
- **파일 기반 저장소** — JSONL append-only 이벤트 + JSON/YAML projection (DB 불필요)
- **LLM 교체 가능** — stub(오프라인 결정론) / 로컬 OSS / OpenAI 호환 / Anthropic API
- **자기완결 단일 HTML UI** — 빌드·번들러·CDN 없음
- **검색증강 추출(RAE)** — 새 원문을 보내기 전 관련 기존 지식을 결정론으로 검색해 첨부 → 중복 대신 연결/병합 제안
- **소프트 삭제·복구** — 하드삭제 없음(휴지통·복원), 이벤트 소싱으로 이력 보존

## 실행

```powershell
python server.py            # http://127.0.0.1:8805
```

`knowledge/config/llm.json`이 없으면 오프라인 결정론 `stub`으로 동작합니다.

## 구조

```
web/index.html      UI (자기완결 단일 HTML, 모바일·고밀도)
server.py           stdlib 서버: 정적 + POST /api/workflow/step + 읽기 엔드포인트
store.py            파일 기반 저장소 (JSONL 이벤트 + JSON/YAML projection)
pipeline.py         단계 디스패처 (CAPTURE→EXTRACT→REVIEW→COMMIT)
llm.py              추출기 어댑터 (stub / http / anthropic / cli)
retrieve.py         검색증강 추출(RAE) — 관련 기존 지식 결정론 검색
reconcile.py        재조정(순서 독립 재연결) · dedup.py 중복 병합
prompts/            LLM 프롬프트 (데이터 파일)
knowledge/config/   기준정보 config (vocabulary 시드; 런타임 데이터는 .gitignore)
scripts/selftest.py 임시 저장소 + 합성 입력으로 전 파이프라인 검증
```

## 셀프테스트

```powershell
python scripts\selftest.py
```

## EXTRACT LLM 전환 (`knowledge/config/llm.json`)

파일이 없으면 `stub`(오프라인 결정론). `llm.json.example`을 복사해 `mode`를 바꾼다.

- **OpenAI 호환 / 로컬·조직 내 LLM** (`mode:http`): `{"mode":"http","url":"http://<endpoint>/v1/chat/completions","model":"...","response_schema":true}`.
  로컬 OSS(`ollama run ...`, LM Studio)나 조직 내 LLM에 같은 경로로 붙는다.
- **Anthropic API** (`mode:anthropic`): `pip install anthropic` + `ANTHROPIC_API_KEY`. 지연 import.
- **Claude CLI** (`mode:cli`, 또는 `tools/claude_proxy.py`로 OpenAI 호환 래핑 후 `mode:http`): host 인증, API 키 불필요.

세 모드 모두 같은 프롬프트 + **관대 파서**(코드펜스·JSON배열·`{entities,relations}`·JSONL, 한 줄 실패 무시)를 쓴다.

### 요청(header·body)은 이렇게 만들어진다 (`mode:http`)

`_http_extract`(llm.py)가 매 추출마다 OpenAI 호환 `POST /chat/completions` 요청을 조립한다:

- **헤더** = `{"Content-Type": "application/json"}` 에 `llm.json`의 `headers`를 그대로 병합. 사내 인증은 여기 넣는다(Bearer 토큰이든 `X-Api-Key` 같은 커스텀 헤더든). urllib가 `Host`·`User-Agent`·`Content-Length`·`Connection`을 자동으로 붙인다.
- **본문(JSON)** =
  - `model` ← `llm.json`의 `model`
  - `messages` = `[{role:"system", …}, {role:"user", …}]`
    - **system** = `prompts/extract.md`(추출 규칙) + 용어집(vocabulary) + 기준시각(캡처시각) + (있으면) RAE '관련 기존 지식'. 약 3,300자.
    - **user** = `"다음 원문에서 …생성하라"` 지시문 + 실제 원문.
  - `response_format` = **`response_schema:true`일 때만** 추가. `{type:"json_schema", json_schema:{name:"candidates", schema:<전체 추출 스키마>}}` — 구조화 출력을 강제한다.
  - `extra_payload`의 키(예: `temperature:0`)가 최상위에 병합.

### 실제 전송 예시

원문 `"N7 GPIO-A 고객 ORT에서 HBM 2kV fail 2건 발생. 어제 접수됨. 원인 파악 필요."` 를 추출할 때 서버가 실제로 보내는 요청(위 로깅으로 캡처):

```
POST http://<endpoint>/v1/chat/completions
Content-Type: application/json
Authorization: Bearer <사내 토큰>          ← llm.json의 headers 그대로
Content-Length: 12846                       ← response_schema:true면 이만큼 (아래 참고)
```
```json
{
  "model": "internal-model-id",
  "messages": [
    { "role": "system", "content": "당신은 업무 지식 추출기다. 입력 원문에서 source_profile 1개와 Entity·Relation 후보를 JSONL로 출력한다.\n\n규칙 (M3 §2):\n1. source_profile, Entity, Relation 후보만 출력한다. …\n(prompts/extract.md 규칙 + 용어집 + 기준시각 + 관련 기존 지식, 약 3,300자)" },
    { "role": "user", "content": "다음 원문에서 위 규칙에 따라 source_profile 1개와 Entity·Relation 후보를 생성하라. …\n\n원문:\nN7 GPIO-A 고객 ORT에서 HBM 2kV fail 2건 발생. 어제 접수됨. 원인 파악 필요." }
  ],
  "response_format": {                                    // ← response_schema:true 일 때만! (스키마가 ~4KB 차지)
    "type": "json_schema",
    "json_schema": { "name": "candidates", "schema": { "required": ["entities", "relations"], "properties": { "…": "…" } } }
  },
  "temperature": 0                                         // ← extra_payload
}
```

- **본문 크기**: `response_schema:true` → 약 **12.8KB**, `false` → 약 **8.9KB** (차이 ~4KB가 스키마).
- 사내 LLM에 **직접 curl 하던 요청과 이 본문을 나란히 비교**하라. `response_format`이 직접 호출엔 없고 여기만 있으면 → 그게 timeout의 유력 원인(크기보다 **구조화 출력 강제 모드**를 사내 LLM이 미지원/지연하는 문제).

## LLM 연동 디버깅 · 로그 사용법

### 켜기

```powershell
$env:ISSUE_LLM_DEBUG=1; python server.py     # PowerShell
```
```bash
ISSUE_LLM_DEBUG=1 python server.py           # bash
```

| 설정 | 출력(stderr) |
|---|---|
| (없음, 기본) | 요약만 — 요청 1줄 + 응답 1줄 + **실패 상세**는 항상 |
| `ISSUE_LLM_DEBUG=1` | 위 + 요청/응답 **본문 전문** + 헤더(토큰 마스킹) |
| `ISSUE_LLM_DEBUG=secrets` | 위 + 헤더 **토큰 원문**(신뢰된 로컬 디버깅에서만) |
| `llm.json` `"debug": true` | env 없이 본문 전문 |

모든 로그는 `[LLM]` 접두로 **stderr**에 나온다. 마커는 ASCII(`->` 요청 `<-` 응답 `[X]` 실패 `[!]` 경고)라 Windows(cp949) 콘솔에서도 깨져 사라지지 않는다.

### 로그 읽는 법

성공:
```
[LLM] extract: mode=http model=internal-model-id url=http://... timeout=60s response_schema=True retrieved=3 src=SRC-000042
[LLM] -> EXTRACT POST http://.../v1/chat/completions | body 12846 bytes | response_format=YES | timeout=60s
[LLM]   [!] EXTRACT 요청에 response_format(json_schema)가 포함됨 — 사내 LLM이 미지원/지연하면 …
[LLM] <- EXTRACT HTTP 200 | 2.31s | 1840 bytes
[LLM] extract: http 성공 2.34s (엔티티 3·관계 2)
```

실패(타임아웃):
```
[LLM] -> EXTRACT POST http://... | body 12846 bytes | response_format=YES | timeout=60s
[LLM] [X] EXTRACT 실패 | 60.02s | http://... | timeout: The read operation timed out
[LLM]   timeout=60s. 직접 호출은 빠른데 여기서만 느리면 위 요청 body의 response_format을 의심 …
[LLM] extract: http 실패(stub 폴백 없음) 60.05s E-2001: LLM 호출 실패(timeout, 60.0s, http://...): …
```

각 줄:
- `-> EXTRACT POST` : 실제 전송 **URL·본문 바이트·response_format 포함 여부·timeout**. 전송 *직전* 로그라 타임아웃돼도 남는다. `ISSUE_LLM_DEBUG=1`이면 바로 뒤에 헤더(토큰 마스킹)와 `body(전문)`이 이어진다.
- `<- EXTRACT HTTP` : 응답 **status·경과시간·바이트**. `=1`이면 `resp(전문)`.
- `[X] EXTRACT 실패` : **실제 예외**(연결거부/타임아웃/TLS/인증)·경과·URL. (예전엔 예외 type명만 남아 원인 불명이었다.)
- 경과가 timeout값과 **같으면** = 응답이 안 온 것(response_format 또는 네트워크). **즉시** 실패면 = URL·인증·연결 문제.

`detect_operations`(자연어 삭제/숨김 명령 감지)도 `DETECT_OPS(시도 N)` 태그로 같은 형식으로 로깅된다.

### timeout 진단 순서 (직접 호출은 빠른데 서버 경유만 느릴 때)

1. `ISSUE_LLM_DEBUG=1`로 서버를 띄우고 추출 1회 실행.
2. `-> EXTRACT POST <url>` 의 URL이 사내 endpoint와 **정확히** 같은지(경로·슬래시 포함) 확인.
3. `body(전문)`을 직접 호출하던 요청과 비교 — `response_format`이 붙어 있으면 → `llm.json`에서 **`"response_schema": false`** (관대 파서가 자유형식 JSON도 처리).
4. `[X] EXTRACT 실패` 줄의 실제 예외로 원인 확정(경과=timeout이면 응답 무, 즉시면 URL/인증).
5. 헤더 인증 확인은 `ISSUE_LLM_DEBUG=secrets` (토큰 노출 — 로컬에서만).

(같은 내용을 `llm.json.example`의 `_troubleshoot_timeout`에도 넣어 뒀다.)

### 실패는 실패로 (stub 폴백 OFF)

LLM 실패는 **stub 결과로 조용히 덮이지 않고** 그대로 에러(E-xxxx)로 UI·로그에 뜬다 — 실패인데 성공처럼 보이는 것을 막는다. 예전 폴백을 원하면 `llm.json`에 `"fallback": "stub"` 명시(opt-in). `mode:"stub"`은 오프라인 결정론 추출로 항상 사용 가능(무설정 기본).

## 설계 노트

- **버전 좌표**: 이슈의 진짜 주어는 단일 이름이 아니라 "버전 조합"(제품 리비전 × 규칙셋 × 구성요소 × 과제)이다. context 엔티티가 이 좌표를 앵커한다.
- **이벤트 소싱**: 모든 변경은 append-only 이벤트로 남고, projection(현재 상태)은 재생 가능하다.
- 이 저장소의 데이터·프롬프트·샘플은 **정의상 예시(샘플)** 다.
