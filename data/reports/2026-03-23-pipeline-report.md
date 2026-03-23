# GlobalNews 파이프라인 종합 실행 보고서

**실행 일시**: 2026-03-23 (일)
**보고 시점**: Stage 3 진행 중 (500/3,137 = 15.9%)
**실행 모드**: ULW + Autopilot, Full Pipeline (Crawl + Analyze)
**환경**: macOS (M2 Pro 16GB), Python 3.13.3, spaCy 3.8.11

---

## 1. 실행 개요 (Executive Summary)

116개 글로벌 뉴스 사이트를 대상으로 크롤링 + 8단계 NLP 분석 파이프라인을 실행했다.
크롤링은 약 5.6시간에 걸쳐 **111개 사이트에서 3,137건의 기사**를 수집했고,
NLP 분석은 Stage 1-2를 완료한 후 Stage 3 (Article Analysis)가 현재 진행 중이다.

| 지표 | 값 |
|------|-----|
| 대상 사이트 | 116개 (10개 그룹, A-J) |
| 크롤링 성공 | 89개 사이트 (76.7%) |
| 크롤링 실패 | 17개 사이트 |
| 크롤링 미도달 | 10개 사이트 (timeout/미시도) |
| 수집 기사 | **3,137건** |
| 발견 URL | 20,292개 |
| 중복 제거 | 317건 |
| 신선도 필터 | 2,446건 (날짜 범위 밖) |
| URL 실패 | 5,160건 |
| 총 단어 수 | **1,274,326단어** (평균 406단어/기사) |
| 데이터 크기 | 12.6 MB (JSONL) + 15.8 MB (Parquet) |
| 크롤링 소요 | 20,197초 (~5시간 37분) |

---

## 2. 크롤링 상세 (Crawling Phase)

### 2.1 사이트별 수집 결과 (상위 20)

| 순위 | 사이트 | 기사 수 | 언어 | 지역 |
|------|--------|---------|------|------|
| 1 | Money Today (mt) | 384 | ko | 한국 |
| 2 | ABC Spain (abc_es) | 145 | es | 스페인 |
| 3 | Clarin | 142 | es | 아르헨티나 |
| 4 | Aftonbladet | 121 | sv | 스웨덴 |
| 5 | El Universal Mexico | 98 | es | 멕시코 |
| 6 | Yonhap (yna) | 96 | ko | 한국 |
| 7 | Chosun Ilbo | 89 | ko | 한국 |
| 8 | Yomiuri Shimbun | 84 | ja | 일본 |
| 9 | El Pais | 77 | es | 스페인 |
| 10 | La Vanguardia | 72 | es | 스페인 |
| 11 | La Nacion Argentina | 70 | es | 아르헨티나 |
| 12 | Rossiyskaya Gazeta (rg) | 70 | ru | 러시아 |
| 13 | La Tercera | 69 | es | 칠레 |
| 14 | PhilStar | 67 | en | 필리핀 |
| 15 | JoongAng Ilbo | 61 | ko | 한국 |
| 16 | La Repubblica | 59 | it | 이탈리아 |
| 17 | The Hindu | 59 | en | 인도 |
| 18 | Der Spiegel | 57 | de | 독일 |
| 19 | The Times | 57 | en | 영국 |
| 20 | Hindustan Times | 53 | en | 인도 |

### 2.2 언어 분포

```
English (en)     ████████████████████████████       846건 (27.0%)
Korean (ko)      ███████████████████████████        821건 (26.2%)
Spanish (es)     ████████████████████████           758건 (24.2%)
German (de)      █████                              140건 (4.5%)
Japanese (ja)    ████                               131건 (4.2%)
Swedish (sv)     ████                               121건 (3.9%)
Russian (ru)     ████                               108건 (3.4%)
Italian (it)     ███                                104건 (3.3%)
Norwegian (no)   ██                                  53건 (1.7%)
Polish (pl)      █                                   34건 (1.1%)
Portuguese (pt)  ▏                                   16건 (0.5%)
French (fr)      ▏                                    5건 (0.2%)
```

**총 12개 언어**, 다국어 NLP 분석 대상. 영어·한국어·스페인어가 77.4%를 차지.

### 2.3 크롤링 실패 분석

#### 차단된 사이트 (17개 — extraction_blocked / discovery_blocked)

| 사이트 | 차단 유형 | URL 발견 | 에러 수 | 도달 Tier | 원인 |
|--------|----------|---------|---------|----------|------|
| MarketWatch | extraction | 476 | 952 | T1 | HTTP 401 (인증 필요) |
| WSJ | extraction | 155 | 310 | T2 | HTTP 403 + Circuit Breaker |
| Bloomberg | extraction | 144 | 288 | T2 | HTTP 403 (봇 차단) |
| FT | extraction | 100 | 166 | T2 | HTTP 403 (봇 차단) |
| Euractiv | extraction | 100 | 200 | T2 | HTTP 403 + Circuit Breaker |
| NDTV | extraction | 99 | 198 | T2 | HTTP 403 (봇 차단) |
| Balkan Insight | extraction | 90 | 180 | T2 | Circuit Breaker |
| LA Times | extraction | 55 | 110 | T2 | Circuit Breaker |
| CNBC | extraction | 30 | 60 | T2 | Circuit Breaker |
| Daily Maverick | extraction | 25 | 50 | T2 | HTTP 403 |
| Axios | extraction | 7 | 14 | T2 | HTTP 403 |
| Inquirer | extraction | 40 | 80 | T2 | Circuit Breaker |
| Liberation | extraction | 1 | 2 | T2 | JS 렌더링 필요 |
| Ouest-France | extraction | 1 | 2 | T2 | JS 렌더링 필요 |
| Iceland Monitor | extraction | 1 | 2 | T2 | JS 렌더링 필요 |
| The Sun | discovery | 0 | 3 | T1 | URL 발견 완전 차단 |
| Le Figaro | discovery | 0 | 3 | T1 | URL 발견 완전 차단 |

#### 부분 성공 사이트 (5개 — partial_timeout)

| 사이트 | 수집 | 발견 URL | 수집률 | 원인 |
|--------|------|---------|--------|------|
| Aftonbladet | 71건 | 5,000 | 1.4% | 사이트맵 URL 과다 → 시간 초과 |
| Clarin | 60건 | 790 | 7.6% | 사이트맵 URL 과다 → 시간 초과 |
| Folha | 10건 | 1,070 | 0.9% | 시간 초과 |
| VNExpress | 5건 | 3,157 | 0.2% | 태그 페이지 과다 발견 |
| Tempo (ID) | 5건 | 685 | 0.7% | 시간 초과 |

#### 미도달 사이트 (5개)

38north, gogo_mn, natureasia, ruv_english, nytimes — Multi-Pass 에스컬레이션에서 도달하지 못하거나 0건 수집.

#### 차단 원인 분류

```
HTTP 403 (봇 차단)     ████████████████  12개 사이트
Circuit Breaker 발동   ████████████      8개 사이트 (중복)
HTTP 401 (인증 필요)   █                 1개 사이트
JS 렌더링 필요         ███               3개 사이트
URL 발견 실패          ██                2개 사이트
```

**핵심 병목**: T2 (Patchright/headless browser) 에스컬레이션이 실제 기사 추출에서 실패.
대부분의 차단 사이트는 URL은 발견했지만(RSS/Sitemap 성공) 기사 본문 추출에서 403을 받음.

### 2.4 재시도(Retry) 통계

| Retry Level | 시도 횟수 |
|-------------|----------|
| Level 1 (기본 재시도) | 5,580회 |
| Level 2 (전략 변경) | 18회 |
| Level 3 (고급 우회) | 0회 |
| Level 4 (최종 수단) | 0회 |

Never-Abandon Multi-Pass 시스템이 13번의 전체 순회를 수행.

---

## 3. NLP 분석 파이프라인 (Analysis Phase)

### 3.1 완료된 단계

#### Stage 1: Preprocessing (완료)
- **소요 시간**: 95.5초
- **처리량**: 36.2 articles/s
- **메모리 피크**: 1.36 GB
- **출력**: `articles.parquet` (4.3 MB, 3,137행 × 12컬럼, ZSTD 압축)
- **컬럼**: article_id, url, title, body, source, category, language, published_at, crawled_at, author, word_count, content_hash
- **처리 내용**: 텍스트 정규화, 언어 감지(12개 언어), 한국어 형태소 분석(Kiwi), 영어 토큰화(spaCy)

#### Stage 2: Feature Extraction (완료)
- **소요 시간**: 2,625초 (~44분)
- **메모리 피크**: 1.52 GB
- **출력 파일**:
  - `embeddings.parquet` (10 MB) — SBERT `paraphrase-multilingual-MiniLM-L12-v2` 384차원 임베딩, MPS GPU 가속
  - `tfidf.parquet` (542 KB) — TF-IDF 벡터 (ko/en 각 vocab 10,000)
  - `ner.parquet` (914 KB) — 개체명 인식 (spaCy fallback, protobuf 부재로 XLM-RoBERTa 불가)
- **KeyBERT**: SBERT 공유 모델로 키워드 추출

#### Stage 3: Article Analysis (진행 중)
- **시작**: 05:38:58 UTC
- **현재 진행**: 500/3,137 (15.9%)
- **속도 추이**:

```
100건  06:48  (+70분)  ━━━━━━━━━━━━━━━━━━━
200건  07:54  (+66분)  ━━━━━━━━━━━━━━━━━━
300건  08:49  (+56분)  ━━━━━━━━━━━━━━━
400건  09:40  (+51분)  ━━━━━━━━━━━━━
500건  10:39  (+59분)  ━━━━━━━━━━━━━━━
```

- **평균 속도**: ~100건/60분 (속도 점진 개선 후 안정화)
- **모델 구성**:
  - 감성 분석(EN): `cardiffnlp/twitter-roberta-base-sentiment-latest` (MPS)
  - 감성 분석(KO): `nlptown/bert-base-multilingual-uncased-sentiment` (fallback, MPS)
  - STEEPS 분류: `facebook/bart-large-mnli` (zero-shot, MPS) — **주요 병목**
- **예상 완료**: 잔여 2,637건 × 60분/100건 ≈ **26시간** (3/24 월요일 오후)

### 3.2 대기 단계 (Stage 4-8)

| Stage | 이름 | 입력 | 예상 소요 |
|-------|------|------|----------|
| 4 | Aggregation | articles + embeddings + tfidf + ner + article_analysis | ~10-30분 |
| 5 | Time Series | articles + topics + article_analysis | ~5-15분 |
| 6 | Cross Analysis | timeseries + topics + networks + embeddings | ~10-30분 |
| 7 | Signal Classification | topics + timeseries + cross_analysis + signals | ~5-15분 |
| 8 | Data Output | 전체 집계 → Parquet/SQLite | ~5-10분 |

Stage 4-8은 BERT 추론 없이 scikit-learn/HDBSCAN/통계 기반이므로 Stage 3 대비 매우 빠름.

---

## 4. 시스템 성능 분석

### 4.1 리소스 사용

| 리소스 | 크롤링 | Stage 1 | Stage 2 | Stage 3 |
|--------|--------|---------|---------|---------|
| CPU | 네트워크 I/O 위주 | 36 art/s | MPS GPU | MPS GPU |
| 메모리 (RSS) | ~500 MB | 1.36 GB | 1.52 GB | ~8 GB (첫 시도 stuck) |
| 디스크 | 13 MB | 4.3 MB | 11.5 MB | - |
| 소요 시간 | 5시간 37분 | 95초 | 44분 | ~26시간 (예상) |

### 4.2 발생한 기술적 이슈

1. **Stage 3 MPS 교착 (첫 실행)**: BART zero-shot이 4시간 후 STATE=stuck, CPU 0%로 교착. 프로세스 강제 종료 후 재실행으로 해결. 두 번째 실행에서는 안정적 진행.
2. **protobuf 미설치**: `Davlan/xlm-roberta-base-ner-hrl` NER 모델이 protobuf 부재로 로딩 실패 → spaCy `en_core_web_sm` fallback. 다국어 NER 정확도 저하 가능.
3. **monologg/kobert trust_remote_code**: 대화형 프롬프트 발생 → 자동 fallback으로 `nlptown/bert-base-multilingual-uncased-sentiment` 사용.
4. **urllib3/chardet 버전 경고**: requests 라이브러리의 의존성 버전 불일치 경고 (기능에 영향 없음).

### 4.3 DynamicBypass 에스컬레이션 효과

```
전략 Tier 분포:
  T0 (표준)         89개 사이트 성공
  T1 (헤더 변경)    5,580회 재시도
  T2 (Patchright)   18회 시도 — 대부분 실패
  T3 (고급)         0회
  T4 (최종 수단)    0회
```

T2 이상 에스컬레이션의 실효성이 낮음. 대부분의 차단 사이트(WSJ, FT, Bloomberg 등)는 서버 측 봇 감지가 IP 기반이라 헤더/UA 변경만으로는 우회 불가.

---

## 5. 데이터 품질 평가

### 5.1 기사 품질 지표

| 지표 | 값 |
|------|-----|
| 평균 단어 수 | 406단어 |
| 중앙값 단어 수 | 279단어 |
| 최대 단어 수 | 13,781단어 |
| 총 단어 수 | 1,274,326단어 |
| 고유 소스 | 90개 |
| 언어 수 | 12개 |
| 중복 제거 | 317건 (SimHash + Jaccard) |

### 5.2 지역별 커버리지

| 지역 | 사이트 수 | 기사 수 | 비율 |
|------|----------|---------|------|
| 한국 (Group A-D) | ~25 | ~950 | 30% |
| 유럽 (Group G) | ~30 | ~800 | 26% |
| 중남미 (Group I) | ~8 | ~550 | 18% |
| 영미권 (Group E) | ~15 | ~350 | 11% |
| 아시아태평양 (Group F) | ~15 | ~350 | 11% |
| 기타 (H, J) | ~8 | ~130 | 4% |

### 5.3 커버리지 갭

- **영미권 주요 매체 부재**: NYT(0건), WSJ(차단), Bloomberg(차단), FT(차단), CNBC(차단), Axios(차단), LA Times(차단). 영미권 대형 매체의 봇 차단이 가장 심각한 데이터 갭.
- **프랑스어 매체 약세**: Le Figaro(차단), Liberation(차단), Ouest-France(차단). 5건만 수집.
- **아프리카 매체 부재**: Daily Maverick(차단), Standard Media/AllAfrica는 수집되었으나 적은 양.

---

## 6. 산출물 목록

### 6.1 크롤링 산출물

| 파일 | 크기 | 설명 |
|------|------|------|
| `data/raw/2026-03-23/all_articles.jsonl` | 12.6 MB | 3,137건 전체 기사 (JSON Lines) |
| `data/raw/2026-03-23/.crawl_state.json` | 596 KB | 사이트별 처리 URL 상태 |
| `data/raw/2026-03-23/crawl_report.json` | 28 KB | 크롤링 종합 리포트 |
| `data/raw/2026-03-23/crawl_exhausted_sites.json` | 24 KB | 22개 실패/부분 실패 사이트 상세 |

### 6.2 분석 산출물 (완료분)

| 파일 | 크기 | 설명 |
|------|------|------|
| `data/processed/2026-03-23/articles.parquet` | 4.3 MB | 전처리된 기사 (12컬럼, ZSTD) |
| `data/features/2026-03-23/embeddings.parquet` | 10.0 MB | SBERT 384차원 임베딩 |
| `data/features/2026-03-23/tfidf.parquet` | 542 KB | TF-IDF 벡터 |
| `data/features/2026-03-23/ner.parquet` | 914 KB | 개체명 인식 결과 |

### 6.3 분석 산출물 (대기 — Stage 3 완료 후 자동 생성)

| 예상 파일 | Stage | 설명 |
|----------|-------|------|
| `article_analysis.parquet` | 3 | 감성·감정·STEEPS 분류 |
| `topics.parquet` | 4 | 토픽 클러스터 (HDBSCAN + BERTopic) |
| `networks.parquet` | 4 | 개체 네트워크 |
| `timeseries.parquet` | 5 | 시계열 분석 (STL, PELT, Kleinberg) |
| `cross_analysis.parquet` | 6 | 교차 분석 (Granger, PCMCI) |
| `signals.parquet` | 7 | 시그널 분류 (5-Layer, BERTrend) |
| `global_news.db` (SQLite) | 8 | 통합 데이터베이스 (FTS5 검색) |
| `*.parquet` (최종) | 8 | 최종 출력 파일 |

**총 디스크 사용**: 201 MB (data/ 디렉토리 전체, 이전 실행 데이터 포함)

---

## 7. 향후 조치 사항

### 7.1 즉시 필요 (Stage 3 완료 후)

1. Stage 4-8 자동 실행 완료 확인
2. 최종 SQLite/Parquet 출력 파일 검증
3. 전체 파이프라인 성능 메트릭 기록

### 7.2 크롤링 개선 (다음 실행 시)

| 우선순위 | 조치 | 예상 효과 |
|---------|------|----------|
| P0 | protobuf 설치 → XLM-RoBERTa NER 활성화 | 다국어 NER 정확도 향상 |
| P0 | monologg/kobert trust_remote_code=True 설정 | KO 감성 분석 정확도 향상 |
| P1 | NYT RSS 피드 추가 (현재 sitemap만 시도) | NYT 기사 수집 가능 |
| P1 | per-site timeout 증가 (300s → 600s) | partial_timeout 5개 사이트 개선 |
| P2 | T2 Patchright 기사 추출 강화 | 17개 차단 사이트 중 일부 우회 |
| P2 | JS 렌더링 필요 사이트 식별 및 headless 전용 전략 | Liberation, Ouest-France 등 |
| P3 | 프록시 풀 도입 | IP 기반 차단(WSJ, Bloomberg) 우회 |

### 7.3 분석 성능 개선

| 조치 | 효과 |
|------|------|
| BART zero-shot → 경량 모델 (DistilBART) 전환 | Stage 3 속도 5-10배 향상 |
| Stage 3 배치 크기 최적화 | MPS GPU 활용률 향상 |
| ONNX Runtime 변환 | CPU/GPU 추론 속도 개선 |

---

## 8. 실행 타임라인

```
06:26  파이프라인 시작 (116개 사이트, full mode)
06:26  크롤링 시작 — 동시성 5, per-site timeout 300s
06:27  한국 사이트(A-D) 크롤링 시작 (chosun, yna, donga, hani, joongang)
06:37  첫 100건 수집 (한국 주요 사이트)
07:00  영어권 사이트 진입 (WSJ, Bloomberg, FT → 403 차단 시작)
08:00  아시아 사이트 확대 (Yomiuri, The Hindu, Jakarta Post)
09:00  유럽·중남미 사이트 (Spiegel, El Pais, Clarin, RG)
10:00  100개 사이트 돌파 (2,000건+)
11:00  Never-Abandon Multi-Pass 10회차+ (잔여 사이트 에스컬레이션)
11:51  all_articles.jsonl 최종 확정 (3,137건)
12:03  크롤링 완료 — crawl_report.json 생성 (20,197초)
12:04  Stage 1 Preprocessing 시작 → 95초 완료
12:05  Stage 2 Feature Extraction 시작
12:47  Stage 2 완료 (44분) — embeddings, tfidf, ner 생성
       ※ 첫 실행의 Stage 3가 4시간 후 MPS 교착 → 프로세스 종료
13:55  분석 재실행 (--mode analyze --all-stages)
14:38  Stage 2 재완료
14:39  Stage 3 시작 (BART zero-shot STEEPS, 두 번째 시도)
15:48  Stage 3: 100/3,137 처리
16:54  Stage 3: 200/3,137 처리
17:49  Stage 3: 300/3,137 처리
18:40  Stage 3: 400/3,137 처리
19:39  Stage 3: 500/3,137 처리 (현재)
~내일  Stage 3 완료 → Stage 4-8 자동 진행 예상
```

---

*보고서 자동 생성: 2026-03-23 | GlobalNews Crawling & Analysis Pipeline*
*파이프라인 버전: v1.0 (20-step workflow complete)*
