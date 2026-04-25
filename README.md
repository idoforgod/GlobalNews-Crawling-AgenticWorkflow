# GlobalNews — Global News Crawling & Big Data Analysis System
# GlobalNews — 글로벌 뉴스 크롤링 & 빅데이터 분석 시스템

> **112 international news sites · 14 languages · 4,200+ articles/day**
> **8-stage NLP + 7-module insights + 18-Question BigData Engine + DCI 14-layer + Personal Newspaper**
>
> **112개 국제 뉴스 사이트 · 14개 언어 · 일일 4,200+ 기사 수집**
> **8단계 NLP + 7모듈 통찰 + 18-Question 빅데이터 엔진 + DCI 14계층 + 나만의 신문 발행**

| | EN | KO |
|---|---|---|
| **What it does** | Crawls 112 sites daily, runs 56 NLP techniques, answers 18 future-research questions, builds Geopolitical Tension Index (GTI), tracks Future Signal Portfolio, publishes a 135,000-word personal newspaper, and exports a doctoral-level Deep Content Intelligence (DCI) report | 112개 사이트를 매일 크롤링, 56개 NLP 기법 적용, 18개 미래연구 질문에 강제 응답, 지정학 긴장 지수(GTI) 산출, 미래신호 포트폴리오 추적, 13.5만 단어 나만의 신문 발행, 박사급 DCI 심층 보고서 생산 |
| **Outputs** | Parquet (ZSTD) · SQLite (FTS5+vec) · 18-Q answers · GTI series · Signal Portfolio · 3-Layer Public Narrative · Chart Interpretations · NYT-style HTML newspaper · DCI doctoral report · LLM Wiki ingest | Parquet · SQLite · 18문 답변 · GTI 시계열 · 신호 포트폴리오 · 3계층 일반인 해석 · 차트 해석 · NYT 스타일 HTML 신문 · DCI 박사급 보고서 · LLM Wiki 자동 ingest |
| **Languages** | English, Korean, Spanish, German, Swedish, Japanese, Russian, Italian, Portuguese, Polish, Czech, French, Norwegian, Mongolian | 영어, 한국어, 스페인어, 독일어, 스웨덴어, 일본어, 러시아어, 이탈리아어, 포르투갈어, 폴란드어, 체코어, 프랑스어, 노르웨이어, 몽골어 |
| **Code base** | 275 Python modules, ~80.7K LOC src + ~41.5K LOC tests (109 test files) · 123 site adapters · 107 sub-agents · 18 slash commands · 6 local skills · 42 hook scripts | 275 Python 모듈, src ~80.7K + tests ~41.5K LOC (109 파일) · 어댑터 123개 · 서브에이전트 107개 · 슬래시 커맨드 18개 · 로컬 스킬 6개 · 훅 스크립트 42개 |
| **Stack** | Python 3.13 · SBERT · BERTopic · spaCy · Kiwi · XLM-RoBERTa · mDeBERTa · DeBERTa-v3-MNLI · PyArrow · DuckDB · Streamlit · Claude CLI | |
| **Framework** | Born from [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) — DNA inheritance | [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) 부모 프레임워크로부터 DNA 유전 |

---

## Quick Start / 빠른 시작

```bash
# 1. Clone / 클론
git clone https://github.com/idoforgod/GlobalNews-Crawling-AgenticWorkflow.git
cd GlobalNews-Crawling-AgenticWorkflow

# 2. venv (Python 3.12 or 3.13 — spaCy/pydantic v1 incompatible with 3.14)
python3.13 -m venv .venv && source .venv/bin/activate

# 3. Install / 설치
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# 4. Verify / 환경 검증
.venv/bin/python scripts/preflight_check.py --project-dir . --mode full --json

# 5. Daily pipeline / 일일 파이프라인 (crawl + 8-stage NLP + insight chain)
.venv/bin/python main.py --mode full --date $(date +%Y-%m-%d)

# 6. Insight analytics (Workflow B) / 빅데이터 통찰 분석
.venv/bin/python main.py --mode insight --window 30 --end-date $(date +%Y-%m-%d)

# 7. Deep Content Intelligence (DCI) / 심층 콘텐츠 인텔리전스
.venv/bin/python main.py --mode dci --date $(date +%Y-%m-%d)

# 8. Dashboards / 대시보드
streamlit run dashboard.py                   # Comprehensive dashboard (6 tabs incl. 18 Questions)
streamlit run insights_dashboard.py --server.port 8502   # Workflow B (M1-M7) dashboard
```

### Claude Code Users / Claude Code 사용자

Type **"start"** or **"시작하자"** — the system auto-routes by workflow state.
**"start"** 또는 **"시작하자"** 만 입력하면 워크플로우 상태에 따라 자동 분기됩니다.

| Say this / 입력 | What happens / 동작 |
|----------|-------------|
| "시작하자" / "start" | Full daily pipeline (crawl + analyze + auto-chain insight) / 전체 일일 파이프라인 |
| "통찰 분석" / "run insights" | Workflow B (M1-M7 + M7 intelligence) / 빅데이터 통찰 분석 |
| "DCI 실행" / "run DCI" | Independent DCI workflow (14-layer doctoral report) / DCI 독립 워크플로우 |
| "신문 만들어줘" / "run newspaper" | WF5 Personal Newspaper daily edition / 나만의 신문 일간판 |
| "일반인 해석" / "public layers" | 3-Layer Public Narrative (Interpretation·Insight·Future) / 일반인 3계층 해석 |
| "크롤링 해줘" / "crawl" | Crawling only / 크롤링만 |
| "상태 확인" / "status" | `main.py --mode status` |

Slash commands: `/run`, `/run-chain`, `/run-crawl-only`, `/run-analyze-only`, `/run-insight-only`, `/run-dci-only`, `/run-newspaper-only`, `/run-newspaper-weekly`, `/generate-public-layers`, `/generate-chart-interpretations`, `/integrate-results`, `/approve-report`, `/start`, `/install`, `/maintenance`, `/review-research`, `/review-architecture`, `/review-final` (총 18개).

---

## What This System Produces / 이 시스템이 생산하는 것

### Daily Pipeline / 일일 파이프라인 (`scripts/run_daily.sh`, 8h timeout)

The cron-driven daily pipeline executes a structured 7-step post-processing chain after the core `main.py --mode full` run. Each step is **idempotent** and **fail-soft** (failure does not block subsequent steps).

cron으로 트리거되는 일일 파이프라인은 `main.py --mode full` 본체 실행 후 다음 7단계 후처리 체인을 실행한다. 각 단계는 **멱등(idempotent)** 하고 **실패 시 다음 단계 차단 없음(fail-soft)** 이다.

| Step | Action / 동작 | Module / 모듈 | Output / 산출물 |
|------|--------|----------|----------|
| **4** | Core pipeline / 본체 파이프라인 | `main.py --mode full` | data/raw, data/processed, data/features, data/analysis, data/output, data/insights |
| **6.3** | W2 narrative report / W2 분석 리포트 | `@analysis-reporter` (Claude CLI) | `workflows/analysis/outputs/analysis-report-{date}.md` |
| **6.4** | W3 insight refinement / W3 인사이트 정제 | `@insight-narrator` | `data/insights/{run_id}/synthesis/insight_report.md` (refined) |
| **6.45a** | W4 Master appendix / W4 마스터 부록 | `src.reports.w4_appendix` | `reports/final/integrated-report-{date}.md` |
| **6.45b** | DCI layer summary / DCI 레이어 요약 | `src.reports.dci_layer_summary` | `data/dci/runs/{id}/final_report.md` (appended) |
| **6.5** | Public Narrative 3-Layer (ADR-080) | `scripts/.../generate_public_layers.py` | `reports/public/{date}/{interpretation,insight,future}.md` (+ `.ko.md`) |
| **6.6** | Chart Interpretations 6 tabs (ADR-082) | `scripts/reports/generate_chart_interpretations.py` | `data/analysis/{date}/interpretations.json` |
| **6.7** | **BigData Engine** — Enriched · 18-Q · GTI · Signal Portfolio · Weekly Future Map | `src.analysis.{question_engine,gti,signal_portfolio,weekly_future_map}` | `data/enriched/{date}/`, `data/answers/{date}/q01-q18.json`, `data/gti/{date}/`, `data/signal_portfolio.yaml`, `reports/weekly_future_map/` |
| **6.8** | **LLM Wiki ingest** (background, after BigData) | `llm-wiki-environmentscanning/scripts/auto-wiki-ingest.sh` | External Wiki repo update |
| **7** | WF5 Personal Newspaper daily (ADR-083) | 14-desk Agent Team via `@newspaper-chief-editor` | `newspaper/daily/{date}/index.html` (~135,000 words) |
| **7b** | WF5 weekly edition (Sundays only, ≥4 dailies) | Same | `newspaper/weekly/{YYYY-W##}/` (~205,000 words) |

### Workflow A — Daily Collection & Analysis / 일일 수집 & 분석

```
112 news sites (14 languages) — Groups A–J / 112개 사이트, 10개 그룹
    │
    ▼ Crawling: 4-Level retry (max 90 attempts) + Never-Abandon Multi-Pass + DynamicBypassEngine
    │           5-worker parallel + SiteDeadline Fairness Yield (max 900s/site)
    │
    ▼ ~4,200 articles/day → data/raw/{date}/all_articles.jsonl
    │
    ▼ 8-Stage NLP (~73 min) — see USER-MANUAL §4 for full table
    │
    ▼ data/output/{date}/{analysis,topics,signals}.parquet + index.sqlite (FTS5 + sqlite-vec)
    │
    ▼ BigData Engine post-processing (Step 6.7) — see below
```

### Workflow B — Insight Analytics (M1-M7) / 통찰 분석 (~60s)

7~40-day rolling window analyzed by 7 modules. Cross-lingual asymmetry, narrative evolution, entity trajectory, temporal patterns, geopolitical bilateral tension, economic uncertainty, and final synthesis with evidence-based intelligence.

7~40일 롤링 윈도우를 7개 모듈이 교차 분석. 교차언어 비대칭, 내러티브 진화, 엔티티 궤적, 시간 패턴, 양자 지정학 긴장, 경제 불확실성, 그리고 증거 기반 인텔리전스를 포함한 최종 종합.

| Module / 모듈 | Key Metrics / 핵심 지표 | Future-prediction use / 미래 예측 활용 |
|--------|----------|----------------------|
| **M1** Cross-Lingual / 교차언어 | JSD divergence, Wasserstein bias, filter-bubble index | Diplomatic conflict precursor / 외교 갈등 선행지표 |
| **M2** Narrative / 내러티브 | Frame changepoints, HHI voice dominance, media health | Propaganda detection / 여론조작 탐지 |
| **M3** Entity / 엔티티 | Burst/plateau classification, Jaccard hidden links | Predict next newsmakers / 차세대 뉴스 주인공 예측 |
| **M4** Temporal / 시간 | Cascade, velocity matrix, cyclicality | News lifecycle prediction / 뉴스 수명 예측 |
| **M5** Geopolitical / 지정학 | BRI index (414 pairs), conflict/cooperation ratio | Bilateral relationship tracking / 양자관계 추적 |
| **M6** Economic / 경제 | Multilingual EPU (12 langs), 5-sector momentum | Economic crisis early warning / 경제위기 조기경보 |
| **M7** Synthesis + Intelligence / 종합 + 인텔리전스 | Entity profiles · pair tensions · evidence articles · risk alerts | **Evidence-based actionable predictions / 증거 기반 행동가능 예측** |

### BigData Engine — 18 Questions + GTI + Signal Portfolio (NEW) / 빅데이터 엔진

The BigData Engine, executed in `run_daily.sh` Step 6.7, **forces structured answers to 18 standing future-research questions every day**. Even on data-sparse days the engine emits `status:'insufficient_data'` rather than a missing file — the 18-question contract is unbreakable.

`run_daily.sh` Step 6.7에서 실행되는 빅데이터 엔진은 **매일 18개 미래연구 질문에 구조화된 답변을 강제 생산**한다. 데이터가 부족한 날에도 파일을 누락하는 대신 `status:'insufficient_data'`로 응답 — 18문 계약은 깨지지 않는다.

| Module / 모듈 | File / 파일 | Output / 산출물 |
|--------|---------|----------|
| Articles Enriched Assembler / 강화 어셈블러 | `src/analysis/articles_enriched_assembler.py` | `data/enriched/{date}/articles_enriched.parquet` (35 fields) |
| Geo Focus Extractor / 지리 포커스 추출 | `src/analysis/geo_focus_extractor.py` | source_country ≠ geo_focus 분리, 120개국 |
| Source Metadata Joiner / 출처 메타 조인 | `src/analysis/source_metadata_joiner.py` | source_tier (GLOBAL~NICHE) + ideology |
| STEEPSS Classifier / STEEPSS 분류 | `src/analysis/steeps_classifier.py` | 8 categories incl. **SPI (Spirituality)** + **CRS (Crisis)** |
| Signal Classifier / 신호 분류 | `src/analysis/signal_classifier.py` | Layer A/B (BREAKING/TREND/WEAK_SIGNAL/NOISE) |
| **18-Question Engine** / 18문 엔진 | `src/analysis/question_engine.py` | `data/answers/{date}/q01-q18.json` + `summary.json` |
| **GTI** (Geopolitical Tension Index) / 지정학 긴장 지수 | `src/analysis/gti.py` | `data/gti/{date}/gti_daily.json` + `gti_history.jsonl`, 0–100 score |
| **Signal Portfolio** / 신호 포트폴리오 | `src/analysis/signal_portfolio.py` | `data/signal_portfolio.yaml` (single SOT, lifecycle tracking) |
| **Weekly Future Map** / 주간 미래 맵 | `src/analysis/weekly_future_map.py` | `reports/weekly_future_map/{YYYY-W##}/future_map.md` (EN + KO) |

**The 18 Questions / 18개 질문 (full text in `src/analysis/question_engine.py`)**:
Q01 burst detection · Q02 trend trajectory · Q03 pre/post-event coverage shift · Q04 cross-language framing · Q05 country sentiment · Q06 dark corners (under-reported) · Q07 bilateral tension · Q08 weak signals · Q09 paradigm-shift precursors · Q10 fringe→mainstream agenda movement (≥21d) · Q11 agenda-setting first-movers · Q12 progressive vs conservative emphasis · Q13 language-exclusive agendas · Q14 inter-media coverage gap · Q15 sentiment-economy lead/lag (≥7d) · Q16 cross-issue causal chain · Q17 simultaneous-burst clusters · Q18 entity centrality map.

### DCI — Deep Content Intelligence (Independent Workflow) / 심층 콘텐츠 인텔리전스

DCI is **independent** of the W1→W2→W3→W4 chain. It consumes only `data/raw/{date}/all_articles.jsonl` and produces a doctoral-level report via 14 layers (L-1 → L11) and the **SG-Superhuman 10-gate** verification (`char_coverage = 1.00`, `triple_lens_coverage ≥ 3.0`, `nli_verification_pass_rate ≥ 0.95`, etc.). Layer L6 Triadic Ensemble + L10 Doctoral Narrator use Claude CLI subprocess; all other layers are pure Python.

DCI는 W1→W2→W3→W4 체인과 **독립**으로 작동한다. `data/raw/{date}/all_articles.jsonl`만 입력으로 사용하여 14개 레이어(L-1 → L11)와 **SG-Superhuman 10-게이트** 검증을 거쳐 박사급 보고서를 생산한다. L6 Triadic Ensemble + L10 Doctoral Narrator는 Claude CLI subprocess 사용, 나머지 모든 레이어는 순수 Python.

```bash
/run-dci-only                          # Recommended (agent-orchestrated)
.venv/bin/python main.py --mode dci --date YYYY-MM-DD     # Direct
DCI_DISABLED=1 .venv/bin/python main.py --mode full       # Disable DCI in chain
```

Output: `data/dci/runs/{run_id}/final_report.md` + `executive_summary.md` + Korean translation. See `prompt/execution-workflows/dci.md` for the full 7-Phase protocol and `DECISION-LOG.md` ADR-079 for the independence promotion.

### Public Narrative + Chart Interpretations (Plain-language layers)

Built on top of the technical artifacts:

- **3-Layer Public Narrative** (ADR-080, Step 6.5): each generated independently from `facts_pool.json` so a non-expert reader can understand "what happened (Interpretation, FKGL ≤ 9), what pattern (Insight, FKGL ≤ 12), what's coming (Future, FKGL ≤ 13)". 8 PUB validators (numeric parity, evidence whitelist, jargon ratio, banned-word check, EN↔KO structural parity).
- **Chart Interpretations** (ADR-082, Step 6.6): each dashboard tab gets 🌱 Interpretation / 💡 Insight / 🔮 Future Outlook cards. 6 tabs covered (Overview, Topics, Sentiment, TimeSeries, WordCloud, W3 Insight). Validators: CI1–CI6 (structure, FKGL, number parity, marker whitelist, banned words, cross-tab refs).

### WF5 — Personal Newspaper / 나만의 신문 (ADR-083)

A 17-agent editorial team produces a NYT-style HTML newspaper from W1 raw articles every day. **15 editorial principles** including P1 full geographic coverage, P2 Balance Code (max 30% per topic), P5 3-tier ranking (global / local / weak signal), P6 source triangulation, P9 fact/context/opinion separation, P13 dark corners, P14 no clickbait. Agents: `@newspaper-chief-editor`, 6 continental desks (`@desk-{africa,asia,europe,north-america,oceania,south-america}`), 6 STEEPS section desks, 4 specialty agents (`@dark-corner-scout`, `@fact-triangulator`, `@future-outlook-writer`, `@newspaper-copy-editor`).

| Edition / 판본 | Schedule / 주기 | Words / 분량 | Path / 경로 |
|--------|----------|--------|--------|
| Daily / 일간 | Every day after pipeline / 매일 파이프라인 후 | ~135,000 (≈9h reading) | `newspaper/daily/{date}/index.html` |
| Weekly / 주간 | Sundays, ≥4 dailies present | ~205,000 | `newspaper/weekly/{YYYY-W##}/` |

---

## Three Dashboards / 세 개의 대시보드

| File | Lines | Role | Launch |
|------|-------|------|--------|
| **`dashboard.py`** | 2,319 | Comprehensive multi-period dashboard. 6 tabs: **Run Summary** (W1→W2→W3→W4 + BigData KPIs) · **Overview** · **Topics** · **Sentiment & Emotions** · **Word Cloud** · **🔢 18 Questions** | `streamlit run dashboard.py` |
| **`insights_dashboard.py`** | 758 | Workflow B–dedicated dashboard. 8 tabs: Overview + M1 Cross-Lingual / M2 Narrative / M3 Entity / M4 Temporal / M5 Geopolitical / M6 Economic / M7 Synthesis | `streamlit run insights_dashboard.py --server.port 8502` |
| `dashboard_insights.py` | 1,222 | **Helper module** (no UI). Deterministic insight-card extraction (no LLM) used by `dashboard.py`. Not standalone. | (imported) |

---

## Crawling Engine / 크롤링 엔진

**112 enabled sites** (groups A=5, B=3, C=2, D=8, E=23, F=20, G=34, H=4, I=9, J=4) defined in `data/config/sources.yaml`. **123 site adapters** in `src/crawling/adapters/{kr_major,kr_tech,english,multilingual}/`.

**4-Level Retry Architecture** — up to 90 automatic attempts per article:

| Level | Target | Retries | Strategy |
|-------|--------|---------|----------|
| L1 NetworkGuard | HTTP request | 5× | Exponential backoff 1–60s |
| L2 Strategy | Extraction mode | 2× | Standard → TotalWar |
| L3 Crawler | Crawl rounds | 1–3× | Adaptive (small / large) |
| L4 Pipeline | Full restart | 3× | Re-run failed sites only |
| **Total** | | **5 × 2 × 3 × 3 = 90** | + Never-Abandon Multi-Pass (max 10 extra) |

Plus: **5-Worker ThreadPoolExecutor**, **SiteDeadline Fairness Yield** (max 900s/site, prevents starvation), **3-Level Deduplication** (URL → Title Jaccard → SimHash), **DynamicBypassEngine** (7 block types × 12 strategies × 5-tier escalation), **Sitemap Capping** (max 50 child sitemaps), **3-fix crawling quality patches** (HTML entity unescape via `html.unescape`, KST→UTC timezone normalization, `source_domain` field).

---

## Project Structure / 프로젝트 구조

```
GlobalNews-Crawling-AgenticWorkflow/
├── main.py                         # CLI entry: --mode {crawl, analyze, full, status, insight, dci}
├── dashboard.py                    # Comprehensive Streamlit dashboard (6 tabs)
├── dashboard_insights.py           # Helper module (deterministic insight cards)
├── insights_dashboard.py           # Workflow B dashboard (M1-M7, port 8502)
│
├── src/                            # Core source — 275 modules, ~80.7K LOC
│   ├── crawling/                   # Crawling engine + 123 site adapters + DynamicBypassEngine
│   ├── analysis/                   # 8-stage NLP + BigData Engine modules:
│   │   │                           #   stage1_preprocessing → stage8_output
│   │   │                           #   articles_enriched_assembler.py · geo_focus_extractor.py
│   │   │                           #   source_metadata_joiner.py · steeps_classifier.py
│   │   │                           #   signal_classifier.py · question_engine.py
│   │   │                           #   gti.py · signal_portfolio.py · weekly_future_map.py
│   │   └── pipeline.py             #   Stage orchestrator
│   ├── insights/                   # Workflow B — M1-M7 + window_assembler + validators
│   ├── dci/                        # Deep Content Intelligence (61 files, ~10.3K LOC)
│   │   ├── layers/                 #   L0 → L10 (l1_5_meaning, l6_triadic, l7_graph_of_thought,
│   │   │                           #              l8_monte_carlo, l9_metacognitive, l10_final_report)
│   │   ├── ensemble/               #   Claude CLI client + verifiers
│   │   └── evidence_ledger.py      #   CE4 3-layer evidence chain
│   ├── newspaper/                  # WF5 — chief_editor + 14 desks + html_renderer + templates
│   ├── public_narrative/           # 3-Layer (facts_extractor + narrator + validators)
│   ├── interpretations/            # Chart Interpretations (facts_pool, prompt_composer, validators)
│   ├── reports/                    # w4_appendix.py · dci_layer_summary.py
│   ├── storage/                    # Parquet writer · SQLite builder
│   ├── utils/                      # Logging, config, error handling
│   └── config/                     # constants.py + insights/__init__.py
│
├── data/config/                    # Tracked configuration
│   ├── sources.yaml                # 112 enabled sites, 10 groups (A-J)
│   ├── pipeline.yaml               # 8-stage pipeline config
│   ├── insights.yaml               # Workflow B + alert thresholds
│   └── bypass_state.json           # DynamicBypassEngine learned state
│
├── data/                           # Runtime data (gitignored)
│   ├── raw/{date}/                 # Raw JSONL
│   ├── processed/{date}/           # Stage 1-2
│   ├── features/{date}/            # Stage 2 features
│   ├── analysis/{date}/            # Stage 3-7 + interpretations.json (Step 6.6)
│   ├── output/{date}/              # Final Parquet + SQLite
│   ├── enriched/{date}/            # NEW — articles_enriched.parquet (35 fields)
│   ├── answers/{date}/             # NEW — q01-q18.json + summary.json
│   ├── gti/{date}/                 # NEW — gti_daily.json
│   ├── signal_portfolio.yaml       # NEW — single SOT for future signals
│   ├── insights/{run_id}/          # Workflow B + M7 intelligence
│   ├── dci/runs/{run_id}/          # DCI 14-layer outputs + final_report.md
│   └── domain-knowledge.yaml       # NEW — DKS structured entities
│
├── reports/
│   ├── public/{date}/              # 3-Layer Public Narrative (interpretation/insight/future + .ko)
│   ├── final/                      # W4 Master integrated reports
│   └── weekly_future_map/          # Weekly Future Map (EN + KO)
│
├── newspaper/
│   ├── daily/{date}/index.html     # WF5 daily edition (~135K words)
│   └── weekly/{YYYY-W##}/          # WF5 weekly edition (~205K words)
│
├── scripts/                        # 60+ Python scripts
│   ├── run_daily.sh                # Daily cron entry (8h timeout, Steps 1-7b)
│   ├── run_weekly_rescan.sh        # Weekly site-structure rescan
│   ├── execution/p1/               # P1 quality gates (w1/w2/w3 metrics, evidence_chain, ...)
│   └── reports/                    # Newspaper / chart-interpretation generators
│
├── tests/                          # 109 test files, ~41.5K LOC
├── .claude/
│   ├── agents/                     # 107 sub-agents
│   ├── skills/                     # 6 local skills (workflow-generator, crawl-master, ...)
│   ├── hooks/scripts/              # 42 hook scripts (P1 validators, safety, context)
│   └── commands/                   # 18 slash commands
│
├── GLOBALNEWS-*.md                 # [Child] System docs (README, USER-MANUAL, ARCHITECTURE,
│                                   #         EVIDENCE-CHAIN, SEMANTIC-GATES, P1-EXTENSIONS,
│                                   #         EXECUTION-WORKFLOWS)
├── AGENTICWORKFLOW-*.md            # [Parent] Framework docs
├── DECISION-LOG.md                 # ADRs (latest: ADR-083 Personal Newspaper)
└── soul.md                         # DNA inheritance philosophy
```

---

## Reproducibility / 재현 가능성

All gitignored runtime files are **automatically regenerated** by re-running the corresponding mode.
gitignore 된 런타임 파일은 해당 모드를 재실행하면 **모두 자동 재생성** 된다.

| Gitignored / git 미추적 | Reproduction / 재생성 |
|-------------------|-----------------|
| `.venv/` | `python3.13 -m venv .venv && pip install -r requirements.txt` |
| NLP models (~2 GB) | Auto-downloaded on first run |
| `data/raw/` | `main.py --mode crawl --date {date}` |
| `data/processed/`, `features/`, `analysis/`, `output/`, `enriched/`, `answers/`, `gti/` | `main.py --mode analyze` (or `--mode full`) |
| `data/insights/{run_id}/` | `main.py --mode insight --window 30` |
| `data/dci/runs/{run_id}/` | `main.py --mode dci --date {date}` |
| `reports/public/{date}/` | `/generate-public-layers --date {date}` |
| `data/analysis/{date}/interpretations.json` | `/generate-chart-interpretations --date {date}` |
| `newspaper/daily/{date}/` | `/run-newspaper-only` (or run_daily.sh Step 7) |

---

## Data Querying / 데이터 쿼리

```python
# DuckDB — instant SQL on Parquet
import duckdb
duckdb.sql("""
    SELECT source, sentiment_label, COUNT(*) AS n
    FROM 'data/output/2026-04-25/analysis.parquet'
    GROUP BY ALL ORDER BY n DESC
""")

# 18-Question answers
import json
with open('data/answers/2026-04-25/q07.json') as f:    # bilateral tension
    q07 = json.load(f)
print(q07['result']['top_pairs'][:5])

# GTI history
import pandas as pd
gti = pd.read_json('data/gti/gti_history.jsonl', lines=True)
gti.set_index('date')['gti_score'].plot()

# Future signal portfolio (single SOT)
import yaml
with open('data/signal_portfolio.yaml') as f:
    portfolio = yaml.safe_load(f)
print([s['name'] for s in portfolio['signals'] if s['status'] == 'active'])

# M7 entity profiles
profiles = pd.read_parquet('data/insights/quarterly-2026-Q2/synthesis/intelligence/entity_profiles.parquet')
profiles.nlargest(10, 'mention_count')[['entity', 'avg_sentiment', 'neg_ratio']]

# SQLite FTS5 full-text search
import sqlite3
conn = sqlite3.connect('data/output/2026-04-25/index.sqlite')
conn.execute("SELECT * FROM articles_fts WHERE articles_fts MATCH 'Iran AND nuclear'").fetchall()
```

---

## Tests / 테스트

```bash
pytest                            # 109 test files (~41.5K LOC)
pytest tests/crawling/            # Crawling tests
pytest tests/unit/                # Unit tests (P1 validators, retention, autopilot gates, ...)
pytest tests/integration/         # End-to-end (incl. test_dci_e2e.py)
pytest -m "not slow"              # Skip NLP model loading
```

---

## Documentation Guide / 문서 가이드

| Document | Audience | Content |
|----------|----------|---------|
| **README.md** (this) | First-time visitors | Project overview, quick start, full pipeline map |
| [GLOBALNEWS-USER-MANUAL.md](GLOBALNEWS-USER-MANUAL.md) | Operators · analysts | CLI usage, dashboards, troubleshooting, BigData Engine queries, slash commands |
| [GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md](GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md) | Developers · architects | Design philosophy, layer architecture, DCI / WF5 / BigData Engine internals |
| [GLOBALNEWS-EXECUTION-WORKFLOWS.md](GLOBALNEWS-EXECUTION-WORKFLOWS.md) | Developers | W1/W2/W3/W4/DCI/WF5 execution protocols |
| [GLOBALNEWS-EVIDENCE-CHAIN.md](GLOBALNEWS-EVIDENCE-CHAIN.md) | Reviewers | CE3/CE4 evidence chain semantics |
| [GLOBALNEWS-SEMANTIC-GATES.md](GLOBALNEWS-SEMANTIC-GATES.md) | Reviewers | SG1–SG3 + SG-Superhuman gates |
| [GLOBALNEWS-P1-EXTENSIONS.md](GLOBALNEWS-P1-EXTENSIONS.md) | Architects | P1 hallucination-prevention extensions |
| [DECISION-LOG.md](DECISION-LOG.md) | Architects | ADR-001 → ADR-083+ |
| [AGENTICWORKFLOW-*.md](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) | Framework users | Parent framework methodology |
| [GLOBALNEWS-README.md](GLOBALNEWS-README.md) | Legacy | Older spec doc (kept for reference) |

---

## License / 라이선스

MIT License. See [COPYRIGHT.md](COPYRIGHT.md).
