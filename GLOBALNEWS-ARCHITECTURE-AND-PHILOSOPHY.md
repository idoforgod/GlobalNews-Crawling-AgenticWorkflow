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

### 1.4 Never Give Up — 4-Level 재시도 철학

GlobalNews의 크롤링 철학은 **포기하지 않는 것**이다:

```
Level 1: NetworkGuard ×5    — HTTP 수준 재시도 (지수 백오프)
Level 2: Standard → TotalWar ×2  — 모드 전환 (undetected-chromedriver)
Level 3: Crawler ×3         — 라운드 딜레이 [30s, 60s, 120s]
Level 4: Pipeline ×3        — 전체 재시작 [60s, 120s, 300s]
───────────────────────────────────────────────────
이론적 최대: 5 × 2 × 3 × 3 = 90회 자동 시도
Tier 6: Claude Code 인터랙티브 분석으로 에스컬레이션
```

90회 자동 재시도가 과도해 보일 수 있다. 그러나:

- **뉴스 사이트는 일시적 장애가 빈번하다**: CDN 장애, 배포 중 다운타임, 지역 차단 등
- **포기 비용 > 재시도 비용**: 하루 데이터 누락은 시계열 분석의 연속성을 깨뜨린다
- **각 Level은 다른 전략을 시도한다**: 단순 재시도가 아니라, UA 교체 → 모드 전환 → 딜레이 증가 → 전체 리셋으로 **에스컬레이션**
- **Circuit Breaker가 보호한다**: 5연속 실패 시 300초 대기(OPEN) → 1건 시도(HALF_OPEN) → 복구 확인(CLOSED)

### 1.5 44개 사이트 — 왜 이 사이트들인가

44개 사이트 선정 기준:

| 기준 | 설명 |
|------|------|
| **지역 다양성** | 한국(19), 영미(12), 아시아태평양(6), 유럽/중동(7) — 4대 권역 커버 |
| **언어 다양성** | 한국어, 영어, 중국어, 일본어, 프랑스어, 독일어, 아랍어, 히브리어 — 9개 언어 |
| **도메인 다양성** | 종합(조선, NYT), 경제(FT, WSJ), 기술(ZDNet, Bloter), 지역(TheHindu, LeMonde) |
| **접근성** | Easy(9), Medium(19), Hard(11), Extreme(5) — 4단계 난이도 분포 |
| **교차 분석 가치** | 동일 사건에 대한 다국어·다문화 프레이밍 비교가 가능한 조합 |

**의도적으로 제외한 것들**: 소셜 미디어(X, Reddit — API 비용/TOS), 방송사 웹(KBS, MBC — 동영상 중심), 포털(네이버, 다음 — 재배포 기사 중복)

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

### 2.1 4-Layer 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                      main.py (CLI)                          │
│            crawl │ analyze │ full │ status                  │
├──────────────────┼──────────────────────────────────────────┤
│                  │                                          │
│   ┌──────────────▼──────────────┐                          │
│   │  Layer 1: CRAWLING ENGINE   │                          │
│   │  44 adapters + anti-block   │                          │
│   │  → data/raw/YYYY-MM-DD/    │                          │
│   └──────────────┬──────────────┘                          │
│                  │ all_articles.jsonl                       │
│   ┌──────────────▼──────────────┐                          │
│   │  Layer 2: ANALYSIS PIPELINE │                          │
│   │  8 stages, 56 techniques   │                          │
│   │  → data/{processed,features,analysis}/                 │
│   └──────────────┬──────────────┘                          │
│                  │ Parquet files                            │
│   ┌──────────────▼──────────────┐                          │
│   │  Layer 3: STORAGE           │                          │
│   │  Parquet ZSTD + SQLite FTS5 │                          │
│   │  → data/output/YYYY-MM-DD/ │                          │
│   └──────────────┬──────────────┘                          │
│                  │                                          │
│   ┌──────────────▼──────────────┐                          │
│   │  Layer 4: PRESENTATION      │                          │
│   │  Streamlit dashboard (6 tabs)│                          │
│   │  + DuckDB/Pandas/FTS5 쿼리  │                          │
│   └─────────────────────────────┘                          │
│                                                             │
│   [Shared] config/ │ utils/ │ constants.py                 │
└─────────────────────────────────────────────────────────────┘
```

각 Layer의 경계는 **파일 시스템의 디렉터리**로 물리적으로 구분된다:
- Layer 1 출력 → `data/raw/`
- Layer 2 출력 → `data/processed/`, `data/features/`, `data/analysis/`
- Layer 3 출력 → `data/output/`
- Layer 4 입력 ← `data/output/`

### 2.2 모듈 구조

```
src/                              (~41,500 LOC)
├── config/
│   └── constants.py              350+ 상수: 경로, 임계값, 스키마
├── crawling/                     크롤링 엔진 (13 모듈 + 44 어댑터)
│   ├── pipeline.py               크롤링 오케스트레이터 (1,080 lines)
│   ├── network_guard.py          5-retry HTTP 클라이언트 (662 lines)
│   ├── url_discovery.py          3-Tier URL 발견 (989 lines)
│   ├── article_extractor.py      추출 체인 (1,193 lines)
│   ├── dedup.py                  3-Level 중복 제거 (916 lines)
│   ├── anti_block.py             6-Tier 에스컬레이션 (~600 lines)
│   ├── block_detector.py         7-type 차단 진단 (671 lines)
│   ├── circuit_breaker.py        상태 머신 (~400 lines)
│   ├── retry_manager.py          4-Level 재시도 조율 (~400 lines)
│   ├── session_manager.py        쿠키/세션 생애주기 (821 lines)
│   ├── ua_manager.py             61+ User-Agent 4-tier (942 lines)
│   ├── contracts.py              RawArticle 데이터 계약 (~100 lines)
│   ├── crawl_report.py           사이트별 리포트 (~200 lines)
│   └── adapters/                 44개 사이트별 어댑터
│       ├── base_adapter.py       추상 기반 클래스 (450+ lines)
│       ├── kr_major/             11: 조선, 중앙, 동아, 한겨레, 연합 등
│       ├── kr_tech/              8: Bloter, 전자신문, ZDNet Korea 등
│       ├── english/              12: NYT, FT, WSJ, CNN, Bloomberg 등
│       └── multilingual/         13: AlJazeera, SCMP, Bild, LeMonde 등
├── analysis/                     8단계 NLP 파이프라인
│   ├── pipeline.py               분석 오케스트레이터 (1,096 lines)
│   ├── stage1_preprocessing.py   전처리: Kiwi + spaCy (1,408 lines)
│   ├── stage2_features.py        피처: SBERT + TF-IDF + NER (1,508 lines)
│   ├── stage3_article_analysis.py 분석: 감성 + 감정 + STEEPS (1,726 lines)
│   ├── stage4_aggregation.py     집계: BERTopic + HDBSCAN (2,175 lines)
│   ├── stage5_timeseries.py      시계열: STL + PELT + Prophet (2,147 lines)
│   ├── stage6_cross_analysis.py  교차: Granger + PCMCI (2,617 lines)
│   ├── stage7_signals.py         신호: 5-Layer 분류 (2,180 lines)
│   └── stage8_output.py          출력: Parquet + SQLite (810 lines)
├── storage/                      데이터 I/O
│   ├── parquet_writer.py         ZSTD 압축 + 원자적 쓰기 (722 lines)
│   └── sqlite_builder.py         FTS5 + vec 인덱스 (695 lines)
└── utils/                        유틸리티
    ├── logging_config.py         구조화 로깅
    ├── config_loader.py          YAML 로딩 + 검증
    ├── error_handler.py          예외 계층 + 재시도 데코레이터
    └── self_recovery.py          자기 복구 메커니즘
```

---

## 3. 크롤링 엔진 (Layer 1)

### 3.1 크롤링 파이프라인 흐름

```
sources.yaml (44 sites)
       │
       ▼
┌──────────────────────────────────────────────────┐
│              CrawlingPipeline                     │
│  ┌────────────┐  ┌───────────────┐               │
│  │ SiteAdapter │  │ NetworkGuard  │               │
│  │ (44개)      │  │ (5-retry HTTP)│               │
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
└──────────────────────────────────────────────────┘
```

### 3.2 사이트 어댑터 시스템

**설계 철학**: 44개 사이트는 DOM 구조, 인코딩, 페이월, 차단 방식이 모두 다르다. 범용 크롤러로는 높은 수집률을 달성할 수 없다. 따라서 **사이트마다 전용 어댑터**를 구현하되, **공통 로직은 기반 클래스에 집중**시킨다.

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

| 그룹 | 디렉터리 | 사이트 수 | 특징 |
|------|---------|----------|------|
| Korean Major | `kr_major/` | 11 | 네이버 연동, Kiwi 토크나이저, 한국어 날짜 파싱 |
| Korean Tech | `kr_tech/` | 8 | 기술 뉴스, 간단한 구조, RSS 중심 |
| English | `english/` | 12 | 페이월 사이트(NYT, FT, WSJ) 포함 |
| Multilingual | `multilingual/` | 13 | CJK 인코딩, RTL(아랍/히브리), 다중 언어 |

### 3.3 안티블록 시스템

**7가지 차단 유형 진단** (BlockDetector):

IP Block, UA Filter, Rate Limit, CAPTCHA, JS Challenge, Fingerprint, Geo-Block

**6-Tier 에스컬레이션**:

```
Tier 1: 딜레이 증가 + UA 회전
    ↓ 실패
Tier 2: 세션/쿠키 재설정
    ↓ 실패
Tier 3: Playwright (headless 브라우저)
    ↓ 실패
Tier 4: Patchright + 핑거프린트 위장
    ↓ 실패
Tier 5: 프록시 (설정 시)
    ↓ 실패
Tier 6: Claude Code 인터랙티브 분석 (에스컬레이션)
```

**Circuit Breaker 상태 머신**:

```
CLOSED ─(5연속 실패)─→ OPEN ─(300초 대기)─→ HALF_OPEN ─(성공)─→ CLOSED
                                                        └(실패)─→ OPEN
```

### 3.4 중복 제거 (3-Level)

| Level | 방법 | 기준 | 비용 |
|-------|------|------|------|
| L1 | URL 정규화 + 정확 매칭 | 쿼리 파라미터 제거, 프로토콜 정규화 | O(1) 해시 |
| L2 | 제목 유사도 | Jaccard ≥ 0.8 + Levenshtein ≤ 0.2 | O(n) 비교 |
| L3 | SimHash 본문 핑거프린트 | 64bit 해밍 거리 ≤ 10 | O(1) XOR |

저장소: `data/dedup.sqlite` (크로스-런 지속)

### 3.5 데이터 계약 (RawArticle)

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
    crawl_tier: int             # 사용된 티어 (1-6)
    crawl_method: str           # RSS/Sitemap/DOM/Playwright
    is_paywall_truncated: bool  # 페이월 절단 여부
```

`frozen=True`로 불변 객체를 보장한다. 크롤링 엔진 출력의 **유일한 계약**이며, 분석 파이프라인은 이 계약만 의존한다.

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

### 4.3 메모리 관리 전략

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

### 7.1 sources.yaml (44개 사이트)

각 사이트 설정: meta (name, url, language, group, enabled, difficulty_tier), crawl (primary_method, rss_urls, sections, rate_limit_seconds, ua_tier, anti_block_tier), selectors (title_css, body_css, date_css), paywall (type)

### 7.2 pipeline.yaml (8단계 파이프라인)

Global (max_memory_gb, gc_between_stages, parquet_compression=zstd). 단계별 (enabled, memory_limit_gb, timeout_seconds, models, dependencies)

### 7.3 constants.py (350+ 상수)

경로 27개, 재시도 파라미터 (MAX_RETRIES=5, BACKOFF_MAX=60s), Circuit Breaker (FAILURE_THRESHOLD=5, RECOVERY_TIMEOUT=300s), 크롤링 (LOOKBACK_HOURS=24, MAX_ARTICLES=1000), 분석 (SBERT_DIM=384, TFIDF_MAX=10000), 신호 (CONFIDENCE=0.5, SINGULARITY=0.65)

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

40 파일, ~2,028 테스트, ~22,000 LOC.

| 카테고리 | 파일 수 | 내용 |
|---------|--------|------|
| unit | 28 | 단계별 파이프라인, SOT, 설정, 중복제거, 유틸리티, 품질 게이트 |
| integration | 1 | SOT 전체 라이프사이클 |
| structural | 3 | 에이전트 구조, 사이트 일관성, 플레이북 정합 |
| crawling | 8 | 안티블록, 서킷브레이커, 파이프라인, 어댑터, 세션, 중복제거 |

```bash
pytest                      # 전체 2,028 테스트
pytest -m unit              # 단위 테스트
pytest -m "not slow"        # NLP 모델 로딩 제외 (빠른 실행)
```

---

## 10. 의존성 아키텍처 (44+ packages)

| 도메인 | 패키지 수 | 주요 패키지 |
|--------|----------|-----------|
| 크롤링 | 13 | requests, aiohttp, beautifulsoup4, lxml, feedparser, trafilatura, newspaper4k, playwright, patchright |
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
| **절대 기준 3 (CCP)** | 91개 모듈 변경 시 의도→영향→설계 3단계 수행 |
| **5계층 QA** | L0(a-d) Anti-Skip → Pre-L1 /simplify → L1 Verification → L1.5 pACS → L2 Review(+Focus) |
| **P1 할루시네이션 봉쇄** | 11개 결정론적 검증 스크립트 (10 `validate_*.py` + `diagnose_context.py`) |
| **P2 전문가 위임** | 32개 전문 서브에이전트 (5개 도메인) |
| **Safety Hooks** | 위험 명령 차단(exit 2) + 시크릿 출력 감지(경고) + TDD 보호 + 예측적 디버깅 |
| **Context Preservation** | 스냅샷 + Knowledge Archive + RLM 복원 + Learned Patterns 표면화 + Importance-Based Retention + Phase-Aware Compact |
| **Adversarial Review** | `@reviewer` (Steps 5, 7, 20), `@fact-checker` (Steps 1, 3) |

### 11.2 도메인 고유 변이 (부모에 없는 새 유전자)

| 변이 | 설명 |
|------|------|
| **4-Level 재시도** (D2) | 90회 자동 시도 + Tier 6 에스컬레이션 — 부모의 재시도는 10회 |
| **44-site Adapter Pattern** | 사이트별 전용 어댑터 — 부모에는 없는 도메인 패턴 |
| **5-Layer Signal Hierarchy** | Fad→Short→Mid→Long→Singularity — 뉴스 도메인 고유 |
| **Date-Partitioned Storage** | YYYY-MM-DD 디렉터리 구조 — 시계열 분석 전제 |
| **Conductor Pattern** (C2) | Claude Code → Python → Bash → 결과 읽기 — C1 제약 대응 |
| **3-Level Dedup** | URL→Title→SimHash — 뉴스 중복의 다층적 특성 대응 |
| **HQ Gates (4종)** | HQ1-HQ4 Human-step 품질 검증 — 자동 승인 시 이전 단계 증거 확인 |
| **Circuit Breaker** (워크플로우) | 3회 연속 ≤5점 개선 시 OPEN — 무진전 재시도 차단 |
| **Abductive Diagnosis** | 품질 게이트 FAIL 시 사전 증거 수집 + 가설 기반 진단 (AD1-AD10) |
| **Decision Log P1** | DL1-DL8 Autopilot 결정 로그 구조적 무결성 검증 |
| **Autopilot Mode** | (human) 단계 + AskUserQuestion 자동 승인 — 품질 극대화 기본값 |

---

## 12. 빌드 히스토리 — AI가 이 시스템을 어떻게 만들었는가

20단계 워크플로우, 32개 전문 서브에이전트, 6개 에이전트 팀이 이 시스템을 자동 구축했다.

### 12.1 에이전트 팀 구성

| 팀 | 에이전트 | 역할 |
|----|---------|------|
| tech-validation-team | dep-validator, nlp-benchmarker, memory-profiler | 기술 검증 |
| crawl-strategy-team | 4개 지역별 전략가 | 크롤링 전략 수립 |
| crawl-engine-team | crawler-core-dev, anti-block-dev, dedup-dev, ua-rotation-dev | 크롤링 엔진 구현 |
| site-adapters-team | 4개 어댑터 개발자 | 44개 사이트 어댑터 |
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

### 12.3 구축 규모

| 지표 | 값 |
|------|-----|
| 소스 코드 | ~41,500 LOC (src/) |
| 테스트 코드 | ~22,000 LOC (tests/) |
| 총 코드 | ~63,500 LOC |
| Python 모듈 | 91개 |
| 테스트 파일 | 40개 |
| 테스트 케이스 | ~2,028개 |
| 서브에이전트 | 32개 (도메인) + 3개 (프레임워크) |
| 에이전트 팀 | 6개 |
| 워크플로우 단계 | 20 (Research 4 + Planning 4 + Implementation 12) |

---

## 13. 관련 문서

| 문서 | 내용 |
|------|------|
| [GLOBALNEWS-README.md](GLOBALNEWS-README.md) | 시스템 개요, 빠른 시작, 실행 결과 |
| [GLOBALNEWS-USER-MANUAL.md](GLOBALNEWS-USER-MANUAL.md) | 일상 운영 가이드 |
| [prompt/workflow.md](prompt/workflow.md) | 20단계 워크플로우 설계도 (구축 과정 기록) |
| [config/sources.yaml](config/sources.yaml) | 44개 사이트 설정 |
| [AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md](AGENTICWORKFLOW-ARCHITECTURE-AND-PHILOSOPHY.md) | 부모 프레임워크 아키텍처 |
