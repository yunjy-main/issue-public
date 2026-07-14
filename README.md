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

## 설계 노트

- **버전 좌표**: 이슈의 진짜 주어는 단일 이름이 아니라 "버전 조합"(제품 리비전 × 규칙셋 × 구성요소 × 과제)이다. context 엔티티가 이 좌표를 앵커한다.
- **이벤트 소싱**: 모든 변경은 append-only 이벤트로 남고, projection(현재 상태)은 재생 가능하다.
- 이 저장소의 데이터·프롬프트·샘플은 **정의상 예시(샘플)** 다.
