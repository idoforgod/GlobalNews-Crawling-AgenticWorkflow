# AgenticWorkflow

Claude Code 기반의 에이전트 워크플로우 자동화 프로젝트.

## 최종 목표

이 코드베이스의 최종 목표는 두 단계로 구성된다:

1. **워크플로우 설계**: 복잡한 작업을 Research → Planning → Implementation 3단계 구조의 `workflow.md`로 설계한다. Sub-agents/Agent Teams/Hooks/Skills/MCP Servers를 조합한 구현 설계를 포함한다.
2. **워크플로우 실행**: 생성된 `workflow.md`를 기준 삼아, 그 안에 정의된 에이전트·스크립트·자동화 구성을 **실제로 구현**한다. 워크플로우 문서는 설계도이고, 최종 산출물은 그 설계도대로 작동하는 실제 시스템이다.

> 워크플로우를 만드는 것은 중간 산출물이다. **워크플로우에 기술된 내용이 실제로 동작하는 것**이 최종 목표다.

### 존재 이유 — DNA 유전

AgenticWorkflow는 그 자체로 완결되는 시스템이 아니다. **또 다른 agentic workflow automation system을 낳는 부모 유기체**다.
`workflow-generator` 스킬이 생산 라인이며, 이 라인에서 태어나는 모든 자식 시스템은 부모의 **전체 게놈**을 구조적으로 내장한다:

- **헌법**: 절대 기준 3개 (품질 > SOT, CCP)
- **구조**: Research → Planning → Implementation 3단계 제약
- **검증**: 4계층 품질 보장 (L0 → L1 → L1.5 → L2) + P1 할루시네이션 봉쇄
- **안전**: Safety Hook + 결정론적 검증
- **기억**: Context Preservation + Knowledge Archive + RLM 패턴
- **비판**: Adversarial Review (Generator-Critic 패턴)
- **투명**: Decision Log + 감사 추적

유전은 선택이 아니라 **구조**다. 자식은 부모의 DNA를 "참고"하는 것이 아니라 **내장**한다. 상세: `soul.md §0`.

## 절대 기준

> 이 프로젝트의 모든 설계·구현·수정 의사결정에 적용되는 최상위 규칙이다.
> 아래의 모든 원칙, 가이드라인, 관례보다 상위에 있다.

### 절대 기준 1: 최종 결과물의 품질

> **속도, 토큰 비용, 작업량, 분량 제한은 완전히 무시한다.**
> 모든 의사결정의 유일한 기준은 **최종 결과물의 품질**이다.
> 단계를 줄여서 빠르게 만드는 것보다, 단계를 늘려서라도 품질을 높이는 방향을 선택한다.

### 절대 기준 2: 단일 파일 SOT + 계층적 메모리 구조

> **단일 파일 SOT(Single Source of Truth) + 계층적 메모리 구조 설계 아래서, 수십 개의 에이전트가 동시에 작동해도 데이터 불일치가 발생하지 않는다.**

설계 함의:
- **상태 관리**: 모든 공유 상태는 단일 파일에 집중. 분산 금지.
- **쓰기 권한**: SOT 파일 쓰기는 Orchestrator/Team Lead만. 나머지는 읽기 전용 + 산출물 파일 생성.
- **충돌 방지**: 병렬 에이전트가 동일 파일을 동시 수정하는 구조 금지.

### 절대 기준 3: 코드 변경 프로토콜 (Code Change Protocol)

> **코드를 작성·수정·추가·삭제하기 전에, 반드시 아래 3단계를 내부적으로 수행한다.**
> 이 프로토콜을 건너뛰는 것은 절대 기준 위반이다.
> 프로토콜은 항상 수행하되, 분석 깊이는 변경의 영향 범위에 비례한다.

**Step 1 — 의도 파악**:
- 변경 목적(버그 수정/기능 추가/리팩토링/성능)과 제약(호환성, 기술 스택)을 1-2문장으로 정의
- 경미한 변경(오타, 주석, 포맷팅)이면 "파급 효과 없음" 확인 후 즉시 실행 가능

**Step 2 — 영향 범위 분석 (Ripple Effect Analysis)**:
- 직접 의존 + 호출 관계 (caller/callee)
- 구조적 관계 (상속, 합성, 참조)
- 데이터 모델/스키마/타입 연쇄 변경
- 테스트, 설정, 문서, API 스펙
- 강결합·샷건 서저리 위험이 있으면 **반드시** 사전 고지 후 사용자와 협의

**Step 3 — 변경 설계 (Change Plan)**:
- 단계별 변경 순서 (어떤 파일/함수부터 → 의존성 전파 → 테스트/문서 정합)
- 결합도 감소 / 응집도 증가 기회가 보이면 함께 제안 (실행은 사용자 승인 후)

**비례성 규칙:**

| 변경 규모 | 적용 깊이 |
|----------|---------|
| 경미 (오타, 주석) | Step 1만 — 파급 효과 없음 확인 |
| 표준 (함수/로직 변경) | 전체 3단계 |
| 대규모 (아키텍처, API) | 전체 3단계 + 사전 사용자 승인 필수 |

**커뮤니케이션 규칙:**
- 불필요하게 장황한 이론 설명은 피하고, 실질적인 코드와 구체적 단계 위주로 설명한다.
- 중요한 설계 선택에는 간단한 이유를 덧붙인다.
- 모호한 부분이 있어도 작업을 회피하지 말고, "합리적인 가정"을 명시한 뒤 최선의 설계를 제안한다.

**코딩 기준점 (CAP):** CCP의 모든 단계는 아래 4가지 태도를 내면화한 상태에서 수행한다:
- **CAP-1**: 코딩 전 사고 — 코드를 읽기 전에 수정 금지. 트레이드오프 표면화. 불명확하면 질문
- **CAP-2**: 단순성 우선 — 최소 코드. 추측성 기능·조기 추상화·불필요 헬퍼 금지
- **CAP-3**: 목표 기반 실행 — 성공 기준 먼저 정의, 구현 후 검증
- **CAP-4**: 외과적 변경 — 요청받은 변경만. 관련 없는 "개선" 금지

> CAP는 CCP 하위이므로, 절대 기준 1(품질)과 충돌 시 품질이 이긴다. 상세: AGENTS.md §2 절대 기준 3.

### 절대 기준 간 우선순위

> **절대 기준 1(품질)이 최상위이다. 절대 기준 2(SOT)와 절대 기준 3(CCP)은 품질을 보장하기 위한 동위 수단이다.**
> 어느 기준이든 절대 기준 1과 충돌하면 품질이 이긴다. SOT와 CCP 모두 품질을 제약하는 **목적**이 아니라, 품질을 보장하기 위한 **수단**이다.

---

## 프로젝트 구조

> **부모-자식 문서 분리**: `AGENTICWORKFLOW-*.md`는 부모 프레임워크(방법론), `GLOBALNEWS-*.md`는 자식 시스템(도메인 고유 아키텍처)을 기술한다.

```
GlobalNews-Crawling-AgenticWorkflow/
├── CLAUDE.md / AGENTS.md / GEMINI.md      ← AI 에이전트 지시서 (Hub-Spoke)
├── GLOBALNEWS-*.md                        ← [자식] 시스템 문서 (README, ARCHITECTURE, USER-MANUAL)
├── AGENTICWORKFLOW-*.md                   ← [부모] 프레임워크 문서
├── soul.md / DECISION-LOG.md / COPYRIGHT.md
├── .claude/
│   ├── settings.json                      ← Hook 설정
│   ├── agents/                            ← Sub-agent 정의 (코어 3 + 도메인 33)
│   ├── commands/                           ← Slash Commands (7개: install, maintenance, start, review-*, run)
│   ├── hooks/scripts/                     ← Hook 스크립트 (23개: context_guard, _context_lib(~7,500줄), save/restore/update, generate, validate_*, setup_*, block_*, predictive_debug_guard)
│   ├── context-snapshots/                 ← 런타임 스냅샷 (latest.md, knowledge-index.jsonl, risk-scores.json)
│   └── skills/                            ← 스킬 (workflow-generator, skill-creator, subagent-creator, insight-report, doctoral-writing)
├── scripts/                               ← 오케스트레이션 스크립트 (sot_manager, workflow_starter, run_quality_gates, validate_*, extract_*, 전처리)
├── src/                                   ← [자식] 핵심 소스 (~55,900 LOC)
│   ├── crawling/                          (Workflow A: 116개 어댑터 + 안티블록 + DynamicBypassEngine)
│   ├── analysis/                          (Workflow A: 8단계 NLP 파이프라인)
│   ├── insights/                          (Workflow B: 7개 모듈 M1-M7 + M7 인텔리전스 확장, 27+4 지표, ~7,800 LOC)
│   ├── storage/                           (Parquet ZSTD + SQLite FTS5/vec)
│   └── utils/                             (로깅, 설정, 에러 처리)
├── tests/                                 ← 3계층 테스트 (unit, integration, structural)
├── config/                                ← 설정 (sources.yaml, pipeline.yaml, output-structure.yaml, review-focus.yaml)
├── data/config/sources.yaml               ← [자식] 런타임 SOT — Workflow A (116개 사이트 설정)
├── data/config/insights.yaml              ← [자식] 런타임 설정 — Workflow B (7개 모듈)
├── main.py / dashboard.py                 ← [자식] CLI + Streamlit 대시보드
├── .venv/                                 ← Python 3.13 venv (도메인 NLP 전용)
├── prompt/workflow.md                     ← 워크플로우 정의
└── translations/glossary.yaml             ← 번역 용어 사전
```

## Context Preservation System

컨텍스트 토큰 초과·`/clear`·압축 시 작업 내역 상실을 방지하는 자동 저장·복원 시스템이다.

### 동작 원리

| Hook 이벤트 | 스크립트 | 동작 |
|------------|---------|------|
| **Setup** (`--init`) | `setup_init.py` | 세션 시작 전 인프라 건강 검증 (Python 버전, 스크립트 구문(26개 — Phase 0.1/0.2 확장), 디렉터리, PyYAML, SOT 쓰기 패턴 검증, 런타임 디렉터리 자동 생성(6개), 도메인 venv P1 검증(ENV1a-ENV1d)) |
| **Setup** (`--maintenance`) | `setup_maintenance.py` | 주기적 건강 검진 (stale archives, knowledge-index 무결성, work_log 크기, doc-code 동기화 검증(DC-1 NEVER DO 재시도 한도, DC-2 D-7 Risk 상수, DC-3 D-7 ULW 패턴, DC-4 D-7 재시도 한도 상수, DC-5 D-7 ENABLED_DEFAULT 동기화)) |
| **PreToolUse** (Bash) | `block_destructive_commands.py` | 위험 명령·시크릿 소스 명령·파괴적 SQL 실행 전 차단 (git push --force, cat .env, DROP TABLE 등). exit code 2로 차단 + stderr 피드백으로 Claude 자기 수정 |
| **PreToolUse** (Edit\|Write) | `block_test_file_edit.py` | TDD 모드(`.tdd-guard` 존재) 시 테스트 파일 수정 차단. Tier 1(디렉터리) + Tier 2(파일명) 2계층 탐지. exit code 2 + stderr 피드백으로 구현 코드 수정 유도 |
| **PreToolUse** (Edit\|Write) | `predictive_debug_guard.py` | 에러 이력 기반 위험 파일 경고. `risk-scores.json` 캐시 조회 → 임계값 초과 시 stderr 경고. exit code 0 (경고 전용) |
| **SessionEnd** (`/clear`) | `save_context.py` | 전체 스냅샷 저장 + Knowledge Archive 아카이빙 |
| **PreCompact** | `save_context.py` | 컨텍스트 압축 전 스냅샷 저장 + Knowledge Archive 아카이빙 |
| **SessionStart** | `restore_context.py` | RLM 패턴: 포인터 + 요약 + 과거 세션 인덱스 포인터 출력 + Error→Resolution 자동 표면화 + Predictive Debugging 캐시 생성 |
| **PostToolUse** | `update_work_log.py` | 9개 도구(Edit, Write, Bash, Task, NotebookEdit, TeamCreate, SendMessage, TaskCreate, TaskUpdate) 작업 로그 누적. 토큰 75% 초과 시 proactive 저장 |
| **PostToolUse** (Bash) | `block_secret_leak.py` | Bash 실행 결과에서 시크릿 패턴 감지 시 stderr 경고. exit code 0 (경고 전용, best-effort) |
| **Stop** | `generate_context_summary.py` | 매 응답 후 증분 스냅샷 + Knowledge Archive 아카이빙 (30초 throttling, 5KB growth threshold) + Autopilot Decision Log 안전망 + ULW 상태 IMMORTAL 보존 + ULW Compliance 안전망 + Traceability 누락 감지 + DKS 누락 감지 + Diagnosis 누락 감지 |

### Claude의 활용 방법

- 세션 시작 시 `[CONTEXT RECOVERY]` 메시지가 표시되면, 안내된 경로의 파일을 **반드시 Read tool로 읽어** 이전 맥락을 복원한다.
- 스냅샷은 `.claude/context-snapshots/latest.md`에 저장된다.
- **Knowledge Archive**: `knowledge-index.jsonl`은 세션 간 축적되는 구조화된 인덱스이다. Stop hook과 SessionEnd/PreCompact 모두에서 기록된다. 각 엔트리에는 completion_summary(도구 성공/실패), git_summary(변경 상태), session_duration_entries(세션 길이), phase(세션 전체 단계), phase_flow(다단계 전환 흐름, 예: `research → implementation`), primary_language(주요 파일 확장자), error_patterns(Error Taxonomy 12패턴 분류 + resolution 매칭), success_patterns(Edit/Write→Bash 성공 시퀀스 — cross-session 성공 패턴 학습), tool_sequence(RLE 압축 도구 시퀀스), final_status(success/incomplete/error/unknown), tags(검색 태그 — 경로 CamelCase/snake_case + 확장자 + 에러 타입 + design 마커)가 포함된다. Grep tool로 프로그래밍적 탐색이 가능하다 (RLM 패턴).
- **Resume Protocol**: 스냅샷에 포함된 "복원 지시" 섹션은 수정/참조 파일 목록과 세션 정보를 결정론적으로 제공한다. `[CONTEXT RECOVERY]` 출력에는 완료 상태(도구 성공/실패)와 Git 변경 상태도 표시된다. **동적 RLM 쿼리 힌트**: 수정 파일 경로에서 추출한 태그(`extract_path_tags()`)와 에러 정보를 기반으로 세션별 맞춤 Grep 쿼리 예시를 자동 생성한다.
- Hook 스크립트는 SOT(`state.yaml`)를 **읽기 전용**으로만 접근한다 (절대 기준 2 준수). SOT 파일 경로는 `sot_paths()` 헬퍼로 중앙 관리되며, `SOT_FILENAMES` 상수(`state.yaml`, `state.yml`, `state.json`)에서 파생된다.
- **절삭 상수 중앙화**: `_context_lib.py`에 10개 절삭 상수(`EDIT_PREVIEW_CHARS=1000`, `ERROR_RESULT_CHARS=3000`, `MIN_OUTPUT_SIZE=100` 등)를 중앙 정의. Edit preview는 5줄×1000자로 편집 의도·맥락을 보존하고, 에러 메시지는 3000자로 stack trace 전체를 보존한다.
- **다단계 전환 감지**: `detect_phase_transitions()` 함수가 sliding window(20개 도구, 50% 오버랩)로 세션 내 단계 전환(research → planning → implementation 등)을 결정론적으로 감지한다. Knowledge Archive의 `phase_flow` 필드에 기록된다.
- **결정 품질 태그 정렬**: 스냅샷의 "주요 설계 결정" 섹션(IMMORTAL 우선순위)은 품질 태그 기반으로 정렬된다 — `[explicit]` > `[decision]` > `[rationale]` > `[intent]` 순으로 15개 슬롯을 채워, 일상적 의도 선언(`하겠습니다` 패턴)이 실제 설계 결정을 밀어내지 않는다. 비교·트레이드오프·선택 패턴도 추출한다.
- **IMMORTAL-aware 압축**: 스냅샷 크기 초과 시 Phase 7 hard truncate에서 IMMORTAL 섹션을 우선 보존한다. 비-IMMORTAL 콘텐츠를 먼저 절삭하고, IMMORTAL 자체가 한계를 초과하는 극단적 경우에도 IMMORTAL 텍스트의 시작 부분을 보존한다. **압축 감사 추적**: 각 압축 Phase가 제거한 문자 수를 HTML 주석(`<!-- compression-audit: ... -->`)으로 스냅샷 끝에 기록한다 (Phase 1~7 단계별 delta + 최종 크기). 렌더링에 영향 없이 Grep으로 디버깅 가능.
- **Error Taxonomy**: 도구 에러를 12개 패턴(file_not_found, permission, syntax, timeout, dependency, edit_mismatch, type_error, value_error, connection, memory, git_error, command_not_found)으로 분류한다. Knowledge Archive의 error_patterns 필드에 기록되어, "unknown" 분류를 ~30%로 감소시킨다. False positive 방지를 위해 negative lookahead, 한정어 매칭 등을 적용한다. **Error→Resolution 매칭**: 에러 발생 후 5 entries 이내의 성공적 도구 호출을 file-aware 매칭으로 탐지하여 `resolution` 필드에 기록한다 (도구명 + 파일명). 매칭 실패 시 `null`. `Grep "resolution" knowledge-index.jsonl`로 해결 패턴을 cross-session 탐색 가능.
- **P1 할루시네이션 봉쇄 (Hallucination Prevention)**: 반복적으로 100% 정확해야 하는 작업을 Python 코드로 강제한다. (1) **KI 스키마 검증**: `_validate_session_facts()`가 knowledge-index 쓰기 직전 RLM 필수 키(session_id, tags, final_status, diagnosis_patterns 등 11개) 존재를 보장 — 누락 시 안전 기본값 채움. (2) **부분 실패 격리**: `archive_and_index_session()`에서 archive 파일 쓰기 실패가 knowledge-index 갱신을 차단하지 않음 — RLM 핵심 자산 보호. (3) **SOT 쓰기 패턴 검증**: `setup_init.py`의 `_check_sot_write_safety()`가 Hook 스크립트에서 SOT 파일명 + 쓰기 패턴 공존을 AST 함수 경계 기반으로 탐지 (Tier 1: 비-SOT 스크립트의 SOT 참조 차단, Tier 2: SOT-aware 스크립트의 함수별 쓰기 패턴 검사). (4) **SOT 스키마 검증**: `validate_sot_schema()`가 워크플로우 state.yaml의 구조적 무결성을 10항목(S1-S6: current_step 타입·범위, outputs 타입·키 형식, 미래 단계 산출물 탐지, workflow_status 유효값, auto_approved_steps 정합성 + S5b: workflow_status "completed" 교차검증(current_step ≥ total_steps) + S7: pacs 5개 필드 검증(S7a dimensions F/C/L 0-100, S7b current_step_score 0-100, S7c weak_dimension F/C/L, S7d history dict→{score, weak}, S7e pre_mortem_flag string) + S8: active_team 5개 필드 검증(S8a name string, S8b status partial|all_completed, S8c tasks_completed list, S8d tasks_pending list, S8e completed_summaries dict→dict) + S9: auto_approved_details 구조 검증(존재 시 dict, 각 값에 timestamp·decision_log 필수))으로 검증 — SessionStart와 Stop hook 양쪽에서 실행. (5) **Adversarial Review P1 검증**: `validate_review_output()`이 리뷰 보고서의 구조적 무결성(파일 존재 R1, 최소 크기 R2, 필수 섹션 4개 R3, PASS/FAIL 명시적 추출 R4, 이슈 테이블 ≥ 1행 R5)을 결정론적으로 검증. `parse_review_verdict()`가 regex로 이슈 심각도(Critical/Warning/Suggestion) 카운트 추출. `calculate_pacs_delta()`가 Generator-Reviewer pACS 차이(Delta ≥ 15 → 재조정 필요)를 산술 연산. `validate_review_sequence()`가 Review PASS → Translation 순서를 파일 타임스탬프로 강제. 독립 실행 스크립트: `validate_review.py`. (5b) **Review Focus Context 검증**: `validate_review_focus()`가 `config/review-focus.yaml` 기반으로 리뷰 집중 영역의 소프트 검증을 4항목(FS1 config 파일 존재+YAML 유효, FS2 해당 step의 focus_areas 존재, FS3 리뷰 보고서에 focus 섹션명 1개 이상 언급, FS4 focus 섹션 언급 시 `[Focus]` 태그 이슈 존재 확인)으로 수행. 모든 체크는 WARNING(비차단 — 소프트 검증). config 부재 → 전체 skip. `validate_review.py --check-focus`로 독립 실행 가능. (6) **Translation P1 검증**: `validate_translation_output()`이 번역 산출물의 구조적 무결성을 7항목(파일 존재 T1, 최소 크기 T2, 영어 원본 존재 T3, .ko.md 확장자 T4, 비-공백 T5, 헤딩 수 ±20% T6, 코드 블록 수 일치 T7)으로 검증. `check_glossary_freshness()`가 glossary 타임스탬프 신선도(T8). `verify_pacs_arithmetic()`이 모든 pACS 로그의 min() 산술 정확성(T9 — 범용). `validate_verification_log()`이 검증 로그 구조적 무결성(V1a-V1c: 파일 존재, 기준별 PASS/FAIL, 논리적 일관성). `validate_translation.py`는 Review verdict=PASS를 필수 체크(--check-sequence 없이도 항상 실행). 독립 실행 스크립트: `validate_translation.py`. (7) **pACS P1 검증**: `validate_pacs_output()`이 pACS 로그의 구조적 무결성을 6항목(PA1 파일 존재, PA2 최소 크기 50 bytes, PA3 차원 점수 ≥ 3개(0-100 범위), PA4 Pre-mortem 섹션 존재, PA5 min() 산술 정확성(verify_pacs_arithmetic 위임), PA7 RED 차단(pACS < 50 → FAIL — 단계 진행 차단))으로 검증. PA6(선택): 점수-색상 영역 정합성(RED/YELLOW/GREEN). 독립 실행 스크립트: `validate_pacs.py`. (8) **L0 Anti-Skip Guard 코드 구현**: `validate_step_output()`이 산출물 파일의 L0 검증을 3항목(L0a SOT outputs.step-N 경로의 파일 존재, L0b 파일 크기 ≥ MIN_OUTPUT_SIZE(100 bytes), L0c 비-공백 확인)으로 수행. Orchestrator가 current_step 증가 전 호출. `validate_pacs.py --check-l0`으로 pACS + L0 + L0d 동시 검증 가능. (8b) **L0d Content Structure Validation**: `validate_output_structure()`가 `config/output-structure.yaml` 기반으로 산출물의 구조적 무결성을 3가지 타입(L0d-heading: regex 마크다운 헤딩 존재, L0d-marker: 리터럴 substring 존재, L0d-count: regex findall 카운트 ≥ min_count)으로 결정론적 검증. config 부재/단계 미정의 → `(True, [])` 반환(후방 호환). 기본 모드: WARNING(비차단 — 캘리브레이션 단계). **L0d Blocking Mode**: `config/output-structure.yaml`의 `blocking_steps: []` 필드에 명시된 단계는 L0d 실패 시 `valid=False` 전파(FAIL — 단계 진행 차단). 기본값 빈 리스트로 하위 호환. 명시적 opt-in만 blocking. `validate_pacs.py --check-l0` 내부에서 자동 실행. (9) **Predictive Debugging P1 검증**: `validate_risk_scores()`가 risk-scores.json의 구조적 무결성을 6항목(RS1 필수 키, RS2 data_sessions 정수, RS3 risk_score 범위, RS4 error_count 산술 정합, RS5 resolution_rate 범위, RS6 top_risk_files 정렬+존재)으로 검증. `aggregate_risk_scores()`가 생성 직후 자기 검증 호출. (10) **Retry Budget P1 검증**: `validate_retry_budget.py`가 재시도 예산을 결정론적으로 판정한다. RB1(카운터 파일 읽기 — 정수, 없으면 0), RB2(ULW 활성 감지 — 스냅샷 regex), RB3(예산 비교 — `retries_used < max_retries`). `max_retries`는 ULW 활성 시 15, 비활성 시 10. `--check-and-increment` 모드가 예산과 Circuit Breaker를 **단일 호출로 동시 강제**(P1 봉쇄 — LLM이 진전도 확인을 우회 불가): 예산 허용 + CB CLOSED → 카운터 증가 + `can_retry: true`; 예산 소진 → `can_retry: false, reason: budget_exhausted`; CB OPEN → `can_retry: false, reason: circuit_breaker_open`(카운터 미증가 — 예산 보존). Orchestrator가 Verification Gate/pACS RED/Review FAIL 재시도 전후에 호출. 카운터 파일: `{gate}-logs/.step-N-retry-count`. 히스토리 파일: `{gate}-logs/.step-N-retry-history.jsonl`. Circuit Breaker(`_validate_retry_progress` — 점수 추이)와 Abductive Diagnosis(`_gather_retry_history` — 재시도 맥락)는 별도 데이터 소스를 사용하는 직교 시스템이다. `validate_retry_budget.py`는 self-contained이며 `_context_lib.py`에 대한 import가 없다(의도적 장애 격리). (11) **Importance-Based Retention P1 검증**: `validate_retention_result()`가 knowledge-index.jsonl 로테이션 후 결과의 구조적 무결성을 5항목(RI1 비어있지 않음, RI2 카운트 ≤ MAX(200), RI4 날짜 수준 시간순 정렬(같은 날 내 재정렬 허용 — 동시 세션 자연 현상), RI5 session_id 중복 없음, RI6 입력 > MAX 시 정확히 MAX개 출력)으로 검증. RI3(JSON 유효성)은 의도적 제외 — 기존 malformed 라인을 Tier 0으로 처리하므로 RI3이 불필요한 FIFO fallback을 유발. `_importance_tier(entry)`가 4계층 중요도(Tier 3: design_decisions/resolved errors/diagnosis_patterns, Tier 2: team_summaries/ulw_active/pacs_min, Tier 1: modified_files, Tier 0: trivial)를 순수 함수로 판정. `cleanup_knowledge_index()`가 3단계 알고리즘(Parse+Score → tier DESC·position ASC 정렬로 상위 MAX개 선택 → 원본 위치 재정렬로 시간순 복원)으로 중요도 기반 보존 수행. P1 검증 실패 시 FIFO fallback(현행 동작)으로 자동 복귀 — 회귀 불가능. `aggregate_risk_scores()` 부수 효과: 에러 이력이 있는 구 세션이 Tier 3(resolved errors)으로 분류되어 더 오래 유지됨 — Predictive Debugging 정확도 향상. 테스트: `tests/unit/test_importance_retention.py` (49 tests). (12) **Decision Log P1 검증**: `validate_decision_log()`이 autopilot 결정 로그의 구조적 무결성을 8항목(DL1 파일 존재, DL2 최소 크기 100 bytes, DL3 필수 섹션 5개(Step, Checkpoint Type, Decision, Rationale, Timestamp), DL4 단계 번호 일치, DL5 Rationale 비공백 ≥ 10자, DL6 Decision 비공백 ≥ 5자, DL7 Rationale에 품질 기준/이전 단계 참조 포함(WARNING — `_DL_EVIDENCE_RE` regex), DL8 Rationale 최소 15단어(WARNING — `_DL_MIN_RATIONALE_WORDS`))으로 검증. DL7/DL8은 WARNING(비차단) — 10+ 세션 캘리브레이션 후 FAIL 승격 예정. 독립 실행 스크립트: `validate_decision_log.py`. Orchestrator가 (human) 단계 자동 승인 후 수동 호출. 테스트: `tests/unit/test_autopilot_gates.py` (20 DL tests). (13) **SM5 Quality Gate Evidence Guard**: `_check_gate_evidence()`가 SOT `advance-step` 내부에서 품질 게이트 증거를 4항목(SM5a verification-log 존재, SM5b pACS-log 존재, SM5c pACS ≥ 50 — 2-stage 파싱 D-7 `_context_lib.py` 정합, SM5d review verdict ≠ FAIL)으로 물리적 강제한다 (Level B → Level A 승격 — LLM 우회 불가). `(human)` 단계(4, 8, 18)는 면제. `--force` 우회 시 `_log_force_audit()`가 `autopilot-logs/sm5-force-audit.jsonl`에 append-only 감사 기록. 체크 순서: lock 내부 CR-1 → SM3 → SM4 → SM5 (올바른 에러 메시지 보장). 테스트: `tests/unit/test_sot_manager.py::TestSM5GateEvidence` (17 tests).
- **Abductive Diagnosis P1 검증**: `validate_diagnosis_log()`이 진단 로그의 구조적 무결성을 10항목(AD1 파일 존재, AD2 최소 크기 100 bytes, AD3 Gate 필드 일치, AD4 선택 가설 존재, AD5 증거 ≥ 1개, AD6 Action Plan 섹션 존재, AD7 순방향 참조 금지, AD8 가설 ≥ 2개, AD9 선택 가설 일관성, AD10 이전 진단 참조(재시도 > 0))으로 검증. `diagnose_failure_context()`가 사전 증거 수집(retry_history, upstream_evidence, hypothesis_priority, fast_path, raw_evidence). Fast-Path(FP1-FP3)로 결정론적 단축. 독립 실행 스크립트: `diagnose_context.py`(사전 분석), `validate_diagnosis.py`(사후 검증). Knowledge Archive에 `diagnosis_patterns`로 아카이빙.
- **Cross-Step Traceability P1 검증**: `validate_cross_step_traceability()`가 산출물의 교차 단계 추적성을 5항목(CT1 trace 마커 존재, CT2 참조 단계 산출물 존재, CT3 섹션 ID 해결(Warning), CT4 최소 밀도 ≥ 3, CT5 순방향 참조 금지)으로 검증. 독립 실행 스크립트: `validate_traceability.py`. Verification 기준에 교차 단계 추적성이 포함된 단계에서 수동 호출.
- **Domain Knowledge Structure P1 검증**: `validate_domain_knowledge()`가 domain-knowledge.yaml의 구조적 무결성을 7항목(DK1 파일 존재+YAML 유효, DK2 metadata 필수 키, DK3 entities 구조(id 유일+slug, type, attributes), DK4 relations 참조 무결성(subject/object→entities.id, confidence), DK5 constraints 구조, DK6 산출물 DKS 참조 해결, DK7 제약 조건 비위반)으로 검증. 독립 실행 스크립트: `validate_domain_knowledge.py`. DKS 패턴 사용 워크플로우에서 수동 호출. 선택적 — 모든 워크플로우가 필요로 하지 않음.
- **Workflow.md DNA Inheritance P1 검증**: `validate_workflow_md()`가 생성된 workflow.md의 DNA 유전 구조적 무결성을 8항목(W1 파일 존재, W2 최소 크기 500 bytes, W3 `## Inherited DNA` 헤더 존재, W4 Inherited Patterns 테이블 ≥ 3행, W5 Constitutional Principles 섹션 존재, W6 Coding Anchor Points(CAP) 참조 존재, W7 교차 단계 추적성 Verification-Validator 정합성(Verification에 CT 기준이 있으면 validate_traceability Post-processing 필수), W8 도메인 지식 구조 Verification-Validator 정합성(DKS 참조 시 validate_domain_knowledge Post-processing 필수))으로 검증. 독립 실행 스크립트: `validate_workflow.py`. workflow-generator 완료 후(SKILL.md Step 13) 수동 호출.
- **Quality Gate 상태 IMMORTAL 보존**: `_extract_quality_gate_state()` 함수가 `pacs-logs/`, `review-logs/`, `verification-logs/`에서 최신 단계의 품질 게이트 결과(pACS 점수·약점 차원, Review verdict·이슈 카운트, Verification PASS/FAIL)를 추출하여 스냅샷에 IMMORTAL 섹션으로 보존한다. 세션 경계(compact/clear)에서 Verification Gate/pACS/Adversarial Review 재개 맥락이 유실되지 않는다.
- **Workflow Progress IMMORTAL 보존**: `_extract_workflow_progress()` 함수가 SOT `outputs` dict + `pacs-logs/` 파일에서 완료된 단계의 pACS 점수와 현재 진행 중인 단계를 추출하여 스냅샷에 IMMORTAL 섹션으로 보존한다. 20단계 워크플로우에서 세션이 끊겨도 이전 단계의 성과 맥락이 유실되지 않는다.
- **Autopilot Decision History IMMORTAL 보존**: `_extract_autopilot_decisions()` 함수가 `autopilot-logs/step-*-decision.md` 파일을 스캔하여 단계별 자동 승인 결정의 Rationale 첫 줄을 추출, 스냅샷에 IMMORTAL 섹션으로 보존한다. Step 8 검토 시 Step 4 승인 근거를 세션 경계에서도 참조할 수 있다.
- **Team State 복원**: `restore_context.py`의 `_build_recovery_output()`이 SOT `active_team` 필드를 읽어 SessionStart 출력에 표면화한다. (team) 단계 중 세션이 끊겨도 teammate 작업 맥락(이름, 상태, 완료/대기 태스크, 요약)이 복원된다. `active_team`이 없으면 섹션 자체를 생략.
- **최근 지시 표면화**: `restore_context.py`가 스냅샷 콘텐츠에서 "최근 지시 (Latest Instruction)" 섹션을 추출하여 SessionStart 출력에 포함한다. 기존 summary에 `latest_instruction`이 없을 때 fallback으로 동작한다.
- **Retry Budget State IMMORTAL 보존**: `_extract_retry_budget_state()` 함수가 `{gate}-logs/.step-N-retry-count` 및 `.step-N-retry-history.jsonl` 파일을 스캔하여 gate별 재시도 횟수 + 최근 pACS 점수를 추출, 스냅샷에 IMMORTAL 섹션으로 보존한다. 세션 경계에서 재시도 컨텍스트가 유실되지 않는다.
- **KA 품질 아카이빙**: `extract_session_facts()`가 3개 신규 필드를 Knowledge Archive에 기록한다: `verification_outcomes`(verification-logs 스캔 → step별 PASS/FAIL), `review_outcomes`(review-logs 스캔 → verdict + issues count), `workflow_quality_summary`(pacs-logs 스캔 → score + grade). cross-session 품질 추세 분석의 데이터 소스.
- **Team Merge 검증**: `validate_team_merge()` 함수가 SOT `active_team.completed_summaries`의 키(task명/agent명)가 병합된 산출물 파일에 포함되는지 교차 검증한다. `run_quality_gates.py`의 `TEAM_STEPS`에서 L0 Anti-Skip Guard 전에 WARNING(비차단)으로 호출된다.
- **SM-ST1 완료 보호**: `sot_manager.py`의 `cmd_set_status()`가 "completed" 설정 시 `current_step < total_steps`이면 거부한다. 워크플로우 미완료 상태에서 `/start` → `/run` 잘못된 라우팅을 방지한다. `validate_sot_schema()` S5b가 동일 교차검증을 스키마 레벨에서 수행.
- **SM5 품질 게이트 증거 강제 (P1 Hallucination Prevention)**: `sot_manager.py`의 `cmd_advance_step()`이 non-human 단계(HUMAN_STEPS {4, 8, 18} 제외)에서 4가지 증거 파일을 검증한다: SM5a(verification-logs/step-N-verify.md 존재), SM5b(pacs-logs/step-N-pacs.md 존재), SM5c(pACS ≥ 50 — RED zone 차단), SM5d(review-logs/step-N-review.md의 verdict ≠ FAIL). 증거 부재 시 `valid: false` 반환으로 단계 진행이 물리적으로 불가. `--force` CLI 플래그로 사용자 명시적 우회 가능. 이전에는 품질 게이트가 문서적 요구사항(Level B — LLM이 호출해야 함)이었으나, SM5 도입으로 SOT 쓰기 인터페이스에서 강제(Level A — Python이 차단)된다. 테스트: `tests/unit/test_sot_manager.py::TestSM5GateEvidence` (12 tests).
- **Phase Transition 스냅샷 헤더**: 다단계 전환이 감지된 세션에서는 스냅샷 헤더에 단일 phase 대신 `Phase flow: research(12) → implementation(25)` 형식의 전환 흐름을 표시한다. 각 단계의 도구 호출 수가 괄호 안에 포함되어 세션 복원 시 작업 흐름 맥락을 즉시 파악할 수 있다.
- **Error→Resolution 자동 표면화**: `restore_context.py`의 `_extract_recent_error_resolutions()` 함수가 Knowledge Archive의 최근 세션에서 error_patterns(type + resolution)을 읽어 SessionStart 출력에 최대 3개의 에러→해결 패턴을 직접 표시한다. 수동 Grep 없이 이전 세션의 에러 해결 경험을 즉시 활용할 수 있다.
- **반복 에러 타입 자동 표면화 (P1 Consumer)**: `_context_lib.py`의 `extract_recurring_error_types()` 함수가 전체 Knowledge Archive에서 3개 이상 세션에 걸쳐 반복되는 에러 타입을 세션 단위로 집계한다 (발생 횟수가 아닌 세션 횟수 기준). SessionStart에서 Python 코드가 직접 결과를 표면화하며, 에러 타입별 태그 Grep 쿼리 힌트도 자동 생성한다. 태그 보강(Producer) → 자동 표면화(Consumer) P1 체인을 완결한다. 테스트: `tests/unit/test_importance_retention.py` (11 tests).
- **학습된 작업 패턴 자동 표면화 (P1 Consumer)**: `_context_lib.py`의 `extract_learned_patterns()` 함수가 전체 Knowledge Archive에서 3개 이상 세션에 걸쳐 반복되는 success_patterns를 시퀀스 단위로 집계한다 (세션별 중복 제거). confidence = min(1.0, session_count / 5). `restore_context.py`의 SessionStart에서 상위 5개 패턴을 자동 표면화한다. success_patterns(Producer) → extract_learned_patterns(Consumer) P1 체인을 완결한다. 테스트: `tests/unit/test_learned_patterns.py` (11 tests).
- **Autopilot Stall Detection**: `_context_lib.py`의 `check_autopilot_progress()` 함수가 `autopilot-logs/.progress-tracker` JSON 파일로 동일 단계에서의 Stop hook 호출 횟수를 추적한다. 동일 단계에서 `_AUTOPILOT_STALL_THRESHOLD`(20) cycles 도달 시 stderr 경고를 출력한다. 단계가 전진하면 cycles가 리셋된다. 비차단(경고만) — autopilot-logs/는 Hook 정상 쓰기 영역(SOT 아님). `generate_context_summary.py` Stop hook에서 호출.
- **Phase-Aware Compact 제안 (P1)**: `generate_context_summary.py`의 `_suggest_compact_if_needed()` 함수가 매 Stop hook에서 2가지 조건으로 /compact 제안을 stderr에 출력한다. 조건 1: 단계 전환 감지(`detect_phase_transitions()` ≥ 2 단계) + 토큰 50%+ 사용. 조건 2: 토큰 65%+ + 도구 50회+. 세션 당 최대 1회(`.last_compact_suggestion` 마커 파일로 중복 방지). 마커 파일은 SessionEnd에서만 정리(PreCompact 제외 — compact가 제안에 의해 트리거되었을 수 있음). 비차단: stderr 경고만, exit code 0. 테스트: `tests/unit/test_compact_suggestion.py` (9 tests).
- **런타임 디렉터리 자동 생성**: `setup_init.py`의 `_check_runtime_dirs()` 함수가 SOT 파일 존재 시 `verification-logs/`, `pacs-logs/`, `review-logs/`, `autopilot-logs/`, `translations/`, `diagnosis-logs/` 6개 디렉터리를 자동 생성한다. 워크플로우 실행 중 디렉터리 부재로 인한 silent failure를 방지한다.
- **시스템 명령 필터링**: 스냅샷의 "현재 작업" 섹션에서 `/clear`, `/help` 등 시스템 명령을 자동 필터링하여 실제 사용자 작업 의도만 캡처한다.
- **Autopilot 런타임 강화**: Autopilot 활성 시 SessionStart가 실행 규칙을 컨텍스트에 주입하고, 스냅샷에 Autopilot 상태 섹션(IMMORTAL 우선순위)을 포함하며, Stop hook이 Decision Log 누락을 감지·보완한다. PostToolUse는 work_log에 autopilot_step 필드를 추가하여 단계 진행을 추적한다.
- **ULW 모드 감지·보존**: `detect_ulw_mode()` 함수가 트랜스크립트에서 word-boundary 정규식으로 `ulw` 키워드를 감지한다. 활성 시 스냅샷에 ULW 상태 섹션(IMMORTAL 우선순위)을 포함하고, SessionStart가 3개 강화 규칙(Intensifiers)을 컨텍스트에 주입한다. **암묵적 해제**: 새 세션(`source=startup`)에서는 이전 스냅샷에 ULW 상태가 있어도 규칙을 주입하지 않는다 — `clear`/`compact`/`resume` source만 ULW를 계승한다. `check_ulw_compliance()` 함수가 3개 강화 규칙의 준수를 결정론적으로 검증하여 스냅샷 IMMORTAL에 경고를 포함한다. Knowledge Archive에 `ulw_active: true`로 태깅되어 `Grep "ulw_active" knowledge-index.jsonl`로 RLM 쿼리 가능하다.
- **Predictive Debugging**: `aggregate_risk_scores()`가 Knowledge Archive의 error_patterns를 파일별로 집계하여 위험 점수를 산출한다 (가중치 × 감쇠). SessionStart 시 1회 실행되어 `risk-scores.json` 캐시를 생성하고, `predictive_debug_guard.py`(PreToolUse Hook)가 매 Edit/Write마다 캐시를 읽어 임계값 초과 시 stderr 경고를 출력한다. **Startup 트레이드오프**: SessionStart matcher가 `clear|compact|resume`이므로 최초 startup에서는 캐시 미생성 — 이전 캐시(2시간 이내)에 의존하거나, 첫 compact/clear 시 생성 (ADR-036). **D-7 상수 동기화**: `predictive_debug_guard.py`의 `RISK_THRESHOLD`/`MIN_SESSIONS`는 `_context_lib.py`의 `_RISK_SCORE_THRESHOLD`/`_RISK_MIN_SESSIONS`와 D-7 의도적 중복이다 — 변경 시 양쪽 동기화 필수. **Basename merge**: bare name(`_context_lib.py`)과 relative path(`.claude/hooks/scripts/_context_lib.py`)가 혼재할 때, 동일 basename 엔트리를 자동 병합하여 risk score 과소평가를 방지한다.

### Hook 설정 위치

모든 Hook은 **Project** (`.claude/settings.json`)에 통합 정의되어 있다. `git clone`만으로 Hook 인프라가 자동 적용된다.

- Stop → `context_guard.py --mode=stop` → `generate_context_summary.py`
- PostToolUse → `context_guard.py --mode=post-tool` → `update_work_log.py` (matcher: `Edit|Write|Bash|Task|NotebookEdit|TeamCreate|SendMessage|TaskCreate|TaskUpdate`)
- PreCompact → `context_guard.py --mode=pre-compact` → `save_context.py --trigger precompact`
- SessionStart → `context_guard.py --mode=restore` → `restore_context.py` (matcher: `clear|compact|resume`)
- **PreToolUse** → `block_destructive_commands.py` (matcher: `Bash`, 독립 실행 — exit code 2 보존)
- **PreToolUse** → `block_test_file_edit.py` (matcher: `Edit|Write`, 독립 실행 — `.tdd-guard` 토글 기반 TDD 테스트 파일 보호)
- **PreToolUse** → `predictive_debug_guard.py` (matcher: `Edit|Write`, 독립 실행 — 경고 전용 exit code 0)
- **PostToolUse** → `block_secret_leak.py` (matcher: `Bash`, 독립 실행 — 경고 전용 exit code 0)
- SessionEnd → `save_context.py --trigger sessionend` (matcher: `clear`)
- Setup (init) → `setup_init.py` — 인프라 건강 검증 (`claude --init`)
- Setup (maintenance) → `setup_maintenance.py` — 주기적 건강 검진 (`claude --maintenance`)

> **`if test -f; then; fi` 패턴 통일**: 모든 Hook 명령이 `if test -f; then; fi` 패턴을 사용한다. 이전의 `|| true` 패턴(exit code 2 차단 신호를 삼키는 잠복 버그)을 제거하여, `context_guard.py` 자식 스크립트에 차단 기능 추가 시에도 exit code 2가 안전하게 전파된다.
> **PreToolUse Safety Hook의 독립 실행 근거**: `block_destructive_commands.py`(안전)와 `block_test_file_edit.py`(TDD 보호)는 컨텍스트 보존과는 다른 도메인이다. exit code 2 보존이 필수이므로, `context_guard.py`를 거치지 않고 직접 실행한다. `block_test_file_edit.py`는 `.tdd-guard` 파일 존재 시에만 활성화된다 (`touch .tdd-guard`로 TDD 모드 시작, `rm .tdd-guard`로 해제).
> **PostToolUse Safety Hook의 독립 실행 근거**: `block_secret_leak.py`(시크릿 출력 감지)는 Bash 전용이며, 기존 PostToolUse(`update_work_log.py` — 9개 도구 추적)와 matcher 범위가 다르다. exit code 항상 0(경고 전용, best-effort)이므로 `context_guard.py`를 거치지 않고 직접 실행한다.
> **D-7 의도적 중복 인스턴스**: (1) `REQUIRED_SCRIPTS` — `setup_init.py` ↔ `setup_maintenance.py` (21개 스크립트 목록). (2) `predictive_debug_guard.py` 상수 — `RISK_THRESHOLD`/`MIN_SESSIONS` ↔ `_context_lib.py`의 `_RISK_SCORE_THRESHOLD`/`_RISK_MIN_SESSIONS`. (3) `ERROR_TAXONOMY` 타입명 — `_classify_error_patterns()` 내 12개 타입 ↔ `_RISK_WEIGHTS` 13개 키. (4) ULW 감지 패턴 — `_context_lib.py`의 `_gather_retry_history()` ↔ `validate_retry_budget.py`의 `_ULW_SNAPSHOT_RE` ↔ `restore_context.py`의 ULW 상태 문자열 검사 (모두 `"ULW 상태"` 기반). (5) 재시도 한도 상수 — `validate_retry_budget.py`의 `DEFAULT_MAX_RETRIES`/`ULW_MAX_RETRIES` ↔ `_context_lib.py`의 `_DEFAULT_MAX_RETRIES`/`_ULW_MAX_RETRIES` ↔ `restore_context.py`의 ULW+Autopilot 주입 텍스트. (6) `HUMAN_STEPS` — `sot_manager.py`의 `HUMAN_STEPS` ↔ `run_quality_gates.py`의 `HUMAN_STEPS` ↔ `validate_step_transition.py`의 `HUMAN_STEPS` ↔ `_context_lib.py`의 `HUMAN_STEPS_SET` (모두 `frozenset({4, 8, 18})`). (7) `GATE_DIRS` 매핑 — `validate_retry_budget.py`의 `GATE_DIRS` ↔ `generate_context_summary.py`의 `_check_missing_retry_records()` 내 `gate_dirs` (모두 `{"verification": "verification-logs", "pacs": "pacs-logs", "review": "review-logs"}`). (8) `TEAM_STEPS` — `run_quality_gates.py`의 `TEAM_STEPS` (frozenset({2, 6, 10, 11, 13, 14})) — 현재 단일 위치이나 향후 validate_step_transition.py 등에 추가될 수 있음. (9) Python 버전 제약 — `pyproject.toml`의 `requires-python = ">=3.12,<3.14"` ↔ `main.py`의 `_check_python_version()` `sys.version_info >= (3, 14)` ↔ `setup_init.py`의 `_check_domain_venv()` `minor in (12, 13)` ↔ `preflight_check.py`의 `check_python_version()` `ver.minor >= 14` (모두 spaCy/pydantic v1의 Python 3.14 비호환에 기인 — spaCy가 3.14 지원 시 4곳 동기화 필수). **(10)** `_PACS_WITH_MIN_RE`/`_PACS_SIMPLE_RE` — `sot_manager.py`의 SM5c pACS 파싱 regex ↔ `_context_lib.py`의 `_PACS_WITH_MIN_RE`/`_PACS_SIMPLE_RE` PA7 파싱 regex (2단계 파싱: min 공식 우선 → simple fallback). **(11)** `_GROUP_TO_TEAM` 매핑 — `distribute_sites_to_teams.py`의 `_GROUP_TO_TEAM` (A-J → kr-major/kr-tech/english/multilingual) ↔ `constants.py`의 `CRAWL_GROUPS` (A-J 정의) ↔ `data/config/sources.yaml`의 `group` 필드 (그룹 체계 변경 시 3곳 동기화 필수). **(12)** `ALTERNATIVE_STRATEGIES` — `retry_manager.py`의 12개 전략 목록 ↔ `dynamic_bypass.py`의 `DynamicBypassEngine` 전략 레지스트리 (12개 전략이 양쪽 동일해야 함 — `tests/crawling/test_dynamic_bypass.py::TestD7StrategyNameSync`가 교차 검증). **(13)** 116개 사이트 레지스트리 — `extract_site_urls.py` ↔ `split_sites_by_group.py` ↔ `validate_site_coverage.py` ↔ `distribute_sites_to_teams.py` ↔ `data/config/sources.yaml` (5개 소스의 도메인 리스트가 동일해야 함 — `validate_site_registry_sync.py`가 RS1-RS4 교차 검증). **(14)** `CRAWL_NEVER_ABANDON` 플래그 — `constants.py`의 `CRAWL_NEVER_ABANDON = True` ↔ `pipeline.py`의 4개 소비자(Circuit Breaker 사이트 수준 조건, Circuit Breaker URL 수준 조건, Never-Abandon Multi-Pass 루프, `_run_single_pass` 로깅) (플래그 변경 시 4곳 동작이 동기화되어야 함). 각 D-7 인스턴스는 코드에 cross-reference 주석이 있으며, 한쪽 변경 시 반드시 대응 쪽도 동기화해야 한다.

## Memory Architecture — Dual-Layer Design

프로젝트는 두 개의 독립적이고 상호보완적인 메모리 계층을 사용한다:

| 계층 | 시스템 | 성격 | 저장소 |
|------|--------|------|--------|
| **Layer 1** | Knowledge Archive (KA) | P1 결정론적 — 구조화 운영 데이터 | `.claude/context-snapshots/knowledge-index.jsonl` |
| **Layer 2** | Auto-memory | Claude 자율 — 유기적 학습 | `~/.claude-cysinsight/.../memory/` |

### 역할 분담

| 데이터 유형 | Layer | 이유 |
|-----------|-------|------|
| 에러 패턴·해결 기록 | KA (Layer 1) | P1 결정론적 분류 — LLM 판단 불필요 |
| pACS 히스토리 | KA (Layer 1) | 산술 데이터 — P1 검증 가능 |
| 위험 점수·Predictive Debugging | KA (Layer 1) | 파일별 집계 — 산술 연산 |
| 세션 팩트(phase, tool_sequence, tags) | KA (Layer 1) | 구조화된 메타데이터 — 프로그래밍적 탐색 |
| 사용자 코딩 스타일·선호 | Auto-memory (Layer 2) | 자연어 선호 — 구조화 어려움 |
| 워크플로우 관례·반복 패턴 | Auto-memory (Layer 2) | 세션 간 유기적 학습 |
| 프로젝트 구조·아키텍처 요약 | Auto-memory (Layer 2) | 자연어 설명 — RLM 검색보다 직접 로딩이 효율적 |

### 중복 금지 규칙

- KA가 이미 추적하는 데이터를 Auto-memory에 중복 저장하지 않는다
- KA의 `error_patterns`가 이미 에러를 12개 타입으로 분류하므로, Auto-memory에 에러 패턴을 별도 기록하지 않는다
- KA의 `success_patterns`가 이미 성공 시퀀스를 추출하므로, Auto-memory에 도구 사용 패턴을 별도 기록하지 않는다
- Auto-memory는 KA가 캡처하지 못하는 **의미론적·맥락적** 학습에 집중한다

## 시작 트리거 (Start Triggers)

> 사용자가 아래 패턴의 자연어 명령을 입력하면, **워크플로우 상태에 따라 적절한 동작을 자동으로 실행**한다.
> 명시적 슬래시 커맨드 입력 없이도 시스템이 가동된다.

### 라우팅 규칙 (2단계 판별)

**Step 1 — 워크플로우 상태 확인**:
1. `prompt/workflow.md`와 `.claude/state.yaml`이 모두 존재해야 한다. 없으면 "워크플로우가 아직 없습니다. `/workflow-generator`로 먼저 생성하세요."
2. `.claude/state.yaml`의 `workflow.status` 값을 읽는다.

**Step 2 — 상태별 라우팅**:

| 워크플로우 상태 | 동작 | 설명 |
|---------------|------|------|
| `status: complete` | **`/run` 실행** | 워크플로우 구축 완료. 실제 시스템 실행 (크롤링 + 분석) |
| 그 외 (`in_progress`, 없음 등) | **`/start` 실행** | 워크플로우 미완성. 구축 단계 실행 |

> 워크플로우가 완료되면, 모든 시작 트리거는 구축(workflow building)이 아닌 **실제 시스템 실행**(crawling + analysis)으로 전환된다.

### 인식 패턴

| 트리거 패턴 (한국어) | 트리거 패턴 (영어) | 라우팅 |
|---------------------|-------------------|--------|
| "시작하자", "시작", "시작해" | "start", "let's start", "begin" | 상태에 따라 `/run` 또는 `/start` |
| "워크플로우 시작", "워크플로우를 시작하자" | "start the workflow", "run the workflow" | 상태에 따라 `/run` 또는 `/start` |
| "크롤링 시작", "크롤링을 하자", "크롤링 해줘" | "start crawling", "begin crawling", "crawl" | `/run` (mode=crawl) |
| "뉴스를 수집하자", "뉴스 수집", "기사를 가져와" | "collect news", "fetch articles" | `/run` (mode=crawl) |
| "분석을 하자", "빅데이터 분석", "분석 시작" | "run analysis", "analyze", "big data analysis" | `/run` (mode=analyze) |
| "전체 실행", "풀 파이프라인", "전부 실행" | "run full pipeline", "run everything" | `/run` (mode=full) |
| "스캐닝 시작", "조사를 시작하자" | "start scanning", "begin research" | 상태에 따라 `/run` 또는 `/start` |
| "다음 단계", "다음", "진행하자", "계속" | "next step", "continue", "proceed" | 상태에 따라 `/run` 또는 `/start` |
| "autopilot으로 시작", "자동 모드로 시작" | "start in autopilot", "auto mode" | `/start` + autopilot 활성화 |
| "통찰 분석", "인사이트 분석", "빅데이터 통찰" | "insight analysis", "run insights", "big data insights" | `/run` (mode=insight) |
| "통찰 결과", "인사이트 결과" | "insight results", "show insights" | `data/insights/` 최신 결과 읽기 |
| "DCI 실행", "심층 분석 시작", "딥 콘텐츠 분석" | "run DCI", "start deep content", "deep content intelligence" | `/run-dci-only` |
| "DCI 재실행", "심층 분석 재개", "DCI 이어서" | "resume DCI", "retry DCI" | `/run-dci-only` (auto-resume via checkpoints) |
| "DCI 결과", "심층 분석 결과" | "DCI results", "show deep report" | `data/dci/runs/{latest}/final_report.md` 읽기 |
| "일반인 해석 만들어줘", "공개 보고서 생성", "쉬운 해석" | "generate public narrative", "public layers", "plain explanation" | `/generate-public-layers --date {date}` |
| "해석/통찰/미래 다시 생성" | "regenerate L2", etc. | `/generate-public-layers --only L2 --date {date}` |
| "상태 확인", "결과 확인", "현황" | "check status", "show results" | `.venv/bin/python main.py --mode status` |

### `/run` 실행 프로토콜 (워크플로우 완료 후)

> **venv 필수**: 도메인 파이프라인(`main.py`, `src/`)은 Python 3.13 venv에서 실행해야 한다. spaCy가 Python 3.14의 pydantic v1 비호환으로 작동하지 않기 때문이다. Hook 스크립트(`.claude/hooks/scripts/`)는 시스템 python3(3.14)로 정상 동작하며 변경 불필요.

```
1. .venv/bin/python scripts/preflight_check.py --project-dir . --mode full --json
2. readiness == "ready" 확인 (degradations 리포트)
3. 실행 배너 출력 (모드, 사이트 수, 날짜)
4. .venv/bin/python main.py --mode full --dry-run (설정 검증)
5. .venv/bin/python main.py --mode full --date YYYY-MM-DD (실제 실행)
6. 결과 리포트 (수집 건수, 분석 결과, 출력 파일)
```

### `/run` Insight 모드 프로토콜 (빅데이터 통찰 분석)

> Stage 1~4 산출물이 축적된 상태에서, 다중 날짜 윈도우에 대한 구조적 통찰을 생산한다.
> 7개 모듈(M1~M7), 27개 지표, 92% 결정론적 Python(P1 봉쇄).

```
1. .venv/bin/python main.py --mode insight --window 30 --end-date YYYY-MM-DD
2. 실행 배너 출력 (윈도우, 날짜 범위, 커버리지)
3. 결과: data/insights/{run_id}/synthesis/insight_report.md 읽기
4. 사용자에게 핵심 발견(key findings) 리포트
```

**윈도우 옵션**: `--window 7` (주간), `--window 30` (월간), `--window 90` (분기)
**특정 모듈**: `--module m1_crosslingual` (교차언어만), `--module m5_geopolitical` (지정학만)

### `/start` 실행 프로토콜 (워크플로우 미완성 시)

```
1. python3 scripts/workflow_starter.py --project-dir .
2. readiness == "ready" 확인
3. 시작 배너 출력 (Step N/20, Phase, Agent)
4. python3 scripts/extract_orchestrator_step_guide.py --step N --project-dir . --include-universal
5. workflow.md Step N의 Verification 기준 읽기
6. Universal Step Protocol 순차 실행
7. 완료 후: autopilot ON → 다음 단계 자동 진행 / OFF → 사용자 대기
```

## 스킬 사용 판별

| 사용자 요청 패턴 | 스킬 | 진입점 |
|----------------|------|--------|
| "워크플로우 만들어줘", "자동화 파이프라인 설계", "작업 흐름 정의" | `workflow-generator` | SKILL.md → 케이스 판별 |
| "스킬 만들어줘", "create a skill", "새 스킬 생성" | `skill-creator` | SKILL.md → DNA 유전 포함 생성 |
| "에이전트 만들어줘", "create an agent", "서브에이전트 생성" | `subagent-creator` | SKILL.md → frontmatter + DNA 주입 |
| "논문 스타일로 써줘", "학술적 글쓰기", "논문 문장 다듬기" | `doctoral-writing` | SKILL.md → 맥락 파악 |
| "통찰 보고서 작성", "분석 보고서", "insight report" | `insight-report` | SKILL.md → 데이터 기반 통찰 보고서 생성 |

## 설계 원칙

1. **P1 — 정확도를 위한 데이터 정제**: AI에게 전달하기 전 Python 등으로 노이즈 제거. 전처리·후처리 명시.
2. **P2 — 전문성 기반 위임 구조**: 전문 에이전트에게 위임하여 품질 극대화. Orchestrator는 조율만.
3. **P3 — 이미지/리소스 정확성**: 정확한 다운로드 경로 명시. placeholder 누락 불가.
4. **P4 — 질문 설계 규칙**: 최대 4개 질문, 각 3개 선택지. 모호함 없으면 질문 없이 진행. Claude Code에서는 `AskUserQuestion` 도구로 구현. Slash Command가 사전 정의된 선택형 개입이라면, AskUserQuestion은 동적 질문이 필요한 상황에 사용.

## Autopilot Mode (Claude Code 구현)

워크플로우 실행 시 `(human)` 단계와 AskUserQuestion을 자동 승인하는 모드. 상세: `AGENTS.md §5.1`

### 활성화 패턴

| 사용자 명령 | 동작 |
|-----------|------|
| "autopilot 모드로 실행", "자동 모드로 워크플로우 실행", "전자동으로 실행" | SOT에 `autopilot.enabled: true` 설정 후 워크플로우 시작 |
| "autopilot 해제", "수동 모드로 전환" | SOT에 `autopilot.enabled: false` — 다음 `(human)` 단계부터 적용 |

### Checkpoint별 동작

| Checkpoint | Autopilot 동작 |
|-----------|---------------|
| `(human)` + Slash Command | 완전한 산출물 생성 → 품질 극대화 기본값으로 자동 승인 → 결정 로그 기록 |
| AskUserQuestion | 선택지 중 품질 극대화 옵션 자동 선택 → 결정 로그 기록 |
| `(hook)` exit code 2 | **변경 없음** — 그대로 차단, 피드백 전달, 재작업 |

### Anti-Skip Guard + Verification Gate + pACS (4계층 품질 보장)

Orchestrator는 `current_step`을 순차적으로만 증가. 각 단계 완료 시 최대 4계층 검증을 통과해야 진행한다:

1. **L0 Anti-Skip Guard** (결정론적) — 산출물 파일 존재 + 최소 크기(100 bytes). Hook 계층의 `validate_step_output()` 함수가 수행.
2. **L1 Verification Gate** (의미론적) — 산출물이 `Verification` 기준을 100% 달성했는지 에이전트 자기 검증. 실패 시 해당 부분만 재실행(최대 10회). `verification-logs/step-N-verify.md`에 기록.
3. **L1.5 pACS Self-Rating** (신뢰도) — Pre-mortem Protocol 수행 후 F/C/L 3차원 채점. `pacs-logs/step-N-pacs.md`에 기록. RED(< 50) 시 재작업.
4. **[L2 Calibration]** (선택적) — 별도 `@verifier` 에이전트가 pACS 점수 교차 검증. 고위험 단계만.

> `Verification` 필드가 없는 단계는 Anti-Skip Guard만으로 진행 (하위 호환). 상세: `AGENTS.md §5.3`, `§5.4`

### 결정 로그

자동 승인된 결정은 `autopilot-logs/step-N-decision.md`에 기록: 단계, 옵션, 선택 근거(절대 기준 1 기반).
Decision Log 표준 템플릿: `references/autopilot-decision-template.md`

### 런타임 강화 메커니즘

Autopilot의 설계 의도를 런타임에서 강화하는 하이브리드(Hook + 프롬프트) 시스템:

| 계층 | 메커니즘 | 강화 내용 |
|------|---------|----------|
| **Hook** (결정론적) | `restore_context.py` — SessionStart | Autopilot 활성 시 6개 실행 규칙 + 이전 단계 산출물 검증 결과를 컨텍스트에 주입 |
| **Hook** (결정론적) | `generate_snapshot_md()` — 스냅샷 | Autopilot 상태 + Agent Team 상태 섹션을 IMMORTAL 우선순위로 보존 (세션 경계에서 유실 방지) |
| **Hook** (결정론적) | `generate_context_summary.py` — Stop | 자동 승인 패턴 감지 → Decision Log 누락 시 보완 생성 (안전망) + Autopilot Stall Detection (동일 단계 20 cycles 경고) |
| **Hook** (결정론적) | `update_work_log.py` — PostToolUse | `autopilot_step` 필드로 단계 진행 추적 (사후 분석 가능) |
| **Hook** (결정론적) | `run_quality_gates.py` — HQ4 | (human) 단계 자동 승인 시 직전 non-human 단계의 verification-logs + pacs-logs 존재 확인 |
| **Hook** (결정론적) | `generate_snapshot_md()` — 스냅샷 | Workflow Progress + Decision History IMMORTAL 섹션으로 세션 경계에서 진행 맥락 보존 |
| **Hook** (결정론적) | `restore_context.py` — SessionStart | Team State 복원 — SOT `active_team` 표면화로 (team) 단계 재개 맥락 제공 |
| **프롬프트** (행동 유도) | Execution Checklist (아래) | 각 단계의 시작/실행/완료 시 필수 행동 명시 |

> Hook 계층은 SOT를 읽기 전용으로만 접근하며 (절대 기준 2 준수), 쓰기는 `context-snapshots/`와 `autopilot-logs/`에만 수행한다.

### Autopilot Execution Checklist (MANDATORY)

> **공통 체크리스트**: `AGENTS.md §5.1` Autopilot Execution Checklist 참조. 아래는 **Claude Code 전용** P1 CLI 명령 매핑이다.

#### P1 검증 CLI 명령 (각 게이트별)

| 게이트 | CLI 명령 |
|--------|---------|
| Verification | `python3 .claude/hooks/scripts/validate_verification.py --step N --project-dir .` |
| Cross-Step Traceability | `python3 .claude/hooks/scripts/validate_traceability.py --step N --project-dir .` |
| DKS (선택적) | `python3 .claude/hooks/scripts/validate_domain_knowledge.py --project-dir . [--check-output --step N]` |
| pACS + L0 | `python3 .claude/hooks/scripts/validate_pacs.py --step N --check-l0 --project-dir .` |
| Review | `python3 .claude/hooks/scripts/validate_review.py --step N --project-dir . --check-pacs-arithmetic` |
| Decision Log | `python3 .claude/hooks/scripts/validate_decision_log.py --step N --project-dir .` |
| Translation | `python3 .claude/hooks/scripts/validate_translation.py --step N --project-dir . --check-pacs --check-sequence` |
| Retry Budget | `python3 .claude/hooks/scripts/validate_retry_budget.py --step N --gate {verification\|pacs\|review} --project-dir . --check-and-increment` |
| Retry Record | `python3 .claude/hooks/scripts/validate_retry_budget.py --step N --gate {gate} --project-dir . --record-attempt --pacs-score {score}` |
| Diagnosis (사전) | `python3 .claude/hooks/scripts/diagnose_context.py --step N --gate {gate} --project-dir .` |
| Diagnosis (사후) | `python3 .claude/hooks/scripts/validate_diagnosis.py --step N --gate {gate} --project-dir .` |

#### Claude Code 전용 추가사항

- **Pre-L1 /simplify**: 코드 단계(Step 9-17)에서 산출물 저장 후 L1 전에 `/simplify` 실행 (비차단 advisory)
- **(team) 단계**: `TeamCreate` → SOT `active_team`, 각 Teammate L1+L1.5 자기 검증, Team Lead L2 종합, `TeamDelete` → SOT `completed_teams` 이동
- **번역**: `@translator` 호출 → `*.ko.md` 생성 → SOT `outputs.step-N-ko` → Translation pACS (`pacs-logs/step-N-translation-pacs.md`)

#### NEVER DO

> 전체 Anti-Rationalization 테이블: `AGENTS.md §5.1` 참조. 아래는 핵심 요약.

- `current_step` 2이상 한 번에 증가 금지, 산출물 없이 진행 금지, "자동이니까 간략하게" 금지
- Verification FAIL인 채로 진행 금지, pACS 전부 90+ 금지, Review 이슈 0건 PASS 금지
- 진단 없이 동일 접근법 재시도 금지, Circuit breaker OPEN인데 재시도 금지
- `(hook)` exit code 2 무시 금지, `(team)` Teammate SOT 직접 수정 금지
- Review FAIL 상태에서 Translation 금지, Reviewer pACS를 Generator pACS 참조 후 채점 금지
- 진단 가설 1개만 금지(AD8), 동일 가설 3회 연속 금지(FP3)

## ULW (Ultrawork) Mode

프롬프트에 `ulw`를 포함하면 **Ultrawork 모드**가 활성화된다. ULW는 Autopilot과 **직교하는 철저함 강도(thoroughness intensity) 오버레이**이다.

- **Autopilot** = 자동화 축(HOW) — `(human)` 승인 건너뛰기
- **ULW** = 철저함 축(HOW THOROUGHLY) — 빠짐없이, 에러 해결까지 완벽 수행

두 축은 독립적이므로, 어떤 조합이든 가능하다. ULW는 프롬프트에 `ulw` 포함으로 활성화, 새 세션에서 `ulw` 없으면 암묵적 해제.

### 활성화 패턴

| 사용자 명령 | 동작 |
|-----------|------|
| "ulw 이거 해줘", "ulw 리팩토링해줘" | 트랜스크립트에서 `ulw` 감지 → ULW 모드 활성화 |
| 새 세션에서 `ulw` 없는 프롬프트 | ULW 비활성 (암묵적 해제 — 명시적 해제 불필요) |

### 3가지 강화 규칙 (Intensifiers)

ULW가 활성화되면 아래 3가지 강화 규칙이 **현재 컨텍스트에 오버레이**된다:

| 강화 규칙 | 설명 | 대화형 효과 | Autopilot 결합 효과 |
|----------|------|-----------|-------------------|
| **I-1. Sisyphus Persistence** | 최대 3회 재시도, 각 시도는 다른 접근법. 100% 완료 또는 불가 사유 보고 | 에러 시 3회까지 대안 시도 | 품질 게이트(Verification/pACS) 재시도 한도 10→15회 상향 |
| **I-2. Mandatory Task Decomposition** | TaskCreate → TaskUpdate → TaskList 필수 | 비-trivial 작업 시 태스크 분해 강제 | 변경 없음 (Autopilot은 이미 SOT 기반 추적) |
| **I-3. Bounded Retry Escalation** | 동일 대상 3회 초과 연속 재시도 금지(품질 게이트는 별도 예산 적용) — 초과 시 사용자 에스컬레이션 | 무한 루프 방지 | Safety Hook 차단은 항상 존중 |

> **런타임 강화**: Hook이 `detect_ulw_mode()` → 스냅샷 IMMORTAL → KA `ulw_active` 태깅 → SessionStart 규칙 주입 → `check_ulw_compliance()` 결정론적 검증. 상세: §Context Preservation System.

### NEVER DO

| 금지 사항 | 흔한 합리화 | 왜 안 되는가 |
|----------|-----------|-------------|
| 3회 초과 연속 재시도 | "한 번만 더 하면 성공" | I-3 위반. 무한 루프 방지가 ULW의 설계 의도 |
| Safety Hook 차단을 ULW로 override | "ULW는 최대 철저함이니까" | ULW는 품질 축. Safety는 안전 축. 직교 |
| Task를 "일부 완료"로 남김 | "나머지는 다음에" | I-1 위반. 100% 완료 또는 불가 사유 보고 |
| 에러 시 대안 없이 포기 | "불가능한 에러" | I-1 위반. 3회까지 다른 접근법 시도 필수 |
| TaskCreate 없이 진행 | "간단한 작업" | I-2 위반. 비-trivial이면 Task 분해 필수 |

## 언어 및 스타일 규칙

- **프레임워크 문서·사용자 대화**: 한국어
- **워크플로우 실행**: 영어 (AI 성능 극대화 — 절대 기준 1 근거)
- **최종 산출물**: 영어 원본 + 한국어 번역 쌍 (각 단계별 `@translator` 서브에이전트가 생성)
- **기술 용어**: 영어 유지 (e.g., SOT, Agent Team, Hooks)
- **시각화**: Mermaid 다이어그램 선호
- **깊이**: 간략 요약보다 포괄적·데이터 기반 서술 선호

### English-First 실행 원칙

워크플로우 **실행** 시 모든 에이전트(Sub-agent, Teammate)는 **영어로 작업**하고 **영어로 산출물을 생성**한다.

| 단계 | 언어 | 근거 |
|------|------|------|
| 워크플로우 설계 (workflow-generator) | 한국어 | 사용자와의 대화 |
| 워크플로우 실행 (에이전트 작업) | **영어** | AI 성능 극대화 |
| 산출물 번역 | 영어→한국어 | `@translator` 서브에이전트 |
| SOT 기록 | 언어 무관 (경로·숫자) | 구조적 데이터 |

### 번역 프로토콜 (워크플로우 실행 시)

1. 각 단계의 영어 산출물이 SOT `outputs.step-N`에 기록된 후
2. 워크플로우에 `Translation: @translator`로 표기된 단계에 한해
3. `@translator` 서브에이전트 호출 (`.claude/agents/translator.md`)
4. 번역 완료 후 SOT `outputs.step-N-ko`에 한국어 경로 기록
5. 용어 사전(`translations/glossary.yaml`)이 자동 유지됨 (RLM 외부 지속 상태)

> **번역 대상**: 텍스트 콘텐츠 산출물만 (`.md`, `.txt` 등). 코드(`.py`, `.js`), 데이터(`.json`, `.csv`), 설정(`.yaml` config) 파일은 번역하지 않는다.
> **SOT 호환성**: `step-N-ko` 키는 Anti-Skip Guard의 `.isdigit()` 가드에 의해 자동으로 건너뛰어진다 (Hook 코드 변경 없음).

## 스킬 개발 규칙

새로운 스킬을 만들거나 기존 스킬을 수정할 때:

1. **모든 절대 기준을 반드시 포함**한다 — 해당 도메인에 맞게 맥락화하여 적용 (코드 변경이 아닌 도메인의 경우 절대 기준 3은 N/A 가능).
2. **파일 간 역할 분담**을 명확히 한다 — SKILL.md(WHY), references/(WHAT/HOW/VERIFY).
3. **절대 기준 간 충돌 시나리오**를 구체적으로 명시한다 — 추상적 규칙이 아닌 실전 판단 기준.
4. 수정 후 반드시 **절대 기준 관점에서 성찰**한다 — 문구만 넣지 않고 기존 내용과 충돌 여부를 점검.

## DCI — Deep Content Intelligence (Independent Workflow, v1.0+)

**DCI는 독립 워크플로우**다. Meta-Orchestrator의 W1→W2→W3→W4(Master) chain과 직교하는 병렬 track으로 작동한다. W1 raw articles(`data/raw/{date}/all_articles.jsonl`)만 입력으로 사용하며, W2/W3 완료 여부와 무관하게 실행 가능하다. Master Integration(WF4)은 기존 역할(W1+W2+W3 산출물 통합)을 그대로 유지한다.

**α-strict 라벨 규칙**: 사용자 UI/triggers에서는 **"DCI"** 고유 이름만 사용한다. "WF4"라는 라벨은 기존 Master Integration 고유의 체인 정체성에 보존되며, DCI에 재사용하지 않는다. 이는 Meta-Orchestrator 절대 규칙 #3 "W1→W2→W3→W4"의 의미적 일관성을 지키기 위함이다.

상세 설계: `prompt/execution-workflows/dci.md` (7-Phase protocol)
구현 기록: `DECISION-LOG.md` ADR-071, ADR-072, **ADR-079** (independence promotion)

### 사용법

```bash
# 권장 — 에이전트 오케스트레이션 하에 실행
/run-dci-only              # 슬래시 커맨드 (주 entry point)

# 직접 Python 실행 (테스트/CI용, 에이전트 미개입)
.venv/bin/python main.py --mode dci --date YYYY-MM-DD --dry-run
.venv/bin/python main.py --mode dci --date YYYY-MM-DD

# 비활성화 (기존 WF1-4 Master만 실행)
DCI_DISABLED=1 .venv/bin/python main.py --mode full
```

### 7-Phase Agent Workflow

| Phase | 역할 | 기술 |
|-------|-----|------|
| 1 Preflight | 8 checks (corpus, 모델, DCI_ENABLED, SG 임계값) | Python CLI only (`validate_dci_preflight.py`) |
| 2 Structural | L-1 → L2 레이어 실행 | Python subprocess, 순차 |
| 3 Graph & Style | L3 → L5 레이어 실행 | Python subprocess |
| 4 Reasoning | L6 Triadic + L7 GoT + L8 MC + L9 Metacog | L6만 LLM-essential, 나머지 Python |
| 5 Narrator | L10 Doctoral report | Claude CLI + CE3 Python 재검증 |
| 6 Review | 3-reviewer Agent Team (SG, Evidence, Narrative) | **유일한 Agent Team — 독립성 필수** |
| 7 Reporting | Executive summary + Korean translation | Python CLI + `@translator` |

**Agent 수**: 5개 (Orchestrator + 3 Reviewers + Translator 재사용). `@dci-preflight`·`@dci-reporter`는 **Python CLI로 대체** (할루시네이션 원천봉쇄 — ADR-079).

### 아키텍처 요약

| Layer | 역할 | 구현 |
|-------|------|------|
| L-1 | 외부 지식 pre-fetch | skeleton (API 사용 안 함) |
| L0 | 문서 구조화 | Kiwi 한국어 + 다국어 regex + PDTB + URL |
| L1 | 의미 단위 추출 | claims/quotes/numerical/CAMEO 규칙 |
| L1.5 | 의미 표상 | 휴리스틱 SPO + FrameNet-lite |
| L2 | 관계·화용 | Allen 13-relation + timex3 + hedging |
| L3 | 지식 그래프 | NetworkX entity 공기 + community |
| L4 | 교차문서 | SimHash + Jaccard thread clustering |
| L5 | 심리·문체 | textstat + TTR/MTLD/HDD + Burrows Delta |
| **L6** | **Triadic Ensemble** | **Claude CLI 4-lens (α/β/γ/δ-critic)** |
| L7 | Graph-of-Thought | 순수 Python Bayesian DAG |
| L8 | Monte Carlo | 1,024-leaf scenario tree |
| L9 | Metacognitive | 블라인드 스팟 + 불확실성 분해 |
| **L10** | **Doctoral narrator** | **Claude CLI + CE3 숫자 검증** |
| L11 | Dashboard | Streamlit 탭 (`dashboard.py`) |

### SG-Superhuman 10-Gate (구조적 할루시네이션 봉쇄)

모든 DCI run은 다음 Python-결정론 gate를 통과해야 `PASS`:

- G1 `char_coverage = 1.00` (본문 모든 문자가 최소 1회 진입)
- G2 `triple_lens_coverage ≥ 3.0` (문자당 평균 3+ 렌즈 참조)
- G3 `llm_body_injection_ratio = 1.00` (L6에 본문 전문 100% 투입)
- G4 `technique_completeness = 93/93` (모드별 준수)
- G5 `nli_verification_pass_rate ≥ 0.95` (DeBERTa-v3-MNLI, ADR-078 재정의)
- G6 `triadic_consensus_rate ≥ 0.60` (productive-disagreement band, ADR-078)
- G7 `adversarial_critic_pass ≥ 0.90` (Critic JSON parse)
- G8 `evidence_3layer_complete = 100%` (CE4 article/segment/char)
- G9 `technique_mode_compliance = 100%` (Registry vs trace)
- G10 `uncertainty_quantified = 100%` (L7/L8/L9 artifacts presence)

독립 검증 CLI:
```bash
python3 .claude/hooks/scripts/validate_dci_sg_superhuman.py --run-id {run_id} --project-dir .
python3 .claude/hooks/scripts/validate_dci_evidence.py --run-id {run_id} --project-dir .
python3 .claude/hooks/scripts/validate_dci_narrative.py --run-id {run_id} --project-dir .
python3 .claude/hooks/scripts/validate_dci_char_coverage.py --run-id {run_id} --project-dir .
```

### 5조항 P1 Hallucination Prevention DNA (모든 DCI 에이전트 상속)

1. **NEVER recompute any number.** Python validators produce all metrics.
2. **NEVER invent `[ev:xxx]` markers.** Only reference markers already in `evidence_ledger.jsonl`.
3. **NEVER declare PASS/FAIL for objective criteria.** Read exit code from `validate_dci_*.py`.
4. **Quote numbers verbatim from Python CLI JSON output.** No rounding, no rephrasing of numeric values.
5. **Subjective judgment is permitted ONLY for:** doctoral prose quality, semantic coherence, narrative framing, failure pattern diagnosis.

### 제약 및 절대 기준

- **외부 API 키**: 사용하지 않음 (사용자 명시). Wikidata/GDELT/SemScholar/FRED/Metaculus 모두 skeleton만.
- **Anthropic API**: `claude --print` CLI subprocess로 구독 계정 사용 (ClaudeCodeCLIClient).
- **HF 모델**: 실제 다운로드 허용 (DeBERTa-v3-MNLI 등).
- **CE3 패턴**: 최종 보고서의 모든 숫자는 Python이 계산 → Claude CLI는 prose만 → Python이 재검증.
- **Evidence Ledger**: 모든 `[ev:...]` 마커는 Python이 사전 등록, LLM은 참조만.

### SOT 경로 (v1.0+ canonical)

```yaml
execution:
  runs:
    {run_id}:                         # dci-{date}-{HHMM}
      workflows:
        dci:                          # canonical — independent workflow
          status: in_progress | completed | failed
          phase: preflight | structural | graph_style | reasoning | narrator | review | reporting
          run_id: {run_id}
          date: YYYY-MM-DD
          preflight: { status: PASS, ... }
          layers:
            L-1_external: { status, elapsed_seconds }
            L0_discourse: { ... }
            L1_5_meaning: { ... }     # "L1.5_meaning"의 SM-AW5 인코딩
            ...
          semantic_gates:
            SG-Superhuman: { decision, gates: [...] }
          review: { sg_verdict, evidence_resolution_rate, narrative_parity, pacs_score }
          outputs: { final_report_en, final_report_ko, executive_summary, ... }
```

**Legacy SOT** (read-only, 과거 v0.5 run): `workflows.master.phases.dci.*` — RLM 무결성 위해 영구 보존. `_context_lib.py`가 canonical 우선, legacy fallback으로 스캔.

### Public Narrative — 3-Layer 일반인 소비 레이어 (ADR-080)

기술적 아티팩트 위에 얹히는 **일반인 친화 translation layer**. `facts_pool.json`을 단일 진실 원천 삼아 Claude CLI가 프로즈를 쓰고 Python이 재검증.

| Layer | 이름 | 질문 | FKGL | 금지 |
|-------|------|------|------|------|
| L1 | 해석 (Interpretation) | "이게 무슨 뜻?" | ≤ 9 | 미래 예측 |
| L2 | 통찰 (Insight) | "무슨 패턴?" | ≤ 12 | L1 반복 |
| L3 | 미래 (Future) | "앞으로는?" | ≤ 13 | 확정적 예측 |

**8 PUB P1 검증** (모두 Python 결정론):
- PUB1 파일 존재 · PUB2 Korean-aware grade · PUB3 jargon ratio
- PUB4 숫자 parity (facts_pool.numbers 화이트리스트)
- PUB5 [ev:xxx] 화이트리스트 (facts_pool.allowed_markers)
- PUB6 필수 섹션 · PUB7 금지어(반드시/100%/확실히) · PUB8 EN↔KO 구조 parity

**실행**:
```bash
python3 .claude/hooks/scripts/generate_public_layers.py --date 2026-04-14
# 또는 슬래시 커맨드
/generate-public-layers
```

**대시보드**: `📋 Run Summary` 탭 → `📖 일반인용 3-Layer 해석` 섹션 (3카드 + EN/KO 토글 + 재생성)

**산출물**: `reports/public/{date}/{interpretation,insight,future}.md` + `.ko.md` + `facts_pool.json` + `generation_metadata.json`

### WF5 Personal Newspaper — 나만의 신문 (ADR-083)

W1 raw 코퍼스를 소비해 **NYTimes-style HTML 신문**을 발행하는 독립 워크플로우. Meta-Orchestrator 무개입. DCI와 같은 standalone 패턴.

**15 편집 원칙**: P1 완전 지리 커버리지 · P2 Balance Code (30% 상한) · P3 계층 일관성 · P4 고정 분량(±20%) · P5 3-Tier (global/local/weak_signal) · P6 Source Triangulation · P7 STEEPS 균형 · P8 CE4 증거 · P9 Fact/Context/Opinion 분리 · P10 Confidence Level · P11 한국어 일차 · P12 미래학자 관점 · P13 Dark Corners · P14 No Clickbait · P15 No Single-Source · P16 No Algorithmic Amplification.

**17 Agent Team**: Chief Editor + 6 Continental Desks + 6 STEEPS Section Desks + 4 Specialty (dark-corner-scout, fact-triangulator, future-outlook-writer, copy-editor).

**발행 주기**:
- 일간 (매일 cron): 135,000 단어, `newspaper/daily/{date}/` — 9시간 읽기
- 주간 (일요일, ≥4 daily 후): 205,000 단어, `newspaper/weekly/{YYYY-W##}/`

**실행**:
```bash
/run-newspaper-only                 # 오늘 일간 생성
/run-newspaper-weekly --week 2026-W16  # 주간 (Sunday 02:00 자동)
# Skeleton (HTML 골격만, LLM 미호출, ~3초)
python3 scripts/reports/generate_newspaper_daily.py \
  --date 2026-04-14 --skeleton-only --project-dir .
```

**검증**: `validate_newspaper.py` NP1-NP12 (파일 존재·단어 수·대륙 커버리지·STEEPS·Dark Corners·Triangulation·CE4 밀도·F/C/O 구조·Confidence 라벨·금지어·단일소스·HTML 유효성).

**대시보드**: `📰 Newspaper (WF5)` 탭 — iframe으로 HTML 직접 렌더, Daily/Weekly sub-tab, 6 KPI 카드 + 📄 풀텍스트.

**SOT**: `execution.runs.{id}.workflows.newspaper.*` canonical. `newspaper` actor 신설.

### Chart Interpretations — 대시보드 탭별 3-Layer 해석 (ADR-082)

각 대시보드 탭 상단에 🌱 해석 / 💡 인사이트 / 🔮 미래통찰 카드를 자동 렌더. `run_daily.sh` Step 6.6에서 생성되어 `data/analysis/{date}/interpretations.json`으로 저장됨. 6 탭(Overview·Topics·Sentiment·TimeSeries·WordCloud·W3Insight)만 LLM 생성 대상이고, Article Explorer는 인터랙티브이므로 Python-only 처리, DCI 탭은 기존 `dci_layer_summary.py` 출력을 surface.

**실행**:
```bash
/generate-chart-interpretations              # 오늘 6 탭 전체 생성
python3 scripts/reports/generate_chart_interpretations.py \
  --date 2026-04-14 --only overview,topics   # 특정 탭만
```

**검증**: CI1(구조)·CI2(FKGL)·CI3(숫자 parity)·CI4(마커 whitelist)·CI5(금지어)·CI6(cross-tab refs). 모두 Public Narrative validator 재사용.

### 일일 cron 내러티브 체인 (ADR-081)

`run_daily.sh` 는 `main.py --mode full` 실행 후 다음 순서로 내러티브 보고서를 자동 생성한다:

```
Step 4 (pipeline: crawl → analyze → insight) 성공 시
  ├─ Step 6.3  @analysis-reporter    → workflows/analysis/outputs/analysis-report-{date}.md
  ├─ Step 6.4  @insight-narrator     → data/insights/{run_id}/synthesis/insight_report.md (refined)
  ├─ Step 6.45a src.reports.w4_appendix    → reports/final/integrated-report-{date}.md에 append
  ├─ Step 6.45b src.reports.dci_layer_summary → data/dci/runs/{run_id}/final_report.md에 append
  ├─ Step 6.5  Public Narrative 3-Layer  → reports/public/{date}/ (기존 ADR-080)
  ├─ Step 6.6  Chart Interpretations (NEW, ADR-082) → data/analysis/{date}/interpretations.json
  ├─ Step 6.6b @interp-fact-auditor 의미 교차 검증 (optional, WARNING-only)
  ├─ Step 7   Personal Newspaper daily (NEW, ADR-083) → newspaper/daily/{date}/
  └─ Step 7b  Personal Newspaper weekly (Sunday 만, ≥4 daily 필요)
```

> **PIPELINE_TIMEOUT 4h → 8h 확장**: ADR-083 WF5가 14 editorial desk × Claude CLI 호출로 ~1.5-2h 추가. `run_daily.sh:PIPELINE_TIMEOUT=28800`.

에이전트 호출은 `scripts/reports/invoke_claude_agent.py` 범용 wrapper가 수행. 에이전트 MD를 Single Source of Truth로 두고 런타임 파싱. 실패 시 WARNING만 출력하고 파이프라인 본체는 계속 진행.

**기존 agent-orchestrated 경로** (`/run-chain`)는 변경 없음 — Meta-Orchestrator가 각 워크플로우 오케스트레이터 호출하는 경로 유지.

### Agent/CLI 매핑

| 역할 | 기술 |
|------|------|
| SOT 단일 writer | `@dci-execution-orchestrator` |
| Phase 1 Preflight | `validate_dci_preflight.py` (CLI only — 할루시네이션 봉쇄) |
| Phase 6 Review (Team) | `@dci-sg-superhuman-auditor` + `@dci-evidence-auditor` + `@dci-narrative-reviewer` |
| Phase 7 Reporter | `dci_executive_summary.py` (CLI only) |
| Phase 7 Translation | `@translator` (기존 재사용) |
| Phase transition gate | `dci_gates.py` |
| Retry budget | `dci_retry_budget.py` |
| Failure isolation | `src/dci/failure_policy.py` (14-layer 결정론 매트릭스) |
| Resumability | `src/dci/resume.py` (checkpoint schema v1) |

### 주요 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `No articles available for real-run` | 해당 날짜 `data/raw/{date}/all_articles.jsonl` 없음 | 먼저 `--mode crawl --date X` 실행 |
| `claude CLI binary not found` | Claude Code 로그인 안 됨 | `claude` CLI 설치 + 로그인 |
| `SM-AW5: segment '.5...' invalid` | dotted layer_id를 SOT에 직접 쓸 때 | sot_manager가 `.`→`_`로 자동 인코딩 |
| `char_coverage < 1.00` | L0/L1/L5/L6 중 하나가 article 스팬을 추가 안 함 | orchestrator가 whitespace-only body 제외하는지 확인 |
