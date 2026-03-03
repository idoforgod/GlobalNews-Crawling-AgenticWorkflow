# GlobalNews — 뉴스 크롤링 & 빅데이터 분석 시스템

> **44개 국제 뉴스 사이트 자동 수집 → 56개 NLP 분석 기법 → 5-Layer 신호 분류 → Parquet/SQLite 출력**

| 항목 | 내용 |
|------|------|
| **시스템 유형** | Staged Monolith — Python 3.12 |
| **부모 프레임워크** | [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) (만능줄기세포 — DNA 유전) |
| **산출물** | Parquet (ZSTD) + SQLite (FTS5/vec) + Streamlit 대시보드 |
| **실행 환경** | MacBook M2 Pro, 48GB RAM, Claude API $0 |
| **상태** | Production-Ready — 20/20 단계 완료 |
| **코드 규모** | 91개 Python 모듈, ~41,500 LOC (src) + ~22,000 LOC (tests) |

> **부모-자식 관계**: 이 프로젝트는 AgenticWorkflow 프레임워크(만능줄기세포)로부터 태어난 **자식 시스템**이다.
> 부모 문서(AGENTICWORKFLOW-*.md)는 방법론·프레임워크를, 자식 문서(GLOBALNEWS-*.md)는 **도메인 고유 시스템**을 기술한다.
> 이 분리는 자식 시스템이 독립적으로 이해·운영될 수 있게 한다.

---

## 핵심 스펙

```
INPUT:  44개 뉴스 사이트 (7개 그룹, 9개 언어)
        ├── Group A: 한국 주요 일간지 (5): 조선, 중앙, 동아, 한겨레, 연합
        ├── Group B: 한국 경제지 (4): 매경, 한경, 파이낸셜, 머니투데이
        ├── Group C: 한국 니치 (3): 노컷, 국민, 오마이
        ├── Group D: 한국 IT/과학 (7): 38North, Bloter, 전자, 과학기술, ZDNet, 로봇, 테크니들
        ├── Group E: 미국/영어권 (12): MarketWatch, VOA, HuffPost, NYT, FT, WSJ, LA Times,
        │                                 BuzzFeed, NationalPost, CNN, Bloomberg, AFMedios
        ├── Group F: 아시아-태평양 (6): People's Daily, GlobalTimes, SCMP, TaiwanNews,
        │                                 Yomiuri, TheHindu
        └── Group G: 유럽/중동 (7): TheSun, Bild, LeMonde, MoscowTimes, ArabNews,
                                     AlJazeera, IsraelHayom

PIPELINE:  8단계 NLP 분석 파이프라인 (56개 분석 기법)
        Stage 1: 전처리 (Kiwi + spaCy) ──────────────→ articles.parquet
        Stage 2: 피처 추출 (SBERT + TF-IDF + NER) ───→ embeddings/tfidf/ner.parquet
        Stage 3: 기사 분석 (감성 + 감정 + STEEPS) ───→ article_analysis.parquet
        Stage 4: 집계 (BERTopic + HDBSCAN + Louvain) → topics/networks.parquet
        Stage 5: 시계열 (STL + PELT + Prophet) ──────→ timeseries.parquet
        Stage 6: 교차 분석 (Granger + PCMCI) ────────→ cross_analysis.parquet
        Stage 7: 신호 분류 (5-Layer + Novelty) ──────→ signals.parquet
        Stage 8: 출력 (Parquet + SQLite + DuckDB) ───→ analysis.parquet + index.sqlite

OUTPUT: data/output/YYYY-MM-DD/
        ├── analysis.parquet   (21 columns, 전체 분석 병합)
        ├── signals.parquet    (12 columns, 5-Layer 신호)
        ├── topics.parquet     (7 columns, 토픽 할당)
        ├── index.sqlite       (FTS5 전문 검색 + vec 의미 검색)
        └── checksums.md5      (무결성 검증)
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

## 실제 실행 결과 (2026-02-27)

| 지표 | 값 |
|------|-----|
| 수집 기사 | 1,286건 (raw JSONL) |
| 처리 기사 | 1,103건 (중복 제거 후) |
| 성공 소스 | 24/44 사이트 |
| 토픽 발견 | 44개 토픽 |
| 분석 컬럼 | 21개 (감성, 감정 8차원, STEEPS, 중요도 등) |
| 출력 크기 | analysis.parquet 2.3MB + index.sqlite 6.0MB |
| 지원 언어 | 한국어, 영어, 중국어, 일본어, 프랑스어, 독일어, 아랍어, 히브리어 |

**소스별 수집량 (상위 10)**:
Money Today (630), The Hindu (94), Yonhap (81), Financial Times (79), SCMP (50), Chosun (49), HuffPost (39), People's Daily (39), Bloter (33), Korea Economic Daily (33)

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
├── src/                         ← 핵심 소스 코드 (91개 모듈, ~41,500 LOC)
│   ├── config/                  ← 상수 + 설정 관리
│   │   └── constants.py         (350+ 상수: 경로, 임계값, 스키마)
│   ├── crawling/                ← 크롤링 엔진
│   │   ├── pipeline.py          (크롤링 오케스트레이터)
│   │   ├── network_guard.py     (5-retry HTTP 클라이언트)
│   │   ├── url_discovery.py     (3-Tier: RSS → Sitemap → DOM)
│   │   ├── article_extractor.py (Fundus → Trafilatura → CSS)
│   │   ├── dedup.py             (3-Level: URL → Title → SimHash)
│   │   ├── anti_block.py        (6-Tier 에스컬레이션)
│   │   ├── retry_manager.py     (4-Level 재시도, 최대 90회)
│   │   └── adapters/            (44개 사이트별 어댑터)
│   │       ├── base_adapter.py  (추상 기반 클래스)
│   │       ├── kr_major/        (11: 조선, 중앙, 동아, 한겨레, 연합 등)
│   │       ├── kr_tech/         (8: Bloter, 전자신문, ZDNet 등)
│   │       ├── english/         (12: NYT, FT, WSJ, CNN, Bloomberg 등)
│   │       └── multilingual/    (13: AlJazeera, SCMP, Bild, LeMonde 등)
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
│   ├── sources.yaml             (44개 사이트 설정)
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
├── scripts/                     ← 운영 스크립트 (28개)
│   ├── preflight_check.py       (사전 검증)
│   ├── run_daily.sh             (일일 cron 파이프라인)
│   ├── run_weekly_rescan.sh     (주간 사이트 점검)
│   └── archive_old_data.sh      (월간 아카이빙)
│
├── tests/                       ← 테스트 (40개 파일, ~2,028 테스트)
│   ├── unit/                    (단위 테스트)
│   ├── integration/             (통합 테스트)
│   ├── structural/              (구조 테스트)
│   └── crawling/                (크롤링 테스트)
│
├── research/                    ← Research Phase 산출물
├── planning/                    ← Planning Phase 산출물
└── docs/                        ← 시스템 문서
```

---

## 핵심 차별화 요소

### D1 — Dynamic-First 크롤링

4단계 크롤링 전략: 정적 HTML → DOM 탐색 → 동적 렌더링(Playwright) → 에지 케이스(Patchright). 각 사이트별 맞춤 어댑터로 44개 사이트를 개별 최적화.

### D2 — Never Give Up (4-Level 재시도)

```
Level 1: NetworkGuard ×5 (HTTP 재시도, 지수 백오프)
Level 2: Standard → TotalWar ×2 (모드 전환)
Level 3: Crawler ×3 (라운드, 딜레이 증가)
Level 4: Pipeline ×3 (전체 재시작)
────────────────────────────────────────────
이론적 최대: 5 × 2 × 3 × 3 = 90회 자동 시도
Tier 6: Claude Code 인터랙티브 분석으로 에스컬레이션
```

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
# 전체 테스트 실행 (2,028 테스트)
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
| 5계층 QA | L0(a-d) Anti-Skip → Pre-L1 /simplify → L1 Verification → L1.5 pACS → L2 Review(+Focus) |
| P1 할루시네이션 봉쇄 | 11개 결정론적 검증 스크립트 (10 `validate_*.py` + `diagnose_context.py`) |
| P2 전문가 위임 | 32개 전문 서브에이전트 |
| Safety Hooks | 위험 명령 차단(exit 2) + 시크릿 출력 감지(경고) + TDD 보호 + 예측적 디버깅 |
| Context Preservation | 스냅샷 + Knowledge Archive + RLM 복원 + Learned Patterns 표면화 + Importance-Based Retention + Phase-Aware Compact |

**도메인 고유 변이**: 4-Level 재시도 (90회, Circuit Breaker 무진전 감지 포함), 44-site Adapter Pattern, 5-Layer Signal Hierarchy, Date-Partitioned Storage, Conductor Pattern, HQ Gates (4종 Human-step 품질 검증), Autopilot Mode

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
