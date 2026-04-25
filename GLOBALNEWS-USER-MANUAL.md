# GlobalNews User Manual

> **GlobalNews Crawling & Analysis System — 운영 가이드**

이 문서는 완성된 GlobalNews 시스템을 **운영하는 방법**을 안내한다.
시스템 구축 과정(워크플로우 20단계)이 아닌, 구축된 시스템의 **일일 크롤링, 분석, 대시보드 조회, 자동화 설정**에 초점을 둔다.

| 항목 | 내용 |
|------|------|
| **대상** | 이 시스템을 운영하는 연구자, 미래학자, 데이터 분석가 |
| **시스템 상태** | Production-Ready — 통합 워크플로우 (A: 수집+분석, B: 통찰+인텔리전스, BigData Engine: 18문+GTI+Portfolio, DCI: 14계층 박사급 보고서, WF5: 나만의 신문, Public Narrative: 3계층 일반인 해석) |
| **하드웨어** | MacBook M2 Pro, 128GB RAM (16GB 환경에서도 단계별 실행 가능) |
| **핵심 산출물** | Parquet + SQLite (FTS5+vec) · 18-Q 답변 · GTI 시계열 · 신호 포트폴리오 · 3계층 일반인 해석 · 차트 해석 · NYT 스타일 HTML 신문(13.5만 단어) · DCI 박사급 보고서 |
| **일일 성과** | 4,200+건 수집 · 8단계 분석 · 7모듈 통찰 · 18문 응답 · GTI 산출 · 리스크 경보 · 일간 신문 발행 · LLM Wiki 자동 ingest |
| **코드 규모** | src 275 모듈(~80.7K LOC) · tests 109 파일(~41.5K LOC) · 어댑터 123개 · 서브에이전트 107개 · 슬래시 18개 · 로컬 스킬 6개 · 훅 42개 |

---

## 0. 가장 쉬운 사용법 — "시작하자"

Claude Code에서 이 프로젝트를 열고, **"시작하자"** (또는 "start", "시작")라고 입력하면 시스템이 자동으로 작동합니다.

### 무슨 일이 일어나는가?

```
사용자 입력: "시작하자"
       │
       ▼
  [자동 라우팅] workflow.status 확인
       │
       ├── status = "complete" → /run 실행 (실제 시스템 실행)
       │     │
       │     ▼
       │   1. Preflight 검증 (환경, 의존성, 디스크)
       │   2. Dry-run (설정 검증)
       │   3. 크롤링 (112개 사이트, ~3-5시간)
       │   4. 분석 Stage 1-8 (~1시간)
       │   5. 결과 리포트
       │
       └── status ≠ "complete" → /start 실행 (워크플로우 빌드)
```

### 다른 자연어 트리거

| 말하면 | 실행되는 것 | 설명 |
|--------|-----------|------|
| "시작하자", "시작" | `/run` (full mode) | 전체 파이프라인 (크롤링+분석+자동 인사이트 체인) |
| "크롤링 해줘", "뉴스 수집" | `/run` (crawl mode) | 크롤링만 |
| "분석 시작", "빅데이터 분석" | `/run` (analyze mode) | 분석만 (기존 데이터 필요) |
| "통찰 분석", "인사이트" | `/run` (insight mode) | Workflow B (M1-M7 통찰) |
| "DCI 실행", "심층 분석 시작" | `/run-dci-only` | DCI 14계층 독립 워크플로우 |
| "신문 만들어줘" | `/run-newspaper-only` | WF5 일간판 발행 (~135K 단어) |
| "주간 신문" | `/run-newspaper-weekly` | WF5 주간판 발행 (일요일, ≥4 일간판) |
| "일반인 해석" | `/generate-public-layers` | 3계층 Public Narrative 생성 |
| "차트 해석" | `/generate-chart-interpretations` | 6탭 차트 해석 카드 생성 |
| "상태 확인", "결과 확인" | `main.py --mode status` | 현재 상태 조회 |
| "다음 단계", "계속" | 자동 판별 | 현재 상태에 따라 라우팅 |

### 슬래시 커맨드 18개

| 카테고리 | 커맨드 | 용도 |
|---------|--------|------|
| **실행** | `/run` | `main.py` 직접 실행 (cron이 호출) |
| | `/run-chain` | W1→W2→W3→W4 에이전트 오케스트레이션 |
| | `/run-crawl-only`, `/run-analyze-only`, `/run-insight-only` | 단계별 실행 |
| | `/run-dci-only` | DCI 독립 워크플로우 |
| | `/run-newspaper-only`, `/run-newspaper-weekly` | WF5 신문 발행 |
| **보고서** | `/generate-public-layers` | 3계층 Public Narrative |
| | `/generate-chart-interpretations` | 차트 해석 카드 |
| **통합·검토** | `/integrate-results` | W4 Master Integration |
| | `/approve-report` | 후보 보고서 승인 → final 승격 |
| | `/review-research`, `/review-architecture`, `/review-final` | 단계별 리뷰 게이트 |
| **인프라** | `/start` | 워크플로우 상태 기반 자동 분기 |
| | `/install` | 인프라 검증 |
| | `/maintenance` | 주기적 건강 검진 |

### 실행 결과 예시

```
── PIPELINE EXECUTION ─────────────────────────
Mode:         full (crawl + analyze)
Sites:        112/112 enabled
Date:         2026-04-07

[CRAWLING] ~3-5시간
  4,230건 수집 (14개 언어, 111개 사이트)

[ANALYSIS] Stage 1-8 (~73분)
  Stage 1: Preprocessing      97s    4,230건
  Stage 2: Feature Extraction  822s   4,230건
  Stage 3: Article Analysis   3234s   4,230건
  Stage 4-8: 집계/시계열/교차/신호/출력 ~5분

[OUTPUT]
  data/output/2026-04-07/analysis.parquet  (7.8 MB)
  data/output/2026-04-07/index.sqlite      (27 MB)
───────────────────────────────────────────────
```

### 통찰 분석 실행 (Workflow B)

크롤링+분석 완료 후, "통찰 분석"이라고 입력하면 Workflow B가 실행됩니다:

```
── INSIGHT PIPELINE ───────────────────────────
M1: Cross-Lingual     14개 언어 정보 비대칭 분석
M2: Narrative          프레임 진화, 정보 흐름 토폴로지
M3: Entity             엔티티 궤적, 숨은 연결 발견
M4: Temporal           정보 전파 속도, 관심 감쇠 분류
M5: Geopolitical       양자관계, 소프트파워, 갈등-협력
M6: Economic           EPU 불확실성, 섹터 감성
M7: Synthesis          핵심 발견 + 증거 기반 인텔리전스
    └── 엔티티 프로파일 (100개)
    └── 양자관계 긴장 (224쌍)
    └── 증거 기사 매칭 (255건)
    └── 리스크 경보 (임계점 자동 판정)
───────────────────────────────────────────────
```

### 일일 파이프라인 — `scripts/run_daily.sh`의 7단계 후처리 (Step 6.x ~ 7b)

`main.py --mode full` 본체(Step 4) 실행 후, 7개 후처리 단계가 자동으로 연쇄 실행된다. 각 단계는 **idempotent + fail-soft**(실패해도 다음 단계 차단 없음).

| Step | 동작 | 호출 대상 | 출력 |
|------|------|---------|------|
| 6.3 | W2 분석 리포트 | `@analysis-reporter` (Claude CLI) | `workflows/analysis/outputs/analysis-report-{date}.md` |
| 6.4 | W3 인사이트 정제 | `@insight-narrator` | `data/insights/{run_id}/synthesis/insight_report.md` |
| 6.45a | W4 Master 부록 | `src.reports.w4_appendix` | `reports/final/integrated-report-{date}.md` |
| 6.45b | DCI 레이어 요약 | `src.reports.dci_layer_summary` | `data/dci/runs/{id}/final_report.md` (append) |
| 6.5 | Public Narrative 3계층 (ADR-080) | `generate_public_layers.py` | `reports/public/{date}/{interpretation,insight,future}.md` |
| 6.6 | Chart Interpretations 6탭 (ADR-082) | `scripts/reports/generate_chart_interpretations.py` | `data/analysis/{date}/interpretations.json` |
| 6.7 | **BigData Engine** (Enriched · 18문 · GTI · Portfolio · WeeklyMap) | `src.analysis.{question_engine,gti,signal_portfolio,weekly_future_map}` | `data/enriched/`, `data/answers/`, `data/gti/`, `data/signal_portfolio.yaml`, `reports/weekly_future_map/` |
| 6.8 | **LLM Wiki 자동 ingest** (백그라운드, BigData 완료 후) | `llm-wiki-environmentscanning/scripts/auto-wiki-ingest.sh` | 외부 Wiki 저장소 동기화 |
| 7 | WF5 Personal Newspaper 일간판 (ADR-083) | 14-desk Agent Team | `newspaper/daily/{date}/index.html` (~135K 단어) |
| 7b | WF5 주간판 (일요일, ≥4 일간판) | 동일 | `newspaper/weekly/{YYYY-W##}/` |

> **타임아웃**: `PIPELINE_TIMEOUT=28800`(8시간). WF5 추가에 따라 4h → 8h로 확장됨 (ADR-083).

---

## 0.1 시스템이 하는 일 — 전체 흐름 상세

### 크롤링 (Workflow A — 수집)

112개 국제 뉴스 사이트에서 기사를 자동 수집합니다.

```
[URL 발견] RSS → Sitemap → DOM (3-Tier 탐색)
    ↓
[기사 추출] Trafilatura → CSS Adaptive → Browser Rendering (4단계)
    ↓
[중복 제거] URL 정규화 → 제목 유사도 → SimHash (3-Level)
    ↓
[저장] data/raw/YYYY-MM-DD/all_articles.jsonl
```

**핵심 기술**: 4-Level 재시도 시스템으로 최대 90회까지 자동 복구합니다.

| Level | 대상 | 재시도 | 전략 |
|-------|------|--------|------|
| L1 NetworkGuard | HTTP 요청 | 5회 | 지수 백오프 (1~60초) |
| L2 Strategy | 추출 방식 | 2모드 | Standard → TotalWar 에스컬레이션 |
| L3 Crawler | 크롤링 라운드 | 1~3회 | 사이트 크기별 적응형 (소:1, 중:2, 대:3) |
| L4 Pipeline | 파이프라인 | 3회 | 전체 사이트 재순회 |
| **합계** | | **최대 90회** | + Never-Abandon 추가 패스 |

**5-Worker 병렬 실행**: 112개 사이트를 5개 스레드로 동시 크롤링. 사이트별 SiteDeadline(최대 900초)으로 느린 사이트가 빠른 사이트를 차단하지 않습니다.

### NLP 분석 (Workflow A — Stage 1~8)

수집된 기사에 56개 NLP 분석 기법을 적용합니다.

| Stage | 이름 | 핵심 기법 | 산출물 |
|-------|------|----------|--------|
| **1** | 전처리 | Kiwi(한국어 형태소) + spaCy(영어 lemma) + 언어 감지 | articles.parquet |
| **2** | 피처 추출 | SBERT 384차원 임베딩 + TF-IDF + **다국어 NER**(Davlan xlm-roberta) | embeddings/tfidf/ner.parquet |
| **3** | 기사 분석 | 감성(다국어) + **Plutchik 8감정**(joy/trust/fear/surprise/sadness/anger/disgust/anticipation) + STEEPS 6분류 + 중요도 | article_analysis.parquet |
| **4** | 집계 | BERTopic+HDBSCAN 토픽 모델링 + Louvain 엔티티 네트워크 + DTM | topics/networks.parquet |
| **5** | 시계열 | STL 분해(주기=7일) + PELT 변화점 탐지 + Kleinberg 버스트 | timeseries.parquet |
| **6** | 교차 분석 | Granger 인과관계(lag=7일) + PCMCI + 교차 상관 | cross_analysis.parquet |
| **7** | 신호 분류 | 5-Layer(Fad/Short/Mid/Long/Singularity) + Novelty Detection | signals.parquet |
| **8** | 출력 | Parquet 병합 + SQLite FTS5 + DuckDB + checksum | analysis.parquet + index.sqlite |

**감성 분석 모델**: 영어(cardiffnlp/twitter-roberta) + 한국어(KoBERT) + 기타 언어(mDeBERTa zero-shot + twitter-xlm-roberta multilingual). **NER**: Davlan/xlm-roberta-base-ner-hrl (10개 언어 지원).

### 통찰 분석 (Workflow B — 7모듈 + M7 인텔리전스)

수집된 데이터가 축적되면(7~40일 윈도우), 구조적 통찰을 생산합니다.

| 모듈 | 분석 내용 | 핵심 지표 | 미래 예측 활용 |
|------|----------|----------|-------------|
| **M1** Cross-Lingual | 14개 언어 간 정보 비대칭 | JSD 비대칭, Wasserstein 감성편향, 필터버블 | 국가 간 인식 차이 → 외교 갈등 선행지표 |
| **M2** Narrative | 프레임 진화 + 정보 흐름 | 프레임 변화점, HHI 음성지배, 미디어건강도 | 여론 조작 탐지, 프로파간다 감지 |
| **M3** Entity | 엔티티 궤적 + 숨은 연결 | burst/plateau 분류, Jaccard 연결, 출현가속 | 떠오르는 인물/기관 → 다음 뉴스 주인공 예측 |
| **M4** Temporal | 정보 전파 속도 + 관심 감쇠 | 캐스케이드, 속도행렬, 주기성 | 뉴스 수명 예측, 재발 가능성 판단 |
| **M5** Geopolitical | 양자관계 + 소프트파워 | BRI 지수, 갈등/협력 비율, 의제설정력 | 국가 간 관계 악화/개선 추적 |
| **M6** Economic | EPU 불확실성 + 섹터 감성 | 다국어 EPU, 섹터별 감성 모멘텀, 내러티브 | 경제 위기 조기 경보 |
| **M7** Synthesis + Intelligence | 종합 리포트 + **증거 기반 인텔리전스** | 엔티티 프로파일, 양자긴장, 증거기사, 경보 | **실제 기사 본문과 매칭된 미래 예측 근거** |

### M7 인텔리전스 — 결과 해석법

M7이 생산하는 4개 Parquet 파일의 활용법:

**1. entity_profiles.parquet** — 엔티티별 미디어 톤 추적
```
entity: Iran | mentions: 496 | avg_sentiment: -0.232 | neg: 38% | pos: 1%
→ 해석: 이란에 대한 글로벌 미디어 톤이 강한 부정. 긍정 1%는 거의 없음.
→ 활용: avg_sentiment이 -0.4 이하로 떨어지면 군사적 확대 임박 신호.
```

**2. pair_tensions.parquet** — 양자관계 긴장 추적
```
Iran+Israel: 143건 동시출현 | avg_sentiment: -0.306
Trump+Iran: 310건 | avg: -0.236
China+Taiwan: 44건 | avg: -0.051 (잠복 상태)
→ 해석: Iran+Israel이 가장 부정적. China+Taiwan은 아직 잠복.
→ 활용: China+Taiwan이 -0.15 이하면 동아시아 긴장 에스컬레이션 시작.
```

**3. evidence_articles.parquet** — 증거 기사 매칭
```
topic: iran trump israel | evidence_score: 0.87
title: "미군, 이란 하르그섬 군시설 공격" (매일경제)
body: "미군이 이란의 최대 원유 수출 터미널인 하르그섬..."
→ 해석: 인사이트의 실제 근거가 되는 기사. 추상적 수치가 아닌 구체적 증거.
→ 활용: 의사결정자에게 "이 데이터의 근거가 뭐냐"는 질문에 즉시 답변 가능.
```

**4. risk_alerts.parquet** — 임계점 경보
```
type: sector_all_negative | triggered: True
→ 해석: 모든 경제 섹터가 부정적 — 전면적 risk-off 국면.
→ 활용: EPU>0.4 + 전섹터 negative + burst>80% = 3중 경보 발동 시 위기 대응 시작.
```

**경보 임계점** (insights.yaml에서 조정 가능):

| 경보 유형 | 임계점 | 의미 |
|----------|--------|------|
| crisis_sentiment | < -0.40 | 엔티티 쌍 감성이 극도로 부정 → 군사적 확대 |
| epu_critical | > 0.40 | 경제 불확실성 임계 → 경제 위기 전조 |
| burst_ratio_chaos | > 0.80 | burst 엔티티 80% 초과 → 카오스 국면 |
| conflict_ratio | > 0.50 | 갈등 양자쌍 50% 초과 → 글로벌 분극화 |
| blind_spot_drop | > 0.70 | 기사 수 70% 급감 → 관심 사각지대 (미탐지 위험) |

---

## 1. 설치 및 초기 설정

### 1.1 필수 환경

| 항목 | 요구 사항 | 확인 방법 |
|------|----------|----------|
| Python | 3.12 이상, 3.14 미만 | `python3 --version` |
| 디스크 공간 | 20GB+ 여유 | 크롤링 데이터 + NLP 모델 저장 |
| RAM | 16GB 이상 (권장 48GB) | `sysctl hw.memsize` |
| 네트워크 | 인터넷 연결 필수 | 116개 해외 뉴스 사이트 접근 |

### 1.2 설치 절차

```bash
# 1. 프로젝트 클론
git clone <repo-url> GlobalNews-Crawling-AgenticWorkflow
cd GlobalNews-Crawling-AgenticWorkflow

# 2. 가상환경 생성 및 의존성 설치
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 브라우저 엔진 설치 (하드 페이월 사이트용)
# Patchright (권장 — C++ 수준 봇 탐지 우회)
pip install patchright && patchright install chromium
# 또는 Playwright (대안)
playwright install chromium

# 4. NLP 모델 다운로드 (분석 파이프라인용)
python3 -m spacy download en_core_web_sm
python3 -m spacy download ko_core_news_sm

# 5. 환경 검증
python3 scripts/preflight_check.py --project-dir . --mode full --json
```

### 1.3 사전 비행 점검 (Preflight Check)

실행 전 환경이 준비되었는지 검증한다:

```bash
python3 scripts/preflight_check.py --project-dir . --mode full --json
```

점검 항목:
- Python 버전 호환성
- 핵심 의존성 설치 상태 (49+ 패키지)
- 설정 파일 유효성 (`sources.yaml`, `pipeline.yaml`)
- 디스크 공간 충분 여부
- 데이터 디렉터리 구조

출력 예시:

```json
{
  "readiness": "ready",
  "critical_failures": [],
  "degradations": ["patchright missing -- Extreme difficulty sites will be skipped"],
  "enabled_sites": 116,
  "disk_free_gb": 128.5
}
```

| 결과 | 의미 | 다음 행동 |
|------|------|----------|
| `readiness: "ready"` | 모든 준비 완료 | 파이프라인 실행 가능 |
| `readiness: "blocked"` | 필수 항목 실패 | `critical_failures` 확인 후 수정 |
| `degradations` 존재 | 일부 기능 제한 | 대부분의 사이트는 정상 작동 |

> **patchright 미설치**: Extreme 난이도 사이트 5곳(FT, NYTimes, WSJ, Bloomberg, Le Monde)의 하드 페이월 바이패스가 불가능하여 title-only로 수집될 뿐, 나머지 116개 사이트는 RSS/Sitemap으로 정상 크롤링된다. Playwright만 설치해도 기본 브라우저 렌더링은 동작하지만, Patchright의 C++ 수준 봇 탐지 우회 기능은 사용할 수 없다.

---

## 2. CLI 사용법 (main.py)

모든 파이프라인 실행은 `main.py`를 통해 이루어진다.

### 2.1 기본 문법

```bash
python3 main.py --mode <MODE> [OPTIONS]
```

### 2.2 실행 모드

| 모드 | 설명 | 예시 |
|------|------|------|
| `crawl` | 뉴스 크롤링만 실행 | `python3 main.py --mode crawl --date 2026-04-25` |
| `analyze` | 8단계 분석만 실행 | `python3 main.py --mode analyze --all-stages` |
| `full` | 크롤링 + 분석 + 자동 인사이트 체인 (`--skip-insight`로 비활성화) | `python3 main.py --mode full --date 2026-04-25` |
| `insight` | Workflow B (M1-M7) 윈도우 분석 | `python3 main.py --mode insight --window 30 --end-date 2026-04-25` |
| `dci` | DCI 14계층 독립 워크플로우 | `python3 main.py --mode dci --date 2026-04-25` |
| `status` | 시스템 상태 확인 | `python3 main.py --mode status` |

### 2.3 옵션

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--date YYYY-MM-DD` | 대상 날짜 | 오늘 |
| `--sites chosun,yna,...` | 특정 사이트만 크롤링 | 전체 활성 사이트 |
| `--groups A,B,...` | 특정 그룹만 크롤링 | 전체 그룹 |
| `--stage N` | 특정 분석 스테이지만 실행 (1-8) | - |
| `--all-stages` | 전체 8스테이지 실행 | - |
| `--dry-run` | 설정 검증만 (실제 실행 안 함) | - |
| `--log-level` | 로그 레벨 (DEBUG/INFO/WARNING/ERROR) | INFO |
| `--window N` | 인사이트 윈도우 일수 (`--mode insight` 전용) | 30 |
| `--end-date YYYY-MM-DD` | 인사이트 윈도우 종료일 | 오늘 |
| `--module M1_crosslingual` | 특정 인사이트 모듈만 실행 | 전체 7개 |
| `--skip-insight` | `--mode full`에서 자동 인사이트 체인 비활성화 | False |
| `--run-id dci-...` | DCI run id (`--mode dci` 전용) | `dci-{date}-{HHMM}` |

### 2.4 실행 예시

```bash
# 오늘 날짜로 전체 파이프라인 실행
python3 main.py --mode full

# 특정 날짜 크롤링
python3 main.py --mode crawl --date 2026-02-27

# 한국 주요 언론만 크롤링 (Group A, B)
python3 main.py --mode crawl --groups A,B

# 조선일보, 연합뉴스만 크롤링
python3 main.py --mode crawl --sites chosun,yna

# 분석만 실행 (기존 크롤링 데이터 사용)
python3 main.py --mode analyze --all-stages

# 특정 스테이지만 재실행
python3 main.py --mode analyze --stage 3

# 실행 전 설정 검증 (dry-run)
python3 main.py --mode full --dry-run

# 시스템 상태 확인
python3 main.py --mode status
```

### 2.5 사이트 그룹

`data/config/sources.yaml` 기준 활성화된 **112개 사이트가 10개 그룹**으로 분류된다 (2026-04-25 기준):

| 그룹 | 카테고리 | 사이트 수 | 예시 |
|------|---------|----------|------|
| A | 한국 주요 일간지 | 5 | 조선, 중앙, 동아, 한겨레, 연합 |
| B | 한국 경제지 | 3 | 매경, 한경 등 |
| C | 한국 니치 | 2 | 노컷, 오마이 등 |
| D | 한국 IT/과학 | 8 | 38North, Bloter, ZDNet 등 |
| E | 영어권 | 23 | NYT, FT, WSJ, CNN, Bloomberg, BBC, Guardian, TechCrunch, TheVerge, Ars Technica, 404 Media 등 |
| F | 아시아-태평양 | 20 | SCMP, Mainichi, TheHindu, Inquirer, VNExpress, Globaltimes 등 |
| G | 유럽/중동 | 34 | Spiegel, LeMonde, Corriere, ElPais, AlJazeera, Haaretz 등 |
| H | 아프리카 | 4 | AllAfrica, Africanews, TheAfricaReport, Panapress |
| I | 라틴 아메리카 | 9 | Clarin, Folha, ElMercurio, BioBioChile, ElTiempo 등 |
| J | 러시아/중앙아시아 | 4 | RIA, RG, RBC, GoGo Mongolia |

> **어댑터 파일은 123개**(`src/crawling/adapters/{kr_major:12, kr_tech:11, english:22, multilingual:78}`). 일부 어댑터는 `enabled:false`로 비활성화되어 있다.

---

## 3. 크롤링 파이프라인

### 3.1 크롤링 흐름

```
URL 발견 (RSS/Sitemap/HTML)
    ↓
3-Level 중복 제거
    ├── L1: URL 정규화
    ├── L2: Title Jaccard (0.85)
    └── L3: SimHash (Hamming ≤ 3)
    ↓
기사 추출 (Fundus → Trafilatura → Newspaper4k → CSS)
    │
    ├── 성공 → RawArticle (crawl_tier=1, crawl_method="rss"/"sitemap")
    │
    └── 실패 또는 페이월 감지
            ↓
        페이월 바이패스 (하드 페이월 사이트)
            ├── BrowserRenderer (Patchright 서브프로세스, fresh context)
            │       ↓
            │   추출 체인 재시도 → 성공 시 RawArticle (crawl_tier=3, "playwright")
            │       ↓ 실패
            │   AdaptiveExtractor (4-stage CSS 선택자)
            │       ↓ 성공 시 RawArticle (crawl_tier=5, "adaptive")
            │       ↓ 실패
            │   Title-only fallback (is_paywall_truncated=True)
            │
            └── 4-Level 자동 재시도 (최대 90회)
                ├── NetworkGuard (5회)
                ├── Mode 에스컬레이션 (2단계: RSS → HTML)
                ├── Crawler 에스컬레이션 (3단계: requests → aiohttp → patchright)
                └── Pipeline 에스컬레이션 (3단계: delay → rotate-UA → circuit-break)
    ↓
JSONL 저장 (data/raw/YYYY-MM-DD/)
    ↓
Never-Abandon Multi-Pass (최대 MULTI_PASS_MAX_EXTRA=10회)
    └── 미완료 사이트 재확인 (CrawlState-first)
        └── 있으면 → 새 SiteDeadline 할당 → 재크롤링
        └── 10회 반복 후 미완료 → crawl_exhausted_sites.json 실패 리포트
```

### 3.2 크롤링 결과 확인

```bash
# 수집된 기사 수 확인
wc -l data/raw/$(date +%Y-%m-%d)/all_articles.jsonl

# 크롤링 리포트 확인
cat data/raw/$(date +%Y-%m-%d)/crawl_report.json | python3 -m json.tool

# 사이트별 수집 현황
python3 -c "
import json
from collections import Counter
with open('data/raw/$(date +%Y-%m-%d)/all_articles.jsonl') as f:
    sources = Counter(json.loads(line)['source_id'] for line in f)
for src, cnt in sources.most_common():
    print(f'{src:25s} {cnt:5d}')
"
```

### 3.3 크롤링 실패 대응

| 실패 유형 | 원인 | 자동 대응 | 수동 대응 |
|----------|------|----------|----------|
| HTTP 403/406 | IP/UA 차단 | UA 회전 → 지연 증가 → Circuit Break → DynamicBypassEngine | 사이트 비활성화 (`enabled: false`) |
| RSS 변경 | 피드 URL 변경 | 자동 감지 불가 | `sources.yaml` URL 업데이트 |
| DOM 구조 변경 | 선택자 불일치 | fallback 선택자 시도 | 어댑터 코드 수정 |
| 타임아웃 | 사이트 응답 지연 | SiteDeadline 만료 → Fairness Yield → 다음 패스에서 재시도 | `sources.yaml` 타임아웃 증가 |
| Paywall 추가 | 유료화 전환 | BrowserRenderer(Patchright) → AdaptiveExtractor → title-only fallback | `sources.yaml` paywall 설정 추가 |
| 페이월 false positive | 정상 기사가 페이월로 오감지 | `is_paywall_body()` 패턴은 strong/weak 2단계 분류로 false positive 최소화 | 로그에서 `is_paywall_truncated=True` 확인 후 패턴 조정 |
| 브라우저 렌더링 실패 | Patchright/Playwright 미설치 | 사이트별 3회 연속 실패 시 자동 건너뛰기 (early bail-out) | `playwright install chromium` 또는 `pip install patchright` |
| 데드라인 만료 | 크롤링 시간 초과 | **포기하지 않음** — Fairness Yield로 워커 양보 후, 다음 패스에서 새 데드라인과 함께 재시도 | 정상 동작 — Never-Abandon 패턴에 의해 자동 처리 |

> **Never-Abandon 원칙**: 크롤링 대상으로 지정된 사이트는 어떤 상황에서도 영구적으로 포기하지 않는다. 데드라인 만료, Circuit Breaker OPEN, 연속 실패 등 모든 장애에 대해 시스템이 자동으로 재시도 전략을 에스컬레이션하고, 모든 사이트가 완료될 때까지 Multi-Pass 루프를 반복한다.

### 3.4 크롤링 진행 모니터링

백그라운드 크롤링 실행 시 진행 상황을 모니터링할 수 있다:

```bash
# 크롤링 진행 상태 확인
.venv/bin/python scripts/check_crawl_progress.py [output_file_path]
```

출력 항목:
- **Sites with articles**: 기사를 수집한 사이트 수 / 116 (SOT `config_loader.get_enabled_sites()` 기반)
- **Sites with 0 articles**: 추출 실패 사이트 수
- **Sites in progress**: 현재 크롤링 중인 사이트 수
- **Deadline yields**: Fairness Yield로 일시 중단된 사이트 수
- **Never-abandon passes**: Multi-Pass 루프 횟수 / `MULTI_PASS_MAX_EXTRA`(10)
- **Retry cap reached**: 재시도 캡에 도달한 사이트 수
- **Freshness filtered**: 24시간 윈도우 밖 기사 필터링 건수
- **Dedup filtered**: 중복 제거된 기사 건수
- **Bypass Discovery**: 바이패스 시도 사이트별 결과 (OK/BLOCKED)
- **Top sites by articles**: 사이트별 기사 수 상위 15개

### 3.5 크롤링 실패 진단

크롤링 완료 후 실패 사이트를 진단할 수 있다:

```bash
# 실패 리포트 확인 (Never-Abandon 루프 완료 후 생성)
cat data/raw/$(date +%Y-%m-%d)/crawl_exhausted_sites.json | python3 -m json.tool

# 실패 패턴 자동 분류 (14개 카테고리)
.venv/bin/python scripts/diagnose_crawl_failures.py data/raw/$(date +%Y-%m-%d)/
```

**실패 카테고리** (`crawl_exhausted_sites.json`):

| 카테고리 | 의미 | 대응 |
|---------|------|------|
| `discovery_blocked` | URL 발견 자체가 차단됨 (RSS/Sitemap/DOM 모두 실패) | `sources.yaml`의 RSS URL 변경, Bypass Discovery 상태 확인 |
| `extraction_blocked` | URL은 발견했으나 기사 추출 0건 | 어댑터 CSS 선택자 업데이트, 페이월 설정 확인 |
| `partial_timeout` | 일부 기사만 수집 후 시간 초과 | 정상 — 부분 결과 보존됨, 다음 크롤링에서 보완 |

**바이패스 상태 확인**:

```bash
# 사이트별 바이패스 전략 성공률 확인
cat data/config/bypass_state.json | python3 -m json.tool
```

`bypass_state.json`은 크롤링 간 학습 데이터를 영속화하여, 이전에 성공한 전략을 우선 시도한다.

---

## 4. 분석 파이프라인 (8 Stages)

### 4.1 스테이지 개요

| Stage | 이름 | 핵심 기법 | 라이브러리 |
|-------|------|----------|-----------|
| 1 | 전처리 | 토큰화, 불용어 제거, 정규화 | Kiwi (한국어), spaCy (영어) |
| 2 | 특성 추출 | TF-IDF, SBERT 임베딩, NER, 키워드 | sentence-transformers, KeyBERT |
| 3 | 기사별 분석 | 감성, 감정, STEEPS 분류, 편향 | KoBERT, transformers |
| 4 | 집계 분석 | BERTopic, HDBSCAN 클러스터링, 커뮤니티 | BERTopic, HDBSCAN, networkx |
| 5 | 시계열 분석 | STL 분해, PELT 변점, Kleinberg 버스트 | statsmodels, ruptures |
| 6 | 교차 분석 | Granger 인과, PCMCI, 네트워크, 프레임 | tigramite, networkx |
| 7 | 신호 분류 | 5-Layer 분류, 노벨티, 특이점 | 커스텀 규칙 엔진 |
| 8 | 최종 출력 | Parquet 저장, SQLite 인덱스 | PyArrow, sqlite3 |

### 4.2 스테이지별 실행

```bash
# 전체 8스테이지 순차 실행
python3 main.py --mode analyze --all-stages

# 특정 스테이지만 실행 (이전 스테이지 출력 필요)
python3 main.py --mode analyze --stage 3

# 체크포인트 기반: Stage 5부터 재실행
python3 main.py --mode analyze --stage 5
# → Stage 5 결과를 덮어쓰고 다음 스테이지 수동 실행
```

### 4.3 메모리 관리

각 스테이지는 "모델 로드 → 처리 → 저장 → 모델 해제" 패턴으로 메모리를 관리한다:

- **Stage 2** (SBERT): ~2GB 피크 (sentence-transformers 모델 로드)
- **Stage 3** (KoBERT): ~3GB 피크 (transformers 모델 로드)
- **Stage 4** (BERTopic): ~4GB 피크 (HDBSCAN + UMAP)
- **나머지**: < 1GB

48GB RAM 환경에서는 모든 스테이지가 안정적으로 실행된다.
16GB RAM 환경에서도 스테이지별 순차 실행으로 안전하게 처리 가능하다.

### 4.4 5-Layer 신호 분류 (Stage 7)

뉴스 트렌드를 5단계 계층으로 분류한다:

| Layer | 이름 | 지속 기간 | 감지 기준 |
|-------|------|----------|----------|
| L1 | Fad | < 1주 | Kleinberg burst, 급격한 상승+하강 |
| L2 | Short-term | 1-4주 | PELT 변점, 지속적 상승 |
| L3 | Mid-term | 1-6개월 | STL 트렌드 성분, Granger 인과 |
| L4 | Long-term | 6개월+ | 장기 트렌드, 다중 소스 교차 확인 |
| L5 | Singularity | 전례 없음 | 노벨티 점수 > 0.9, 교차 도메인 확산 |

---

## 5. 대시보드 (Streamlit)

이 시스템은 **두 개의 인터랙티브 대시보드 + 한 개의 헬퍼 모듈**로 구성된다:

| 파일 | 줄수 | 역할 | 실행 |
|------|------|------|------|
| **`dashboard.py`** | 2,319 | **종합 대시보드** — 일/월/분기/연 다기간 집계, BigData Engine 결과 통합 | `streamlit run dashboard.py` (port 8501) |
| **`insights_dashboard.py`** | 758 | **Workflow B 전용** — M1-M7 모듈별 상세 시각화 | `streamlit run insights_dashboard.py --server.port 8502` |
| `dashboard_insights.py` | 1,222 | **헬퍼 모듈** (LLM 미사용 결정론적 인사이트 카드 추출) — `dashboard.py`가 import 함. 단독 실행 X | (라이브러리) |

### 5.1 종합 대시보드 — `dashboard.py`

```bash
streamlit run dashboard.py
```

**6개 탭**:

| 탭 | 내용 |
|----|------|
| **Run Summary** | W1→W2→W3→W4 종합 현황 + BigData KPI 6종(Enriched 기사수, Geo 추출률, GTI, 신뢰도 등) + STEEPSS 파이차트 + Geo Top10 + 18문 일람 + GTI 시계열 + Signal Portfolio + Weekly Future Map |
| **Overview** | 빅데이터 KPI + 총 기사 수 + 소스/카테고리/언어 분포 |
| **Topics** | BERTopic 토픽 목록, 비율 파이차트 |
| **Sentiment & Emotions** | 감성 분포 + Plutchik 8감정 히트맵 |
| **Word Cloud** | 한국어 + 영어 워드 클라우드 |
| **🔢 18 Questions** | 18문 인터랙티브 카드, 날짜선택, 히스토리 스택바, GTI 시계열, Portfolio 추적 |

**사이드바 컨트롤**: 기간(Daily / Monthly / Quarterly / Yearly), 기준 날짜.

### 5.2 Workflow B 전용 — `insights_dashboard.py`

```bash
.venv/bin/python -m streamlit run insights_dashboard.py --server.port 8502
```

`data/insights/{run_id}/` 산출물을 읽어 8개 탭으로 시각화: **Overview · M1 Cross-Lingual · M2 Narrative · M3 Entity · M4 Temporal · M5 Geopolitical · M6 Economic · M7 Synthesis**.

### 5.3 다기간 집계

종합 대시보드는 다중 기간(Daily/Monthly/Quarterly/Yearly)을 지원하여 월간 선택 시 해당 월 전체를 자동 병합한다.

---

## 6. 데이터 조회

### 6.1 데이터 디렉터리 구조

```
data/
├── raw/YYYY-MM-DD/                ← 크롤링 원본 (JSONL) — W1
│   ├── all_articles.jsonl         ← 전체 기사 (1행 = 1기사)
│   ├── crawl_report.json          ← 크롤링 리포트
│   └── crawl_exhausted_sites.json ← Never-Abandon 미완료 사이트 (있으면)
├── processed/YYYY-MM-DD/          ← Stage 1-2 전처리
├── features/YYYY-MM-DD/           ← Stage 2 특성 (NER, 임베딩, TF-IDF)
├── analysis/YYYY-MM-DD/           ← Stage 3-7 + interpretations.json (Step 6.6)
├── output/YYYY-MM-DD/             ← Stage 8 최종 산출물
│   ├── articles.parquet           ← 정제 기사 (ZSTD)
│   ├── analysis.parquet           ← 분석 결과 (21 cols)
│   ├── topics.parquet             ← BERTopic 토픽
│   ├── signals.parquet            ← 5-Layer 신호
│   └── index.sqlite               ← FTS5 + sqlite-vec 인덱스
│
├── enriched/YYYY-MM-DD/           ← [NEW] BigData Engine — articles_enriched.parquet (35 fields)
├── answers/YYYY-MM-DD/            ← [NEW] BigData Engine — q01.json ~ q18.json + summary.json
├── gti/                           ← [NEW] BigData Engine — Geopolitical Tension Index
│   ├── YYYY-MM-DD/gti_daily.json
│   └── gti_history.jsonl
├── signal_portfolio.yaml          ← [NEW] BigData Engine — 신호 포트폴리오 단일 SOT (lifecycle 추적)
│
├── insights/{run_id}/             ← Workflow B (M1-M7) — synthesis/insight_report.md + intelligence/*.parquet
├── dci/runs/{run_id}/             ← [NEW] DCI 14계층 출력 + final_report.md
├── domain-knowledge.yaml          ← [NEW] DKS — 구조화된 엔티티/관계 (P1 검증 대상)
├── dedup.sqlite                   ← 중복 제거 DB (전역)
├── models/                        ← 학습된 모델 가중치 (HF 모델 캐시)
└── logs/                          ← 실행 로그
    ├── daily/                     ← 일일 파이프라인 로그 + Step별 분리 로그(w4-appendix, dci-appendix, chart-interp, bigdata-engine, wiki-ingest)
    ├── weekly/                    ← 주간 리스캔 리포트
    ├── errors.log                 ← 에러 로그 (누적)
    └── alerts/                    ← 실패 알림

reports/                           ← 사람이 읽는 보고서
├── public/YYYY-MM-DD/             ← [NEW] Public Narrative 3계층 (interpretation/insight/future + .ko.md)
├── final/                         ← W4 Master Integration (integrated-report-{date}.md)
└── weekly_future_map/YYYY-W##/    ← [NEW] Weekly Future Map (EN + KO)

newspaper/                         ← [NEW] WF5 Personal Newspaper
├── daily/YYYY-MM-DD/index.html    ← 일간판 (~135K 단어)
└── weekly/YYYY-W##/               ← 주간판 (~205K 단어, 일요일만)
```

### 6.2 Parquet 스키마

**articles.parquet** (정제된 기사):

| 컬럼 | 타입 | 설명 |
|------|------|------|
| article_id | string | 고유 ID (source_hash) |
| source_id | string | 소스 식별자 (chosun, bbc 등) |
| url | string | 원문 URL |
| title | string | 기사 제목 |
| content | string | 본문 텍스트 |
| published_at | timestamp | 발행 시각 |
| language | string | 언어 코드 (ko, en, ja 등) |
| category | string | 카테고리 (politics, tech 등) |
| author | string | 저자 |
| word_count | int32 | 단어 수 |
| crawled_at | timestamp | 수집 시각 |
| group | string | 그룹 코드 (A-G) |

**analysis.parquet** (분석 결과):

| 컬럼 | 타입 | 설명 |
|------|------|------|
| article_id | string | 기사 ID (articles.parquet 조인 키) |
| sentiment_score | float64 | 감성 점수 (-1.0 ~ 1.0) |
| sentiment_label | string | 감성 레이블 (positive/negative/neutral) |
| emotions | string (JSON) | Plutchik 8감정 점수 |
| steeps_category | string | STEEPS 분류 (Social/Tech/Economic/Env/Political/Security) |
| keywords | string (JSON) | KeyBERT 추출 키워드 |
| ner_entities | string (JSON) | NER 엔티티 목록 |
| topic_id | int32 | BERTopic 토픽 번호 |
| topic_label | string | 토픽 레이블 (대표 키워드) |
| embedding | binary | SBERT 임베딩 (384차원) |
| bias_score | float64 | 편향 점수 |
| ... | ... | 추가 분석 필드 |

### 6.3 DuckDB로 조회

```python
import duckdb

con = duckdb.connect()

# 소스별 기사 수 집계
con.sql("""
    SELECT source_id, COUNT(*) as cnt
    FROM 'data/output/2026-02-27/articles.parquet'
    GROUP BY source_id
    ORDER BY cnt DESC
""").show()

# 긍정 기사 Top 10
con.sql("""
    SELECT a.title, a.source_id, b.sentiment_score
    FROM 'data/output/2026-02-27/articles.parquet' a
    JOIN 'data/output/2026-02-27/analysis.parquet' b USING (article_id)
    WHERE b.sentiment_label = 'positive'
    ORDER BY b.sentiment_score DESC
    LIMIT 10
""").show()

# 토픽별 기사 수
con.sql("""
    SELECT topic_label, COUNT(*) as cnt
    FROM 'data/output/2026-02-27/topics.parquet'
    GROUP BY topic_label
    ORDER BY cnt DESC
""").show()

# L5 Singularity 신호 검색
con.sql("""
    SELECT signal_label, burst_score, novelty_score, evidence_summary
    FROM 'data/output/2026-02-27/signals.parquet'
    WHERE signal_layer = 'L5_singularity'
""").show()

# 여러 날짜 범위 집계
con.sql("""
    SELECT source_id, COUNT(*) as total
    FROM 'data/output/*/articles.parquet'
    GROUP BY source_id
    ORDER BY total DESC
""").show()
```

### 6.4 Pandas로 조회

```python
import pandas as pd

# 기사 데이터 로드
articles = pd.read_parquet("data/output/2026-02-27/articles.parquet")
analysis = pd.read_parquet("data/output/2026-02-27/analysis.parquet")

# 병합
df = articles.merge(analysis, on="article_id")

# 소스별 평균 감성
print(df.groupby("source_id")["sentiment_score"].mean().sort_values())

# 카테고리별 기사 수
print(df["category"].value_counts())

# 특정 키워드 포함 기사 필터
ai_articles = df[df["title"].str.contains("AI|인공지능", na=False)]
print(f"AI 관련 기사: {len(ai_articles)}건")
```

### 6.5 SQLite 전문 검색 (FTS5)

```python
import sqlite3

con = sqlite3.connect("data/output/2026-02-27/index.sqlite")

# 한국어 전문 검색
results = con.execute("""
    SELECT article_id, title, snippet(articles_fts, 1, '<b>', '</b>', '...', 20)
    FROM articles_fts
    WHERE articles_fts MATCH '인공지능 AND 트렌드'
    ORDER BY rank
    LIMIT 10
""").fetchall()

for row in results:
    print(f"[{row[0]}] {row[1]}")
    print(f"  {row[2]}")

# 영어 전문 검색
results = con.execute("""
    SELECT article_id, title
    FROM articles_fts
    WHERE articles_fts MATCH 'climate change OR global warming'
    LIMIT 10
""").fetchall()

# 토픽 인덱스 조회
topics = con.execute("""
    SELECT topic_id, topic_label, article_count
    FROM topics_index
    ORDER BY article_count DESC
""").fetchall()

con.close()
```

### 6.6 CLI에서 빠른 데이터 확인

```bash
# DuckDB CLI (설치: pip install duckdb-cli 또는 brew install duckdb)
duckdb -c "SELECT source_id, COUNT(*) FROM 'data/output/2026-02-27/articles.parquet' GROUP BY 1 ORDER BY 2 DESC"

# SQLite CLI
sqlite3 data/output/2026-02-27/index.sqlite "SELECT COUNT(*) FROM articles_fts"

# 원본 JSONL 한 줄 확인
head -1 data/raw/2026-02-27/all_articles.jsonl | python3 -m json.tool
```

---

## 7. 자동화 (Cron 설정)

### 7.1 자동화 스크립트 요약

| 스크립트 | 주기 | 시각 | 기능 |
|---------|------|------|------|
| `scripts/run_daily.sh` | 매일 | 02:00 AM | 전체 크롤링 + 분석 |
| `scripts/run_weekly_rescan.sh` | 매주 일요일 | 01:00 AM | 사이트 구조 변경 감지 |
| `scripts/archive_old_data.sh` | 매월 1일 | 03:00 AM | 30일 이상 데이터 아카이빙 |

### 7.2 Cron 등록

```bash
crontab -e
```

아래 내용 추가:

```cron
# GlobalNews -- Daily Pipeline (02:00 AM)
0 2 * * * /path/to/GlobalNews-Crawling-AgenticWorkflow/scripts/run_daily.sh >> /path/to/data/logs/cron/cron-daily.log 2>&1

# GlobalNews -- Weekly Rescan (Sunday 01:00 AM)
0 1 * * 0 /path/to/GlobalNews-Crawling-AgenticWorkflow/scripts/run_weekly_rescan.sh >> /path/to/data/logs/cron/cron-weekly.log 2>&1

# GlobalNews -- Monthly Archive (1st of month, 03:00 AM)
0 3 1 * * /path/to/GlobalNews-Crawling-AgenticWorkflow/scripts/archive_old_data.sh >> /path/to/data/logs/cron/cron-archive.log 2>&1
```

> `/path/to/`를 실제 프로젝트 경로로 변경한다.

### 7.3 일일 파이프라인 (run_daily.sh) 상세

실행 흐름:
1. **Step 1-2**: 가상환경 자동 감지 및 활성화 + 사전 건강 점검 (디스크 공간, 의존성)
2. **Step 3**: 잠금 파일 획득 (동시 실행 방지, `src.utils.self_recovery --acquire-lock daily`)
3. **Step 4**: `main.py --mode full` 실행 (8시간 타임아웃 — `PIPELINE_TIMEOUT=28800`)
4. **Step 5**: 로그 회전 (30일 이상 로그 삭제)
5. **Step 6.x ~ 7b**: 7단계 후처리 체인 (위 §0.1 표 참고)
6. 잠금 파일 해제

Exit codes:
| 코드 | 의미 |
|------|------|
| 0 | 성공 |
| 1 | 파이프라인 실패 |
| 2 | 건강 점검 실패 |
| 3 | 잠금 획득 실패 (다른 인스턴스 실행 중) |
| 4 | 타임아웃 (8시간 초과) |

> **타임아웃 4h → 8h (ADR-083)**: WF5 Personal Newspaper 일간판이 14개 편집국 × Claude CLI 호출로 ~1.5–2h 추가 소요. 후처리 단계는 모두 fail-soft이므로, 일부 단계가 실패해도 본체 파이프라인 exit code에 영향 없음.

```bash
# 수동 실행
scripts/run_daily.sh

# 특정 날짜
scripts/run_daily.sh --date 2026-02-27

# 설정 검증만
scripts/run_daily.sh --dry-run
```

### 7.4 주간 리스캔 (run_weekly_rescan.sh)

사이트 구조 변경을 감지한다:
- RSS 피드 URL 유효성
- DOM 선택자(CSS selector) 작동 여부
- 새 페이월 감지
- HTTP 상태 코드 변화

깨진 사이트가 5개 이상이면 알림 파일을 생성한다.

```bash
# 수동 실행
scripts/run_weekly_rescan.sh

# 리스캔 결과 확인
cat data/logs/weekly/rescan-$(date +%Y-%m-%d).md
```

### 7.5 월간 아카이빙 (archive_old_data.sh)

30일 이상 지난 원본 데이터를 압축 아카이빙한다:

```
data/archive/YYYY/MM/raw-YYYY-MM-DD.tar.gz
data/archive/YYYY/MM/raw-YYYY-MM-DD.tar.gz.sha256
```

- SHA256 체크섬 검증 후에만 원본 삭제
- 아카이빙 실패 시 원본 보존 (데이터 손실 0%)

```bash
# 수동 실행
scripts/archive_old_data.sh

# 60일 기준으로 변경
scripts/archive_old_data.sh --days 60

# 미리보기
scripts/archive_old_data.sh --dry-run
```

---

## 8. 새 사이트 추가

### 8.1 sources.yaml에 사이트 정의 추가

```yaml
# config/sources.yaml 에 추가
new_site:
  group: E                         # A-J 중 적절한 그룹
  meta:
    name: "New Site"
    url: "https://new-site.com"
    language: en
    region: us
    daily_article_estimate: 50
    enabled: true                   # 기본값: constants.py ENABLED_DEFAULT (SOT). validate_enabled_default_sync.py로 동기화 검증
  crawl:
    primary_method: rss            # rss | sitemap | html_listing
    rss_url: "https://new-site.com/rss"
    selectors:
      article_body: "article .content"
      title: "h1.headline"
      date: "time[datetime]"
      author: "span.author"
    rate_limit_rpm: 30
    respect_robots_txt: true
```

### 8.2 어댑터 파일 생성

```python
# src/crawling/adapters/new_site.py
from src.crawling.adapters.base_adapter import BaseAdapter

class NewSiteAdapter(BaseAdapter):
    SOURCE_ID = "new_site"

    def discover_urls(self, date_str: str) -> list[str]:
        return self._discover_via_rss()

    def extract_article(self, url: str) -> dict | None:
        return self._extract_with_newspaper(url)
```

### 8.3 검증

```bash
# 사이트 커버리지 검증
python3 scripts/validate_site_coverage.py --file config/sources.yaml --project-dir .

# 테스트 크롤링 (1개 사이트만)
python3 main.py --mode crawl --sites new_site --log-level DEBUG
```

---

## 9. 모니터링 및 트러블슈팅

### 9.1 로그 위치

| 로그 | 경로 | 설명 |
|------|------|------|
| 일일 파이프라인 | `data/logs/daily/YYYY-MM-DD-daily.log` | 크롤링+분석 전체 로그 |
| 에러 누적 | `data/logs/errors.log` | 모든 에러 집계 |
| 알림 | `data/logs/alerts/` | 실패 시 생성되는 알림 파일 |
| 주간 리스캔 | `data/logs/weekly/rescan-YYYY-MM-DD.md` | 사이트 구조 변경 리포트 |
| cron 출력 | `data/logs/cron/` | cron 실행 stdout/stderr |

### 9.2 일반적 문제와 해결

| 증상 | 원인 | 해결 |
|------|------|------|
| `Lock acquisition failed` | 이전 실행이 아직 진행 중 | `ps aux \| grep main.py` 확인, 필요 시 프로세스 종료 |
| `Pipeline timed out` | 4시간 타임아웃 초과 | 사이트 그룹을 나누어 실행 (`--groups A,B`) |
| `spaCy model not found` | NLP 모델 미설치 | `python3 -m spacy download en_core_web_sm` |
| `ImportError: No module named 'xxx'` | 패키지 미설치 | `pip install -r requirements.txt` |
| 기사 0건 수집 | 네트워크/차단 이슈 | `--log-level DEBUG`로 재실행, 로그에서 HTTP 상태 확인 |
| 분석 Stage N 실패 | 이전 Stage 미실행 | `--all-stages`로 Stage 1부터 순차 실행 |
| `MemoryError` | RAM 부족 | 사이트 수를 줄이거나 (`--groups A`) 스테이지별 실행 |
| `sqlite3.OperationalError: database is locked` | 동시 접근 | 다른 프로세스가 SQLite 사용 중인지 확인 |
| `Retry budget exhausted` | 품질 게이트 재시도 10/15회 소진 | 산출물 품질 근본 원인 분석 후 수동 재작업. ULW 활성 시 15회, 비활성 시 10회 |
| `Circuit breaker OPEN` | 3회 연속 ≤5점 개선 | 동일 접근법 반복 중단. 다른 전략으로 전환하거나 사용자 개입 |
| `Autopilot stall detected` | 동일 단계 20 cycles 경과 | 워크플로우 진행이 멈춘 상태. 품질 게이트 실패 원인 확인 후 수동 개입 |
| `SM5a: verification log missing` | advance-step 시 verification-logs 없음 | L1 Verification Gate를 수행하고 `verification-logs/step-N-verify.md` 생성 후 재시도 |
| `SM5b: pACS log missing` | advance-step 시 pacs-logs 없음 | L1.5 pACS 채점을 수행하고 `pacs-logs/step-N-pacs.md` 생성 후 재시도 |
| `SM5c: pACS score is N (RED zone)` | pACS < 50 상태에서 advance 시도 | 약점 차원 개선 후 재채점. 긴급 시 `--force` 사용 (감사 기록 생성) |
| `SM5d: Review verdict is FAIL` | 리뷰 FAIL 상태에서 advance 시도 | 리뷰 이슈 해결 후 재리뷰. 또는 `--force` 사용 |
| `ENABLED_DEFAULT out of sync` | D-7 Instance 13 비동기화 (7개 파일) | `python3 scripts/validate_enabled_default_sync.py --project-dir .` 실행 후 지적된 파일 동기화 |

### 9.3 Tier 6 수동 개입

90회 자동 재시도가 모두 실패한 사이트가 있을 때:

```bash
# 실패 로그 분석 (Claude Code 내부에서)
Tier 6 분석해줘 [사이트명]
```

Claude Code가 실패 패턴을 분석하고 사이트 특화 우회 코드를 생성한다.

대안:
1. `sources.yaml`에서 해당 사이트 `enabled: false` 설정
2. 어댑터의 `primary_method`를 변경 (예: `rss` → `html_listing`)
3. 커스텀 헤더/쿠키 추가

### 9.4 데이터 무결성 확인

```bash
# Parquet 파일 검증
python3 -c "
import pyarrow.parquet as pq
for f in ['articles', 'analysis', 'topics', 'signals']:
    try:
        t = pq.read_table(f'data/output/2026-02-27/{f}.parquet')
        print(f'{f}.parquet: {t.num_rows} rows, {t.num_columns} cols -- OK')
    except Exception as e:
        print(f'{f}.parquet: ERROR -- {e}')
"

# SQLite 무결성 검사
sqlite3 data/output/2026-02-27/index.sqlite "PRAGMA integrity_check"

# 중복 제거 DB 통계
sqlite3 data/dedup.sqlite "SELECT COUNT(*) FROM url_hashes"
```

---

## 10. Claude Code 통합

워크플로우 구축이 완료된 후에도, Claude Code에서 자연어로 시스템을 제어할 수 있다.

### 10.1 자연어 명령

| 입력 | 실행되는 동작 |
|------|-------------|
| `시작하자` | 전체 파이프라인 실행 (`/run` → `main.py --mode full`) |
| `크롤링 시작` | 크롤링만 실행 |
| `분석을 하자` | 분석만 실행 |
| `상태 확인` | 시스템 상태 표시 |
| `한국 뉴스만` | `--groups A,B`로 크롤링 |
| `결과 확인` | `main.py --mode status` |

### 10.2 `/run` 스킬 실행 프로토콜

Claude Code 내에서 `/run` 또는 시작 트리거를 입력하면:

1. **Preflight Check**: `scripts/preflight_check.py` 실행 → 환경 준비 상태 확인
2. **Dry Run**: `main.py --mode full --dry-run` → 설정 검증
3. **실행**: `main.py --mode full --date YYYY-MM-DD` → 크롤링 + 분석
4. **결과 리포트**: 수집 건수, 분석 결과, 출력 파일 목록 표시
5. **데이터 인벤토리**: 생성된 파일 크기 및 경로 표시

### 10.3 Autopilot Mode

워크플로우 실행 시 `(human)` 단계와 AskUserQuestion을 자동 승인하는 모드이다.

**활성화**:

| 입력 | 동작 |
|------|------|
| `autopilot 모드로 실행` | SOT에 `autopilot.enabled: true` 설정 후 워크플로우 시작 |
| `자동 모드로 워크플로우 실행` | 동일 |
| `autopilot 해제` | SOT에 `autopilot.enabled: false` — 다음 `(human)` 단계부터 수동 전환 |

**HQ Gates (4종 Human-step 품질 검증)**:

| Gate | 검증 내용 |
|------|----------|
| HQ1 | 자동 승인된 단계의 Decision Log 존재 |
| HQ2 | Decision Log P1 검증 통과 (DL1-DL6) |
| HQ3 | SOT `auto_approved_steps` 정합성 |
| HQ4 | 직전 non-human 단계의 verification-logs + pacs-logs 존재 |

**Decision Log**: 자동 승인된 결정은 `autopilot-logs/step-N-decision.md`에 기록된다 (단계, 옵션, 선택 근거).

**SM5 Quality Gate Evidence Guard**: SOT의 `advance-step` 명령 자체에 품질 게이트 증거 검증이 물리적으로 내장되어 있다 (Level A 보호 — LLM이 우회 불가):

| 체크 | 검증 내용 |
|------|----------|
| SM5a | `verification-logs/step-N-verify.md` 존재 확인 |
| SM5b | `pacs-logs/step-N-pacs.md` 존재 확인 |
| SM5c | pACS 점수 ≥ 50 확인 (RED zone 차단, 2-stage 파싱) |
| SM5d | `review-logs/step-N-review.md` 존재 시 FAIL verdict 차단 |

`(human)` 단계(4, 8, 18)는 SM5를 건너뛴다. 긴급 시 `--force` 플래그로 우회 가능하지만, `autopilot-logs/sm5-force-audit.jsonl`에 감사 기록이 남는다.

### 10.4 /start 스킬과 워크플로우 상태 관리

워크플로우 상태에 따라 자동 라우팅된다:

| 워크플로우 상태 | 동작 | 설명 |
|---------------|------|------|
| `status: complete` | `/run` 실행 | 구축 완료 → 실제 시스템 실행 (크롤링 + 분석) |
| 그 외 (`in_progress` 등) | `/start` 실행 | 구축 미완성 → 워크플로우 단계 실행 |

자연어 시작 트리거 (`시작하자`, `start`, `다음 단계` 등)는 이 라우팅 규칙에 따라 적절한 동작을 자동 실행한다.

### 10.5 ULW (Ultrawork) Mode

프롬프트에 `ulw`를 포함하면 철저함 강도(thoroughness intensity)가 최대로 강화된다.

**3가지 강화 규칙**:

| 규칙 | 내용 |
|------|------|
| **I-1. Sisyphus Persistence** | 최대 3회 재시도, 각 시도는 다른 접근법. 100% 완료 또는 불가 사유 보고 |
| **I-2. Mandatory Task Decomposition** | 비-trivial 작업 시 TaskCreate → TaskUpdate → TaskList 필수 |
| **I-3. Bounded Retry Escalation** | 동일 대상 3회 초과 연속 재시도 금지. 초과 시 사용자 에스컬레이션 |

**암묵적 해제**: 새 세션에서 `ulw` 없이 프롬프트를 입력하면 자동 비활성화된다 (명시적 해제 불필요).

**Autopilot과의 관계**: ULW는 철저함 축(HOW THOROUGHLY), Autopilot은 자동화 축(HOW)으로 직교한다. 두 모드를 함께 사용하면 품질 게이트 재시도 한도가 10→15회로 상향된다.

---

## 11. BigData Engine — 18-Question · GTI · Signal Portfolio (Step 6.7)

`scripts/run_daily.sh` Step 6.7에서 BigData Engine이 실행된다. **18개 미래연구 질문에 매일 강제 응답**하고, 지정학 긴장 지수(GTI)를 산출하며, 미래 신호 포트폴리오 생애주기를 추적한다. 데이터 부족일에도 `status:'insufficient_data'`로 응답하여 18문 계약을 유지한다.

### 11.1 18-Question 답변 조회

```bash
# 오늘 18문 요약
cat data/answers/$(date +%Y-%m-%d)/summary.json | python3 -m json.tool

# 특정 질문 (예: Q07 양국 관계 긴장)
cat data/answers/$(date +%Y-%m-%d)/q07.json | python3 -m json.tool

# Python으로 모든 질문 로드
python3 -c "
import json, glob
for f in sorted(glob.glob('data/answers/$(date +%Y-%m-%d)/q*.json')):
    d = json.load(open(f))
    print(f\"{d['question_id']}: {d['title']} ({d['status']})\")
"
```

### 11.2 18개 질문 목록

전체 정의는 `src/analysis/question_engine.py` (line 287~1441)에 있다. 요약:

| ID | 질문 | 비고 |
|----|------|------|
| Q01 | 이 주제는 언제 갑자기 터졌나? (버스트 탐지) | |
| Q02 | 성장 vs 소멸 트렌드는? | |
| Q03 | 사건 전후 보도 구조 변화 | |
| Q04 | 같은 주제를 언어 간 어떻게 다르게 프레이밍하는가? | |
| Q05 | 특정 국가에 대한 글로벌 감성 변화 | |
| Q06 | 국제 뉴스의 'dark corners' (보도 사각지대) | |
| Q07 | 어떤 양국 관계가 급격히 긴장/완화되는가? | |
| Q08 | 부상하는 약한 신호 | |
| Q09 | 패러다임 전환의 전조 | |
| Q10 | 비주류→주류 의제 이동 패턴 | 21일+ 데이터 필요 |
| Q11 | 의제 선점(최초 보도) 언론사 | |
| Q12 | 진보/보수 미디어 강조점 차이 | |
| Q13 | 언어권별 독자적 의제 | |
| Q14 | 미디어 간 보도 격차 | |
| Q15 | 뉴스 감성이 경제 지표를 선행하는가? | 7일+ 데이터 필요 |
| Q16 | 이슈 인과 연쇄 (STEEPS 공기 기반) | |
| Q17 | 동시 급증 이슈 클러스터 | |
| Q18 | 인물·기관·국가 글로벌 의제 중심성 | |

### 11.3 GTI (Geopolitical Tension Index)

```bash
# 오늘 GTI
cat data/gti/$(date +%Y-%m-%d)/gti_daily.json | python3 -m json.tool

# 시계열 히스토리 로드
python3 -c "
import pandas as pd
gti = pd.read_json('data/gti/gti_history.jsonl', lines=True)
print(gti.tail(14)[['date', 'gti_score', 'level']])
"
```

GTI 공식: `40% × G1(Q05 국가별 감성) + 35% × G2(Q06 dark corners) + 25% × G3(Q07 양국 긴장)`. 0–100 스케일. 임계값은 `data/config/`에 정의.

### 11.4 Signal Portfolio (단일 SOT)

`data/signal_portfolio.yaml`은 미래 신호의 생애주기(CANDIDATE → ACTIVE → CONFIRMED → ARCHIVED)를 추적하는 **단일 SOT**이다. Step 6.7에서 매일 갱신.

```bash
python3 -c "
import yaml
p = yaml.safe_load(open('data/signal_portfolio.yaml'))
for s in p['signals']:
    if s['status'] == 'active':
        print(f\"[{s['steeps']}] {s['name']} (since {s['first_seen']})\")
"
```

### 11.5 Weekly Future Map

매주 자동 생성되는 7일 종합 미래 맵 (18문 + GTI + Portfolio).

```bash
ls reports/weekly_future_map/$(date +%Y-W%V)/
# future_map.md      (English original)
# future_map.ko.md   (Korean translation)
```

---

## 12. DCI — Deep Content Intelligence (독립 워크플로우)

DCI는 W1→W2→W3→W4 체인과 **독립**으로 작동한다. `data/raw/{date}/all_articles.jsonl`만 입력으로 사용.

### 12.1 실행

```bash
# 권장 — 에이전트 오케스트레이션
/run-dci-only

# 직접 Python (CI/테스트용)
.venv/bin/python main.py --mode dci --date YYYY-MM-DD --dry-run
.venv/bin/python main.py --mode dci --date YYYY-MM-DD

# 비활성화 (파이프라인 체인에서 제외)
DCI_DISABLED=1 .venv/bin/python main.py --mode full
```

### 12.2 출력

```
data/dci/runs/{run_id}/
├── final_report.md              ← 박사급 종합 보고서 (L10 Doctoral Narrator)
├── final_report.ko.md           ← 한국어 번역 (@translator)
├── executive_summary.md
├── evidence_ledger.jsonl        ← CE4 3계층 증거 체인
├── layers/                      ← L0 ~ L9 중간 산출물
└── sg_superhuman_verdict.json   ← 10-게이트 검증 결과
```

### 12.3 14개 레이어 (L-1 → L11)

| Layer | 역할 | 구현 |
|-------|------|------|
| L-1 | 외부 지식 pre-fetch | skeleton (API 미사용) |
| L0 | 문서 구조화 (Kiwi + 다국어 regex + PDTB + URL) | 순수 Python |
| L1 | 의미 단위 추출 (claims/quotes/numerical/CAMEO) | 순수 Python |
| L1.5 | 의미 표상 (휴리스틱 SPO + FrameNet-lite) | 순수 Python |
| L2 | 관계·화용 (Allen 13-relation + timex3 + hedging) | 순수 Python |
| L3 | 지식 그래프 (NetworkX entity 공기 + community) | 순수 Python |
| L4 | 교차문서 (SimHash + Jaccard thread clustering) | 순수 Python |
| L5 | 심리·문체 (textstat + TTR/MTLD/HDD + Burrows Delta) | 순수 Python |
| **L6** | **Triadic Ensemble (4-lens α/β/γ/δ-critic)** | **Claude CLI** |
| L7 | Graph-of-Thought (Bayesian DAG) | 순수 Python |
| L8 | Monte Carlo (1,024-leaf scenario tree) | 순수 Python |
| L9 | Metacognitive (블라인드 스팟 + 불확실성 분해) | 순수 Python |
| **L10** | **Doctoral Narrator (CE3 숫자 검증)** | **Claude CLI** |
| L11 | Dashboard 탭 표시 | Streamlit |

### 12.4 SG-Superhuman 10-Gate 검증

모든 DCI run은 다음 게이트를 통과해야 PASS:

- G1 `char_coverage = 1.00` — 본문 전수 진입
- G2 `triple_lens_coverage ≥ 3.0` — 문자당 평균 3+ 렌즈
- G3 `llm_body_injection_ratio = 1.00` — L6에 본문 100% 투입
- G4 `technique_completeness = 93/93`
- G5 `nli_verification_pass_rate ≥ 0.95` (DeBERTa-v3-MNLI)
- G6 `triadic_consensus_rate ≥ 0.60`
- G7 `adversarial_critic_pass ≥ 0.90`
- G8 `evidence_3layer_complete = 100%` (CE4 article/segment/char)
- G9 `technique_mode_compliance = 100%`
- G10 `uncertainty_quantified = 100%`

독립 검증:
```bash
python3 .claude/hooks/scripts/validate_dci_sg_superhuman.py --run-id {run_id} --project-dir .
python3 .claude/hooks/scripts/validate_dci_evidence.py --run-id {run_id} --project-dir .
python3 .claude/hooks/scripts/validate_dci_narrative.py --run-id {run_id} --project-dir .
```

상세: `prompt/execution-workflows/dci.md` (7-Phase protocol), `DECISION-LOG.md` ADR-071·072·079.

---

## 13. Public Narrative — 3계층 일반인 해석 (Step 6.5)

기술적 산출물 위에 얹히는 **일반인 친화 translation layer**. `facts_pool.json`을 단일 진실 원천 삼아 Claude CLI가 프로즈를 쓰고 Python이 재검증.

| Layer | 이름 | 질문 | FKGL | 금지 |
|-------|------|------|------|------|
| L1 | 해석 (Interpretation) | "이게 무슨 뜻?" | ≤ 9 | 미래 예측 |
| L2 | 통찰 (Insight) | "무슨 패턴?" | ≤ 12 | L1 반복 |
| L3 | 미래 (Future) | "앞으로는?" | ≤ 13 | 확정적 예측 |

**8개 PUB P1 검증** (모두 Python 결정론):
- PUB1 파일 존재 · PUB2 Korean-aware grade · PUB3 jargon ratio
- PUB4 숫자 parity (`facts_pool.numbers` 화이트리스트)
- PUB5 `[ev:xxx]` 화이트리스트 · PUB6 필수 섹션
- PUB7 금지어 (반드시/100%/확실히) · PUB8 EN↔KO 구조 parity

```bash
# 수동 실행
python3 .claude/hooks/scripts/generate_public_layers.py --date 2026-04-25
# 또는 슬래시 커맨드
/generate-public-layers --date 2026-04-25
# 특정 레이어만 재생성
/generate-public-layers --only L2 --date 2026-04-25
```

산출물: `reports/public/{date}/{interpretation,insight,future}.md` + `.ko.md` + `facts_pool.json` + `generation_metadata.json`. 대시보드 `📋 Run Summary` 탭의 `📖 일반인용 3-Layer 해석` 섹션에서 즉시 표시.

---

## 14. Chart Interpretations — 6탭 차트 해석 카드 (Step 6.6)

각 대시보드 탭 상단에 🌱 해석 / 💡 인사이트 / 🔮 미래통찰 카드를 자동 렌더. 6 탭(Overview·Topics·Sentiment·TimeSeries·WordCloud·W3Insight)만 LLM 생성. Article Explorer는 인터랙티브이므로 Python-only 처리.

```bash
/generate-chart-interpretations              # 오늘 6 탭 전체 생성
python3 scripts/reports/generate_chart_interpretations.py \
  --date 2026-04-25 --only overview,topics   # 특정 탭만
```

**검증** (모두 Python 결정론, Public Narrative validator 재사용):
- CI1 구조 · CI2 FKGL · CI3 숫자 parity · CI4 마커 whitelist · CI5 금지어 · CI6 cross-tab refs

---

## 15. WF5 — Personal Newspaper / 나만의 신문 (ADR-083)

W1 raw 코퍼스를 소비해 NYT-style HTML 신문을 발행하는 독립 워크플로우. 17명의 편집 에이전트 팀 (Chief Editor + 6 Continental Desks + 6 STEEPS Section Desks + 4 Specialty).

### 15.1 발행 주기

| 판본 | 주기 | 분량 | 경로 |
|------|------|------|------|
| 일간 | 매일 (cron) | ~135,000 단어 (≈9시간 읽기) | `newspaper/daily/{date}/` |
| 주간 | 일요일, ≥4 일간판 후 | ~205,000 단어 | `newspaper/weekly/{YYYY-W##}/` |

### 15.2 실행

```bash
/run-newspaper-only                          # 오늘 일간 생성
/run-newspaper-weekly --week 2026-W17        # 특정 주간
# Skeleton (HTML 골격만, LLM 미호출, ~3초)
python3 scripts/reports/generate_newspaper_daily.py \
  --date 2026-04-25 --skeleton-only --project-dir .
```

### 15.3 15 편집 원칙

P1 완전 지리 커버리지 · P2 Balance Code (30% 상한) · P3 계층 일관성 · P4 고정 분량(±20%) · P5 3-Tier (global/local/weak_signal) · P6 Source Triangulation · P7 STEEPS 균형 · P8 CE4 증거 · P9 Fact/Context/Opinion 분리 · P10 Confidence Level · P11 한국어 일차 · P12 미래학자 관점 · P13 Dark Corners · P14 No Clickbait · P15 No Single-Source.

### 15.4 검증

`validate_newspaper.py` NP1-NP12 (파일 존재 · 단어 수 · 대륙 커버리지 · STEEPS · Dark Corners · Triangulation · CE4 밀도 · F/C/O 구조 · Confidence 라벨 · 금지어 · 단일소스 · HTML 유효성).

대시보드: 종합 대시보드의 `📰 Newspaper (WF5)` 탭 — iframe으로 HTML 직접 렌더.

---

## 16. 관련 문서

| 문서 | 내용 |
|------|------|
| [`README.md`](README.md) | 영한 병기 시스템 개요, 빠른 시작 |
| [`GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md`](GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md) | 설계 철학, 시스템 아키텍처 |
| [`GLOBALNEWS-EXECUTION-WORKFLOWS.md`](GLOBALNEWS-EXECUTION-WORKFLOWS.md) | W1/W2/W3/W4/DCI/WF5 실행 프로토콜 |
| [`GLOBALNEWS-EVIDENCE-CHAIN.md`](GLOBALNEWS-EVIDENCE-CHAIN.md) | CE3/CE4 증거 체인 |
| [`GLOBALNEWS-SEMANTIC-GATES.md`](GLOBALNEWS-SEMANTIC-GATES.md) | SG1–SG3 + SG-Superhuman |
| [`GLOBALNEWS-P1-EXTENSIONS.md`](GLOBALNEWS-P1-EXTENSIONS.md) | P1 할루시네이션 봉쇄 확장 |
| [`prompt/execution-workflows/dci.md`](prompt/execution-workflows/dci.md) | DCI 7-Phase protocol 상세 |
| [`prompt/workflow.md`](prompt/workflow.md) | 20-step 워크플로우 설계도 (구축 과정 기록) |
| [`data/config/sources.yaml`](data/config/sources.yaml) | 112개 사이트 설정 |
| [`data/config/pipeline.yaml`](data/config/pipeline.yaml) | 8-Stage 분석 파이프라인 설정 |
| [`data/config/insights.yaml`](data/config/insights.yaml) | Workflow B + 경보 임계값 |
| [`DECISION-LOG.md`](DECISION-LOG.md) | ADR-001 → ADR-083+ |
