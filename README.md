# GlobalNews — Global News Crawling & Big Data Analysis System

> **112 international news sites, 14 languages, 4,230+ articles/day**
> **8-stage NLP pipeline + 7-module insight analytics + evidence-based future intelligence**

| | |
|---|---|
| **What it does** | Crawls 112 news sites daily, runs 56 NLP techniques, produces geopolitical/economic/entity intelligence with risk alerts |
| **Output** | Parquet (ZSTD) + SQLite (FTS5) + Evidence-based intelligence + Automated risk alerts |
| **Languages** | English, Korean, Spanish, German, Swedish, Japanese, Russian, Italian, Portuguese, Polish, Czech, French, Norwegian, Mongolian |
| **Performance** | 4,230 articles/day, ~5h crawling, ~73min analysis, NER accuracy 79%, 7,635 insight findings |
| **Stack** | Python 3.13, SBERT, BERTopic, spaCy, Kiwi, Davlan XLM-RoBERTa, mDeBERTa, PyArrow, DuckDB |
| **Framework** | Born from [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) (parent organism — DNA inheritance) |

---

## Quick Start

```bash
# 1. Clone and setup
git clone https://github.com/idoforgod/GlobalNews-Crawling-AgenticWorkflow.git
cd GlobalNews-Crawling-AgenticWorkflow

# 2. Create venv (Python 3.12-3.13 required, spaCy incompatible with 3.14)
python3.13 -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
python -m spacy download en_core_web_sm

# 4. Verify environment
.venv/bin/python scripts/preflight_check.py --project-dir . --mode full --json

# 5. Run full pipeline (crawl 112 sites + 8-stage NLP analysis)
.venv/bin/python main.py --mode full --date $(date +%Y-%m-%d)

# 6. Run insight analytics (7 modules + evidence-based intelligence)
.venv/bin/python main.py --mode insight --window 30 --end-date $(date +%Y-%m-%d)
```

### Claude Code Users

Just type **"start"** or **"시작하자"** in Claude Code. The system auto-routes to the right mode.

| Say this | What happens |
|----------|-------------|
| "시작하자" / "start" | Full pipeline (crawl + analyze) |
| "통찰 분석" / "run insights" | Workflow B (7-module insight analytics) |
| "크롤링 해줘" / "crawl" | Crawling only |
| "상태 확인" / "status" | Show results |

---

## What This System Produces

### Workflow A: Daily Collection & Analysis (~5 hours)

```
112 news sites (14 languages)
    │
    ▼ Crawling: 4-Level retry (max 90 attempts), 5-worker parallel
    │           Never-Abandon policy, adaptive rounds, sitemap capping
    │
    ▼ 4,230 articles/day (raw JSONL, 16 MB)
    │
    ▼ 8-Stage NLP Pipeline (73 minutes):
    │
    │  Stage 1: Preprocessing         Kiwi (Korean) + spaCy (English)
    │  Stage 2: Feature Extraction     SBERT 384-dim + TF-IDF + Multilingual NER
    │  Stage 3: Article Analysis       Sentiment + Plutchik 8 Emotions + STEEPS
    │  Stage 4: Aggregation            BERTopic + HDBSCAN + Entity Networks
    │  Stage 5: Time Series            STL Decomposition + PELT Changepoints
    │  Stage 6: Cross Analysis         Granger Causality + PCMCI
    │  Stage 7: Signal Classification  5-Layer (Fad → Singularity)
    │  Stage 8: Data Output            Parquet + SQLite FTS5 + DuckDB
    │
    ▼ Output: data/output/YYYY-MM-DD/
       ├── analysis.parquet   (7.8 MB, 4,230 rows, 21 columns)
       ├── topics.parquet     (52 topics)
       ├── signals.parquet    (5-Layer classification)
       └── index.sqlite       (27 MB, full-text search)
```

### Workflow B: Insight Analytics (~60 seconds)

Accumulated data (7-40 day window) is analyzed by 7 modules:

| Module | Analysis | Key Metrics | Future Prediction Use |
|--------|----------|-------------|----------------------|
| **M1** Cross-Lingual | Information asymmetry across 14 languages | JSD divergence, Wasserstein sentiment bias, filter bubble index | Cross-national perception gaps → diplomatic conflict precursor |
| **M2** Narrative | Frame evolution + information flow topology | Changepoint detection, HHI voice dominance, media health | Propaganda detection, opinion manipulation |
| **M3** Entity | Entity trajectories + hidden connections | Burst/plateau classification, Jaccard links, emergence acceleration | Predict next newsmakers, discover hidden relationships |
| **M4** Temporal | Information velocity + attention decay | Cascade detection, velocity matrix, cyclicality | News lifecycle prediction, recurrence probability |
| **M5** Geopolitical | Bilateral relations + soft power | BRI index (414 pairs), conflict/cooperation ratio, agenda-setting | Track relationship deterioration/improvement in real-time |
| **M6** Economic | EPU uncertainty + sector sentiment | Multilingual EPU (12 languages), 5-sector momentum, narrative economics | Economic crisis early warning |
| **M7** Synthesis + Intelligence | Key findings + **evidence-based future intelligence** | Entity profiles (100), pair tensions (224), evidence articles (255), risk alerts | **Actionable predictions with article-level evidence** |

### M7 Intelligence — Evidence-Based Outputs

The system automatically matches **raw article content with NLP analysis results** to produce actionable intelligence:

| Output | What It Contains | Example |
|--------|-----------------|---------|
| `entity_profiles.parquet` | Per-entity sentiment profile (mentions, sentiment, source distribution) | "Iran: 496 mentions, avg sentiment -0.232, neg 38%" |
| `pair_tensions.parquet` | Bilateral tension tracking (co-occurrence, sentiment, evidence) | "Iran+Israel: 143 co-occurrences, avg -0.306" |
| `evidence_articles.parquet` | Best evidence articles per topic (scored by importance + extremity) | Actual article titles, bodies, sources matched to each insight |
| `risk_alerts.parquet` | Automated threshold-based alerts | "EPU > 0.4 + all sectors negative = economic crisis precursor" |

**Alert Thresholds** (configurable in `data/config/insights.yaml`):

| Alert | Threshold | Meaning |
|-------|-----------|---------|
| Crisis Sentiment | < -0.40 | Entity pair sentiment extreme → military escalation |
| EPU Critical | > 0.40 | Economic uncertainty → crisis precursor |
| Burst Chaos | > 80% | 80%+ entities in burst mode → chaos phase |
| Global Polarization | > 50% | Conflict-dominant pairs exceed 50% |

---

## Latest Results (2026-04-07)

| Metric | Value |
|--------|-------|
| Articles collected | **4,230** (16 MB JSONL) |
| Active sites | **111/112** |
| Languages | **14** |
| NER extraction rate | **79% (person), 89% (org), 85% (location)** |
| Sentiment distribution | Negative 40%, Neutral 33%, Positive 27% |
| Topics discovered | **52** |
| Crawl time | **~5 hours** (was 12.5h before optimization) |
| Analysis time | **73 minutes** (8 stages) |
| Insight findings | **7,635** |
| Risk alerts | **2 triggered** (sector_all_negative, EPU warning) |
| Peak memory | 19.76 GB |

**Top entities**: Iran (4,084), Israel (2,596), US (2,448), Trump (2,431), AI (1,729)

---

## Multilingual NLP Models

| Task | Model | Languages | Accuracy |
|------|-------|-----------|----------|
| NER | `Davlan/xlm-roberta-base-ner-hrl` | ar, de, en, es, fr, it, lv, nl, pt, zh + cross-lingual | 79% (Korean via transfer) |
| Sentiment (EN) | `cardiffnlp/twitter-roberta-base-sentiment-latest` | English | Production |
| Sentiment (KO) | `monologg/kobert` + mDeBERTa fallback | Korean | Production |
| Sentiment (Multi) | `twitter-xlm-roberta-base-sentiment-multilingual` + `mDeBERTa-v3-base-mnli-xnli` | 8+ languages | Production |
| Emotions | `facebook/bart-large-mnli` (zero-shot) | Multilingual | Plutchik 8 dimensions |
| STEEPS | `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` (zero-shot) | Multilingual | 6 categories |
| Embeddings | `paraphrase-multilingual-MiniLM-L12-v2` | 50+ languages | 384-dim |
| Topics | BERTopic + HDBSCAN | Language-agnostic (embedding-based) | 52 topics |

All models are **automatically downloaded** on first run. No API keys required (Claude API = $0).

---

## Crawling Engine

**4-Level Retry Architecture** — up to 90 automatic recovery attempts per article:

| Level | Target | Retries | Strategy |
|-------|--------|---------|----------|
| L1 NetworkGuard | HTTP request | 5x | Exponential backoff 1-60s |
| L2 Strategy | Extraction mode | 2x | Standard → TotalWar escalation |
| L3 Crawler | Crawl rounds | 1-3x | Adaptive (small sites: 1, large: 3) |
| L4 Pipeline | Full restart | 3x | Re-run failed sites only |
| **Total** | | **5 x 2 x 3 x 3 = 90** | + Never-Abandon extra passes |

**Additional features**:
- **5-Worker ThreadPoolExecutor**: 5 sites crawled simultaneously
- **SiteDeadline Fairness Yield**: Max 900s per site, prevents starvation
- **3-Level Deduplication**: URL normalization → Title Jaccard → SimHash
- **DynamicBypassEngine**: 7 block types, 12 strategies, 5-tier escalation
- **Sitemap Capping**: Max 50 child sitemaps to prevent hour-long scans

---

## Project Structure

```
GlobalNews-Crawling-AgenticWorkflow/
├── main.py                         CLI entry point (crawl/analyze/insight/status)
├── dashboard.py                    Streamlit dashboard (6 tabs)
│
├── src/                            Core source (183 modules, ~55,900 LOC)
│   ├── crawling/                   Crawling engine + 116 site adapters
│   ├── analysis/                   8-stage NLP pipeline (Stage 1-8)
│   ├── insights/                   Workflow B (M1-M7 + M7 intelligence extension)
│   ├── storage/                    Parquet + SQLite I/O
│   ├── config/                     Constants + configuration
│   └── utils/                      Logging, error handling
│
├── data/config/                    Configuration (tracked)
│   ├── sources.yaml                112 site definitions (groups A-J)
│   ├── pipeline.yaml               8-stage pipeline config
│   └── insights.yaml               Workflow B config + alert thresholds
│
├── data/                           Runtime data (gitignored)
│   ├── raw/YYYY-MM-DD/             Raw JSONL articles
│   ├── processed/                  Preprocessed Parquet
│   ├── features/                   NER, embeddings, TF-IDF
│   ├── analysis/                   Topics, networks, timeseries
│   ├── output/YYYY-MM-DD/          Final Parquet + SQLite
│   └── insights/{run_id}/          Workflow B outputs + intelligence
│
├── scripts/                        Operations scripts (34)
├── tests/                          Tests (60 files, 2,708 tests)
├── .claude/                        AI agent infrastructure
│   ├── agents/                     36 specialized sub-agents
│   ├── skills/                     6 reusable skills
│   ├── hooks/scripts/              25 automation hooks (P1 validation, safety)
│   └── commands/                   7 slash commands
│
├── GLOBALNEWS-*.md                 [Child] System documentation
├── AGENTICWORKFLOW-*.md            [Parent] Framework documentation
├── DECISION-LOG.md                 Architecture Decision Records (ADR-001~070)
└── soul.md                         DNA inheritance philosophy
```

---

## Reproducibility

All gitignored files are **automatically regenerated**:

| What's gitignored | How to reproduce |
|-------------------|-----------------|
| `.venv/` | `python3.13 -m venv .venv && pip install -r requirements.txt` |
| NLP models | Auto-downloaded on first run (~2 GB, cached in `~/.cache/huggingface/`) |
| `data/raw/` | Re-run crawling: `main.py --mode crawl` |
| `data/processed/`, `features/`, `analysis/`, `output/` | Re-run analysis: `main.py --mode analyze` |
| `data/insights/` | Re-run insights: `main.py --mode insight` |
| `data/dedup.sqlite` | Auto-created on crawl |

**Everything needed to reproduce results is in the repository**: source code, configuration, site definitions, pipeline settings, alert thresholds.

---

## Data Querying

```python
# DuckDB — instant SQL on Parquet
import duckdb
duckdb.sql("""
    SELECT source, sentiment_label, COUNT(*) as n
    FROM 'data/output/2026-04-07/analysis.parquet'
    GROUP BY ALL ORDER BY n DESC
""")

# SQLite FTS5 — full-text search
import sqlite3
conn = sqlite3.connect('data/output/2026-04-07/index.sqlite')
conn.execute("SELECT * FROM articles_fts WHERE articles_fts MATCH 'Iran AND nuclear'").fetchall()

# Pandas — analysis
import pandas as pd
df = pd.read_parquet('data/output/2026-04-07/analysis.parquet')
df.groupby('topic_label')['sentiment_score'].mean().sort_values()

# Intelligence — entity profiles
profiles = pd.read_parquet('data/insights/quarterly-2026-Q2/synthesis/intelligence/entity_profiles.parquet')
profiles.nlargest(10, 'mention_count')[['entity', 'mention_count', 'avg_sentiment', 'neg_ratio']]

# Risk alerts
alerts = pd.read_parquet('data/insights/quarterly-2026-Q2/synthesis/intelligence/risk_alerts.parquet')
alerts[alerts['triggered'] == True]
```

---

## Documentation Guide

| Document | Content | Audience |
|----------|---------|----------|
| **README.md** (this file) | Project overview, quick start, capabilities | First-time visitors |
| [GLOBALNEWS-README.md](GLOBALNEWS-README.md) | Detailed system specs, performance data | System evaluators |
| [GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md](GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md) | Design philosophy, architecture deep-dive | Developers |
| [GLOBALNEWS-USER-MANUAL.md](GLOBALNEWS-USER-MANUAL.md) | Operations guide, CLI, dashboard, intelligence interpretation | Operators & analysts |
| [DECISION-LOG.md](DECISION-LOG.md) | Architecture decision records (ADR-001~070) | Architects |
| [soul.md](soul.md) | DNA inheritance philosophy | Framework users |

---

## Tests

```bash
pytest                            # All 2,708 tests
pytest tests/crawling/            # Crawling tests (1,233)
pytest tests/unit/                # Unit tests
pytest -m "not slow"              # Skip NLP model loading (fast)
```

---

## License

MIT License. See [COPYRIGHT.md](COPYRIGHT.md).
