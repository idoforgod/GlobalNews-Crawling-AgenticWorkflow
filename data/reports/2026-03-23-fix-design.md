# 크롤링 실패 사이트 해결 설계안

**작성일**: 2026-03-23
**근거**: 실패 사이트 전수조사 보고서 (같은 날짜)
**목표**: 17개 항상 실패 사이트 해결 → 일일 수집량 3,137건 → 4,200건+

---

## 0. 현재 아키텍처 요약 (변경 전)

```
RSS/Sitemap URL 발견  →  개별 URL HTTP fetch  →  HTML에서 본문 추출
      (url_discovery.py)    (pipeline.py)         (article_extractor.py)
            │                     │                       │
            │                     ▼                       │
            │               403/401 차단  ──────→  실패 기록
            │                     │
            │                     ▼
            │            DynamicBypass 에스컬레이션
            │            (T0-T4, 13개 전략)
            │                     │
            │                     ▼
            │              여전히 실패 → 0건
            ▼
       DiscoveredURL(url, title_hint, ...)
       ※ RSS 본문(content:encoded)은 버려짐
```

**문제의 핵심**: RSS 피드에서 기사 URL만 추출하고 본문은 버린다. 그런데 기사 페이지에 직접 접근하면 403이다. RSS에 이미 본문이 있는데 사용하지 않는 구조적 결함.

---

## 1. 해결 전략 3가지

| # | 전략 | 대상 | 변경 범위 | 효과 |
|---|------|------|----------|------|
| A | **RSS Content Extraction** | 6개 사이트 | contracts + url_discovery + pipeline | +350건/일 |
| B | **대체 사이트 등록** | 7개 교체 + 2개 삭제 | sources.yaml + adapters | +500건/일 |
| C | **Timeout/Config 수정** | 5+3개 사이트 | sources.yaml | +200건/일 |

---

## 2. 전략 A: RSS Content Extraction (핵심 변경)

### 2.1 설계 원리

```
변경 후:

RSS 피드 파싱  →  content:encoded 있음?  ─── Yes ──→  RSS에서 직접 RawArticle 생성
      │                                                (페이지 fetch 건너뜀)
      │                   No
      ▼                    │
  URL만 추출               ▼
  (기존 흐름)        title+summary만 → 기존 흐름 유지
```

### 2.2 변경 파일 및 상세

#### 파일 1: `src/crawling/contracts.py` — DiscoveredURL에 body_hint 필드 추가

```python
# 변경: DiscoveredURL dataclass에 body_hint 추가
@dataclass
class DiscoveredURL:
    url: str
    source_id: str
    discovered_via: str = "rss"
    published_at: datetime | None = None
    title_hint: str | None = None
    body_hint: str | None = None      # ← NEW: RSS <content:encoded> 본문
    author_hint: str | None = None    # ← NEW: RSS <dc:creator> 저자
    priority: int = 0
```

**이유**: RSS 피드에서 파싱한 본문을 URL 객체에 실어서 pipeline까지 전달. 기존 `title_hint`와 동일한 패턴.

#### 파일 2: `src/crawling/url_discovery.py` — RSS 파싱 시 본문 추출

```python
# 변경 위치: _parse_rss_feed() 메서드 내부, 약 line 250-270

# 기존: title만 추출
title_hint = entry.get("title", None)

# 추가: content:encoded 또는 summary 추출
body_hint = None
content_list = entry.get("content", [])
if content_list and isinstance(content_list, list):
    body_hint = content_list[0].get("value", "")
if not body_hint or len(body_hint) < 200:
    body_hint = entry.get("summary", "")
# HTML 태그 제거 (RSS content는 보통 HTML)
if body_hint:
    from bs4 import BeautifulSoup
    body_hint = BeautifulSoup(body_hint, "html.parser").get_text(separator=" ").strip()
    if len(body_hint) < 100:
        body_hint = None  # 너무 짧으면 버림

author_hint = entry.get("author", None)

results.append(DiscoveredURL(
    url=article_url,
    source_id=source_id,
    discovered_via="rss",
    published_at=pub_dt,
    title_hint=title_hint,
    body_hint=body_hint,        # ← NEW
    author_hint=author_hint,    # ← NEW
))
```

**동일 변경**: `_parse_xml_text()` 메서드 (raw XML 파서)에도 적용. `<content:encoded>` 태그에서 추출.

#### 파일 3: `src/crawling/pipeline.py` — RSS 본문이 있으면 fetch 건너뛰기

```python
# 변경 위치: _crawl_urls() 내부, 개별 URL 처리 루프
# 약 line 2100 부근, URL fetch 직전에 삽입

# ---- NEW: RSS Content Extraction shortcut ----
if (url_obj.body_hint
        and len(url_obj.body_hint) >= MIN_RSS_BODY_LENGTH  # 200자
        and url_obj.title_hint):
    article = self._create_rss_content_article(
        url_obj, site_id, site_cfg)
    if article is not None:
        article_id = str(uuid.uuid4())
        dedup_result = self._dedup.is_duplicate(
            url=article.url, title=article.title,
            body=article.body, source_id=site_id,
            article_id=article_id)
        if not dedup_result.is_duplicate:
            writer.write_article(article)
            result.extracted_count += 1
            logger.info(
                "rss_content_extracted url=%s site_id=%s words=%d",
                url_obj.url[:80], site_id,
                len(article.body.split()))
            self._circuit_breakers.record_success(site_id)
            continue
# ---- END NEW ----

# 기존 흐름: HTTP fetch → extract_article()
```

#### 파일 3 (계속): `_create_rss_content_article()` 새 메서드

```python
MIN_RSS_BODY_LENGTH = 200  # RSS 본문 최소 길이 (자 기준)

def _create_rss_content_article(
    self,
    url_obj: DiscoveredURL,
    site_id: str,
    site_cfg: dict[str, Any],
) -> RawArticle | None:
    """RSS feed에서 추출한 본문으로 RawArticle 생성.

    기존 _create_rss_fallback_article()은 title_only(제목만).
    이 메서드는 body_hint(본문 전체)를 사용하여 완전한 기사를 생성.
    """
    body = url_obj.body_hint
    if not body or len(body) < MIN_RSS_BODY_LENGTH:
        return None

    title = (url_obj.title_hint or "").strip()
    if not title:
        return None

    source_name = site_cfg.get("name", site_id)
    language = site_cfg.get("language", "en")
    published_at = url_obj.published_at
    if published_at is not None:
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if published_at < self._lookback_cutoff:
            return None

    return RawArticle(
        url=url_obj.url,
        title=title,
        body=body,
        source=source_name,
        language=language,
        published_at=published_at.isoformat() if published_at else "",
        crawled_at=datetime.now(timezone.utc).isoformat(),
        author=url_obj.author_hint or "",
        category="",
        content_hash=compute_content_hash(body),
        is_paywall_truncated=False,  # RSS 본문은 완전체
    )
```

### 2.3 동작 흐름 (변경 후)

```
사이트: CNBC (rss_url: cnbc.com/id/100003114/device/rss/rss.html)

1. url_discovery.py → RSS 파싱
   └─ entry.content[0].value = "<p>CNBC reported that...</p>" (500자)
   └─ DiscoveredURL(url=".../article.html", body_hint="CNBC reported...", title_hint="...")

2. pipeline.py → URL 처리
   └─ body_hint 존재 + len >= 200 + title 존재
   └─ _create_rss_content_article() 호출
   └─ RawArticle 생성 (HTTP fetch 건너뜀!)
   └─ dedup 체크 → writer.write_article()

3. 결과: 403 에러 없이 기사 수집 성공
```

### 2.4 대상 사이트 RSS 본문 예상

| 사이트 | RSS 본문 형태 | 예상 길이 | 비고 |
|--------|-------------|----------|------|
| Axios | `<content:encoded>` | 300-800자 | Axios 기사가 원래 짧음 |
| NDTV | `<description>` | 200-500자 | FeedBurner summary |
| CNBC | `<description>` | 200-400자 | Summary 수준 |
| Balkan Insight | `<content:encoded>` | 500-2000자 | 완전 본문 가능성 높음 |
| Daily Maverick | `<content:encoded>` | 500-2000자 | 완전 본문 가능성 높음 |
| Inquirer Newsinfo | `<content:encoded>` | 500-1500자 | 완전 본문 가능성 높음 |

### 2.5 영향 범위 분석 (Ripple Effect)

| 변경 | 직접 영향 | 간접 영향 |
|------|----------|----------|
| `DiscoveredURL.body_hint` 추가 | url_discovery → pipeline 전달 | 직렬화 불필요 (메모리만) |
| `_parse_rss_feed()` 변경 | RSS 파싱 시 BeautifulSoup 추가 | 성능 영향 미미 (피드당 1회) |
| `_crawl_urls()` shortcut | 6개 사이트만 해당 | 기존 사이트는 body_hint=None → 기존 흐름 |
| 새 메서드 추가 | pipeline.py 100줄 | 기존 메서드와 독립 |

**위험도**: 낮음. 기존 사이트는 body_hint=None이므로 새 shortcut을 타지 않음. 완전한 하위 호환.

---

## 3. 전략 B: 대체 사이트 등록

### 3.1 sources.yaml 변경

#### 삭제 (2개)

```yaml
# 삭제: icelandmonitor (ruv_english이 이미 등록)
# 삭제: euractiv (FeedBurner 2025-12 정지, 직접 접근 차단)
```

#### 교체 (7개)

```yaml
# bloomberg → reuters
reuters:
  name: Reuters
  url: https://www.reuters.com
  region: global
  language: en
  group: E
  crawl:
    primary_method: rss
    fallback_methods: [sitemap, dom]
    rss_url: https://www.reuters.com/arc/outboundfeeds/news-sitemap-index/?outputType=xml
    sitemap_url: https://www.reuters.com/arc/outboundfeeds/sitemap-index/?outputType=xml
    rate_limit_seconds: 3

# wsj → apnews
apnews:
  name: AP News
  url: https://apnews.com
  region: us
  language: en
  group: E
  crawl:
    primary_method: rss
    fallback_methods: [sitemap]
    rss_url: https://apnews.com/index.rss
    sitemap_url: https://apnews.com/sitemap.xml
    rate_limit_seconds: 3

# marketwatch → yahoo_finance
yahoo_finance:
  name: Yahoo Finance
  url: https://finance.yahoo.com
  region: us
  language: en
  group: E
  crawl:
    primary_method: rss
    fallback_methods: [dom]
    rss_url: https://finance.yahoo.com/news/rssindex
    rate_limit_seconds: 3

# latimes → washingtonpost
washingtonpost:
  name: The Washington Post
  url: https://www.washingtonpost.com
  region: us
  language: en
  group: E
  crawl:
    primary_method: rss
    fallback_methods: [sitemap]
    rss_url: https://feeds.washingtonpost.com/rss/world
    rss_fallback_url: https://feeds.washingtonpost.com/rss/national
    sitemap_url: https://www.washingtonpost.com/sitemaps/index.xml
    rate_limit_seconds: 5

# lefigaro → lemonde
lemonde:
  name: Le Monde
  url: https://www.lemonde.fr
  region: fr
  language: fr
  group: G
  crawl:
    primary_method: rss
    fallback_methods: [sitemap]
    rss_url: https://www.lemonde.fr/rss/une.xml
    sitemap_url: https://www.lemonde.fr/sitemap.xml
    rate_limit_seconds: 5

# liberation → franceinfo
franceinfo:
  name: France Info
  url: https://www.francetvinfo.fr
  region: fr
  language: fr
  group: G
  crawl:
    primary_method: rss
    fallback_methods: [sitemap, dom]
    rss_url: https://www.francetvinfo.fr/titres.rss
    sitemap_url: https://www.francetvinfo.fr/sitemap.xml
    rate_limit_seconds: 3

# ouestfrance → 20minutes
vingtminutes:
  name: 20 Minutes
  url: https://www.20minutes.fr
  region: fr
  language: fr
  group: G
  crawl:
    primary_method: rss
    fallback_methods: [sitemap]
    rss_url: https://www.20minutes.fr/feeds/rss-une.xml
    sitemap_url: https://www.20minutes.fr/sitemap.xml
    rate_limit_seconds: 3
```

### 3.2 추가 교체 (Euractiv)

```yaml
# euractiv → euobserver
euobserver:
  name: EUobserver
  url: https://euobserver.com
  region: eu
  language: en
  group: E
  crawl:
    primary_method: rss
    fallback_methods: [dom]
    rss_url: https://euobserver.com/rss.xml
    rate_limit_seconds: 5
```

### 3.3 영향 범위

- **sources.yaml**: 9개 사이트 추가/수정, 2개 삭제
- **scripts/**: `extract_site_urls.py`, `split_sites_by_group.py`, `distribute_sites_to_teams.py`, `validate_site_coverage.py` — 사이트 수 116 → 123 (또는 재조정)
- **D-7 동기화**: `CRAWL_GROUPS`, site registry 5곳 동기화 필요 (validate_site_registry_sync.py가 검증)
- **어댑터**: 신규 사이트용 전용 어댑터 불필요 (RSS 기본 추출기로 충분)

---

## 4. 전략 C: Timeout/Config 수정

### 4.1 per_site_timeout 상향 (5개 사이트)

```yaml
# data/config/sources.yaml 내 각 사이트의 crawl 섹션
aftonbladet:
  crawl:
    per_site_timeout: 600  # 300 → 600

clarin:
  crawl:
    per_site_timeout: 600

folha:
  crawl:
    per_site_timeout: 600

vnexpress:
  crawl:
    per_site_timeout: 600

tempo_id:
  crawl:
    per_site_timeout: 600
```

### 4.2 RSS URL 수정 (3개 사이트)

```yaml
# NDTV: 잘못된 FeedBurner URL 수정
ndtv:
  crawl:
    rss_url: https://feeds.feedburner.com/ndtvnews-top-stories  # 기존: ndtvnews-latest

# Axios: redirect를 따라가는 최종 URL로 변경
axios:
  crawl:
    rss_url: https://api.axios.com/feed/  # 기존과 동일하지만 명시적

# Daily Maverick: 정확한 RSS URL
dailymaverick:
  crawl:
    rss_url: https://www.dailymaverick.co.za/dmrss/  # 기존: /rss (잘못됨)
```

### 4.3 영향 범위

- **sources.yaml만 변경** — 코드 변경 없음
- 파이프라인 재실행 시 자동 적용

---

## 5. 구현 순서 (실행 계획)

```
Phase 1: Config 수정 (즉시, 위험도 없음)
├─ 4.1 per_site_timeout 상향 (5개 사이트)
├─ 4.2 RSS URL 수정 (3개 사이트)
└─ 검증: dry-run으로 설정 확인

Phase 2: RSS Content Extraction (핵심, 중간 위험도)
├─ 2.2-A contracts.py: body_hint, author_hint 필드 추가
├─ 2.2-B url_discovery.py: RSS 파싱 시 본문 추출
├─ 2.2-C pipeline.py: RSS 본문 shortcut + _create_rss_content_article()
├─ 테스트: CNBC 1개 사이트로 단위 테스트
└─ 검증: 6개 사이트 대상 --sites cnbc,axios,ndtv,balkaninsight,dailymaverick,inquirer_newsinfo

Phase 3: 대체 사이트 등록 (중간 위험도)
├─ 3.1 sources.yaml: 7개 추가, 2개 삭제
├─ 3.2 사이트 수 변경 반영 (116 → 121)
├─ 3.3 D-7 동기화 검증 (validate_site_registry_sync.py)
└─ 검증: 신규 사이트 개별 크롤링 테스트

Phase 4: 통합 테스트
├─ full pipeline --dry-run
├─ full pipeline 실행 (121개 사이트)
└─ 결과 비교 (3,137건 → 4,200건+ 목표)
```

---

## 6. 변경 파일 요약

| 파일 | 변경 유형 | 변경 크기 |
|------|----------|----------|
| `src/crawling/contracts.py` | 필드 추가 (2줄) | S |
| `src/crawling/url_discovery.py` | RSS 본문 추출 로직 (15줄 × 2곳) | M |
| `src/crawling/pipeline.py` | shortcut + 새 메서드 (50줄) | M |
| `data/config/sources.yaml` | 사이트 추가/수정/삭제 (~120줄) | L |
| `scripts/validate_site_registry_sync.py` | 사이트 수 상수 업데이트 | S |
| `scripts/extract_site_urls.py` | 사이트 수 상수 업데이트 | S |

**총 코드 변경**: ~100줄 Python + ~120줄 YAML

---

## 7. 위험 분석 및 완화

| 위험 | 확률 | 영향 | 완화 |
|------|------|------|------|
| RSS body_hint가 너무 짧음 (summary만) | 중 | CNBC, NDTV에서 짧은 기사 | MIN_RSS_BODY_LENGTH=200으로 필터 |
| 대체 사이트도 403 | 저 | 해당 사이트 0건 | 사전 RSS 접근 테스트 완료 |
| DiscoveredURL 메모리 증가 | 저 | RSS 본문 캐시 | 피드당 50-100건 × 1KB = 50-100KB |
| D-7 동기화 누락 | 중 | 사이트 수 불일치 | validate_site_registry_sync.py 실행 |
| 대체 사이트 RSS 구조 다름 | 저 | 파싱 실패 | feedparser가 범용 처리 |

---

## 8. 성공 기준

| 지표 | 현재 | 목표 |
|------|------|------|
| 일일 수집 기사 | 3,137건 | 4,200건+ |
| 활성 사이트 | 89개 (76.7%) | 110개+ (90%+) |
| 항상 실패 사이트 | 17개 | 5개 이하 |
| 프랑스어 기사 | 5건 | 100건+ |
| 미국 경제 뉴스 | 0건 | 200건+ (Reuters, AP, Yahoo Finance) |

---

*설계안 작성 완료: 2026-03-23*
*다음: 사용자 승인 후 Phase 1부터 순차 구현*
