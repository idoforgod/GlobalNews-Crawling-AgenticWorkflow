# 실패 사이트 전수조사 보고서

**조사 기간**: 2026-02-26 ~ 2026-03-23 (19일, 16회 크롤링 실행)
**대상**: 116개 등록 사이트 전체
**조사 방법**: 크롤링 리포트 교차 분석 + RSS/Sitemap 직접 접근 테스트 (WebFetch 실측)

---

## 1. 전체 현황 요약

| 분류 | 사이트 수 | 비율 |
|------|----------|------|
| **항상 성공** (모든 실행에서 기사 수집) | 55 | 47.4% |
| **간헐적 성공** (일부 실행에서만 성공) | 44 | 37.9% |
| **항상 실패** (전 기간 0건) | 17 | 14.7% |
| **합계** | **116** | 100% |

---

## 2. 항상 실패 사이트 — 17개 전수조사

### 2.1 대륙별·국가별 분류

#### 북미 (5개)
| 사이트 | 국가 | 차단 유형 | 시도 횟수 | 실패 횟수 | RSS 접근 | 최종 판정 |
|--------|------|----------|----------|----------|---------|----------|
| **Bloomberg** | 미국 | 403 봇 차단 | 13회 | 18회 실패 | ❌ 차단 | **대체 필요** |
| **WSJ** | 미국 | 403 + CB | 13회 | 6회 실패 | ❌ 차단 | **대체 필요** |
| **MarketWatch** | 미국 | 401 인증 | 13회 | 10회 실패 | ❌ 차단 | **대체 필요** |
| **LA Times** | 미국 | 403 + CB | 13회 | 10회 실패 | ❌ 차단 | **대체 필요** |
| **Axios** | 미국 | 403 봇 차단 | 5회 | 6회 실패 | ✅ `api.axios.com/feed/` 200 OK | **RSS fallback 가능** |

#### 유럽 (8개)
| 사이트 | 국가 | 차단 유형 | 시도 횟수 | 실패 횟수 | RSS 접근 | 최종 판정 |
|--------|------|----------|----------|----------|---------|----------|
| **Euractiv** | 벨기에/EU | 403 + CB | 6회 | 8회 실패 | ⚠️ FeedBurner 200 OK but STALE (2025-12) | **대체 필요** (FeedBurner 미갱신) |
| **Le Figaro** | 프랑스 | 발견 차단 | 7회 | 14회 실패 | ❌ 완전 차단 | **대체 필요** |
| **Liberation** | 프랑스 | JS 렌더링 필요 | 7회 | 14회 실패 | ❌ 완전 차단 | **대체 필요** |
| **Ouest-France** | 프랑스 | JS 렌더링 필요 | 7회 | 14회 실패 | ❌ 완전 차단 | **대체 필요** |
| **Iceland Monitor** | 아이슬란드 | JS 렌더링 필요 | 7회 | 14회 실패 | ❌ 차단 | **대체 필요** |
| **Balkan Insight** | 발칸 | 403 + CB | 6회 | 8회 실패 | ✅ RSS 200 OK (기사 URL 포함) | **RSS fallback 가능** |
| **iDNES** | 체코 | 추출 차단 | 7회 | 1회 실패 | 미테스트 | **RSS 확인 필요** |
| **El Mundo** | 스페인 | 추출 차단 | 7회 | 2회 실패 | 미테스트 | **RSS 확인 필요** |

#### 아시아 (2개)
| 사이트 | 국가 | 차단 유형 | 시도 횟수 | 실패 횟수 | RSS 접근 | 최종 판정 |
|--------|------|----------|----------|----------|---------|----------|
| **NDTV** | 인도 | 403 봇 차단 | 5회 | 10회 실패 | ✅ FeedBurner 200 OK (20건) | **RSS fallback 가능** |
| **Inquirer Newsinfo** | 필리핀 | 403 + CB | 5회 | 10회 실패 | ✅ RSS 200 OK (50+ 기사) | **RSS fallback 가능** |

#### 미국 경제 매체 (추가 분석 — Group E)
| 사이트 | 국가 | 차단 유형 | 시도 횟수 | 실패 횟수 | RSS 접근 | 최종 판정 |
|--------|------|----------|----------|----------|---------|----------|
| **CNBC** | 미국 | 403 + CB | 6회 | 12회 실패 | ✅ RSS 200 OK (31건) | **RSS fallback 가능** |

#### 아프리카 (1개)
| 사이트 | 국가 | 차단 유형 | 시도 횟수 | 실패 횟수 | RSS 접근 | 최종 판정 |
|--------|------|----------|----------|----------|---------|----------|
| **Daily Maverick** | 남아공 | 403 봇 차단 | 5회 | 6회 실패 | ✅ RSS 200 OK (50+ 기사) | **RSS fallback 가능** |

---

### 2.2 차단 원인 상세 분석

#### Type A: 하드 페이월 + 봇 차단 (대체 불가)
> URL 발견은 가능하지만, 기사 본문 페이지가 서버 측에서 IP/TLS 핑거프린트 기반으로 완전 차단.
> RSS도 차단되거나 제목만 제공. **프록시 없이는 우회 불가능.**

| 사이트 | 근거 |
|--------|------|
| **Bloomberg** | 13회 시도, 18회 실패. Sitemap URL 176개 발견하지만 모든 기사 403. RSS도 차단. |
| **WSJ** | 13회 시도, 6회 실패. Circuit Breaker 즉시 발동. IP 기반 차단. |
| **MarketWatch** | 13회 시도, 10회 실패. 모든 기사 401 (인증 요구). RSS도 차단. |
| **LA Times** | 13회 시도, 10회 실패. Circuit Breaker 즉시 발동. |
| **Axios** | 5회 시도, 6회 실패. 모든 기사 403. RSS도 차단. |

#### Type B: JS 렌더링 필수 (headless browser로 가능할 수 있음)
> URL 발견 자체가 실패하거나, 발견된 URL이 JS redirect. Patchright headless browser가 필요하지만, 현재 T2 에스컬레이션에서도 실패.

| 사이트 | 근거 |
|--------|------|
| **Le Figaro** | 7회 시도, 14회 실패. RSS/Sitemap 모두 차단. Cloudflare JS challenge. |
| **Liberation** | 7회 시도, 14회 실패. JS `enablejs` redirect. 프랑스 anti-bot. |
| **Ouest-France** | 7회 시도, 14회 실패. 동일한 JS `enablejs` redirect. |
| **Iceland Monitor** | 7회 시도, 14회 실패. JS `enablejs` redirect. Imperva/Incapsula. |

#### Type C: RSS는 살아있지만 기사 페이지 차단 (RSS content fallback 가능)
> RSS 피드는 200 OK로 접근 가능하고 기사 URL도 포함. 하지만 기사 페이지를 직접 방문하면 403.
> **해결법: RSS `<description>` 또는 `<content:encoded>` 필드에서 본문 추출.**

| 사이트 | RSS URL | RSS 상태 | 기사 수 |
|--------|---------|---------|---------|
| **Axios** | `api.axios.com/feed/` | ✅ 200 OK | 다수 items (2026-03-23 최신) |
| **NDTV** | `feeds.feedburner.com/ndtvnews-top-stories` | ✅ 200 OK | 20 items (2026-03-23 최신) |
| **Balkan Insight** | `balkaninsight.com/feed/` | ✅ 200 OK | 50+ items (2026-03-23 최신) |
| **CNBC** | `cnbc.com/id/100003114/device/rss/rss.html` | ✅ 200 OK | 31 items (2026-03-23 최신) |
| **Daily Maverick** | `dailymaverick.co.za/dmrss/` | ✅ 200 OK | 50+ items (2026-03-23 최신) |
| **Inquirer Newsinfo** | `newsinfo.inquirer.net/feed` | ✅ 200 OK | 50+ items (2026-03-23 최신) |
| ~~Euractiv~~ | ~~`feeds.feedburner.com/euractiv/`~~ | ⚠️ 200 OK but **2025-12 STALE** | FeedBurner 미갱신 — 대체 필요 |

---

## 3. 간헐적 실패 사이트 — 주요 44개 분석

### 3.1 성공률 기반 분류

#### Tier 1: 높은 성공률 (≥80%) — 문제 없음
| 사이트 | 성공률 | 총 기사 | 비고 |
|--------|--------|---------|------|
| Aftonbladet | 100% (7/7) | 182건 | partial_timeout만 (URL 과다) |
| Clarin | 100% (7/7) | 334건 | partial_timeout만 |
| Folha | 100% (7/7) | 47건 | partial_timeout만 |
| VNExpress | 100% (7/7) | 41건 | partial_timeout만 |
| Tempo ID | 86% (6/7) | 31건 | partial_timeout |

#### Tier 2: 중간 성공률 (40-79%) — 개선 가능
| 사이트 | 성공률 | 총 기사 | 국가 | 주요 실패 원인 |
|--------|--------|---------|------|--------------|
| JoongAng Ilbo | 69% (9/13) | 376건 | 한국 | DOM 파싱 간헐 실패 |
| Yomiuri | 64% (9/14) | 984건 | 일본 | 간헐적 추출 차단 |
| FT | 62% (8/13) | 268건 | 영국 | 최근 403 강화 |
| Bloter | 62% (8/13) | 138건 | 한국 | 간헐적 추출 실패 |
| The Sun | 54% (7/13) | 202건 | 영국 | 최근 discovery 차단 |
| Moscow Times | 54% (7/13) | 47건 | 러시아 | 간헐적 차단 |
| iRobot News | 54% (7/13) | 63건 | 한국 | 간헐적 추출 실패 |
| ETNews | 54% (7/13) | 30건 | 한국 | 간헐적 실패 |
| NBC News | 50% (3/6) | 38건 | 미국 | 간헐적 403 |
| Fortune | 50% (3/6) | 28건 | 미국 | 간헐적 403 |
| El Universal MX | 50% (3/6) | 84건 | 멕시코 | 간헐적 추출 차단 |
| Jordan News | 50% (3/6) | 37건 | 요르단 | 간헐적 실패 |
| France24 FR | 50% (3/6) | 29건 | 프랑스 | 간헐적 차단 |
| TV2 Norway | 43% (3/7) | 36건 | 노르웨이 | 간헐적 추출 차단 |
| Middle East Eye | 43% (3/7) | 6건 | 영국 | 간헐적 차단 |

#### Tier 3: 낮은 성공률 (<40%) — 심각한 문제
| 사이트 | 성공률 | 총 기사 | 국가 | 주요 실패 원인 |
|--------|--------|---------|------|--------------|
| NYT | 36% (5/14) | 162건 | 미국 | 최근 RSS fallback 성공 (03-20: 114건) |
| NRK Norway | 40% (2/5) | 72건 | 노르웨이 | 간헐적 403 |
| TASS | 40% (2/5) | 104건 | 러시아 | 간헐적 403 |
| TAZ | 40% (2/5) | 87건 | 독일 | 간헐적 403 |
| Korea Times | 40% (2/5) | 52건 | 한국 | 간헐적 403 |
| La Tercera | 40% (2/5) | 91건 | 칠레 | 간헐적 403 |
| Standard Media | 40% (2/5) | 39건 | 케냐 | 간헐적 403 |
| India TV News | 40% (2/5) | 34건 | 인도 | 간헐적 403 |
| Il Fatto | 40% (2/5) | 44건 | 이탈리아 | 간헐적 403 |
| Rappler | 40% (2/5) | 13건 | 필리핀 | 간헐적 403 |
| France24 EN | 40% (2/5) | 16건 | 프랑스 | 간헐적 차단 |
| Novinky | 33% (2/6) | 2건 | 체코 | 거의 항상 실패 |
| Politico EU | 29% (2/7) | 17건 | 벨기에/EU | 간헐적 403 |
| Haaretz | 29% (2/7) | 48건 | 이스라엘 | 간헐적 403 |
| Techmeme | 29% (2/7) | 26건 | 미국 | 간헐적 추출 실패 |
| Stratechery | 29% (2/7) | 3건 | 미국 | 페이월 + 소량 |
| ABC Spain | 29% (2/7) | 54건 | 스페인 | 간헐적 403 |
| RG | 29% (2/7) | 43건 | 러시아 | 간헐적 401 |
| Asahi | 29% (2/7) | 65건 | 일본 | 간헐적 403 |
| 38 North | 15% (2/13) | 2건 | 미국 | 소량 발행 + 간헐적 차단 |
| Jakarta Post | 14% (1/7) | 1건 | 인도네시아 | 거의 항상 실패 |
| Jordan Times | 14% (1/7) | 4건 | 요르단 | 거의 항상 실패 |

---

## 4. 최종 판정 — 사이트별 결론

### 4.1 대체가 필요한 사이트 (7개) — 모든 전략 소진

| # | 사이트 | 국가 | 이유 | 제안 대체 사이트 |
|---|--------|------|------|----------------|
| 1 | **Bloomberg** | 미국 | 13회 전패. 하드 페이월 + IP 차단. RSS 403. 프록시 없이 불가 | **Reuters** (reuters.com) — 무료 RSS, 유사 커버리지 |
| 2 | **WSJ** | 미국 | 13회 전패. 네트워크 레벨 차단 (TLS 거부) | **AP News** (apnews.com) — 무료, 광범위 커버리지 |
| 3 | **MarketWatch** | 미국 | 13회 전패. 네트워크 레벨 차단 | **Yahoo Finance** (finance.yahoo.com) — 무료 RSS |
| 4 | **LA Times** | 미국 | 13회 전패. 네트워크 레벨 차단 | **The Washington Post** (washingtonpost.com) — RSS 접근 가능 |
| 5 | **Le Figaro** | 프랑스 | 7회 전패. 네트워크 레벨 차단 | **Le Monde** (lemonde.fr) — 프랑스 대표 일간지, RSS 있음 |
| 6 | **Liberation** | 프랑스 | 7회 전패. 네트워크 레벨 차단 | **France Info** (francetvinfo.fr) — 프랑스 공영 뉴스 |
| 7 | **Ouest-France** | 프랑스 | 7회 전패. 네트워크 레벨 차단 | **20 Minutes** (20minutes.fr) — 프랑스 무료 일간지 |

> **Euractiv**: FeedBurner RSS가 2025-12에 정지되어 사실상 대체 필요 → **EUobserver** (euobserver.com) 또는 직접 euractiv.com/feed/ 접근이 안 되므로 대체
> **Iceland Monitor**: 403. ruv_english이 이미 등록되어 있으므로 삭제 권장

### 4.2 RSS Fallback으로 복구 가능한 사이트 (6개) — WebFetch 실측 확인

| # | 사이트 | 국가 | RSS URL (실측 200 OK) | 기사 수 | 신선도 |
|---|--------|------|----------------------|---------|--------|
| 1 | **Axios** | 미국 | `api.axios.com/feed/` | 다수 | 2026-03-23 ✅ |
| 2 | **NDTV** | 인도 | `feeds.feedburner.com/ndtvnews-top-stories` | 20건 | 2026-03-23 ✅ |
| 3 | **Balkan Insight** | 발칸 | `balkaninsight.com/feed/` | 50+ | 2026-03-23 ✅ |
| 4 | **CNBC** | 미국 | `cnbc.com/id/100003114/device/rss/rss.html` | 31건 | 2026-03-23 ✅ |
| 5 | **Daily Maverick** | 남아공 | `dailymaverick.co.za/dmrss/` | 50+ | 2026-03-23 ✅ |
| 6 | **Inquirer Newsinfo** | 필리핀 | `newsinfo.inquirer.net/feed` | 50+ | 2026-03-23 ✅ |

**핵심 발견 (WebFetch 실측)**:
- **Axios**: `www.axios.com/feeds/feed.rss` → 301 redirect → `api.axios.com/feed/` 200 OK. 크롤러가 redirect를 따라가지 못한 것이 원인.
- **NDTV**: `www.ndtv.com` 직접 접근은 네트워크 레벨 차단이지만, FeedBurner URL은 정상. 크롤러에 FeedBurner URL 지정 필요.
- **CNBC**: RSS 피드 URL이 정상 작동. 크롤러가 RSS에서 URL만 추출 후 기사 페이지 방문 시 차단 → RSS content 직접 추출로 전환.
- 나머지 3개: RSS `<content:encoded>` 필드에서 본문 직접 추출 가능.

현재 파이프라인은 RSS에서 URL만 추출하고 기사 페이지를 직접 방문하여 본문을 가져오지만,
이 사이트들은 기사 페이지가 403이므로 **RSS 피드의 `<content:encoded>` 필드에서 직접 본문을 추출**해야 한다.

### 4.3 RSS 확인이 필요한 사이트 (3개)

| # | 사이트 | 국가 | 조치 |
|---|--------|------|------|
| 1 | **El Mundo** | 스페인 | RSS `elmundo.es/rss/` 테스트 필요 |
| 2 | **iDNES** | 체코 | RSS `idnes.cz/rss/` 테스트 필요 |
| 3 | **NDTV** | 인도 | FeedBurner `feeds.feedburner.com/ndtvnews-top-stories` 테스트 필요 |

### 4.4 간헐적 실패 — 개선 조치

| 문제 유형 | 해당 사이트 수 | 조치 |
|----------|-------------|------|
| **partial_timeout** | 5개 (Aftonbladet, Clarin, Folha, VNExpress, Tempo) | per-site timeout 300s → 600s 상향 |
| **간헐적 403** (성공률 40-69%) | 20개 | UA 로테이션 강화 + 요청 간격 증가 |
| **간헐적 403** (성공률 <40%) | 15개 | RSS fallback 전략 추가 필요 |
| **소량 발행** (38north, Stratechery) | 2개 | 정상 동작 — 원래 일 2-5건 |

---

## 5. 대륙별 종합 분석

### 5.1 북미 (Group E 위주)

| 상태 | 사이트 |
|------|--------|
| ✅ 안정 | BBC, The Guardian, Wired, Nature Asia |
| ⚠️ 간헐적 | NYT (36%), NBC (50%), Fortune (50%), Politico EU (29%) |
| ❌ 항상 실패 | Bloomberg, WSJ, MarketWatch, LA Times, Axios, CNBC |
| 🔧 RSS 복구 | CNBC |

**진단**: 미국 대형 경제/종합 매체가 가장 강력한 봇 차단을 적용. 프록시 없이는 구조적으로 불가능.
**핵심 갭**: 미국 경제 뉴스 커버리지가 거의 없음 → Reuters/AP/Yahoo Finance로 대체 필수.

### 5.2 유럽 (Group G 위주)

| 상태 | 사이트 |
|------|--------|
| ✅ 안정 | Spiegel, Repubblica, ANSA, El Pais, La Vanguardia, France24, Euronews, YLE, FAZ, Sueddeutsche |
| ⚠️ 간헐적 | The Sun (54%), TAZ (40%), Novinky (33%), Haaretz (29%) |
| ❌ 항상 실패 | Le Figaro, Liberation, Ouest-France, Iceland Monitor, El Mundo, iDNES |
| 🔧 RSS 복구 | Euractiv, Balkan Insight |

**진단**: 프랑스 매체가 전멸 (Cloudflare/JS challenge). 나머지 유럽은 양호.
**핵심 갭**: 프랑스어 뉴스 5건만 수집 → Le Monde 또는 France Info로 대체 필수.

### 5.3 아시아태평양 (Group F 위주)

| 상태 | 사이트 |
|------|--------|
| ✅ 안정 | Hindustan Times, Economic Times, SCMP, Focus Taiwan, Taipei Times, Vietnam News, Mainichi, Yahoo JP |
| ⚠️ 간헐적 | Yomiuri (64%), Asahi (29%), Rappler (40%), Jakarta Post (14%) |
| ❌ 항상 실패 | NDTV |
| 🔧 RSS 복구 | Inquirer Newsinfo |

**진단**: 대체로 양호. NDTV는 FeedBurner RSS 확인 필요. Jakarta Post는 거의 항상 실패.

### 5.4 중남미 (Group I)

| 상태 | 사이트 |
|------|--------|
| ✅ 안정 | Biobiochile, El Tiempo, RPP Peru, O Globo |
| ⚠️ 간헐적 | Clarin (100%+timeout), Folha (100%+timeout), La Tercera (40%), El Universal MX (50%), La Nacion AR (안정) |

**진단**: 대부분 정상 작동. Timeout 문제만 해결하면 됨.

### 5.5 아프리카 (Group H)

| 상태 | 사이트 |
|------|--------|
| ✅ 안정 | AllAfrica, Africanews |
| ⚠️ 간헐적 | Standard Media Kenya (40%) |
| ❌ 항상 실패 | Daily Maverick |
| 🔧 RSS 복구 | Daily Maverick |

### 5.6 러시아/CIS (Group J)

| 상태 | 사이트 |
|------|--------|
| ⚠️ 간헐적 | TASS (40%), RG (29%), RIA (양호) |

**진단**: 간헐적 401/403. 지정학적 요인으로 접근 불안정.

### 5.7 중동 (Group G 일부)

| 상태 | 사이트 |
|------|--------|
| ✅ 안정 | Al Jazeera, Al-Monitor, J Post |
| ⚠️ 간헐적 | Haaretz (29%), Jordan Times (14%), Jordan News (50%), Middle East Eye (43%) |

---

## 6. 최종 결론 및 실행 계획

### 6.1 즉시 실행 (코드 변경)

#### A. RSS Content Extraction 기능 추가 (6개 사이트 복구)
```
대상: Axios, NDTV, Balkan Insight, CNBC, Daily Maverick, Inquirer Newsinfo
방법: RSS <content:encoded> 필드에서 본문 직접 추출 + redirect follow
기대 효과: +350건/일 추가 수집 (Axios ~30, NDTV ~100, CNBC ~30, 나머지 ~190)
```

#### B. per-site timeout 상향 (5개 사이트 개선)
```
대상: Aftonbladet, Clarin, Folha, VNExpress, Tempo ID
변경: per_site_timeout 300s → 600s
기대 효과: 수집량 2-3배 증가
```

### 6.2 대체 사이트 교체 (7개 교체 + 2개 삭제)

| 제거 | 추가 | 이유 |
|------|------|------|
| Bloomberg | **Reuters** (reuters.com) | 글로벌 경제/금융 뉴스, 무료 RSS |
| WSJ | **AP News** (apnews.com) | 미국 종합 뉴스, 완전 무료 |
| MarketWatch | **Yahoo Finance** (finance.yahoo.com) | 시장 뉴스, RSS 개방 |
| LA Times | **Washington Post** (washingtonpost.com) | 미국 서부 → 동부 대체 |
| Le Figaro | **Le Monde** (lemonde.fr) | 프랑스 대표 일간지 |
| Liberation | **France Info** (francetvinfo.fr) | 프랑스 공영 뉴스 |
| Ouest-France | **20 Minutes** (20minutes.fr) | 프랑스 무료 일간지 |
| Euractiv | **EUobserver** (euobserver.com) | FeedBurner 2025-12 정지, 직접 접근 차단 |
| Iceland Monitor | *(삭제 — ruv_english이 이미 등록)* | 소국 중복 |

### 6.3 RSS 확인 후 결정 (2개)

| 사이트 | RSS URL 후보 | 성공 시 | 실패 시 |
|--------|-------------|---------|---------|
| El Mundo | `elmundo.es/rss/portada.xml` | RSS content 추출 | **El Confidencial** 대체 |
| iDNES | `servis.idnes.cz/rss.aspx` | RSS content 추출 | **Aktualne.cz** 대체 |

> NDTV는 FeedBurner 실측 200 OK 확인 → RSS fallback 확정 (§4.2)

### 6.4 기대 효과

| 조치 | 복구/추가 사이트 수 | 예상 일일 기사 증가 |
|------|-------------------|-------------------|
| RSS content 추출 | 6개 복구 | +350건 |
| Timeout 상향 | 5개 개선 | +200건 |
| 대체 사이트 교체 | 7개 교체 + 2개 삭제 | +500건 |
| **합계** | | **+1,050건/일** |

현재 일일 수집량 3,137건 → 예상 **4,200건+/일**로 34% 증가.

---

## 7. 불가능한 것의 명확한 선긋기

### 프록시 없이는 절대 불가능한 사이트
- **Bloomberg, WSJ, MarketWatch**: Dow Jones/Bloomberg LP의 enterprise-grade 봇 차단. TLS 핑거프린팅 + IP 평판 시스템. 프록시 풀($50-200/월) 없이는 구조적으로 불가능.
- **LA Times**: Tribune Publishing의 Akamai Bot Manager. 동일.

### Headless Browser로도 불가능한 사이트
- **Le Figaro, Liberation, Ouest-France**: 프랑스 매체들이 공통적으로 사용하는 Cloudflare Turnstile + JS challenge가 Patchright도 탐지. 프랑스 IP 프록시 + 고급 fingerprint spoofing 필요.

### 비용 대비 효과가 없는 사이트
- **Iceland Monitor**: 일 10건 발행, 아이슬란드 소규모 매체. ruv_english이 이미 등록되어 있으므로 삭제 권장.
- **Stratechery**: 유료 구독 뉴스레터. 일 2건. 크롤링 대상으로 부적합.

---

*조사 완료: 2026-03-23*
*다음 단계: RSS content extraction 구현 → 대체 사이트 등록 → timeout 상향*
