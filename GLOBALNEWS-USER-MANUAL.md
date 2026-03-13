# GlobalNews User Manual

> **GlobalNews Crawling & Analysis System — 운영 가이드**

이 문서는 완성된 GlobalNews 시스템을 **운영하는 방법**을 안내한다.
시스템 구축 과정(워크플로우 20단계)이 아닌, 구축된 시스템의 **일일 크롤링, 분석, 대시보드 조회, 자동화 설정**에 초점을 둔다.

| 항목 | 내용 |
|------|------|
| **대상** | 이 시스템을 운영하는 연구자, 데이터 분석가 |
| **시스템 상태** | Production-Ready (20/20 단계 완료) |
| **하드웨어** | MacBook M2 Pro, 48GB RAM |
| **핵심 산출물** | Parquet (ZSTD) + SQLite (FTS5) + Streamlit 대시보드 |

---

## 1. 설치 및 초기 설정

### 1.1 필수 환경

| 항목 | 요구 사항 | 확인 방법 |
|------|----------|----------|
| Python | 3.12 이상, 3.14 미만 | `python3 --version` |
| 디스크 공간 | 20GB+ 여유 | 크롤링 데이터 + NLP 모델 저장 |
| RAM | 16GB 이상 (권장 48GB) | `sysctl hw.memsize` |
| 네트워크 | 인터넷 연결 필수 | 121개 해외 뉴스 사이트 접근 |

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
  "enabled_sites": 121,
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
| `crawl` | 뉴스 크롤링만 실행 | `python3 main.py --mode crawl --date 2026-02-27` |
| `analyze` | 분석 파이프라인만 실행 | `python3 main.py --mode analyze --all-stages` |
| `full` | 크롤링 + 분석 전체 실행 | `python3 main.py --mode full --date 2026-02-27` |
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

121개 사이트는 10개 그룹으로 분류된다:

| 그룹 | 카테고리 | 사이트 수 | 예시 |
|------|---------|----------|------|
| A | 한국 주요 일간지 | 5 | 조선, 중앙, 동아, 한겨레, 연합 |
| B | 한국 경제지 | 4 | 매경, 한경, 파이낸셜, 머니투데이 |
| C | 한국 니치 | 3 | 노컷, 국민, 오마이 |
| D | 한국 IT/과학 | 10 | 38North, Bloter, 전자, ZDNet, Insight, Stratechery 등 |
| E | 영어권 | 22 | NYT, FT, WSJ, CNN, Bloomberg, BBC, Guardian, Wired 등 |
| F | 아시아-태평양 | 23 | SCMP, Yomiuri, Mainichi, TheHindu, Inquirer, VNExpress 등 |
| G | 유럽/중동 | 38 | Spiegel, LeMonde, Corriere, ElPais, AlJazeera, Haaretz 등 |
| H | 아프리카 | 4 | AllAfrica, Africanews, TheAfricaReport, Panapress |
| I | 라틴 아메리카 | 8 | Clarin, Folha, ElMercurio, BioBioChile, ElTiempo 등 |
| J | 러시아/중앙아시아 | 4 | RIA, RG, RBC, GoGo Mongolia |

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
- **Sites with articles**: 기사를 수집한 사이트 수 / 121 (SOT `config_loader.get_enabled_sites()` 기반)
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

### 5.1 실행

```bash
streamlit run dashboard.py
```

브라우저에서 `http://localhost:8501` 접속.

### 5.2 사이드바 컨트롤

| 컨트롤 | 설명 |
|--------|------|
| **기간** | Daily / Monthly / Quarterly / Yearly |
| **기준 날짜** | 데이터가 존재하는 날짜 선택 |

대시보드는 `data/raw/` 하위의 `YYYY-MM-DD` 디렉터리를 자동 탐색하여 사용 가능한 날짜를 표시한다.

### 5.3 탭 구성

| 탭 | 내용 |
|----|------|
| **Overview** | 총 기사 수, 소스별 분포, 카테고리별 분포, 언어별 분포 차트 |
| **Topics** | BERTopic 토픽 목록, 토픽별 기사 수, 대표 키워드, 토픽 비율 파이 차트 |
| **Sentiment & Emotions** | 소스별/카테고리별 감성 분포, 감정(Plutchik 8가지) 히트맵 |
| **Time Series** | 시계열 트렌드, STL 분해 결과, 버스트 감지 시각화 |
| **Word Cloud** | 전체/토픽별/카테고리별 워드 클라우드 (한국어 + 영어) |
| **Article Explorer** | 개별 기사 검색, 필터, 원문 링크, 분석 결과 상세 |

### 5.4 대시보드 스크린샷 예시

대시보드는 다중 기간(Daily/Monthly/Quarterly/Yearly) 집계를 지원한다.
월간 선택 시 해당 월의 모든 날짜 데이터를 자동 병합하여 보여준다.

---

## 6. 데이터 조회

### 6.1 데이터 디렉터리 구조

```
data/
├── raw/YYYY-MM-DD/           ← 크롤링 원본 (JSONL)
│   ├── all_articles.jsonl    ← 전체 기사 (1행 = 1기사)
│   ├── crawl_report.json     ← 크롤링 리포트
│   └── *.jsonl               ← 사이트별 개별 파일
├── processed/YYYY-MM-DD/     ← 전처리 결과 (Stage 1-2)
├── features/YYYY-MM-DD/      ← 특성 추출 결과 (Stage 2)
├── analysis/YYYY-MM-DD/      ← 분석 결과 (Stage 3-7)
│   └── analysis.parquet      ← 기사별 분석 결과 (21 columns)
├── output/YYYY-MM-DD/        ← 최종 산출물
│   ├── articles.parquet      ← 정제된 기사 (12 columns, ZSTD)
│   ├── analysis.parquet      ← 분석 결과 (21 columns, ZSTD)
│   ├── topics.parquet        ← 토픽 정보 (7 columns)
│   ├── signals.parquet       ← 신호 분류 (12 columns)
│   └── index.sqlite          ← 검색 인덱스 (FTS5)
├── dedup.sqlite              ← 중복 제거 DB (전역)
└── logs/                     ← 실행 로그
    ├── daily/                ← 일일 파이프라인 로그
    ├── weekly/               ← 주간 리스캔 로그
    ├── errors.log            ← 에러 로그 (누적)
    └── alerts/               ← 실패 알림 파일
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
1. 가상환경 자동 감지 및 활성화
2. 사전 건강 점검 (디스크 공간, 의존성)
3. 잠금 파일 획득 (동시 실행 방지)
4. `main.py --mode full` 실행 (4시간 타임아웃)
5. 로그 회전 (500MB 초과 시 30일 이상 로그 삭제)
6. 잠금 파일 해제

Exit codes:
| 코드 | 의미 |
|------|------|
| 0 | 성공 |
| 1 | 파이프라인 실패 |
| 2 | 건강 점검 실패 |
| 3 | 잠금 획득 실패 (다른 인스턴스 실행 중) |
| 4 | 타임아웃 (4시간 초과) |

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

## 11. 관련 문서

| 문서 | 내용 |
|------|------|
| [`GLOBALNEWS-README.md`](GLOBALNEWS-README.md) | 시스템 개요, 빠른 시작, 실행 결과 |
| [`GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md`](GLOBALNEWS-ARCHITECTURE-AND-PHILOSOPHY.md) | 설계 철학, 4-Layer 시스템 아키텍처 + 5계층 QA |
| [`prompt/workflow.md`](prompt/workflow.md) | 20-step 워크플로우 설계도 (구축 과정 기록) |
| [`config/sources.yaml`](config/sources.yaml) | 121개 사이트 설정 (URL, 선택자, 제한) |
| [`data/config/pipeline.yaml`](data/config/pipeline.yaml) | 8-Stage 분석 파이프라인 설정 |
