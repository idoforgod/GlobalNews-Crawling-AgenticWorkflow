# GlobalNews — 뉴스 크롤링 & 빅데이터 분석 시스템

> **112개 국제 뉴스 사이트 자동 수집 → 8단계 NLP 분석 → 7모듈 통찰 분석 → 증거 기반 미래 인텔리전스**

| 항목 | 내용 |
|------|------|
| **시스템 유형** | Staged Monolith — Python 3.13, Dual Workflow (A: 수집+분석, B: 통찰+인텔리전스) |
| **산출물** | Parquet (ZSTD) + SQLite (FTS5) + 증거 기반 인텔리전스 + 리스크 경보 |
| **실행 환경** | MacBook M2 Pro, 128GB RAM, Claude API $0 |
| **상태** | Production-Ready — 4,230건/일, 14개 언어, NER 79%, 7,635 findings |
| **코드 규모** | 183개 Python 모듈, ~55,900 LOC (src) + ~26,000 LOC (tests) |

---

## 부모-자식 관계

이 프로젝트는 [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) 프레임워크(만능줄기세포)로부터 태어난 **자식 시스템**이다.

- **부모 문서** (`AGENTICWORKFLOW-*.md`): 방법론, 프레임워크, DNA 유전 철학
- **자식 문서** (`GLOBALNEWS-*.md`): 도메인 고유 아키텍처, 운영 가이드

이 분리는 자식 시스템이 **독립적으로 이해·운영**될 수 있게 한다.

---

## 빠른 시작

```bash
# 1. 의존성 설치
pip install -r requirements.txt
playwright install chromium
python -m spacy download en_core_web_sm

# 2. 환경 검증
python3 scripts/preflight_check.py --project-dir . --mode full

# 3. 전체 파이프라인 실행 (크롤링 + 8단계 분석)
python3 main.py --mode full --date 2026-02-27

# 4. 대시보드
streamlit run dashboard.py
```

### 주요 CLI 명령

```bash
python3 main.py --mode crawl --date 2026-04-07        # 크롤링만
python3 main.py --mode analyze --all-stages            # 분석만 (Stage 1-8)
python3 main.py --mode insight --window 30             # 통찰 분석 (Workflow B: 7모듈)
python3 main.py --mode full --dry-run                  # 설정 검증
python3 main.py --mode status                          # 상태 확인
python3 main.py --mode crawl --groups A,B              # 특정 그룹만
```

### Claude Code에서 가장 쉽게 사용하기

```
"시작하자"     → 전체 파이프라인 자동 실행
"통찰 분석"    → Workflow B (빅데이터 통찰)
"상태 확인"    → 현재 결과 조회
```

---

## 시스템 개요

### 116개 뉴스 사이트 (10개 그룹, 14+ 언어)

| 그룹 | 지역 | 사이트 수 | 예시 |
|------|------|----------|------|
| A | 한국 주요 일간지 | 5 | 조선, 중앙, 동아, 한겨레, 연합 |
| B | 한국 경제지 | 4 | 매경, 한경, 파이낸셜, 머니투데이 |
| C | 한국 니치 | 3 | 노컷, 국민, 오마이 |
| D | 한국 IT/과학 | 10 | 38North, Bloter, ZDNet, 전자신문, Insight 등 |
| E | 영어권 | 22 | NYT, FT, WSJ, CNN, Bloomberg, BBC, Guardian 등 |
| F | 아시아-태평양 | 23 | SCMP, Yomiuri, TheHindu, Inquirer, VNExpress 등 |
| G | 유럽/중동 | 38 | Spiegel, LeMonde, Corriere, AlJazeera, Haaretz 등 |
| H | 아프리카 | 4 | AllAfrica, Africanews, TheAfricaReport, Panapress |
| I | 라틴 아메리카 | 8 | Clarin, Folha, ElMercurio, ElTiempo 등 |
| J | 러시아/중앙아시아 | 4 | RIA, RG, RBC, GoGo Mongolia |

### 8단계 NLP 분석 파이프라인 (56개 분석 기법)

```
Stage 1: 전처리 (Kiwi + spaCy)
Stage 2: 피처 추출 (SBERT + TF-IDF + NER)
Stage 3: 기사 분석 (감성 + 감정 + STEEPS)
Stage 4: 집계 (BERTopic + HDBSCAN + Louvain)
Stage 5: 시계열 (STL + PELT + Prophet)
Stage 6: 교차 분석 (Granger + PCMCI)
Stage 7: 신호 분류 (5-Layer + Novelty)
Stage 8: 출력 (Parquet + SQLite)
```

### 5-Layer 신호 분류

| Layer | 이름 | 기간 | 특성 |
|-------|------|------|------|
| L1 | Fad | < 1주 | 급등-급락 패턴 |
| L2 | Short-term | 1-4주 | 단기 트렌드 |
| L3 | Mid-term | 1-6개월 | 구조적 변화 |
| L4 | Long-term | 6개월+ | 장기 전환 |
| L5 | Singularity | 12개월+ | 패러다임 전환 (2-of-3 합의 필요) |

---

## 프로젝트 구조

```
GlobalNews-Crawling-AgenticWorkflow/
├── main.py                      ← CLI 진입점 (crawl/analyze/full/status)
├── dashboard.py                 ← Streamlit 대시보드 (6개 탭)
│
├── src/                         ← 핵심 소스 코드 (171개 모듈, ~48,800 LOC)
│   ├── crawling/                ← 크롤링 엔진 (116개 어댑터 + 안티블록 + DynamicBypassEngine + 페이월 바이패스 + Never-Abandon Multi-Pass)
│   ├── analysis/                ← 8단계 NLP 파이프라인
│   ├── storage/                 ← Parquet + SQLite I/O
│   └── utils/                   ← 로깅, 설정, 에러 처리
│
├── config/                      ← 설정 파일
│   ├── sources.yaml             (116개 사이트)
│   ├── review-focus.yaml        (단계별 리뷰 집중 영역 — Framework config)
│   ├── output-structure.yaml    (단계별 산출물 구조 패턴 — Framework config)
│   └── crontab.txt              (cron 설정 템플릿)
│
├── data/                        ← 날짜별 파티션 데이터
│   ├── raw/YYYY-MM-DD/          (원시 JSONL)
│   ├── processed/               (전처리 Parquet)
│   ├── analysis/                (분석 Parquet)
│   └── output/YYYY-MM-DD/       (최종 출력: Parquet + SQLite)
│
├── scripts/                     ← 운영 스크립트 (32개)
├── tests/                       ← 테스트 (55개 파일, ~2,588 테스트)
│
├── GLOBALNEWS-README.md                       ← 시스템 상세 소개
├── GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md  ← 설계 철학 + 아키텍처 심층
├── GLOBALNEWS-USER-MANUAL.md                  ← 운영 가이드 (CLI, 대시보드, 자동화)
│
├── AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md  ← [부모] 프레임워크 설계 철학
├── AGENTICWORKFLOW-USER-MANUAL.md                  ← [부모] 프레임워크 사용 매뉴얼
├── CLAUDE.md                                       ← [부모] Claude Code 지시서
├── AGENTS.md                                       ← [부모] AI 에이전트 공통 지시서
├── soul.md                                         ← [부모] DNA 유전 철학
└── DECISION-LOG.md                                 ← 설계 결정 로그 (ADR)
```

---

## 실제 실행 결과 (2026-02-27)

| 지표 | 값 |
|------|-----|
| 수집 기사 | 1,286건 (raw JSONL) |
| 처리 기사 | 1,103건 (중복 제거 후) |
| 성공 소스 | 24/44 사이트 (44-site 설정 기준) |
| 토픽 발견 | 44개 토픽 |
| 분석 컬럼 | 21개 (감성, 감정 8차원, STEEPS, 중요도 등) |
| 출력 크기 | analysis.parquet 2.3MB + index.sqlite 6.0MB |
| 지원 언어 | 한국어, 영어, 중국어, 일본어, 프랑스어, 독일어, 아랍어, 히브리어 |

---

## 자동화 (Cron)

```bash
# 일일 실행 (매일 02:00)
0 2 * * * /path/to/scripts/run_daily.sh

# 주간 사이트 점검 (매주 일요일 01:00)
0 1 * * 0 /path/to/scripts/run_weekly_rescan.sh

# 월간 데이터 아카이빙 (매월 1일 03:00)
0 3 1 * * /path/to/scripts/archive_old_data.sh
```

---

## 데이터 쿼리

```python
# DuckDB
import duckdb
duckdb.sql("SELECT source, sentiment_label, COUNT(*) FROM 'data/output/2026-02-27/analysis.parquet' GROUP BY ALL")

# SQLite FTS5
import sqlite3
conn = sqlite3.connect('data/output/2026-02-27/index.sqlite')
conn.execute("SELECT * FROM articles_fts WHERE articles_fts MATCH 'AI AND economy'").fetchall()

# Pandas
import pandas as pd
df = pd.read_parquet('data/output/2026-02-27/analysis.parquet')
df.groupby('topic_label')['sentiment_score'].mean().sort_values()
```

---

## DNA 유전 — 부모 프레임워크로부터 물려받은 것

| DNA 구성요소 | GlobalNews에서의 발현 |
|-------------|---------------------|
| 3단계 구조 | Research (4) → Planning (4) → Implementation (12) |
| SOT 패턴 | `.claude/state.yaml` — Orchestrator만 쓰기 |
| 5계층 QA + SM5 | L0(a-d) Anti-Skip → Pre-L1 /simplify → L1 Verification → L1.5 pACS → L2 Review(+Focus) + SM5 SOT-Level 강제 |
| P1 봉쇄 | 19개 결정론적 검증 스크립트 + SM5 gate evidence guard + 353개 P1 Layer 3 테스트 |
| 전문가 위임 | 32개 전문 서브에이전트, 6개 에이전트 팀 |
| Safety Hooks | 위험 명령·시크릿·SQL 차단(exit 2) + 시크릿 출력 감지(경고) + TDD 보호 + 예측적 디버깅 |
| Context Preservation | 스냅샷 + Knowledge Archive + RLM 복원 + Learned Patterns 표면화 + Phase-Aware Compact + Retry Progress Circuit Breaker |

**도메인 고유 변이**: 4-Level 재시도 (90회, Circuit Breaker 무진전 감지 포함), 116-site Adapter Pattern (10 Groups, A-J), DynamicBypassEngine (12전략, 5-Tier, 7 BlockTypes) + Never-Abandon 루프, **SiteDeadline Fairness Yield** (데드라인 만료 시 워커 양보 → 재큐잉 → 최대 `MULTI_PASS_MAX_EXTRA`(10)회 반복, P1 `deadline_yielded` 플래그로 false completion 봉쇄), **CRAWL_NEVER_ABANDON Multi-Pass**, 5-Layer Signal Hierarchy, Date-Partitioned Storage, HQ Gates (4종 Human-step 품질 검증), Paywall Bypass System (BrowserRenderer + AdaptiveExtractor + is_paywall_body 영어/프랑스어 26패턴), SM5 Quality Gate Evidence Guard, P1 사이트 레지스트리 교차 검증

---

## 문서 가이드

### 자식 시스템 (GlobalNews) 문서

| 문서 | 내용 | 대상 |
|------|------|------|
| **[README.md](README.md)** (이 문서) | 프로젝트 진입점, 빠른 시작 | 처음 접하는 사람 |
| [GLOBALNEWS-README.md](GLOBALNEWS-README.md) | 시스템 상세 소개, 실행 결과, 전체 구조 | 시스템 이해 |
| [GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md](GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md) | 설계 철학, 아키텍처 심층 분석, 선택의 근거 | 설계를 이해하려는 개발자 |
| [GLOBALNEWS-USER-MANUAL.md](GLOBALNEWS-USER-MANUAL.md) | CLI, 대시보드, 자동화, 트러블슈팅 | 시스템을 운영하는 연구자 |

### 부모 프레임워크 (AgenticWorkflow) 문서

| 문서 | 내용 |
|------|------|
| [AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) | 프레임워크 설계 철학 |
| [AGENTICWORKFLOW-USER-MANUAL.md](AGENTICWORKFLOW-USER-MANUAL.md) | 프레임워크 사용 매뉴얼 |
| [soul.md](soul.md) | DNA 유전 철학 |
| [DECISION-LOG.md](DECISION-LOG.md) | 설계 결정 로그 (ADR-001~067) |

---

## 테스트

```bash
pytest                      # 전체 ~2,588 테스트
pytest -m unit              # 단위 테스트
pytest -m "not slow"        # NLP 모델 로딩 제외 (빠른 실행)
```

---

## 라이선스

MIT License. 자세한 내용은 [COPYRIGHT.md](COPYRIGHT.md) 참조.
