# GlobalNews — 뉴스 크롤링 & 빅데이터 분석 시스템

> **116개 국제 뉴스 사이트 자동 수집 → 56개 NLP 분석 기법 → 5-Layer 신호 분류 → Parquet/SQLite 출력**

| 항목 | 내용 |
|------|------|
| **시스템 유형** | Staged Monolith — Python 3.12 |
| **부모 프레임워크** | [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) (만능줄기세포 — DNA 유전) |
| **산출물** | Parquet (ZSTD) + SQLite (FTS5/vec) + Streamlit 대시보드 |
| **실행 환경** | MacBook M2 Pro, 128GB RAM, Claude API $0 |
| **상태** | Production-Ready — 20/20 단계 완료, Dual Workflow (A+B), 증거 기반 인텔리전스 |
| **코드 규모** | 183개 Python 모듈, ~55,900 LOC (src) + ~26,000 LOC (tests) |
| **최근 성과** | 4,230건/일 수집, 8단계+7모듈 분석 완료 (2026-04-07) |

> **부모-자식 관계**: 이 프로젝트는 AgenticWorkflow 프레임워크(만능줄기세포)로부터 태어난 **자식 시스템**이다.
> 부모 문서(AGENTICWORKFLOW-*.md)는 방법론·프레임워크를, 자식 문서(GLOBALNEWS-*.md)는 **도메인 고유 시스템**을 기술한다.
> 이 분리는 자식 시스템이 독립적으로 이해·운영될 수 있게 한다.

---

## 핵심 스펙

```
INPUT:  116개 뉴스 사이트 (10개 그룹, 14+ 언어)
        ├── Group A: 한국 주요 일간지 (5): 조선, 중앙, 동아, 한겨레, 연합
        ├── Group B: 한국 경제지 (4): 매경, 한경, 파이낸셜, 머니투데이
        ├── Group C: 한국 니치 (3): 노컷, 국민, 오마이
        ├── Group D: 한국 IT/과학 (10): 38North, Bloter, 전자, 과학기술, ZDNet,
        │                                  로봇, 테크니들, Insight, Stratechery, Techmeme
        ├── Group E: 영어권 (22): NYT, FT, WSJ, CNN, Bloomberg, BBC, Guardian,
        │                           Wired, HuffPost, MarketWatch, Politico EU 등
        ├── Group F: 아시아-태평양 (23): SCMP, Yomiuri, Mainichi, TheHindu,
        │                                  Inquirer, JakartaPost, VNExpress 등
        ├── Group G: 유럽/중동 (38): LeMonde, Spiegel, Corriere, ElPais,
        │                              AlJazeera, Haaretz, France24 등
        ├── Group H: 아프리카 (4): AllAfrica, Africanews, TheAfricaReport, Panapress
        ├── Group I: 라틴 아메리카 (8): Clarin, Folha, ElMercurio, ElTiempo 등
        └── Group J: 러시아/중앙아시아 (4): RIA, RG, RBC, GoGo Mongolia

PIPELINE:  8단계 NLP 분석 파이프라인 (56개 분석 기법)
        Stage 1: 전처리 (Kiwi + spaCy) ──────────────→ articles.parquet
        Stage 2: 피처 추출 (SBERT + TF-IDF + NER) ───→ embeddings/tfidf/ner.parquet
        Stage 3: 기사 분석 (감성 + 감정 + STEEPS) ───→ article_analysis.parquet
        Stage 4: 집계 (BERTopic + HDBSCAN + Louvain) → topics/networks.parquet
        Stage 5: 시계열 (STL + PELT + Prophet) ──────→ timeseries.parquet
        Stage 6: 교차 분석 (Granger + PCMCI) ────────→ cross_analysis.parquet
        Stage 7: 신호 분류 (5-Layer + Novelty) ──────→ signals.parquet
        Stage 8: 출력 (Parquet + SQLite + DuckDB) ───→ analysis.parquet + index.sqlite

OUTPUT (Workflow A): data/output/YYYY-MM-DD/
        ├── analysis.parquet   (21 columns, 전체 분석 병합)
        ├── signals.parquet    (12 columns, 5-Layer 신호)
        ├── topics.parquet     (7 columns, 토픽 할당)
        ├── index.sqlite       (FTS5 전문 검색 + vec 의미 검색)
        └── checksums.md5      (무결성 검증)

INSIGHT (Workflow B): data/insights/{run_id}/
        ├── crosslingual/      (정보 비대칭, 필터 버블, 감성 편향)
        ├── narrative/         (프레임 진화, 정보 흐름 토폴로지, 미디어 건강도)
        ├── entity/            (엔티티 궤적, 숨은 연결, 출현 가속)
        ├── temporal/          (정보 전파 속도, 관심 감쇠, 주기성)
        ├── geopolitical/      (양자관계, 소프트파워, 갈등-협력 비율)
        ├── economic/          (EPU 불확실성, 섹터 감성, 내러티브 경제학)
        └── synthesis/
            ├── insight_report.md
            ├── key_findings.json
            └── intelligence/  (M7 확장: 증거 기반 미래 인텔리전스)
                ├── entity_profiles.parquet    (엔티티별 감성 프로파일)
                ├── pair_tensions.parquet      (양자관계 긴장 추적)
                ├── evidence_articles.parquet  (증거 기사 매칭)
                └── risk_alerts.parquet        (임계점 경보)
```

---

## 빠른 시작

### 사전 요구사항

- Python 3.12+
- macOS (Apple Silicon) 또는 Linux
- 디스크 여유 공간 ≥ 5GB (ML 모델 포함)

### 설치

```bash
# 1. 의존성 설치 (44+ 패키지)
pip install -r requirements.txt

# 2. Playwright 브라우저 설치 (JS 렌더링 사이트용)
playwright install chromium

# 3. spaCy 영어 모델 다운로드
python -m spacy download en_core_web_sm

# 4. 환경 검증
python3 scripts/preflight_check.py --project-dir . --mode full
```

### 실행

```bash
# 전체 파이프라인 (크롤링 + 8단계 분석)
python3 main.py --mode full --date 2026-02-27

# 크롤링만
python3 main.py --mode crawl --date 2026-02-27

# 분석만 (기존 크롤 데이터 필요)
python3 main.py --mode analyze --all-stages

# 특정 사이트/그룹만
python3 main.py --mode crawl --sites chosun,yna --date 2026-02-27
python3 main.py --mode crawl --groups A,B --date 2026-02-27

# 설정 검증 (Dry Run)
python3 main.py --mode full --dry-run

# 빅데이터 통찰 분석 (Workflow B: 7모듈 + M7 인텔리전스)
python3 main.py --mode insight --window 30 --end-date 2026-04-07

# 상태 확인
python3 main.py --mode status
```

### 대시보드

```bash
# Streamlit 대시보드 실행 (6개 탭)
streamlit run dashboard.py
```

대시보드 탭: Overview | Topics | Sentiment & Emotions | Time Series | Word Cloud | Article Explorer

### 자동화 (Cron)

```bash
# 일일 실행 (매일 02:00)
0 2 * * * /path/to/scripts/run_daily.sh

# 주간 사이트 점검 (매주 일요일 01:00)
0 1 * * 0 /path/to/scripts/run_weekly_rescan.sh

# 월간 데이터 아카이빙 (매월 1일 03:00)
0 3 1 * * /path/to/scripts/archive_old_data.sh
```

---

## 실제 실행 결과 (2026-04-07, 최신)

| 지표 | 이전 (2026-02) | 최신 (2026-04-07) | 변화 |
|------|-------------|-----------------|------|
| 수집 기사 | 1,286건 | **4,230건** | +229% |
| 활성 사이트 | 24/44 | **111/112** | +362% |
| NER 추출률 | 0% | **79~89%** | 다국어 NER 수정 |
| 감성 neutral | 72% | **33%** | 다국어 감성 수정 |
| 토픽 발견 | 44개 | **52개** | |
| 크롤링 시간 | 12.5h+ | **~5h** | -60% |
| 분석 시간 | 미완료 | **73분** (8단계) | |
| 통찰 분석 | 없음 | **7모듈, 7,635 findings** | Workflow B 추가 |
| 인텔리전스 | 없음 | **100 엔티티, 224 양자쌍, 2 경보** | M7 확장 |
| 지원 언어 | 8개 | **14개** | +6 언어 |
| 출력 크기 | 8.3MB | **35MB** (Parquet+SQLite) | |

**Workflow A (수집+분석)**: 4,230건 기사 → 8단계 NLP → Parquet/SQLite
**Workflow B (통찰 분석)**: 52,764건 누적 → 7모듈(교차언어/내러티브/엔티티/시간/지정학/경제/종합) → 27개 지표
**M7 인텔리전스**: 증거 기반 미래 인텔리전스 — 엔티티 프로파일, 양자관계 긴장, 리스크 경보

**소스별 수집량 (상위 10)**:
Aftonbladet (182), ABC Spain (127), Rossiyskaya Gazeta (109), HuffPost (105), Yonhap (101), Money Today (101), El Universal MX (98), La Tercera (97), El Pais (96), Economic Times (85)

---

## 프로젝트 구조

```
GlobalNews-Crawling-AgenticWorkflow/
├── main.py                      ← CLI 진입점 (crawl/analyze/full/status)
├── dashboard.py                 ← Streamlit 대시보드 (6개 탭)
├── requirements.txt             ← 44+ Python 의존성
├── pyproject.toml               ← 프로젝트 메타데이터 + 린터 설정
├── pytest.ini                   ← 테스트 설정
│
├── src/                         ← 핵심 소스 코드 (171개 모듈, ~48,800 LOC)
│   ├── config/                  ← 상수 + 설정 관리
│   │   └── constants.py         (350+ 상수: 경로, 임계값, 스키마)
│   ├── crawling/                ← 크롤링 엔진
│   │   ├── pipeline.py          (크롤링 오케스트레이터)
│   │   ├── network_guard.py     (5-retry HTTP 클라이언트)
│   │   ├── url_discovery.py     (3-Tier: RSS → Sitemap → DOM)
│   │   ├── article_extractor.py (Fundus → Trafilatura → CSS + 페이월 감지)
│   │   ├── browser_renderer.py  (서브프로세스 Patchright/Playwright 렌더링)
│   │   ├── adaptive_extractor.py (4-stage CSS 적응형 추출)
│   │   ├── dedup.py             (3-Level: URL → Title → SimHash)
│   │   ├── anti_block.py        (6-Tier 에스컬레이션 + DynamicBypassEngine)
│   │   ├── retry_manager.py     (4-Level 재시도, 최대 90회, 12개 전략)
│   │   └── adapters/            (116개 사이트별 어댑터)
│   │       ├── base_adapter.py  (추상 기반 클래스)
│   │       ├── kr_major/        (12: 조선, 중앙, 동아, 한겨레, 연합 등)
│   │       ├── kr_tech/         (10: Bloter, 전자신문, ZDNet, Insight 등)
│   │       ├── english/         (22: NYT, FT, WSJ, CNN, Bloomberg, BBC 등)
│   │       └── multilingual/    (72: AlJazeera, SCMP, Spiegel, Corriere 등)
│   ├── analysis/                ← 8단계 NLP 파이프라인
│   │   ├── pipeline.py          (분석 오케스트레이터)
│   │   ├── stage1_preprocessing.py   (전처리: Kiwi + spaCy)
│   │   ├── stage2_features.py        (피처: SBERT + TF-IDF + NER)
│   │   ├── stage3_article_analysis.py (분석: 감성 + 감정 + STEEPS)
│   │   ├── stage4_aggregation.py     (집계: BERTopic + HDBSCAN)
│   │   ├── stage5_timeseries.py      (시계열: STL + PELT + Prophet)
│   │   ├── stage6_cross_analysis.py  (교차: Granger + PCMCI)
│   │   ├── stage7_signals.py         (신호: 5-Layer 분류)
│   │   └── stage8_output.py          (출력: Parquet + SQLite)
│   ├── storage/                 ← 데이터 I/O
│   │   ├── parquet_writer.py    (ZSTD 압축 + 원자적 쓰기)
│   │   └── sqlite_builder.py   (FTS5 + vec 인덱스)
│   └── utils/                   ← 유틸리티
│       ├── logging_config.py    (구조화 로깅)
│       ├── config_loader.py     (YAML 로딩 + 검증)
│       ├── error_handler.py     (예외 계층 + 재시도 데코레이터)
│       └── self_recovery.py     (자기 복구 메커니즘)
│
├── config/                      ← 설정 파일
│   ├── sources.yaml             (116개 사이트 설정)
│   ├── review-focus.yaml        (단계별 리뷰 집중 영역 — Framework config)
│   ├── output-structure.yaml    (단계별 산출물 구조 패턴 — Framework config)
│   └── crontab.txt              (cron 설정 템플릿)
│
├── data/                        ← 날짜별 파티션 데이터
│   ├── raw/YYYY-MM-DD/          (원시 JSONL)
│   ├── processed/YYYY-MM-DD/    (전처리 Parquet)
│   ├── features/YYYY-MM-DD/     (피처 Parquet)
│   ├── analysis/YYYY-MM-DD/     (분석 Parquet)
│   ├── output/YYYY-MM-DD/       (최종 출력)
│   ├── models/                  (ML 모델 캐시)
│   ├── logs/                    (실행 로그)
│   └── dedup.sqlite             (전역 중복 제거 DB)
│
├── scripts/                     ← 운영 스크립트 (32개)
│   ├── preflight_check.py       (사전 검증)
│   ├── run_daily.sh             (일일 cron 파이프라인)
│   ├── run_weekly_rescan.sh     (주간 사이트 점검)
│   └── archive_old_data.sh      (월간 아카이빙)
│
├── tests/                       ← 테스트 (55개 파일, ~2,588 테스트)
│   ├── unit/                    (단위 테스트)
│   ├── integration/             (통합 테스트)
│   ├── structural/              (구조 테스트 + D-7 동기화 검증)
│   └── crawling/                (크롤링 테스트)
│
├── research/                    ← Research Phase 산출물
├── planning/                    ← Planning Phase 산출물
└── docs/                        ← 시스템 문서
```

---

## 핵심 차별화 요소

### D1 — Dynamic-First 크롤링 + 페이월 바이패스

5단계 크롤링 전략: 정적 HTML → DOM 탐색 → 동적 렌더링(Playwright/Patchright) → 적응형 CSS 추출(AdaptiveExtractor) → Title-only fallback. 각 사이트별 맞춤 어댑터로 116개 사이트를 개별 최적화. **DynamicBypassEngine**이 7가지 차단 유형별 최적 전략을 5-Tier로 자동 에스컬레이션하며, Phase A(12개 전략 디스패치) → Phase B(TotalWar fallback)의 **Never-Abandon 루프**로 수집률을 극대화한다.

**하드 페이월 사이트** (FT, NYTimes, WSJ, Bloomberg, Le Monde): `BrowserRenderer`가 서브프로세스에서 Patchright를 실행하여 쿠키 없는 "첫 방문" 경험으로 기사 전문 추출. 실패 시 `AdaptiveExtractor`가 4-stage CSS 선택자 전략으로 본문 추출. `is_paywall_body()`가 영어+프랑스어 14개 강력 패턴 + 12개 약한 패턴으로 페이월 잔존 여부를 결정론적으로 판별.

### D2 — Never Give Up + Never Abandon (4-Level 재시도 + Fairness Yield)

```
Level 1: NetworkGuard ×5 (HTTP 재시도, 지수 백오프)
Level 2: Standard → TotalWar ×2 (모드 전환)
Level 3: Crawler ×3 (라운드, 딜레이 증가)
Level 4: Pipeline ×3 (전체 재시작)
────────────────────────────────────────────
이론적 최대: 5 × 2 × 3 × 3 = 90회 자동 시도
Never-Abandon: DynamicBypassEngine (Phase A) → TotalWar fallback (Phase B)
Multi-Pass: Fairness Yield → 재큐잉 → 최대 MULTI_PASS_MAX_EXTRA(10)회 반복
Tier 6: Claude Code 인터랙티브 분석으로 에스컬레이션
```

**SiteDeadline Fairness Yield**: 각 사이트에 동적 타임아웃(최대 900초)이 할당된다. 데드라인 만료 시 사이트를 **포기하지 않고** 현재 워커를 양보(yield)하여 다른 대기 사이트에 워커를 배분한다. 부분 결과는 보존되고, 해당 사이트는 다음 패스에서 새 데드라인과 함께 재시도된다. **P1 `deadline_yielded` 플래그**가 CrawlResult에 결정론적으로 설정되어, yield된 사이트가 완료로 잘못 표시되는 할루시네이션을 원천 봉쇄한다.

### D3 — 한국어 + 다국어 융합 분석

한국어 NLP (Kiwi + KoBERT + KcELECTRA)와 다국어 NLP (spaCy + BART-MNLI + SBERT multilingual)를 단일 파이프라인에서 결합. 교차 언어 토픽 정렬(T43)과 프레임 분석(T44)으로 국제 뉴스 흐름을 비교.

### D4 — 5-Layer 신호 분류

| Layer | 이름 | 기간 | 특성 |
|-------|------|------|------|
| L1 | Fad | < 1주 | 급등-급락 패턴 |
| L2 | Short-term | 1-4주 | 단기 트렌드 |
| L3 | Mid-term | 1-6개월 | 중기 변동 |
| L4 | Long-term | 6개월+ | 장기 전환 |
| L5 | Singularity | 12개월+ | 패러다임 전환, 3개 독립 경로 중 2개 합의 필요 |

---

## 하드 제약 조건

| # | 제약 | 설명 |
|---|------|------|
| C1 | Claude API = $0 | 모든 분석은 로컬 Python 라이브러리만 사용. Claude Code 구독은 오케스트레이션만 |
| C2 | Conductor Pattern | Claude Code가 Python 스크립트 생성 → Bash 실행 → 결과 읽기 → 결정 |
| C3 | 단일 머신 | MacBook M2 Pro에서 전체 파이프라인 실행. 클라우드 GPU 없음 |
| C4 | Parquet/SQLite | 구조화된 데이터 출력. 보고서나 시각화가 아닌 데이터 |
| C5 | 합법 크롤링 | robots.txt 준수, 속도 제한 적용, 개인정보 미수집 |

---

## 데이터 쿼리 예시

### DuckDB (Parquet 직접 쿼리)

```sql
-- 소스별 감성 분포
SELECT source, sentiment_label, COUNT(*) as cnt
FROM read_parquet('data/output/2026-02-27/analysis.parquet')
GROUP BY source, sentiment_label
ORDER BY source, cnt DESC;
```

### SQLite FTS5 (전문 검색)

```python
import sqlite3
conn = sqlite3.connect('data/output/2026-02-27/index.sqlite')
results = conn.execute(
    "SELECT * FROM articles_fts WHERE articles_fts MATCH 'AI AND economy'"
).fetchall()
```

### Pandas (DataFrame 분석)

```python
import pandas as pd
df = pd.read_parquet('data/output/2026-02-27/analysis.parquet')
# 토픽별 평균 감성
df.groupby('topic_label')['sentiment_score'].mean().sort_values()
```

---

## 테스트

```bash
# 전체 테스트 실행 (~2,588 테스트)
pytest

# 카테고리별 실행
pytest -m unit           # 단위 테스트
pytest -m integration    # 통합 테스트
pytest -m structural     # 구조 테스트
pytest -m "not slow"     # 느린 NLP 모델 테스트 제외
```

---

## DNA 유전

이 시스템은 [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) 프레임워크로부터 태어났다.

| DNA 구성요소 | 유전 형태 |
|-------------|----------|
| 3단계 구조 | Research (4단계) → Planning (4단계) → Implementation (12단계) |
| SOT 패턴 | `.claude/state.yaml` — 단일 상태 파일, Orchestrator만 쓰기 |
| 5계층 QA | L0(a-d) Anti-Skip → Pre-L1 /simplify → L1 Verification → L1.5 pACS → L2 Review(+Focus) + SM5 SOT-Level 강제 |
| P1 할루시네이션 봉쇄 | 19개 결정론적 검증 스크립트 (8 `scripts/validate_*.py` + 11 `hooks/scripts/validate_*.py` + `diagnose_context.py` + SM5 gate evidence guard in `sot_manager.py`) + D-7 동기화 테스트 (14개 인스턴스) |
| P2 전문가 위임 | 32개 전문 서브에이전트 |
| Safety Hooks | 위험 명령 차단(exit 2) + 시크릿 출력 감지(경고) + TDD 보호 + 예측적 디버깅 |
| Context Preservation | 스냅샷 + Knowledge Archive + RLM 복원 + Learned Patterns 표면화 + Importance-Based Retention + Phase-Aware Compact |

**도메인 고유 변이**: 4-Level 재시도 (90회, Circuit Breaker 무진전 감지 포함), 116-site Adapter Pattern (10 Groups, A-J), DynamicBypassEngine (12개 전략, 5-Tier, 7 BlockTypes) + Never-Abandon 루프, **SiteDeadline Fairness Yield** (데드라인 만료 시 워커 양보 → 재큐잉 → 바운디드 반복, P1 `deadline_yielded` 플래그로 false completion 봉쇄), **CRAWL_NEVER_ABANDON Multi-Pass** (L4 재시작 후 최대 `MULTI_PASS_MAX_EXTRA`(10)회 반복 크롤링, 미완료 사이트 실패 시 `crawl_exhausted_sites.json` 리포트 생성, CrawlState-first 완료 판정), 5-Layer Signal Hierarchy, Date-Partitioned Storage, Conductor Pattern, HQ Gates (4종 Human-step 품질 검증), Autopilot Mode, Paywall Bypass System (BrowserRenderer + AdaptiveExtractor + is_paywall_body 영어/프랑스어 26패턴), SM5 Quality Gate Evidence Guard (SOT advance 시 verification+pACS 증거 물리적 강제), P1 사이트 레지스트리 교차 검증 (`validate_site_registry_sync.py` — 5개 소스 동기화), ENABLED_DEFAULT SOT 중앙화 (`constants.py` 단일 SOT → 5개 consumer import + `validate_enabled_default_sync.py` ED1-ED7/ED-CROSS 교차 검증)

---

## 문서 가이드

| 문서 | 내용 | 대상 |
|------|------|------|
| **[GLOBALNEWS-README.md](GLOBALNEWS-README.md)** (이 문서) | 시스템 개요, 빠른 시작, 실행 결과 | 처음 접하는 사용자 |
| [GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md](GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md) | 설계 철학, 아키텍처 심층 분석, 선택의 근거 | 시스템을 깊이 이해하려는 개발자 |
| [GLOBALNEWS-USER-MANUAL.md](GLOBALNEWS-USER-MANUAL.md) | 일상 운영 가이드, CLI, 대시보드, 자동화 | 시스템을 운영하는 연구자 |
| [prompt/workflow.md](prompt/workflow.md) | 워크플로우 정의 (20단계 구축 설계도) | 구축 과정을 이해하려는 사람 |
| [AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) | 부모 프레임워크 아키텍처 | 프레임워크 자체에 관심 있는 사람 |

---

## 라이선스

MIT License. 자세한 내용은 [COPYRIGHT.md](COPYRIGHT.md) 참조.
