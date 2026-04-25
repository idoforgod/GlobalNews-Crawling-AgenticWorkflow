# GlobalNews: Architecture and Philosophy

이 문서는 GlobalNews 시스템의 **설계 철학**과 **아키텍처 전체 조감도**를 기술한다.
"무엇이 있는가"(README)와 "어떻게 쓰는가"(USER-MANUAL)를 넘어, **"왜 이렇게 설계했는가"**를 체계적으로 서술하는 문서이다.

> **부모-자식 관계**: 이 시스템은 [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) 프레임워크(만능줄기세포)로부터 태어난 **자식 시스템**이다. 부모 문서가 방법론·프레임워크를 기술하는 반면, 이 문서는 **도메인 고유 아키텍처와 그 선택의 근거**를 기술한다.

---

## 1. 설계 철학 (Design Philosophy)

### 1.1 핵심 명제: 데이터가 보고서보다 앞선다

GlobalNews의 근본적 신념:

> **최종 산출물은 보고서가 아니라 구조화된 데이터(Parquet + SQLite)이다.**
> 시각화와 보고서는 데이터 위에 올라가는 소비자이지, 데이터를 대체하지 않는다.

이 신념이 설계 전반에 관통한다:

| 설계 결정 | 일반적 선택 | GlobalNews의 선택 | 근거 |
|----------|-----------|-----------------|------|
| 최종 산출물 형태 | PDF 보고서, 슬라이드 | Parquet + SQLite | 데이터를 2차 분석에 재사용 가능 |
| 분석 결과 저장 | JSON, CSV | Parquet (ZSTD) | 컬럼 지향 + 압축 + 스키마 강제 |
| 텍스트 검색 | Elasticsearch 외부 서비스 | SQLite FTS5 내장 | 단일 파일, 외부 의존성 제로 |
| 벡터 검색 | Pinecone, Qdrant 외부 서비스 | sqlite-vec 내장 | 384-dim 벡터를 SQLite 안에서 직접 쿼리 |
| 시각화 | 정적 차트 이미지 | Streamlit 대시보드 | 인터랙티브, 데이터 위에서 즉시 탐색 |

### 1.2 Conductor Pattern — AI는 지휘자, Python이 연주자

```
Claude Code (Conductor)
    │
    ├── Python 스크립트 생성
    │       ↓
    ├── Bash로 실행
    │       ↓
    ├── 결과(stdout, 파일) 읽기
    │       ↓
    └── 다음 행동 결정
```

Claude Code는 **데이터를 직접 처리하지 않는다**. Python 스크립트를 생성하고, Bash로 실행하고, 결과를 읽어 판단하는 **지휘자(Conductor)** 역할만 수행한다. 이 패턴의 근거:

- **C1 제약(Claude API = $0)**: 모든 NLP 분석은 로컬 Python 라이브러리만 사용. Claude API 비용 제로
- **결정론적 처리**: 동일 입력 → 동일 출력. LLM의 확률적 특성을 분석 파이프라인에서 제거
- **디버깅 용이**: Python 스크립트는 독립적으로 실행·테스트 가능
- **RLM 이론과의 정합**: "프롬프트를 신경망에 직접 넣지 말고, 외부 환경의 객체로 취급하라"

### 1.3 Staged Monolith — 왜 마이크로서비스가 아닌가

GlobalNews는 **단일 프로세스 내에서 4개 계층이 순차적으로 실행**되는 Staged Monolith 아키텍처를 채택했다.

마이크로서비스가 아닌 모놀리스를 선택한 이유:

| 판단 기준 | 마이크로서비스 | Staged Monolith (선택) |
|----------|-------------|---------------------|
| C3 제약 (단일 머신) | 여러 서비스 오케스트레이션 필요 | 단일 프로세스로 충분 |
| 메모리 제어 | 서비스 간 메모리 격리 | 단계 간 `gc.collect()`로 정밀 제어 |
| 데이터 전달 | 네트워크 I/O (gRPC, REST) | 디스크 파일 직접 전달 (Parquet) |
| 디버깅 | 분산 추적 필요 (Jaeger 등) | 단일 프로세스 내 상태 추적 |
| 운영 복잡도 | Docker, K8s, 서비스 디스커버리 | `python3 main.py --mode full` |
| 장애 격리 | 서비스 단위 독립 장애 | 단계별 체크포인트로 재개 가능 |

**"Staged"의 의미**: 모놀리스이지만 8개 분석 단계가 **명확한 경계**를 갖는다. 각 단계는 Parquet 파일을 입출력으로 사용하므로, 특정 단계만 재실행하거나 단계별로 디버깅할 수 있다. 마이크로서비스의 이점(독립 배포, 독립 확장)은 불필요하지만, 모듈 경계의 이점은 확보했다.

### 1.4 Never Give Up, Never Abandon — 4-Level 재시도 + Fairness Yield + Multi-Pass

GlobalNews의 크롤링 철학은 **포기하지 않는 것**이다. 크롤링 대상으로 지정된 사이트는 **어떤 상황에서도 영구적으로 포기하지 않는다**:

```
Level 1: NetworkGuard ×5    — HTTP 수준 재시도 (지수 백오프)
Level 2: Standard → TotalWar ×2  — 모드 전환 (undetected-chromedriver)
Level 3: Crawler ×3         — 라운드 딜레이 [30s, 60s, 120s]
Level 4: Pipeline ×3        — 전체 재시작 [60s, 120s, 300s]
───────────────────────────────────────────────────
이론적 최대: 5 × 2 × 3 × 3 = 90회 자동 시도

Never-Abandon Multi-Pass:
  L4 재시작 후 → 미완료 사이트 반복 크롤링 (최대 MULTI_PASS_MAX_EXTRA=10회)
  각 패스마다 새 SiteDeadline 할당 → Fairness Yield → 재큐잉
  10회 반복 후에도 미완료 → crawl_exhausted_sites.json 실패 리포트 생성

DynamicBypassEngine:
  Phase A: 12개 전략 디스패치 (5-Tier, 7 BlockTypes)
  Phase B: TotalWar fallback (전면 전환)
Tier 6: Claude Code 인터랙티브 분석으로 에스컬레이션
```

**SiteDeadline Fairness Yield 패턴 (ADR-065~067)**:

ThreadPoolExecutor(max_workers=5)에서 112개 사이트 (어댑터 123개)를 병렬 크롤링할 때, 한 사이트가 느리거나 차단되면 워커 스레드를 독점하여 다른 사이트의 크롤링이 지연된다. 이를 해결하기 위한 **협력적 공정성 메커니즘**:

1. **SiteDeadline 할당**: 각 사이트에 동적 타임아웃(최대 900초) 기반 데드라인 할당
2. **Fairness Yield**: 데드라인 만료 시 현재 워커를 양보(`break`) — 부분 결과 보존
3. **재큐잉**: yield된 사이트는 다음 패스에서 새 데드라인과 함께 재시도
4. **완료까지 반복**: `_get_incomplete_sites()` → `_run_single_pass()` 최대 `MULTI_PASS_MAX_EXTRA`(10)회 반복, 모든 112개 사이트 (어댑터 123개) 완료까지. 반복 후에도 미완료 사이트는 `crawl_exhausted_sites.json` 실패 리포트에 기록된다

**P1 `deadline_yielded` 플래그**: `CrawlResult.deadline_yielded: bool` 필드가 yield 시점에서 결정론적으로 `True`로 설정된다. 이 플래그는 3곳에서 할루시네이션을 봉쇄한다:

| 소비자 | 효과 |
|--------|------|
| `mark_site_complete` 게이팅 | `deadline_yielded=True` → CrawlState 완료 마킹 차단 |
| `_merge_result` Sticky 전파 | 완료된 결과(`deadline_yielded=False`)만 yielded 결과를 대체 |
| `_get_incomplete_sites` 판정 | CrawlState 확인(권위적 소스) → yielded 상태 확인 → 미완료 판정 |

**CrawlState-first 완료 판정**: `_get_incomplete_sites()`에서 CrawlState(권위적 소스)를 **먼저** 확인하여, 후속 패스에서 완료된 사이트의 stale `deadline_yielded=True` 결과가 무한 루프를 유발하는 것을 방지한다.

**DynamicBypassEngine**: 차단 유형(IP Block, UA Filter, Rate Limit, CAPTCHA, JS Challenge, Fingerprint, Geo-Block)을 진단하고, 해당 유형에 최적화된 전략을 5-Tier(T0~T4)로 에스컬레이션한다. 12개 전략(rotate_user_agent, exponential_backoff, stealth_headers, proxy_rotation, browser_rendering, captcha_solver, javascript_rendering, fingerprint_randomization, session_rotation, residential_proxy, distributed_crawling, human_simulation)이 등록되어 있으며, `retry_manager.py`의 `ALTERNATIVE_STRATEGIES`와 D-7 동기화로 정합성을 보장한다.

90회 자동 재시도가 과도해 보일 수 있다. 그러나:

- **뉴스 사이트는 일시적 장애가 빈번하다**: CDN 장애, 배포 중 다운타임, 지역 차단 등
- **포기 비용 > 재시도 비용**: 하루 데이터 누락은 시계열 분석의 연속성을 깨뜨린다
- **각 Level은 다른 전략을 시도한다**: 단순 재시도가 아니라, UA 교체 → 모드 전환 → 딜레이 증가 → 전체 리셋으로 **에스컬레이션**
- **Circuit Breaker가 보호한다**: 5연속 실패 시 300초 대기(OPEN) → 1건 시도(HALF_OPEN) → 복구 확인(CLOSED). `CRAWL_NEVER_ABANDON=True` 시 Circuit Breaker OPEN에서도 즉시 최대 에스컬레이션으로 재시도
- **DynamicBypassEngine가 지능적으로 대응한다**: 차단 유형을 진단한 뒤 해당 유형에 최적화된 전략부터 시도
- **24시간 안전 타임아웃**: 전체 파이프라인 수준의 안전망. 정상적으로는 도달하지 않지만, 치명적 hang 방지

### 1.5 112개 사이트 (어댑터 123개) — 왜 이 사이트들인가

112개 사이트 (어댑터 123개)는 10개 그룹(A-J)으로 조직된다:

| 그룹 | 지역 | 사이트 수 | 예시 |
|------|------|----------|------|
| A | 한국 주요 일간지 | 5 | 조선, 중앙, 동아, 한겨레, 연합 |
| B | 한국 경제지 | 4 | 매경, 한경, 파이낸셜, 머니투데이 |
| C | 한국 니치 | 3 | 노컷, 국민, 오마이 |
| D | 한국 IT/과학 | 10 | 38North, Bloter, 전자, ZDNet, Insight, Stratechery 등 |
| E | 영어권 | 22 | NYT, FT, WSJ, CNN, Bloomberg, BBC, Guardian 등 |
| F | 아시아-태평양 | 23 | SCMP, Yomiuri, Mainichi, TheHindu, Inquirer, VNExpress 등 |
| G | 유럽/중동 | 38 | Spiegel, LeMonde, Corriere, ElPais, AlJazeera, Haaretz 등 |
| H | 아프리카 | 4 | AllAfrica, Africanews, TheAfricaReport, Panapress |
| I | 라틴 아메리카 | 8 | Clarin, Folha, ElMercurio, BioBioChile, ElTiempo 등 |
| J | 러시아/중앙아시아 | 4 | RIA, RG, RBC, GoGo Mongolia |

선정 기준:

| 기준 | 설명 |
|------|------|
| **지역 다양성** | 한국(22), 영어권(22), 아시아태평양(23), 유럽/중동(38), 아프리카(4), 라틴아메리카(8), 러시아/중앙아시아(4) — 7대 권역 커버 |
| **언어 다양성** | 한국어, 영어, 중국어, 일본어, 프랑스어, 독일어, 스페인어, 이탈리아어, 아랍어, 히브리어, 포르투갈어, 러시아어, 베트남어 등 14+ 언어 |
| **도메인 다양성** | 종합(조선, NYT), 경제(FT, WSJ), 기술(ZDNet, Bloter), 지역(TheHindu, LeMonde, Clarin, AllAfrica) |
| **접근성** | Easy ~ Extreme — 4단계 난이도 분포 |
| **교차 분석 가치** | 동일 사건에 대한 다국어·다문화 프레이밍 비교가 가능한 조합 |

**의도적으로 제외한 것들**: 소셜 미디어(X, Reddit — API 비용/TOS), 방송사 웹(KBS, MBC — 동영상 중심), 포털(네이버, 다음 — 재배포 기사 중복)

**P1 사이트 레지스트리 동기화**: `validate_site_registry_sync.py`가 5개 하드코딩된 사이트 리스트(extract_site_urls, split_sites_by_group, validate_site_coverage, distribute_sites_to_teams, sources.yaml)를 교차 검증하여 도메인 정규화(6개 접두어 제거 + 2개 별칭 해소) 후 불일치를 탐지한다. 112개 사이트 (어댑터 123개) 간 desync는 silent failure의 근본 원인이다.

### 1.6 56개 분석 기법 — 왜 이렇게 많은가

"56개 기법이 정말 필요한가?"에 대한 답:

> **단일 기법으로는 뉴스 신호의 다면적 특성을 포착할 수 없다.**

기법들은 **계층적으로 조직**되어 있다:

```
기사 수준 (T01-T19): 각 기사의 언어·감성·감정·분류
    ↓ 집계
토픽 수준 (T21-T28): 기사 군집화, 토픽 모델링
    ↓ 시간축
시계열 수준 (T29-T36): 트렌드, 버스트, 변화점
    ↓ 관계
교차 분석 (T37-T50): 인과관계, 네트워크, 프레임
    ↓ 종합
신호 분류 (T47-T55): 5-Layer 최종 판정
```

각 계층은 이전 계층의 출력을 입력으로 사용한다. 56개 기법 중 하나만 빠져도 후속 계층의 정보가 불완전해진다. 이것은 "많이 넣을수록 좋다"가 아니라, **분석 목표(5-Layer 신호 분류)를 역산하여 필요한 기법을 도출**한 결과이다.

### 1.7 5-Layer 신호 계층 — 설계 근거

| Layer | 이름 | 기간 | 핵심 질문 |
|-------|------|------|----------|
| L1 | Fad | < 1주 | "이 뉴스가 일시적 유행인가?" |
| L2 | Short-term | 1-4주 | "이 뉴스가 단기 트렌드인가?" |
| L3 | Mid-term | 1-6개월 | "이 뉴스가 구조적 변화의 일부인가?" |
| L4 | Long-term | 6개월+ | "이 뉴스가 장기 전환을 나타내는가?" |
| L5 | Singularity | 12개월+ | "이 뉴스가 패러다임 전환을 예고하는가?" |

**L5 Singularity의 특별한 설계**:

L5는 단일 지표로 판정하지 않는다. **3개 독립 경로 중 2개 이상의 합의**를 요구한다:

1. **OOD 경로**: LOF + Isolation Forest — 통계적 이상치 감지
2. **Structural 경로**: 변화점(PELT) + BERTrend — 구조적 변화 + 토픽 생애주기
3. **Distribution 경로**: Zipf 편차 + KL 다이버전스 — 분포적 이상

이 "2-of-3" 합의 메커니즘은 단일 경로의 오탐(false positive)을 억제한다. Singularity라 불릴 만한 신호는 **여러 관점에서 동시에 비정상적**이어야 한다.

### 1.8 날짜별 파티션 — 시간은 1등 시민이다

```
data/
├── raw/2026-02-27/
├── processed/2026-02-27/
├── features/2026-02-27/
├── analysis/2026-02-27/
└── output/2026-02-27/
```

모든 데이터 경로에 날짜가 포함되는 이유:

- **재실행 안전성**: 특정 날짜만 재처리 가능, 다른 날짜 데이터 영향 없음
- **시계열 분석 전제**: Stage 5-7의 시계열 분석은 날짜별 데이터가 기본 단위
- **아카이빙**: 30일 이상 데이터를 날짜 단위로 압축 보관
- **DuckDB glob 쿼리**: `SELECT * FROM 'data/output/*/analysis.parquet'`로 기간 범위 집계

### 1.9 의도적으로 하지 않은 것들

| 하지 않은 것 | 이유 |
|-------------|------|
| 실시간 스트리밍 | 일일 배치가 뉴스 분석에 충분. 실시간은 복잡도 대비 가치 부족 |
| GPU 의존 | M2 Pro CPU로 충분. Model2Vec이 BERTopic에서 CPU 500x 가속 |
| 클라우드 배포 | C3 제약(단일 머신). cron + 로컬 실행이 단순하고 비용 제로 |
| Claude API 호출 분석 | C1 제약. 모든 NLP는 로컬 라이브러리(KoBERT, SBERT, BART-MNLI 등) |
| 전체 기사 번역 | 교차 언어 분석은 SBERT multilingual 임베딩으로 해결. 번역은 불필요 |
| 사용자 계정 시스템 | 단일 연구자 도구. 멀티테넌시 불필요 |
| 외부 데이터베이스 | Parquet + SQLite만으로 수천~수만 건 분석에 충분 |

---

## 2. 시스템 아키텍처 (System Architecture)

### 2.1 7+ Layer 아키텍처 (2026-Q2 확장)

```
┌──────────────────────────────────────────────────────────────────────┐
│                         main.py (CLI)                                │
│      crawl │ analyze │ full │ status │ insight │ dci                 │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   ┌────────────────────────┐                                         │
│   │ Layer 1: CRAWLING      │  112 enabled sites · 123 adapters       │
│   │ pipeline + anti-block  │  4-Level retry · DynamicBypassEngine    │
│   │ → data/raw/{date}/     │  Never-Abandon Multi-Pass               │
│   └──────────┬─────────────┘                                         │
│              │ all_articles.jsonl                                    │
│   ┌──────────▼─────────────┐                                         │
│   │ Layer 2: ANALYSIS      │  Stage 1-8 NLP, 56 techniques           │
│   │ pipeline (8 stages)    │  → data/{processed,features,analysis}/  │
│   └──────────┬─────────────┘                                         │
│              │ Parquet                                               │
│   ┌──────────▼─────────────┐                                         │
│   │ Layer 3: STORAGE       │  Parquet ZSTD + SQLite FTS5 + sqlite-vec│
│   │ → data/output/{date}/  │                                         │
│   └──────────┬─────────────┘                                         │
│              │                                                       │
│   ┌──────────▼──────────────────────────────────────────────────┐    │
│   │ Layer 4: BIGDATA ENGINE — POST-PROCESSING (Step 6.7)        │    │
│   │  · articles_enriched_assembler   (35-field enriched parquet)│    │
│   │  · geo_focus_extractor / source_metadata_joiner             │    │
│   │  · steeps_classifier (8 cats incl. SPI Spirituality + CRS)  │    │
│   │  · question_engine (18 forced-answer questions)             │    │
│   │  · gti (Geopolitical Tension Index, 0-100)                  │    │
│   │  · signal_portfolio (single SOT, lifecycle tracking)        │    │
│   │  · weekly_future_map (7-day synthesis, EN+KO)               │    │
│   │ → data/{enriched,answers,gti}/, data/signal_portfolio.yaml  │    │
│   │ → reports/weekly_future_map/                                │    │
│   └──────────┬──────────────────────────────────────────────────┘    │
│              │                                                       │
│   ┌──────────▼─────────────┐                                         │
│   │ Layer 5: INSIGHT (B)   │  ← Workflow B (read-only on Layer 2)    │
│   │ 7 modules (M1-M7)      │  27 metrics, multi-date window          │
│   │ → data/insights/{id}/  │                                         │
│   └──────────┬─────────────┘                                         │
│              │                                                       │
│   ┌──────────▼──────────────────────────────────────────────────┐    │
│   │ Layer 6: NARRATIVE LAYERS                                    │   │
│   │  · Public Narrative 3-Layer  (Step 6.5, ADR-080)             │   │
│   │       reports/public/{date}/{interpretation,insight,future}  │   │
│   │  · Chart Interpretations 6-tab (Step 6.6, ADR-082)           │   │
│   │       data/analysis/{date}/interpretations.json              │   │
│   │  · W4 Master / DCI appendix (Step 6.45a/b)                   │   │
│   │  · @insight-narrator refinement (Step 6.4)                   │   │
│   └──────────┬──────────────────────────────────────────────────┘    │
│              │                                                       │
│   ┌──────────▼──────────────────────────────────────────────────┐    │
│   │ Layer 7: PUBLISHING — WF5 Personal Newspaper (Step 7, ADR-083)│  │
│   │  17-agent editorial team (Chief + 6 continental + 6 STEEPS    │  │
│   │  + 4 specialty); 15 editorial principles                      │  │
│   │  → newspaper/daily/{date}/, newspaper/weekly/{week}/          │  │
│   └───────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   [Independent track] DCI — Deep Content Intelligence (--mode dci)   │
│   ┌────────────────────────────────────────────────────────────┐     │
│   │  L-1 → L11 (14 layers); SG-Superhuman 10-gate verification │     │
│   │  CE4 3-layer evidence chain; 5-agent team (5조항 P1 DNA)   │     │
│   │  ← data/raw/{date}/all_articles.jsonl  (no W2/W3 deps)     │     │
│   │  → data/dci/runs/{run_id}/final_report.md                  │     │
│   └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│   [External hop] Step 6.8: LLM Wiki ingest (background)              │
│       nohup auto-wiki-ingest.sh → llm-wiki-environmentscanning/      │
│                                                                      │
│   ┌──────────────────────────────────────────────────────────────┐   │
│   │ Layer 8: PRESENTATION — 2 Streamlit apps + DuckDB/Pandas     │   │
│   │  · dashboard.py (port 8501) — 6 tabs incl. 18 Questions      │   │
│   │  · insights_dashboard.py (port 8502) — 8 tabs (M1-M7)        │   │
│   │  · dashboard_insights.py — helper (no UI; deterministic)     │   │
│   └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   [Shared] config/ │ utils/ │ constants.py                           │
└──────────────────────────────────────────────────────────────────────┘
```

각 Layer의 경계는 **파일 시스템 디렉터리**로 물리적으로 구분된다 — 단일 디렉터리에 단일 producer가 쓴다 (절대 기준 2 SOT 준수). DCI는 W2/W3에 대한 **읽기 의존이 없는** 독립 워크플로우이며 (ADR-079), Layer 4 BigData Engine은 Layer 2 산출물을 read-only로 소비하여 Layer 6/7의 입력을 준비한다.

### 2.2 모듈 구조

```
src/                              (275 modules · ~80,733 LOC)
├── config/
│   └── constants.py              350+ 상수: 경로, 임계값, 스키마, ENABLED_DEFAULT, CRAWL_NEVER_ABANDON
├── crawling/                     크롤링 엔진 + 123 어댑터 (~149 모듈)
│   ├── pipeline.py               크롤링 오케스트레이터 + SiteDeadline + Multi-Pass
│   ├── network_guard.py          5-retry HTTP 클라이언트
│   ├── url_discovery.py          3-Tier URL 발견 (RSS · Sitemap · DOM) + KST→UTC 보정
│   ├── article_extractor.py      추출 체인 + 페이월 감지 + html.unescape (HTML 엔티티 해제)
│   ├── browser_renderer.py       서브프로세스 기반 Patchright/Playwright
│   ├── adaptive_extractor.py     4-stage CSS 선택자 적응형 추출
│   ├── dedup.py                  3-Level 중복 제거 (URL · Title Jaccard · SimHash)
│   ├── anti_block.py             6-Tier 에스컬레이션
│   ├── dynamic_bypass.py         DynamicBypassEngine — 12전략, 5-Tier, 7 BlockTypes
│   ├── block_detector.py         7-type 차단 진단
│   ├── circuit_breaker.py        상태 머신 (CLOSED → OPEN → HALF_OPEN)
│   ├── retry_manager.py          4-Level 재시도 + 12개 ALTERNATIVE_STRATEGIES (D-7 동기화)
│   ├── session_manager.py        쿠키/세션 생애주기
│   ├── ua_manager.py             61+ User-Agent 4-tier
│   ├── crawler.py                ENABLED_DEFAULT SOT 소비자 — 런타임 enabled 판정
│   ├── contracts.py              RawArticle + CrawlResult(deadline_yielded) + source_domain 필드
│   ├── crawl_report.py           사이트별 리포트
│   └── adapters/                 123개 사이트별 어댑터
│       ├── base_adapter.py       추상 기반 클래스
│       ├── kr_major/             12: 조선, 중앙, 동아, 한겨레, 연합 등 (Groups A+B+C)
│       ├── kr_tech/              11: Bloter, 전자신문, ZDNet 등 (Group D)
│       ├── english/              22: NYT, FT, WSJ, CNN, Bloomberg, BBC, TechCrunch, TheVerge,
│       │                              Ars Technica, 404 Media 등 (Group E)
│       └── multilingual/         78: AlJazeera, SCMP, Spiegel, Globaltimes, etc. (Groups F-J)
│
├── analysis/                     8단계 NLP + BigData Engine (20 모듈)
│   ├── pipeline.py               분석 오케스트레이터 (Stage 1-8 + post-processing trigger)
│   ├── stage1_preprocessing.py   전처리: Kiwi + spaCy
│   ├── stage2_features.py        피처: SBERT + TF-IDF + NER (Davlan XLM-RoBERTa)
│   ├── stage3_article_analysis.py 감성 + 감정 + STEEPS + DistilBART (3× 속도 향상)
│   ├── stage4_aggregation.py     BERTopic + HDBSCAN + Louvain
│   ├── stage5_timeseries.py      STL + PELT + Kleinberg + Prophet
│   ├── stage6_cross_analysis.py  Granger + PCMCI + co-occurrence (betweenness 노이즈 98% 제거)
│   ├── stage7_signals.py         5-Layer 신호 분류
│   ├── stage8_output.py          Parquet + SQLite FTS5 + sqlite-vec
│   │
│   ├── articles_enriched_assembler.py  [BigData] 35-field articles_enriched.parquet
│   ├── geo_focus_extractor.py          [BigData] source_country ≠ geo_focus 분리, 120개국
│   ├── source_metadata_joiner.py       [BigData] source_tier(GLOBAL~NICHE) + ideology
│   ├── steeps_classifier.py            [BigData] STEEPSS 8 카테고리 (SOC/TEC/ECO/ENV/POL/SEC/SPI/CRS)
│   │                                   Hybrid: 키워드 Tier 1 (<1ms) → XLM-RoBERTa Tier 2 (150ms)
│   ├── signal_classifier.py            [BigData] Layer A/B (BREAKING/TREND/WEAK_SIGNAL/NOISE)
│   ├── question_engine.py              [BigData] 18-Question 강제 응답 엔진
│   │                                   → data/answers/{date}/q01-q18.json + summary.json
│   ├── gti.py                          [BigData] Geopolitical Tension Index (40·35·25 합성)
│   │                                   → data/gti/{date}/gti_daily.json + gti_history.jsonl
│   ├── signal_portfolio.py             [BigData] 신호 포트폴리오 단일 SOT (lifecycle 추적)
│   │                                   → data/signal_portfolio.yaml
│   └── weekly_future_map.py            [BigData] 7-day 종합 미래 맵 (EN + KO, pure Python)
│                                       → reports/weekly_future_map/{YYYY-W##}/
│
├── insights/                     Workflow B: 빅데이터 통찰 분석 (12 모듈)
│   ├── pipeline.py               통찰 오케스트레이터
│   ├── window_assembler.py       다중 날짜 코퍼스 조립 + 지연 로딩
│   ├── validators.py             P1 결정론적 검증 — 27개 지표 수학적 경계
│   ├── constants.py              27개 지표 임계값 중앙화
│   ├── m1_crosslingual.py        교차언어 정보 비대칭 (JSD, Wasserstein, filter-bubble)
│   ├── m2_narrative.py           내러티브 & 프레이밍
│   ├── m3_entity.py              엔티티 궤적 & 숨은 연결
│   ├── m4_temporal.py            시간 패턴 & 캐스케이드
│   ├── m5_geopolitical.py        지정학 분석 & BRI (414 pairs)
│   ├── m6_economic.py            경제 인텔리전스 & EPU (12 langs)
│   └── m7_synthesis.py           M7 Intelligence — entity_profiles, pair_tensions,
│                                 evidence_articles, risk_alerts (4 Parquet)
│
├── dci/                          Deep Content Intelligence (61 모듈, ~10.3K LOC)
│   ├── orchestrator.py           7-Phase 오케스트레이션
│   ├── layers/                   14 layers (L-1 → L11):
│   │   ├── l0_discourse.py        Kiwi + 다국어 regex + PDTB
│   │   ├── l1_semantic.py         claims/quotes/numerical/CAMEO
│   │   ├── l1_5_meaning.py        SPO + FrameNet-lite
│   │   ├── l2_relations.py        Allen 13-relation + timex3 + hedging
│   │   ├── l3_kg_hypergraph.py    NetworkX entity 공기 + community
│   │   ├── l4_cross_document.py   SimHash + Jaccard thread clustering
│   │   ├── l5_psycho_style.py     textstat + TTR/MTLD/HDD + Burrows Delta
│   │   ├── l6_triadic.py          [Claude CLI] 4-lens (α/β/γ/δ-critic)
│   │   ├── l6_checkpoint.py       L6 중간 체크포인트
│   │   ├── l7_graph_of_thought.py 순수 Python Bayesian DAG
│   │   ├── l8_monte_carlo.py      1,024-leaf scenario tree
│   │   ├── l9_metacognitive.py    블라인드 스팟 + 불확실성 분해
│   │   └── l10_final_report.py    [Claude CLI] Doctoral narrator + CE3 숫자 검증
│   ├── ensemble/                 Claude CLI 클라이언트 + 재검증
│   ├── verifiers/                NLI/Triadic/Critic 검증
│   ├── evidence_ledger.py        CE4 3-layer evidence chain (article/segment/char)
│   ├── failure_policy.py         14-layer 결정론 매트릭스
│   ├── resume.py                 checkpoint schema v1
│   └── sg_superhuman.py          10-gate 검증 엔진
│
├── newspaper/                    WF5 — Personal Newspaper (8 모듈, ADR-083)
│   ├── prompt_builder.py         14-desk 프롬프트 조립
│   ├── story_clusterer.py        스토리 클러스터링
│   ├── organizers.py             Country/STEEPS 조직화
│   ├── budget_adjuster.py        분량 ±20% 자동 조정
│   ├── country_mapper.py         지역 → 대륙 매핑
│   ├── entity_linker.py          엔티티 link
│   ├── html_renderer.py          NYT-style HTML 렌더링
│   ├── agent_prompts/            14-desk 프롬프트 템플릿
│   └── templates/                HTML 템플릿
│
├── public_narrative/             3-Layer Public Narrative (ADR-080)
│   ├── facts_extractor.py        facts_pool.json 생성 (단일 진실 원천)
│   ├── narrator.py               Claude CLI prose 생성
│   ├── validators.py             8 PUB 검증 (FKGL · jargon · number parity · 금지어 · EN↔KO)
│   ├── glossary_simple.yaml      일반인 용어 사전
│   └── templates/                3-Layer 템플릿
│
├── interpretations/              Chart Interpretations 6-tab (ADR-082)
│   ├── facts_pool.py             탭별 facts pool
│   ├── salient_facts.py          Salient fact 추출
│   ├── baseline_builder.py       baseline 비교 데이터
│   ├── future_linker.py          Future Outlook 카드 링킹
│   ├── prompt_composer.py        Claude CLI 프롬프트 조립
│   ├── validators.py             CI1-CI6 검증
│   └── templates/                탭별 카드 템플릿
│
├── reports/                      후처리 보고서 (Step 6.45a/b)
│   ├── w4_appendix.py            W4 Master Integration 부록
│   └── dci_layer_summary.py      DCI 레이어 요약 부록
│
├── storage/                      데이터 I/O
│   ├── parquet_writer.py         ZSTD 압축 + 원자적 쓰기
│   └── sqlite_builder.py         FTS5 + vec 인덱스
│
└── utils/                        유틸리티
    ├── logging_config.py         구조화 로깅
    ├── config_loader.py          YAML 로딩 + 검증
    ├── error_handler.py          예외 계층 + 재시도 데코레이터
    └── self_recovery.py          자기 복구 메커니즘 + 잠금 관리
```

**루트 진입점**:
- `main.py` (874 lines) — CLI: 6개 모드 (crawl · analyze · full · status · insight · dci)
- `dashboard.py` (2,319 lines) — 종합 Streamlit 대시보드 (6 tabs incl. 18 Questions)
- `dashboard_insights.py` (1,222 lines) — Helper 모듈 (LLM 미사용 결정론적 인사이트 카드)
- `insights_dashboard.py` (758 lines) — Workflow B 전용 Streamlit (8 tabs M1-M7)
- `repair-pipeline.py` — 파이프라인 복구 보조 스크립트

**테스트 인벤토리**: `tests/` 109 파일, ~41,524 LOC.

---

## 3. 크롤링 엔진 (Layer 1)

### 3.1 크롤링 파이프라인 흐름

```
sources.yaml (112 enabled sites)
       │
       ▼
┌──────────────────────────────────────────────────┐
│              CrawlingPipeline                     │
│  ┌────────────┐  ┌───────────────┐               │
│  │ SiteAdapter │  │ NetworkGuard  │               │
│  │ (116개)     │  │ (5-retry HTTP)│               │
│  └──────┬─────┘  └───────┬───────┘               │
│         │                │                        │
│  ┌──────▼────────────────▼───────┐               │
│  │       URL Discovery            │               │
│  │  Tier 1: RSS (feedparser)      │               │
│  │  Tier 2: Sitemap (lxml)        │               │
│  │  Tier 3: DOM (BeautifulSoup)   │               │
│  └──────────────┬─────────────────┘               │
│                 │ DiscoveredURL[]                  │
│  ┌──────────────▼─────────────────┐               │
│  │    Article Extraction           │               │
│  │  Chain: Fundus → Trafilatura    │               │
│  │        → Newspaper4k → CSS     │               │
│  │                                 │               │
│  │  Paywall Branch (하드 페이월):  │               │
│  │    BrowserRenderer (Patchright) │               │
│  │    → Extraction Chain 재시도    │               │
│  │    → AdaptiveExtractor (CSS)   │               │
│  │    → Title-only fallback       │               │
│  └──────────────┬─────────────────┘               │
│                 │ RawArticle                       │
│  ┌──────────────▼─────────────────┐               │
│  │    Deduplication (3-Level)      │               │
│  │  L1: URL normalize (O(1))      │               │
│  │  L2: Title Jaccard (≥0.8)      │               │
│  │  L3: SimHash Hamming (≤10bit)  │               │
│  └──────────────┬─────────────────┘               │
│                 ▼                                  │
│     data/raw/YYYY-MM-DD/all_articles.jsonl        │
│                                                    │
│  ┌─────────────────────────────────────────┐      │
│  │       Never-Abandon Multi-Pass          │      │
│  │  for pass in range(MULTI_PASS_MAX_EXTRA):│     │
│  │    → SiteDeadline(fresh) 할당           │      │
│  │    → _run_single_pass(incomplete)       │      │
│  │    → P1 merge: completed replaces yielded│     │
│  │    → _get_incomplete_sites() 재판정     │      │
│  │    (CrawlState-first 완료 확인)         │      │
│  │  cap 도달 후 미완료 → 실패 리포트 생성  │      │
│  └─────────────────────────────────────────┘      │
└──────────────────────────────────────────────────┘
```

### 3.2 사이트 어댑터 시스템

**설계 철학**: 112개 사이트 (어댑터 123개)는 DOM 구조, 인코딩, 페이월, 차단 방식이 모두 다르다. 범용 크롤러로는 높은 수집률을 달성할 수 없다. 따라서 **사이트마다 전용 어댑터**를 구현하되, **공통 로직은 기반 클래스에 집중**시킨다.

**기반 클래스** `BaseSiteAdapter` (450+ lines)가 제공하는 것:
- URL 발견 (RSS/Sitemap/DOM) 공통 로직
- 기사 추출 체인 (Fundus → Trafilatura → Newspaper4k → CSS)
- 재시도, 안티블록, 세션 관리 위임
- 날짜 파싱, 인코딩 처리

**어댑터가 오버라이드하는 것**:
- 사이트 메타 (`SITE_ID`, `SITE_URL`, `LANGUAGE`, `RSS_URLS`)
- CSS 선택자 (`TITLE_CSS`, `BODY_CSS`, `DATE_CSS`)
- 안티블록 티어, UA 티어, 요청 간격
- 페이월 처리 로직 (있는 경우)

| 그룹 | 디렉터리 | 사이트 수 | 소스 그룹 | 특징 |
|------|---------|----------|----------|------|
| Korean Major | `kr_major/` | 12 | A+B+C | 네이버 연동, Kiwi 토크나이저, 한국어 날짜 파싱 |
| Korean Tech | `kr_tech/` | 10 | D | 기술 뉴스, RSS 중심, 영어 니치(Stratechery, Techmeme) 포함 |
| English | `english/` | 22 | E | 페이월 사이트(NYT, FT, WSJ) 포함, Politico EU, Nature Asia 등 |
| Multilingual | `multilingual/` | 77 | F+G+H+I+J | CJK, RTL(아랍/히브리), 유럽 6개 언어, 라틴아메리카, 아프리카, 러시아 |

### 3.3 안티블록 시스템

**7가지 차단 유형 진단** (BlockDetector):

IP Block, UA Filter, Rate Limit, CAPTCHA, JS Challenge, Fingerprint, Geo-Block

**7-Tier 에스컬레이션 + DynamicBypassEngine**:

```
Tier 1: 딜레이 증가 + UA 회전
    ↓ 실패
Tier 2: 세션/쿠키 재설정
    ↓ 실패
Tier 3: Playwright (headless 브라우저) — BrowserRenderer (서브프로세스, fresh context)
    ↓ 실패
Tier 4: Patchright + 핑거프린트 위장 — BrowserRenderer (C++ 수준 패치)
    ↓ 실패
Tier 5: AdaptiveExtractor — 4-stage CSS 선택자 적응형 추출 (§3.6.2)
    ↓ 실패
Tier 6: Never-Abandon (DynamicBypassEngine Phase A → TotalWar Phase B)
    ↓ 실패
Tier 7: Claude Code 인터랙티브 분석 (에스컬레이션)
```

**DynamicBypassEngine** (`dynamic_bypass.py`): 차단 유형(7 BlockTypes)에 따라 최적 전략을 5-Tier(T0~T4)로 자동 에스컬레이션한다. `is_at_max_escalation()` 메서드로 최대 에스컬레이션 도달을 확인하고, `get_all_max_escalation_sites()`로 전체 사이트의 에스컬레이션 상태를 조회한다.

**Circuit Breaker 상태 머신**:

```
CLOSED ─(5연속 실패)─→ OPEN ─(300초 대기)─→ HALF_OPEN ─(성공)─→ CLOSED
                                                        └(실패)─→ OPEN
```

### 3.4 Bypass Discovery Fallback + 실패 리포트

**Bypass Discovery**: URL 발견 단계에서 RSS/Sitemap이 차단되면, `DynamicBypassEngine`이 최대 `DISCOVERY_BYPASS_MAX_ATTEMPTS`(5)회까지 대체 전략으로 피드를 재요청한다. 응답은 `_parse_discovery_response()`가 결정론적 XML 태그 검사(`<rss>`, `<feed>`, `<urlset>`, `<sitemapindex>`)로 콘텐츠 타입을 판별하고, `URLDiscovery`의 공개 프록시 메서드(`parse_feed_from_text()`, `parse_sitemap_from_text()`)로 파싱한다. 바이패스 상태는 `data/config/bypass_state.json`에 크로스-크롤링 학습 데이터로 영속화된다.

**Producer-Consumer 계약**: `pipeline.py::_generate_failure_report()`(생산자)가 Never-Abandon 루프 완료 후 미완료 사이트의 실패 리포트를 `crawl_exhausted_sites.json`으로 생성한다. `scripts/check_crawl_progress.py`(소비자)가 이 리포트를 읽어 실패 사이트를 표시한다. 스키마 계약: `{"exhausted_sites": [{"site_id": str, "failure_category": str, "recommendation": str, ...}]}`. 3가지 실패 카테고리: `discovery_blocked`(URL 발견 0건), `extraction_blocked`(URL 발견 > 0 but 기사 0건), `partial_timeout`(일부 기사 수집).

**P1 로그 계약**: `pipeline.py`의 로그 포맷 문자열과 `check_crawl_progress.py`의 regex 패턴이 정합한다. 바이패스 경로 로그는 `bypass_` 접두어(`bypass_article_outside_24h`, `bypass_article_deduped`)로 구분되어 메인 경로 소비자의 substring 매칭에 의한 오수집을 방지한다.

### 3.5 중복 제거 (3-Level)

| Level | 방법 | 기준 | 비용 |
|-------|------|------|------|
| L1 | URL 정규화 + 정확 매칭 | 쿼리 파라미터 제거, 프로토콜 정규화 | O(1) 해시 |
| L2 | 제목 유사도 | Jaccard ≥ 0.8 + Levenshtein ≤ 0.2 | O(n) 비교 |
| L3 | SimHash 본문 핑거프린트 | 64bit 해밍 거리 ≤ 10 | O(1) XOR |

저장소: `data/dedup.sqlite` (크로스-런 지속)

### 3.6 페이월 바이패스 시스템 (Paywall Bypass)

하드 페이월 사이트(FT, NYTimes, WSJ, Bloomberg, Le Monde)에서 기사 본문을 추출하기 위한 3-layer 시스템이다.

**설계 근거**: Wayback Machine은 하드 페이월 사이트에 비효과적이다 (아카이브된 콘텐츠도 페이월 상태 유지). 따라서 **브라우저 렌더링 + 적응형 추출**이 주요 전략이다.

#### 3.6.1 BrowserRenderer (서브프로세스 기반)

```
Main Pipeline (동기, httpx)
    │
    └── subprocess.run(python -c RENDER_SCRIPT, timeout=45s)
            │
            └── Patchright (async) → fresh browser context → HTML
```

**핵심 설계 결정**:

| 결정 | 근거 |
|------|------|
| 서브프로세스 격리 | 메인 파이프라인은 동기(httpx). Patchright는 비동기. 서브프로세스로 이벤트 루프 충돌 방지 + 프로세스 수준 장애 격리 |
| Fresh context (쿠키 없음) | 메터드 페이월 사이트에 "첫 방문" 경험 제공 → 기사 전문 노출 |
| Patchright 우선 | C++ 수준 자동화 패치로 봇 탐지 불가. Playwright로 fallback |
| Hard timeout (45초) | `subprocess.run(timeout=)` — 브라우저 프로세스 hang 시 강제 kill 보장 |
| 사이트별 실패 카운터 | 3회 연속 실패 시 해당 사이트 렌더링 건너뛰기 (early bail-out) — 파이프라인 전체 지연 방지 |

#### 3.6.2 AdaptiveExtractor (4-stage CSS 선택자)

브라우저 렌더링된 HTML에서 표준 추출 체인이 실패할 때 동작하는 적응형 추출기:

```
Stage 1: 캐시된 선택자 (이전 성공 패턴)
    ↓ 실패
Stage 2: 사이트별 알려진 선택자 (_KNOWN_SELECTORS)
    ↓ 실패
Stage 3: 범용 CSS 선택자 (article, main p, div.post-content 등)
    ↓ 실패
Stage 4: 휴리스틱 단락 추출 (<p> 태그 중 40자 이상, nav/footer/header 제외)
```

**보안**: exec()/eval() 완전 제거 (P1-3). 모든 추출은 BeautifulSoup CSS 선택자만 사용. 네트워크·파일·시스템 호출 없음.

#### 3.6.3 페이월 텍스트 감지 (is_paywall_body)

브라우저 렌더링 후에도 페이월이 남아있는지 판별하는 결정론적 감지기:

| 분류 | 패턴 예시 | 개수 |
|------|----------|------|
| **Strong** (명령형, 독자 지시) | "subscribe to unlock", "this article is for subscribers only", "abonnez-vous" | 14개 (EN 8 + FR 6) |
| **Weak** (사실적, 모호) | "premium content", "per month", "to continue reading" | 12개 (EN 9 + FR 3) |

**판정 로직**: `strong ≥ 2` → 페이월 확정. `strong ≥ 1 AND len < 2000` → 짧은 본문 + 하나라도 강력한 지표 = 페이월 판정.

**다국어 지원**: 영어 + 프랑스어(Le Monde) 패턴. "to continue reading" 등 일반 문장에서도 나타나는 모호한 패턴은 WEAK으로 분류하여 false positive 방지.

#### 3.6.4 페이월 브랜치 흐름 (article_extractor.py)

```
기사 추출 시도 (Fundus → Trafilatura → CSS)
    │
    ├── 성공 (body ≥ 200자 + is_paywall_body=False) → RawArticle 반환
    │
    └── 실패 또는 페이월 감지
            │
            ├── BrowserRenderer.render(url, source_id)
            │       ↓ 성공
            │   추출 체인 재시도 (렌더링된 HTML에서)
            │       ↓ 성공 + is_paywall_body=False → RawArticle (crawl_tier=3, crawl_method="playwright")
            │       ↓ 실패
            │   AdaptiveExtractor.extract_body(html, source_id)
            │       ↓ 성공 → RawArticle (crawl_tier=5, crawl_method="adaptive")
            │       ↓ 실패
            │   Title-only fallback (is_paywall_truncated=True)
            │
            └── BrowserRenderer 없음 또는 실패
                    → Title-only fallback (is_paywall_truncated=True)
```

### 3.7 데이터 계약 (RawArticle)

```python
@dataclass(frozen=True)
class RawArticle:
    url: str                    # 원본 URL (필수)
    title: str                  # 제목 (필수)
    body: str                   # 본문 (필수)
    source_id: str              # 사이트 ID (필수)
    source_name: str            # 사이트 이름
    language: str               # ISO 639-1
    published_at: datetime      # 발행일시
    crawled_at: datetime        # 크롤링일시
    author: str | None          # 저자
    category: str | None        # 카테고리
    content_hash: str           # SHA-256 본문 해시
    crawl_tier: int             # 사용된 티어 (1=RSS/Sitemap, 2=DOM, 3=Playwright, 4=Patchright, 5=Adaptive)
    crawl_method: str           # "rss", "sitemap", "dom", "playwright", "adaptive", "api"
    is_paywall_truncated: bool  # 페이월 절단 여부 (하드 페이월 → title-only)
```

`frozen=True`로 불변 객체를 보장한다. 크롤링 엔진 출력의 **유일한 계약**이며, 분석 파이프라인은 이 계약만 의존한다.

**CrawlResult 계약** (사이트별 크롤링 결과):

```python
@dataclass
class CrawlResult:
    source_id: str
    articles: list[RawArticle]
    discovered_urls: int = 0
    extracted_count: int = 0
    failed_count: int = 0
    skipped_dedup_count: int = 0
    skipped_freshness_count: int = 0
    elapsed_seconds: float = 0.0
    tier_used: int = 1
    errors: list[str] = field(default_factory=list)
    deadline_yielded: bool = False  # P1: Fairness Yield 시 True, false completion 봉쇄
```

`deadline_yielded` 필드는 SiteDeadline 만료 시 `break` 지점에서 결정론적으로 `True`로 설정된다. 이 플래그가 `True`인 CrawlResult는 `mark_site_complete()` 호출이 차단되어, 부분 크롤링 결과가 완료로 잘못 기록되는 것을 방지한다.

---

## 4. 분석 파이프라인 (Layer 2)

### 4.1 8단계 파이프라인 흐름

```
all_articles.jsonl
  │
  ▼
Stage 1 (전처리) → articles.parquet
  │
  ▼
Stage 2 (피처) → embeddings/tfidf/ner.parquet
  │
  ▼
Stage 3 (기사 분석) → article_analysis.parquet + mood_trajectory.parquet
  │
  ▼
Stage 4 (집계) → topics/networks/dtm/aux_clusters.parquet
  │
  ├─────────────────────────────────────┐
  ▼                                     ▼
Stage 5 (시계열, 독립)              Stage 6 (교차분석, 독립)
→ timeseries.parquet                → cross_analysis.parquet
  │                                     │
  └──────────────┬──────────────────────┘
                 ▼
Stage 7 (신호 분류) → signals.parquet
  │
  ▼
Stage 8 (출력) → analysis.parquet + index.sqlite + checksums.md5
```

**Stage 5와 6의 독립성**: 이 두 단계는 Stage 4의 출력만을 입력으로 사용하며, 서로 의존하지 않는다. 이론적으로는 병렬 실행이 가능하지만, 메모리 제약(C3)으로 순차 실행한다. Stage 7은 Stage 5와 6 모두의 출력을 필요로 한다.

**듀얼 워크플로우 분기점 — Stage 4 이후**:

Stage 4까지의 출력은 두 개의 독립적 워크플로우가 소비한다:

```
Stage 4 출력 (topics, networks, article_analysis, etc.)
  │
  ├──→ Workflow A (Daily): Stage 5-8 (시계열 → 교차분석 → 신호 → 출력)
  │    매일 실행. 단일 날짜 데이터. data/output/{date}/
  │
  └──→ Workflow B (Insight): M1-M7 (교차언어 → 내러티브 → 엔티티 → 시간 → 지정학 → 경제 → 종합)
       주간/월간/온디맨드. 다중 날짜 윈도우(7/30/90일). data/insights/{run_id}/
       READ-ONLY 소비. Workflow A 코드에 대한 의존 없음.
```

상세: §4.4 Workflow B (Insight Analytics Pipeline)

### 4.2 단계별 상세

#### Stage 1: 전처리 (T01-T06) — ~1.0 GB

| 기법 | 라이브러리 | 역할 |
|------|----------|------|
| T01 한국어 형태소 분석 | kiwipiepy (singleton) | 형태소 단위 토큰화 |
| T02 영어 레마타이제이션 | spaCy en_core_web_sm | 원형 복원 |
| T03 문장 분리 | Kiwi (ko) / spaCy (en) | 문장 경계 감지 |
| T04 언어 감지 | langdetect | 자동 언어 식별 |
| T05 텍스트 정규화 | NFKC + 공백 | 유니코드 통일 |
| T06 불용어 제거 | 커스텀 (ko) + spaCy (en) | 노이즈 제거 |

**Kiwi singleton 패턴**: Kiwi 재로딩 시 +125MB 메모리 누수가 발생한다. 따라서 프로세스 수명 동안 단일 인스턴스를 유지한다.

#### Stage 2: 피처 추출 (T07-T12) — ~2.4 GB (피크)

| 기법 | 라이브러리 | 출력 |
|------|----------|------|
| T07 SBERT 임베딩 | sentence-transformers (384-dim) | embeddings.parquet |
| T08 TF-IDF | sklearn (10,000 features, ngram 1-2) | tfidf.parquet |
| T09 NER | xlm-roberta / spaCy | ner.parquet |
| T10 키워드 | keybert (SBERT 공유) | 기사별 키워드 |
| T12 단어 통계 | custom | 단어 수, 문장 수 |

**SBERT 모델 공유**: KeyBERT, BERTopic, Stage 6의 교차 언어 정렬이 모두 동일한 SBERT 인스턴스를 사용한다. 384-dim 벡터는 성능과 메모리의 최적 균형점이다.

#### Stage 3: 기사 분석 (T13-T19, T49) — ~1.8 GB

| 기법 | 라이브러리 | 성능 |
|------|----------|------|
| T13 한국어 감성 | KoBERT | F1=94% |
| T14 영어 감성 | cardiffnlp/twitter-roberta | |
| T15 8차원 감정 | BART-MNLI / KcELECTRA | Plutchik 모델 |
| T16 STEEPS 분류 | BART-MNLI zero-shot | 6개 카테고리 |
| T17 논조 감지 | BART-MNLI zero-shot | |
| T18 Social Mood Index | 집계 공식 | |
| T19 감정 궤적 | 7일 이동 델타 | |
| T49 내러티브 추출 | BART-MNLI zero-shot | |

**NLP 모델 선택 근거**:
- KoBERT: 한국어 감성 분석에서 F1=94%로 최고 성능. 한국어 BERT 모델 중 가장 안정적
- BART-MNLI: zero-shot 분류에 최적. 별도 학습 데이터 없이 레이블만으로 분류 가능
- KcELECTRA: 한국어 감정 분석에 특화. KoBERT와 상호보완

#### Stage 4: 집계 (T21-T28) — ~1.5 GB

| 기법 | 라이브러리 | 역할 |
|------|----------|------|
| T21 BERTopic | BERTopic + Model2Vec | 토픽 모델링 (CPU 500x 가속) |
| T22 동적 토픽 | BERTopic topics_over_time | 토픽 시간 변화 |
| T23 HDBSCAN | hdbscan (cosine) | 밀도 기반 클러스터링 |
| T24 NMF | sklearn | 비음수 행렬 분해 |
| T25 LDA | sklearn | 확률적 토픽 모델 |
| T26 k-means | sklearn (silhouette 최적화) | 중심 기반 클러스터링 |
| T27 계층적 클러스터링 | scipy Ward | 덴드로그램 |
| T28 Louvain 커뮤니티 | python-louvain | 네트워크 커뮤니티 |

**Model2Vec 선택 근거**: BERTopic은 기본적으로 SBERT를 사용하지만, CPU에서 느리다. Model2Vec은 단어 벡터를 사전 계산하여 CPU에서 **500배 가속**을 달성한다. GPU 없는 M2 Pro 환경(C3)에서 BERTopic을 실용적으로 만드는 핵심 의사결정이었다.

#### Stage 5: 시계열 (T29-T36) — ~0.5 GB (독립)

| 기법 | 라이브러리 | 출력 |
|------|----------|------|
| T29 STL 분해 | statsmodels (주기=7) | 트렌드/계절/잔차 분리 |
| T30 Kleinberg 버스트 | custom automaton | 뉴스 폭발 감지 |
| T31 PELT 변화점 | ruptures (RBF, BIC) | 구조적 변화 시점 |
| T32 Prophet 예측 | prophet (7d + 30d) | 단기/중기 예측 |
| T33 웨이블릿 | pywt (Daubechies-4) | 주파수 도메인 분석 |
| T34 ARIMA | statsmodels (grid search) | 전통적 시계열 예측 |
| T35 이동평균 교차 | pandas (3d vs 14d) | 골든/데드 크로스 |
| T36 계절성 | scipy periodogram | 주기적 패턴 감지 |

#### Stage 6: 교차 분석 (T37-T46, T20, T50) — ~0.8 GB (독립)

| 기법 | 라이브러리 | 역할 |
|------|----------|------|
| T37 Granger 인과관계 | statsmodels | 시간적 인과 추정 |
| T38 PCMCI 인과 추론 | tigramite (ParCorr) | 편상관 기반 인과 |
| T39 공출현 네트워크 | networkx | 엔티티 동시 출현 |
| T40 지식 그래프 | networkx (NER 기반) | 관계 그래프 |
| T41 중심성 분석 | networkx (PageRank 등) | 영향력 측정 |
| T42 네트워크 진화 | networkx | 시간별 구조 변화 |
| T43 교차 언어 토픽 정렬 | SBERT multilingual centroid | 다국어 토픽 매칭 |
| T44 프레임 분석 | sklearn TF-IDF KL divergence | 관점 차이 계량 |
| T45 의제 설정 | scipy 교차 상관 | 매체 간 의제 흐름 |
| T46 시간적 정렬 | DTW | 시계열 유사도 |
| T20 GraphRAG | networkx | 그래프 기반 질의응답 |
| T50 모순 감지 | SBERT + NLI | 상충 보도 탐지 |

**T43 교차 언어 토픽 정렬**의 설계 근거: 번역 없이 한국어와 영어 기사의 토픽을 정렬하는 핵심 기법. SBERT multilingual 모델이 양 언어의 임베딩을 동일 벡터 공간에 매핑하므로, 코사인 유사도로 직접 비교 가능하다.

#### Stage 7: 신호 분류 (T47-T55) — ~0.5 GB

**5-Layer 신호 계층** (§1.7 설계 근거 참조):

```
L1 Fad:        burst_score 높음 + duration < 7d
L2 Short-term: PELT changepoint + 4주 지속
L3 Mid-term:   STL 트렌드 성분 + Granger 인과
L4 Long-term:  다중 소스 교차 확인 + 6개월 지속
L5 Singularity: 2-of-3 독립 경로 합의 + threshold=0.65
```

| 기법 | 역할 |
|------|------|
| T47 LOF 이상치 | 국소 밀도 기반 이상치 |
| T48 Isolation Forest | 격리 기반 이상치 |
| T51 Z-score | 통계적 이상 감지 |
| T52 엔트로피 변화 | 정보량 급변 감지 |
| T53 Zipf 편차 | 단어 분포 이상 |
| T54 생존 분석 | Kaplan-Meier (토픽 수명) |
| T55 KL 다이버전스 | 분포 간 차이 |
| BERTrend | 토픽 생애주기 감지 |
| Singularity composite | 7개 지표 가중 합성 |

#### Stage 8: 출력 (T56) — ~0.5 GB

1. **analysis.parquet 병합** (21 columns): 5개 소스 Parquet 조인 → ZSTD level-3 압축
2. **signals.parquet 최종화**: 스키마 검증 (12 columns)
3. **topics.parquet 복사**: ZSTD 압축 (7 columns)
4. **SQLite 인덱스 생성**: FTS5 전문검색 + sqlite-vec 벡터검색 + signals_index + topics_index + crawl_status
5. **DuckDB 검증**: 모든 Parquet 읽기 확인
6. **품질 검증**: 중복 ID 없음, 임베딩 384-dim, NOT NULL 필수 컬럼
7. **체크섬**: `checksums.md5` 무결성 파일

### 4.4 Workflow B: Insight Analytics Pipeline (빅데이터 통찰 분석)

> Workflow A(Daily)의 Stage 1-4 출력이 축적된 후, 다중 날짜 윈도우(7/30/90일)에 대해 구조적 통찰을 생산하는 **완전 분리된 제2 파이프라인**이다.

#### 설계 철학: Producer-Consumer 디커플링

```
Workflow A (Producer)                Workflow B (Consumer)
━━━━━━━━━━━━━━━━━━━━━━               ━━━━━━━━━━━━━━━━━━━━━━
매일 실행                             주간/월간/온디맨드
단일 날짜                             다중 날짜 윈도우
src/crawling/ + src/analysis/        src/insights/ (독립 모듈)
Stage 1-4 Parquet 생산  ──────────→  Stage 1-4 Parquet READ-ONLY 소비
run_metadata.json (A SOT)            insight_state.json (B SOT)
data/output/{date}/                  data/insights/{run_id}/
```

**분리 원칙**: Workflow A의 코드(`src/crawling/`, `src/analysis/`, `src/storage/`)를 **단 한 줄도 import하지 않는다**. `src/insights/`의 모든 모듈은 `src/config/constants.py`(공유 설정)와 자체 모듈만 import한다.

#### 7개 분석 모듈 (M1-M7, 27개 지표)

| 모듈 | 지표 | 최소 데이터 | 핵심 라이브러리 | 설계 타입 |
|------|------|-----------|---------------|----------|
| **M1: 교차언어 분석** | CL-1 JSD 비대칭, CL-2 어텐션 갭, CL-3 감성 다이버전스, CL-4 필터 버블 | 7일, 3개 언어 | scipy (JSD, Wasserstein) | Type A (순수 산술) |
| **M2: 내러티브 분석** | NF-1 프레임 진화, NF-2 변화점 감지, NF-3 HHI 음성 지배, NF-4 미디어 건강, NF-5 정보 흐름, NF-6 출처 신뢰도 | 14일 | networkx, ruptures(opt) | Type A/B |
| **M3: 엔티티 분석** | EA-1 궤적 분류, EA-2 숨은 연결, EA-3 부상 지수, EA-4 교차언어 도달 | 14일 | networkx | Type A/B |
| **M4: 시간 패턴** | TP-1 이벤트 캐스케이드, TP-2 정보 속도, TP-3 어텐션 감쇠, TP-4 구조적 주기성 | 14일 | scipy, statsmodels(opt) | Type A |
| **M5: 지정학 분석** | GI-1 양자관계지수(BRI), GI-2 소프트파워, GI-3 의제설정력, GI-4 갈등-협력 스펙트럼 | 14일 | — | Type A/B |
| **M6: 경제 분석** | EI-1 다국어 EPU, EI-2 섹터 감성, EI-3 모멘텀, EI-4 내러티브 경제, EI-5 하이프 사이클 | 7일 | numpy | Type A/B |
| **M7: 종합 + 인텔리전스** | 리포트 생성 + 핵심 발견 + **증거 기반 미래 인텔리전스** | 7일 | pandas, pyarrow | Type A/B |

- **Type A**: 순수 산술 (closed-form 수학, ML 추론 없음)
- **Type B**: 규칙 기반 분류 (결정론적 임계값)
- **P1 결정론적**: 92% 이상의 로직이 동일 입력 → 동일 출력

#### M7 확장: 증거 기반 미래 인텔리전스 (2026-04-07 추가)

> M7에 4개의 P1 결정론적 인텔리전스 패널을 추가하여, 기사 원문과 분석 결과를 매칭한 **증거 기반 미래 예측 데이터**를 자동 생산한다.

| 패널 | 산출물 | P1 로직 | 미래 예측 활용 |
|------|--------|---------|-------------|
| **FI-1: 엔티티 프로파일** | entity_profiles.parquet | NER + sentiment groupby→mean | 엔티티별 미디어 톤 추적 (위기/부상 감지) |
| **FI-2: 양자관계 긴장** | pair_tensions.parquet | NER pair co-occurrence + sentiment | 양자관계 악화/개선 실시간 모니터링 |
| **FI-3: 증거 기사 매칭** | evidence_articles.parquet | EVIDENCE_SCORE_WEIGHTS 가중합 | 인사이트의 근거가 되는 실제 기사 제시 |
| **FI-4: 리스크 경보** | risk_alerts.parquet | ALERT_THRESHOLDS 임계점 비교 | 자동 경보 (위기 감성, EPU, 전 섹터 부정 등) |

**설계 원칙**:
- **P1 결정론적**: 모든 계산이 Python 산술 (LLM 판단 없음). 동일 입력 → 동일 출력.
- **C4 준수**: 산출물은 Parquet만 (HTML 대시보드는 별도 명령으로 분리).
- **SOT**: 증거 선택 가중치 `EVIDENCE_SCORE_WEIGHTS`, 경보 임계점 `ALERT_THRESHOLDS`는 `src/insights/constants.py`에 SOT로 등록. `insights.yaml`에서 오버라이드 가능.
- **P1 검증**: `validate_intelligence.py`가 FI1-FI4 산출물의 스키마 + 비어있지 않음을 검증.

#### 데이터 흐름

```
data/processed/{date}/articles.parquet     ─┐
data/features/{date}/embeddings.parquet     │
data/features/{date}/ner.parquet            ├─→ WindowCorpus (컬럼 선택적, 지연 로딩)
data/features/{date}/tfidf.parquet          │     ↓
data/analysis/{date}/article_analysis.parquet│   M1-M6 (독립 실행, 모듈 간 gc.collect())
data/analysis/{date}/topics.parquet         │     ↓
data/analysis/{date}/networks.parquet      ─┘   M7 종합 (M1-M6 결과 집약)
                                                  ↓
                                            data/insights/{run_id}/
                                              ├── crosslingual/   (M1 Parquet)
                                              ├── narrative/      (M2 Parquet)
                                              ├── entity/         (M3 Parquet)
                                              ├── temporal/       (M4 Parquet)
                                              ├── geopolitical/   (M5 Parquet)
                                              ├── economic/       (M6 Parquet)
                                              └── synthesis/      (M7 보고서 + 인텔리전스)
                                                  ├── insight_report.md
                                                  ├── insight_data.json
                                                  ├── key_findings.json
                                                  └── intelligence/     (M7 확장)
                                                      ├── entity_profiles.parquet
                                                      ├── pair_tensions.parquet
                                                      ├── evidence_articles.parquet
                                                      └── risk_alerts.parquet
```

#### SOT 분리

| 항목 | Workflow A | Workflow B |
|------|-----------|-----------|
| **상태 파일** | `.claude/state.yaml` (빌드), `run_metadata.json` (런타임) | `data/insights/insight_state.json` |
| **설정** | `data/config/sources.yaml`, `config/pipeline.yaml` | `data/config/insights.yaml` |
| **상수** | `CRAWL_*`, `DATA_*_DIR` | `INSIGHT_*` (`src/config/constants.py`에 네임스페이스 분리) |
| **실행** | `main.py --mode crawl\|analyze\|full` | `main.py --mode insight --window N` |

#### 실행 인터페이스

```bash
# 월간 통찰 (30일 윈도우)
.venv/bin/python main.py --mode insight --window 30 --end-date 2026-04-06

# 주간 통찰
.venv/bin/python main.py --mode insight --window 7

# 특정 모듈만
.venv/bin/python main.py --mode insight --window 30 --module m1_crosslingual
```

#### Workflow A Stage 5-8과의 관계

Workflow B는 Workflow A의 Stage 5-8(시계열, 교차분석, 신호, 출력)과 **기능적으로 유사하지만 완전히 독립적**이다:

| 차이점 | Stage 5-8 (Workflow A) | M1-M7 (Workflow B) |
|--------|----------------------|-------------------|
| **시간 범위** | 단일 날짜 | 다중 날짜 윈도우 (7-365일) |
| **관점** | 일일 신호 탐지 (이상치, 버스트) | 구조적 패턴 탐지 (트렌드, 비대칭) |
| **출력** | signals.parquet (12 columns) | 27개 지표 + 종합 보고서 |
| **코드** | `src/analysis/stage5-8_*.py` | `src/insights/m1-m7_*.py` |
| **의존성** | Stage 4 출력 | Stage 1-4 출력 (다중 날짜) |

Stage 5-8은 "오늘 무엇이 튀었는가?"를, Workflow B는 "지난 N일간 구조적으로 무엇이 변하고 있는가?"를 답한다.

### 4.5 메모리 관리 전략

각 단계는 **"로드 → 처리 → 저장 → 해제"** 패턴으로 메모리를 관리한다:

```python
# 각 Stage 의사 코드
model = load_model()         # 1. 모델 로드
results = process(data)      # 2. 처리
save_parquet(results)        # 3. Parquet 저장
del model, results           # 4. 참조 해제
gc.collect()                 # 5. 가비지 컬렉션
```

| 단계 | 피크 메모리 | 주요 모델 |
|------|-----------|----------|
| Stage 2 | ~2.4 GB | SBERT (sentence-transformers) |
| Stage 3 | ~1.8 GB | KoBERT (transformers) |
| Stage 4 | ~1.5 GB | BERTopic + HDBSCAN + UMAP |
| Stage 5-8 | < 1 GB | statsmodels, networkx 등 |

**SBERT 공유**: KeyBERT(Stage 2)와 BERTopic(Stage 4)이 동일한 SBERT 인스턴스를 참조하여 중복 로딩을 방지한다.

---

## 5. 데이터 아키텍처

### 5.1 날짜별 파티션 구조

```
data/
├── raw/YYYY-MM-DD/              # 원시 JSONL
│   ├── all_articles.jsonl       # 전체 기사
│   ├── crawl_report.json        # 크롤링 리포트
│   ├── .crawl_state.json        # 재개 체크포인트
│   └── backup/                  # 롤링 백업
├── processed/YYYY-MM-DD/        # Stage 1 출력
│   └── articles.parquet
├── features/YYYY-MM-DD/         # Stage 2 출력
│   ├── embeddings.parquet
│   ├── tfidf.parquet
│   └── ner.parquet
├── analysis/YYYY-MM-DD/         # Stage 3-6 출력
│   ├── article_analysis.parquet
│   ├── topics.parquet
│   ├── networks.parquet
│   ├── timeseries.parquet
│   ├── cross_analysis.parquet
│   ├── dtm.parquet
│   ├── aux_clusters.parquet
│   └── mood_trajectory.parquet
├── output/YYYY-MM-DD/           # Stage 7-8 최종 출력
│   ├── analysis.parquet         # 21 columns 병합
│   ├── signals.parquet          # 12 columns 신호
│   ├── topics.parquet           # 7 columns 토픽
│   ├── index.sqlite             # FTS5 + vec
│   └── checksums.md5
├── insights/                    # Workflow B 출력 (§4.4)
│   ├── insight_state.json       # Workflow B 런타임 SOT
│   └── {run_id}/               # 분석 윈도우별 (monthly-2026-03/ 등)
│       ├── crosslingual/        # M1 Parquet
│       ├── narrative/           # M2 Parquet
│       ├── entity/              # M3 Parquet
│       ├── temporal/            # M4 Parquet
│       ├── geopolitical/        # M5 Parquet
│       ├── economic/            # M6 Parquet
│       └── synthesis/           # M7 보고서
├── models/                      # ML 모델 캐시
├── logs/                        # 실행 로그
└── dedup.sqlite                 # 전역 중복 제거 DB (크로스-런)
```

### 5.2 Parquet 스키마

**ARTICLES** (12 columns):

| 컬럼 | 타입 | 설명 |
|------|------|------|
| article_id | string | 고유 ID (source_hash) |
| url | string | 원문 URL |
| title | string | 제목 |
| body | string | 본문 |
| source | string | 소스 식별자 |
| category | string | 카테고리 |
| language | string | ISO 639-1 |
| published_at | timestamp | 발행일시 |
| crawled_at | timestamp | 수집일시 |
| author | string | 저자 |
| word_count | int32 | 단어 수 |
| content_hash | string | SHA-256 |

**ANALYSIS** (21 columns):

article_id, sentiment_label, sentiment_score, emotion_{joy,trust,fear,surprise,sadness,disgust,anger,anticipation}, topic_id, topic_label, topic_probability, steeps_category, importance_score, keywords, entities_{person,org,location}, embedding

**SIGNALS** (12 columns):

signal_id, signal_layer, signal_label, detected_at, topic_ids, article_ids, burst_score, changepoint_significance, novelty_score, singularity_composite, evidence_summary, confidence

**Parquet 설정**: ZSTD level-3 압축, 원자적 쓰기 (임시 파일 → rename)

### 5.3 SQLite 인덱스 구조

```sql
articles_fts     -- FTS5 (title + body + keywords) — 전문 검색
signals_index    -- topic_id + layer + confidence   — 신호 조회
topics_index     -- topic_label + article_count     — 토픽 조회
crawl_status     -- site_id + crawl_date + count    — 크롤링 현황
-- sqlite-vec: 384-dim 벡터 유사도 검색
```

---

## 6. Presentation Layer

### 6.1 Streamlit 대시보드

| 탭 | 시각화 |
|----|--------|
| Overview | 기사 수, 소스/그룹/언어별 분포, 일일 볼륨, 파이프라인 상태 |
| Topics | Top 20 토픽, STEEPS 분류, 신뢰도 히스토그램, 토픽 트렌드 |
| Sentiment & Emotions | 감성 파이차트, 8차원 Plutchik 레이더, 소스×감정 히트맵, 무드 궤적 |
| Time Series | 메트릭별 그래프, 이동평균 교차, Prophet 예측 + 신뢰구간 |
| Word Cloud | 다국어 워드클라우드 (한국어/영어 필터), Top-30 빈도 |
| Article Explorer | 소스/언어/키워드 필터, 정렬, 상세 보기 |

사이드바: 기간(Daily/Monthly/Quarterly/Yearly), 날짜 선택 — `data/raw/` 하위 디렉터리 자동 탐색

### 6.2 프로그래매틱 쿼리

- **DuckDB**: `SELECT ... FROM read_parquet('data/output/YYYY-MM-DD/analysis.parquet')`
- **Pandas**: `pd.read_parquet('data/output/YYYY-MM-DD/analysis.parquet')`
- **SQLite FTS5**: `SELECT * FROM articles_fts WHERE articles_fts MATCH 'query'`
- **Cross-date glob**: `SELECT * FROM 'data/output/*/analysis.parquet'`

---

## 7. 설정 시스템

### 7.1 sources.yaml (112개 사이트 (어댑터 123개))

각 사이트 설정: meta (name, url, language, group, enabled, difficulty_tier), crawl (primary_method, rss_urls, sections, rate_limit_seconds, ua_tier, anti_block_tier), selectors (title_css, body_css, date_css), paywall (type)

### 7.2 pipeline.yaml (8단계 파이프라인)

Global (max_memory_gb, gc_between_stages, parquet_compression=zstd). 단계별 (enabled, memory_limit_gb, timeout_seconds, models, dependencies)

### 7.3 constants.py (350+ 상수)

경로 27개, 재시도 파라미터 (MAX_RETRIES=5, BACKOFF_MAX=60s), Circuit Breaker (FAILURE_THRESHOLD=5, RECOVERY_TIMEOUT=300s), 크롤링 (LOOKBACK_HOURS=24, MAX_ARTICLES=5000, CRAWL_NEVER_ABANDON=True), 분석 (SBERT_DIM=384, TFIDF_MAX=10000), 신호 (CONFIDENCE=0.5, SINGULARITY=0.65)

---

## 8. 운영 인프라

### 8.1 CLI (main.py)

```bash
python3 main.py --mode {crawl|analyze|full|status}
                --date YYYY-MM-DD --sites X,Y --groups A,B
                --stage N --all-stages --dry-run
                --log-level {DEBUG|INFO|WARNING|ERROR}
```

### 8.2 자동화

| 스케줄 | 스크립트 | 역할 |
|--------|---------|------|
| 일일 02:00 | `run_daily.sh` | 전체 파이프라인 (4시간 타임아웃, 잠금 파일, 로그 회전) |
| 주간 일요일 01:00 | `run_weekly_rescan.sh` | 사이트 건강 점검 (RSS, CSS 선택자, 페이월) |
| 월간 1일 03:00 | `archive_old_data.sh` | 30일 이상 데이터 아카이빙 (SHA256 검증 후 삭제) |

### 8.3 Preflight Check

`python3 scripts/preflight_check.py --project-dir . --mode full --json`

Python 3.12+, 20개 의존성, 44개 사이트 설정, 디스크 ≥2GB, spaCy 모델, 네트워크, 디렉터리 구조 검증. 결과: `readiness: "ready" | "blocked"` + `degradations` 목록

---

## 9. 테스트 인프라

55 파일, ~2,588 테스트, ~24,700 LOC.

| 카테고리 | 파일 수 | 내용 |
|---------|--------|------|
| unit | 29 | 단계별 파이프라인, SOT, 설정, 중복제거, 유틸리티, 품질 게이트 |
| integration | 1 | SOT 전체 라이프사이클 |
| structural | 4 | 에이전트 구조, 사이트 일관성, 플레이북 정합, **D-7 동기화 검증**, **H-13 ENABLED_DEFAULT 동기화** |
| crawling | 11 | 안티블록, 서킷브레이커, 파이프라인, 어댑터, 세션, 중복제거, **DynamicBypass**, **Discovery bypass + Producer-Consumer 계약** |

```bash
pytest                      # 전체 ~2,565 테스트
pytest -m unit              # 단위 테스트
pytest -m "not slow"        # NLP 모델 로딩 제외 (빠른 실행)
```

---

## 10. 의존성 아키텍처 (44+ packages)

| 도메인 | 패키지 수 | 주요 패키지 |
|--------|----------|-----------|
| 크롤링 | 13 | httpx, beautifulsoup4, lxml, feedparser, trafilatura, newspaper4k, playwright, patchright (봇 탐지 우회) |
| NLP | 12 | kiwipiepy, spacy, sentence-transformers, transformers, torch, bertopic, keybert, langdetect, scikit-learn, hdbscan |
| 시계열/네트워크 | 9 | statsmodels, prophet, ruptures, PyWavelets, lifelines, networkx, python-louvain, tigramite |
| 저장/유틸 | 10 | pyarrow, pandas, duckdb, sqlite-vec, pyyaml, pydantic, structlog, pytest |

---

## 11. DNA 유전 — 부모로부터 무엇을 물려받았는가

이 시스템은 [AgenticWorkflow](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md)(만능줄기세포)로부터 **전체 게놈**을 물려받았다. 목적은 다르지만 DNA는 동일하다.

### 11.1 유전된 DNA 구성요소

| 부모 DNA | 자식(GlobalNews) 발현 형태 |
|---------|--------------------------|
| **3단계 구조** | Research (Steps 1-4) → Planning (Steps 5-8) → Implementation (Steps 9-20) |
| **절대 기준 1 (품질 최우선)** | 56개 분석 기법, 4-Level 재시도 (90회), 모든 산출물 Parquet 스키마 강제 |
| **절대 기준 2 (단일 파일 SOT)** | `.claude/state.yaml` — Orchestrator만 쓰기, 에이전트는 읽기 전용 |
| **절대 기준 3 (CCP)** | 171개 모듈 변경 시 의도→영향→설계 3단계 수행 |
| **5계층 QA + SM5** | L0(a-d) Anti-Skip → Pre-L1 /simplify → L1 Verification → L1.5 pACS → L2 Review(+Focus) + SM5 SOT-Level 강제 |
| **P1 할루시네이션 봉쇄** | 14개 결정론적 검증 스크립트 (12 `validate_*.py` + `diagnose_context.py` + SM5 gate evidence in `sot_manager.py`) + D-7 동기화 테스트 (14 instances) |
| **P2 전문가 위임** | 32개 전문 서브에이전트 (5개 도메인) |
| **Safety Hooks** | 위험 명령 차단(exit 2) + 시크릿 출력 감지(경고) + TDD 보호 + 예측적 디버깅 |
| **Context Preservation** | 스냅샷 + Knowledge Archive + RLM 복원 + Learned Patterns 표면화 + Importance-Based Retention + Phase-Aware Compact |
| **Adversarial Review** | `@reviewer` (Steps 5, 7, 20), `@fact-checker` (Steps 1, 3) |

### 11.2 도메인 고유 변이 (부모에 없는 새 유전자)

| 변이 | 설명 |
|------|------|
| **4-Level 재시도 + Fairness Yield** (D2) | 90회 자동 시도 + DynamicBypassEngine(12전략, 5-Tier) + SiteDeadline Fairness Yield + CRAWL_NEVER_ABANDON Multi-Pass + P1 `deadline_yielded` 할루시네이션 봉쇄 + Tier 7 에스컬레이션 — 부모의 재시도는 10회 |
| **116-site Adapter Pattern** | 10개 그룹(A-J), 사이트별 전용 어댑터 — 부모에는 없는 도메인 패턴 |
| **5-Layer Signal Hierarchy** | Fad→Short→Mid→Long→Singularity — 뉴스 도메인 고유 |
| **Date-Partitioned Storage** | YYYY-MM-DD 디렉터리 구조 — 시계열 분석 전제 |
| **Conductor Pattern** (C2) | Claude Code → Python → Bash → 결과 읽기 — C1 제약 대응 |
| **3-Level Dedup** | URL→Title→SimHash — 뉴스 중복의 다층적 특성 대응 |
| **Paywall Bypass System** | BrowserRenderer(서브프로세스 Patchright) + AdaptiveExtractor(4-stage CSS) + is_paywall_body(Strong/Weak 26패턴, EN+FR) — 하드 페이월 사이트 5곳 대응 (ADR-054~057) |
| **Dual Workflow Architecture** | Workflow A(Daily: crawl+analyze) + Workflow B(Insight: M1-M7 구조적 통찰). Producer-Consumer 디커플링. 독립 SOT, 독립 코드, 독립 스케줄 |
| **HQ Gates (4종)** | HQ1-HQ4 Human-step 품질 검증 — 자동 승인 시 이전 단계 증거 확인 |
| **Circuit Breaker** (워크플로우) | 3회 연속 ≤5점 개선 시 OPEN — 무진전 재시도 차단 |
| **Abductive Diagnosis** | 품질 게이트 FAIL 시 사전 증거 수집 + 가설 기반 진단 (AD1-AD10) |
| **Decision Log P1** | DL1-DL8 Autopilot 결정 로그 구조적 무결성 검증 |
| **Autopilot Mode** | (human) 단계 + AskUserQuestion 자동 승인 — 품질 극대화 기본값 |
| **SM5 Quality Gate Evidence Guard** | SOT `advance-step`에서 verification+pACS 증거를 물리적으로 강제 (Level A 보호 — LLM 우회 불가). SM5a-SM5d 4개 체크, `--force` 감사 추적, 2-stage pACS 파싱(D-7 `_context_lib.py` 정합) |
| **D-7 Instance 13: ENABLED_DEFAULT SOT 중앙화** | `constants.py`의 `ENABLED_DEFAULT` 상수를 SOT로, 4개 소비자(`sources_generator.py`, `split_sites_by_group.py`, `distribute_sites_to_teams.py`, `crawler.py`)의 AST 교차 검증(ED1-ED7). `validate_enabled_default_sync.py` P1 스크립트 + H-13 구조적 테스트 |

---

## 12. 빌드 히스토리 — AI가 이 시스템을 어떻게 만들었는가

20단계 워크플로우, 32개 전문 서브에이전트, 6개 에이전트 팀이 이 시스템을 자동 구축했다.

### 12.1 에이전트 팀 구성

| 팀 | 에이전트 | 역할 |
|----|---------|------|
| tech-validation-team | dep-validator, nlp-benchmarker, memory-profiler | 기술 검증 |
| crawl-strategy-team | 4개 지역별 전략가 | 크롤링 전략 수립 |
| crawl-engine-team | crawler-core-dev, anti-block-dev, dedup-dev, ua-rotation-dev | 크롤링 엔진 구현 |
| site-adapters-team | 4개 어댑터 개발자 | 112개 사이트 (어댑터 123개) 어댑터 |
| analysis-foundation-team | preprocessing-dev, feature-extraction-dev, article-analysis-dev, aggregation-dev | Stage 1-4 구현 |
| analysis-signal-team | timeseries-dev, cross-analysis-dev, signal-classifier-dev, storage-dev | Stage 5-8 구현 |

### 12.2 품질 점수 이력 (pACS)

| 단계 | 내용 | pACS |
|------|------|------|
| 1-4 | Research (정찰, 기술검증, 실현가능성) | 70-74 |
| 5-8 | Planning (아키텍처, 전략, 설계) | 72-80 |
| 9-12 | Crawling 구현 | 78-82 |
| 13-15 | Analysis 구현 | 80-82 |
| 16-20 | 테스트, 자동화, 문서, 리뷰 | 65-84 |

### 12.3 후속 강화 (Post-Build Enhancements)

| 시기 | 내용 | 영향 |
|------|------|------|
| ADR-054~057 | 페이월 바이패스 시스템 (BrowserRenderer + AdaptiveExtractor + is_paywall_body) | 하드 페이월 5곳 대응 |
| ADR-060~063 | 44→121→116 사이트 확장 + DynamicBypassEngine + P1 레지스트리 검증 | 크롤링 커버리지 확대 |
| ADR-064 | D-7 Instance 13: ENABLED_DEFAULT SOT 중앙화 + AST 교차 검증(ED1-ED7) + `validate_enabled_default_sync.py` P1 스크립트 + `crawler.py` SOT 소비자 추가 | 사이트 활성화 상태 불일치 근절 |
| ADR-065~067 | SiteDeadline Fairness Yield + P1 `deadline_yielded` 플래그 + CRAWL_NEVER_ABANDON Multi-Pass + CrawlState-first 완료 판정 + MAX_ARTICLES 1000→5000 | **크롤링 절대 원칙 실현** — 112개 사이트 완벽한 크롤링 완수 보장 |
| Post-ADR-067 | Bypass Discovery Fallback + Producer-Consumer 계약 정합 + P1 로그 포맷 봉쇄 + 바운디드 `MULTI_PASS_MAX_EXTRA=10` 루프 + `crawl_exhausted_sites.json` 실패 리포트 | P1 할루시네이션 봉쇄 — 하드코딩·로직 중복 근절 |
| Workflow B | 빅데이터 통찰 분석 파이프라인 (M1-M7, 27개 지표). `src/insights/` 완전 독립 모듈 | 듀얼 워크플로우 — 일일 신호(A) + 구조적 통찰(B) |
| ADR-071·072·079 | DCI 14-layer 독립 워크플로우 + SG-Superhuman 10-gate + 5조항 P1 DNA + `--mode dci` CLI | W4와 직교하는 박사급 deep-content 트랙 |
| ADR-080 | Public Narrative 3-Layer (해석·통찰·미래) + 8 PUB 검증 (FKGL · jargon · number parity · 금지어) | 일반인 해석 자동 생성 (Step 6.5) |
| ADR-081 | `run_daily.sh` cron 내러티브 체인 — Step 6.3/6.4 (W2/W3 narrators), 6.45a/b (Master·DCI appendix) | 매일 사람-읽기 보고서 자동 생성 |
| ADR-082 | Chart Interpretations 6-tab — 대시보드 탭별 🌱 해석 / 💡 인사이트 / 🔮 미래통찰 카드 | 대시보드 인지 부담 감소 |
| ADR-083 | WF5 Personal Newspaper — 17-agent editorial team, 15 편집 원칙, 13.5만/20.5만 단어 일/주간판. PIPELINE_TIMEOUT 4h→8h | 매일 NYT-style HTML 신문 자동 발행 |
| BigData Engine (Step 6.7) | 9 신규 모듈 — `articles_enriched_assembler`, `geo_focus_extractor`, `source_metadata_joiner`, `steeps_classifier`(SPI+CRS 추가), `signal_classifier`, `question_engine`(18문 강제 응답), `gti`(40·35·25 합성), `signal_portfolio`(단일 SOT lifecycle), `weekly_future_map`(7-day 종합) | 매일 18개 미래연구 질문 강제 응답 + GTI 시계열 + 신호 포트폴리오 |
| Step 6.8 | LLM Wiki 자동 ingest — BigData Engine 완료 후 `auto-wiki-ingest.sh` 백그라운드 호출 | 외부 LLM Wiki 저장소 자동 동기화 |
| Crawling Quality 3-fix | (1) HTML 엔티티 unescape `html.unescape` (2) KST→UTC regex 보정 (3) `source_domain` 필드 추가 | 데이터 무결성 확보 |

### 12.4 구축 규모 (현재)

| 지표 | 값 |
|------|-----|
| 소스 코드 | **~80,733 LOC** (src/, 275 모듈) |
| 테스트 코드 | **~41,524 LOC** (tests/, 109 파일) |
| 총 코드 | **~122,257 LOC** |
| Python 모듈 | **275개** |
| 크롤링 어댑터 | **123개** (kr_major 12 + kr_tech 11 + english 22 + multilingual 78) |
| 활성 사이트 | **112개** (sources.yaml `enabled:true`) |
| DCI 레이어 | **14개** (L-1 → L11, 13 구현 + L11 dashboard) |
| 서브에이전트 | **107개** (.claude/agents/) |
| 슬래시 커맨드 | **18개** (.claude/commands/) |
| 로컬 스킬 | **6개** (.claude/skills/: workflow-generator, skill-creator, subagent-creator, crawl-master, doctoral-writing, insight-report) |
| 훅 스크립트 | **42개** (.claude/hooks/scripts/, P1 검증 + 안전 + 컨텍스트) |
| 워크플로우 단계 | 20 (Research 4 + Planning 4 + Implementation 12) |
| ADR | ADR-001 → **ADR-083+** |

---

## 12.5 BigData Engine 아키텍처 (`scripts/run_daily.sh` Step 6.7)

`main.py --mode full` 본체가 끝난 후 실행되는 **Layer 4 후처리 파이프라인**. 9개 모듈이 순차 실행되어 W2 산출물을 enrich → 18개 미래연구 질문 강제 응답 → GTI 산출 → 신호 포트폴리오 갱신 → 주간 미래 맵 합성한다.

### 12.5.1 데이터 흐름

```
data/output/{date}/analysis.parquet (Stage 8 산출물)
         │
         ▼ articles_enriched_assembler.py
data/enriched/{date}/articles_enriched.parquet (35 fields:
   url · title · body · STEEPSS · sentiment · embedding · NER ·
   source_country · geo_focus(120 countries) · source_tier · ideology · ...)
         │
         ▼ question_engine.py (18 questions, forced answer)
data/answers/{date}/q01.json … q18.json + summary.json
         │              ↓
         │              status: 'ok' | 'degraded' | 'insufficient_data'
         │              (file presence is unbreakable contract)
         │
         ▼ gti.py
data/gti/{date}/gti_daily.json + data/gti/gti_history.jsonl
   GTI = 40%·G1(Q05) + 35%·G2(Q06) + 25%·G3(Q07), 0-100
         │
         ▼ signal_portfolio.py
data/signal_portfolio.yaml (단일 SOT, 단일 writer, lifecycle:
   CANDIDATE → ACTIVE → CONFIRMED → ARCHIVED)
         │
         ▼ weekly_future_map.py (every Sunday or N-day rollup)
reports/weekly_future_map/{YYYY-W##}/future_map.md (+ .ko.md)
```

### 12.5.2 18개 질문 정의 위치

전체 18개 질문 정의는 `src/analysis/question_engine.py` line 287~1441. 카테고리:
- **버스트·트렌드** (Q01-Q03): 시간적 변화점
- **언어·국가** (Q04-Q06): 교차언어 프레이밍, 국가 감성, dark corners
- **양국·약신호** (Q07-Q09): 양국 긴장, 약한 신호, 패러다임 전조
- **의제 이동** (Q10-Q14): fringe→mainstream, 의제 선점, 이념·언어·매체 격차
- **인과·클러스터** (Q15-Q18): 감성-경제 선행, 이슈 인과 연쇄, 동시 클러스터, 엔티티 중심성

### 12.5.3 P1 보장 — 18문 계약의 결정론

`question_engine.py`는 입력 데이터가 부족해도 **18개 파일을 모두 생성**한다. 데이터 부족 시 `status:'insufficient_data'`로 응답. 이로써 다운스트림(대시보드 Tab 5, Wiki ingest, Weekly Future Map)이 파일 부재 분기를 가질 필요가 없다.

### 12.5.4 STEEPSS 분류 — SPI(영성) + CRS(위기) 추가

`steeps_classifier.py`는 기존 6 카테고리(STEEP+S=Security)에 **SPI(Spirituality)** 와 **CRS(Crisis)** 를 추가하여 8 카테고리 분류한다.
- SPI: 종교·영성·가치관 — 기존엔 POL/SOC로 흡수되어 패러다임 전환 신호 소실
- CRS: 위기·재난 — 기존 카테고리로는 분리 불가능한 별도 시간 패턴

Hybrid 추론: 키워드 Tier 1 (<1ms, 80% 커버) → 모호한 경우 XLM-RoBERTa Tier 2 (150ms).

---

## 12.6 DCI 아키텍처 (Independent Workflow, ADR-079)

DCI는 **W1→W2→W3→W4 체인과 의존성이 없는** 독립 워크플로우이다. 입력은 오직 `data/raw/{date}/all_articles.jsonl`. 14개 레이어(L-1 → L11)를 순차 실행하여 박사급 보고서 (`final_report.md`)를 생산한다.

### 12.6.1 7-Phase Agent Workflow

| Phase | 역할 | 기술 |
|-------|------|------|
| 1 Preflight | 8 checks (corpus, 모델, DCI_ENABLED, SG 임계값) | Python CLI only (`validate_dci_preflight.py`) |
| 2 Structural | L-1 → L2 레이어 실행 | Python subprocess, 순차 |
| 3 Graph & Style | L3 → L5 레이어 실행 | Python subprocess |
| 4 Reasoning | L6 Triadic + L7 GoT + L8 MC + L9 Metacog | L6만 LLM-essential, 나머지 Python |
| 5 Narrator | L10 Doctoral report | Claude CLI + CE3 Python 재검증 |
| 6 Review | 3-reviewer Agent Team (SG, Evidence, Narrative) | **유일한 Agent Team — 독립성 필수** |
| 7 Reporting | Executive summary + Korean translation | Python CLI + `@translator` |

**에이전트 5개**: `@dci-execution-orchestrator` (단일 SOT writer) + `@dci-sg-superhuman-auditor`, `@dci-evidence-auditor`, `@dci-narrative-reviewer` (Phase 6 Team) + `@translator` (재사용). Preflight·Reporter는 의도적으로 **Python CLI로 대체** — LLM 할루시네이션 원천봉쇄.

### 12.6.2 SG-Superhuman 10-Gate

모든 DCI run은 다음 게이트를 통과해야 PASS (Python 결정론):

| Gate | 기준 | 의미 |
|------|------|------|
| G1 | `char_coverage = 1.00` | 본문 전수 진입 |
| G2 | `triple_lens_coverage ≥ 3.0` | 문자당 평균 3+ 렌즈 참조 |
| G3 | `llm_body_injection_ratio = 1.00` | L6에 본문 100% 투입 |
| G4 | `technique_completeness = 93/93` | 모드별 준수 |
| G5 | `nli_verification_pass_rate ≥ 0.95` | DeBERTa-v3-MNLI |
| G6 | `triadic_consensus_rate ≥ 0.60` | productive-disagreement band |
| G7 | `adversarial_critic_pass ≥ 0.90` | Critic JSON parse |
| G8 | `evidence_3layer_complete = 100%` | CE4 article/segment/char |
| G9 | `technique_mode_compliance = 100%` | Registry vs trace |
| G10 | `uncertainty_quantified = 100%` | L7/L8/L9 artifacts presence |

### 12.6.3 5조항 P1 Hallucination Prevention DNA

모든 DCI 에이전트가 상속:

1. NEVER recompute any number. Python validators produce all metrics.
2. NEVER invent `[ev:xxx]` markers. Only reference markers already in `evidence_ledger.jsonl`.
3. NEVER declare PASS/FAIL for objective criteria. Read exit code from `validate_dci_*.py`.
4. Quote numbers verbatim from Python CLI JSON output.
5. Subjective judgment is permitted ONLY for: doctoral prose quality, semantic coherence, narrative framing, failure pattern diagnosis.

상세: `prompt/execution-workflows/dci.md`.

---

## 12.7 WF5 Personal Newspaper 아키텍처 (ADR-083)

### 12.7.1 17-Agent Editorial Team

| 그룹 | 에이전트 | 역할 |
|------|---------|------|
| Lead | `@newspaper-chief-editor` | 14-desk 오케스트레이션, 헤드라인 에세이, 사설, 심층 분석, 단일 SOT writer |
| Continental Desks (6) | `@desk-{africa, asia, europe, north-america, oceania, south-america}` | 대륙별 3,000-6,000 단어 섹션, P5 3-Tier ranking |
| STEEPS Section Desks (6) | `@section-{political, economic, social, technology, environmental, security}` | 대륙 횡단 주제 섹션 3,500-5,000 단어 |
| Specialty (4) | `@dark-corner-scout`, `@fact-triangulator`, `@future-outlook-writer`, `@newspaper-copy-editor` | dark corners 6,000 단어 / triangulation 1,000 / future outlook 12,000 / Phase 6 카피 에디팅 |

### 12.7.2 15 편집 원칙

P1 완전 지리 커버리지 · P2 Balance Code (30% 상한) · P3 계층 일관성 · P4 고정 분량(±20%) · P5 3-Tier (global/local/weak_signal) · P6 Source Triangulation · P7 STEEPS 균형 · P8 CE4 증거 · P9 Fact/Context/Opinion 분리 · P10 Confidence Level · P11 한국어 일차 · P12 미래학자 관점 · P13 Dark Corners · P14 No Clickbait · P15 No Single-Source · P16 No Algorithmic Amplification.

### 12.7.3 발행 주기 + SOT

- 일간: 매일 `run_daily.sh` Step 7. ~135,000 단어 (≈9시간 읽기). `newspaper/daily/{date}/index.html`.
- 주간: 일요일 Step 7b, ≥4 일간판 누적 시. ~205,000 단어. `newspaper/weekly/{YYYY-W##}/`.
- SOT: `execution.runs.{id}.workflows.newspaper.*` canonical. `newspaper` actor 신설.

검증: `validate_newspaper.py` NP1-NP12.

---

## 13. 관련 문서

| 문서 | 내용 |
|------|------|
| [README.md](README.md) | 영한 병기 시스템 개요, 빠른 시작 |
| [GLOBALNEWS-USER-MANUAL.md](GLOBALNEWS-USER-MANUAL.md) | 일상 운영 가이드 (CLI · 대시보드 · 18문 · DCI · WF5) |
| [GLOBALNEWS-EXECUTION-WORKFLOWS.md](GLOBALNEWS-EXECUTION-WORKFLOWS.md) | W1/W2/W3/W4/DCI/WF5 실행 프로토콜 |
| [GLOBALNEWS-EVIDENCE-CHAIN.md](GLOBALNEWS-EVIDENCE-CHAIN.md) | CE3 / CE4 evidence chain 의미론 |
| [GLOBALNEWS-SEMANTIC-GATES.md](GLOBALNEWS-SEMANTIC-GATES.md) | SG1-SG3 + SG-Superhuman 게이트 |
| [GLOBALNEWS-P1-EXTENSIONS.md](GLOBALNEWS-P1-EXTENSIONS.md) | P1 할루시네이션 봉쇄 확장 |
| [prompt/workflow.md](prompt/workflow.md) | 20단계 워크플로우 설계도 (구축 과정 기록) |
| [prompt/execution-workflows/dci.md](prompt/execution-workflows/dci.md) | DCI 7-Phase protocol 상세 |
| [data/config/sources.yaml](data/config/sources.yaml) | 112개 활성 사이트 설정 (어댑터 123개) |
| [data/config/insights.yaml](data/config/insights.yaml) | Workflow B + 경보 임계값 |
| [DECISION-LOG.md](DECISION-LOG.md) | ADR-001 → ADR-083+ |
| [GLOBALNEWS-README.md](GLOBALNEWS-README.md) | (Legacy) 시스템 개요 — 신규 README.md가 정본 |
| [AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) | 부모 프레임워크 아키텍처 |
