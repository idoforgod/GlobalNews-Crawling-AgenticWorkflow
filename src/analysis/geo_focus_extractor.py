"""Geo Focus Extractor — 기사가 다루는 국가/지역 추출기.

source_country(어디서 발행했나) ≠ geo_focus(무엇에 관한 기사인가).
이 분리가 Q4·Q5·Q6·Q7 분석의 핵심 전제다.

처리 흐름:
    Layer 1 — 제목 딕셔너리 매칭 (< 0.1ms, 가중치 5×)
    Layer 2 — 본문 딕셔너리 매칭 (< 1ms, 앞 2000자만)
    Layer 3 — Stage 2 NER entities_location 정규화 (NER 재실행 없음)
    → 세 레이어 점수 합산 → 순위 결정 → primary / all 확정

출력:
    geo_focus_primary: str          최고 점수 국가 ISO2 코드 (또는 지역 코드)
    geo_focus_all:     list[str]    0.10 이상 국가/지역 코드 전체
    geo_confidence:    float        primary 상대 점수
    geo_method:        str          "dict" | "ner" | "hybrid" | "none"
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ─────────────────────────────────────────────────────────────
# ISO2 코드 → 표준 국가명 (디버깅·출력용)
# ─────────────────────────────────────────────────────────────

ISO2_NAMES: dict[str, str] = {
    "US": "United States",  "CN": "China",         "JP": "Japan",
    "KR": "South Korea",    "KP": "North Korea",   "RU": "Russia",
    "DE": "Germany",        "FR": "France",         "GB": "United Kingdom",
    "IT": "Italy",          "CA": "Canada",         "AU": "Australia",
    "IN": "India",          "BR": "Brazil",         "MX": "Mexico",
    "TR": "Turkey",         "SA": "Saudi Arabia",   "IL": "Israel",
    "IR": "Iran",           "UA": "Ukraine",        "PL": "Poland",
    "TW": "Taiwan",         "HK": "Hong Kong",      "SG": "Singapore",
    "ID": "Indonesia",      "MY": "Malaysia",       "TH": "Thailand",
    "VN": "Vietnam",        "PH": "Philippines",    "PK": "Pakistan",
    "BD": "Bangladesh",     "EG": "Egypt",          "NG": "Nigeria",
    "ZA": "South Africa",   "KE": "Kenya",          "ET": "Ethiopia",
    "AR": "Argentina",      "CL": "Chile",          "CO": "Colombia",
    "PE": "Peru",           "VE": "Venezuela",      "SE": "Sweden",
    "NO": "Norway",         "DK": "Denmark",        "FI": "Finland",
    "NL": "Netherlands",    "BE": "Belgium",        "CH": "Switzerland",
    "AT": "Austria",        "PT": "Portugal",       "ES": "Spain",
    "GR": "Greece",         "CZ": "Czech Republic", "HU": "Hungary",
    "RO": "Romania",        "BG": "Bulgaria",       "RS": "Serbia",
    "HR": "Croatia",        "SK": "Slovakia",       "BY": "Belarus",
    "AZ": "Azerbaijan",     "GE": "Georgia",        "AM": "Armenia",
    "UZ": "Uzbekistan",     "KZ": "Kazakhstan",     "MN": "Mongolia",
    "NZ": "New Zealand",    "QA": "Qatar",          "AE": "UAE",
    "KW": "Kuwait",         "JO": "Jordan",         "LB": "Lebanon",
    "SY": "Syria",          "IQ": "Iraq",           "AF": "Afghanistan",
    "LY": "Libya",          "TN": "Tunisia",        "DZ": "Algeria",
    "MA": "Morocco",        "SD": "Sudan",          "GH": "Ghana",
    "SN": "Senegal",        "TZ": "Tanzania",       "UG": "Uganda",
    "MM": "Myanmar",        "LK": "Sri Lanka",      "NP": "Nepal",
    "CU": "Cuba",           "BO": "Bolivia",        "EC": "Ecuador",
    "PY": "Paraguay",       "UY": "Uruguay",        "CR": "Costa Rica",
    "PA": "Panama",         "GT": "Guatemala",      "MK": "North Macedonia",
    "BA": "Bosnia",         "AL": "Albania",        "ME": "Montenegro",
    "LT": "Lithuania",      "LV": "Latvia",         "EE": "Estonia",
    "IS": "Iceland",        "IE": "Ireland",        "LU": "Luxembourg",
    "MT": "Malta",          "CY": "Cyprus",         "MD": "Moldova",
    # 지역 코드 (국가가 아닌 지역 블록)
    "EU":  "European Union",     "NATO": "NATO",
    "ASEAN": "ASEAN",            "BRICS": "BRICS",
    "G7":  "G7",                 "G20":  "G20",
    "UN":  "United Nations",     "IMF":  "IMF",
    "ME_REGION": "Middle East",  "SE_ASIA": "Southeast Asia",
    "C_ASIA": "Central Asia",    "C_AMERICA": "Central America",
    "W_AFRICA": "West Africa",   "E_AFRICA": "East Africa",
    "BALKANS": "Balkans",        "CAUCASUS": "Caucasus",
    "SCANDINAVIA": "Scandinavia",
}


# ─────────────────────────────────────────────────────────────
# 국가 별칭 사전 (alias → ISO2)
# 언어: ko / en / de / fr / es / ja / zh / pt / it / pl / ar / ru
# ─────────────────────────────────────────────────────────────

_ALIAS: dict[str, str] = {
    # ── 미국 ──────────────────────────────────────────────────
    "미국": "US", "미": "US", "워싱턴": "US", "백악관": "US",
    "미 정부": "US", "미국 정부": "US", "미군": "US",
    "united states": "US", "u.s.": "US", "u.s.a.": "US", "usa": "US",
    "america": "US", "american": "US", "americans": "US",
    "washington": "US", "the white house": "US", "pentagon": "US",
    "wall street": "US", "congress": "US", "senate": "US",
    "vereinigte staaten": "US", "usa ": "US",
    "états-unis": "US", "etats-unis": "US", "americain": "US",
    "estados unidos": "US", "eeuu": "US",
    "アメリカ": "US", "米国": "US", "ワシントン": "US",
    "美国": "US", "美國": "US", "华盛顿": "US", "華盛頓": "US",
    "estados unidos": "US",
    "stati uniti": "US",
    "stany zjednoczone": "US",
    "сша": "US", "соединённые штаты": "US",

    # ── 중국 ──────────────────────────────────────────────────
    "중국": "CN", "베이징": "CN", "상하이": "CN", "북경": "CN",
    "중국 정부": "CN", "시진핑": "CN", "공산당": "CN",
    "china": "CN", "chinese": "CN", "beijing": "CN", "shanghai": "CN",
    "prc": "CN", "peking": "CN",
    "chine": "CN", "pékin": "CN",
    "中国": "CN", "北京": "CN", "上海": "CN",
    "중공": "CN",
    "中國": "CN", "北京": "CN", "上海": "CN",   # 번체 (Traditional)
    "cina": "CN", "chiny": "CN",
    "中美": "CN",    # 中美 언급 시 CN + US 둘 다 포함되도록
    "китай": "CN", "пекин": "CN",

    # ── 일본 ──────────────────────────────────────────────────
    "일본": "JP", "도쿄": "JP", "동경": "JP",
    "japan": "JP", "japanese": "JP", "tokyo": "JP",
    "japon": "JP", "tokio": "JP",
    "日本": "JP", "東京": "JP",
    "japão": "JP", "giappone": "JP", "japonia": "JP",
    "япония": "JP", "токио": "JP",

    # ── 한국 ──────────────────────────────────────────────────
    "한국": "KR", "대한민국": "KR", "서울": "KR",
    "south korea": "KR", "republic of korea": "KR", "seoul": "KR",
    "corée du sud": "KR", "corea del sur": "KR",
    "韓国": "KR", "서울": "KR",
    "남한": "KR",

    # ── 북한 ──────────────────────────────────────────────────
    "북한": "KP", "조선": "KP", "평양": "KP",
    "north korea": "KP", "dprk": "KP", "pyongyang": "KP",
    "corée du nord": "KP", "corea del norte": "KP",
    "북조선": "KP",

    # ── 러시아 ────────────────────────────────────────────────
    "러시아": "RU", "모스크바": "RU",
    "russia": "RU", "russian": "RU", "moscow": "RU", "kremlin": "RU",
    "russie": "RU", "moscou": "RU",
    "rusia": "RU", "moscú": "RU",
    "ロシア": "RU", "モスクワ": "RU",
    "俄罗斯": "RU", "莫斯科": "RU",
    "россия": "RU",

    # ── 독일 ──────────────────────────────────────────────────
    "독일": "DE", "베를린": "DE",
    "germany": "DE", "german": "DE", "berlin": "DE",
    "deutschland": "DE", "berliner": "DE",
    "allemagne": "DE", "berlin": "DE",
    "alemania": "DE",
    "ドイツ": "DE", "ベルリン": "DE",
    "德国": "DE", "柏林": "DE",
    "германия": "DE",

    # ── 프랑스 ────────────────────────────────────────────────
    "프랑스": "FR", "파리": "FR",
    "france": "FR", "french": "FR", "paris": "FR", "élysée": "FR",
    "frankreich": "FR",
    "법국": "FR",
    "フランス": "FR", "パリ": "FR",
    "法国": "FR", "巴黎": "FR",
    "франция": "FR",

    # ── 영국 ──────────────────────────────────────────────────
    "영국": "GB", "런던": "GB",
    "united kingdom": "GB", "britain": "GB", "british": "GB",
    "uk": "GB", "england": "GB", "london": "GB",
    "großbritannien": "GB", "großbrittanien": "GB",
    "grande-bretagne": "GB", "royaume-uni": "GB",
    "gran bretaña": "GB", "reino unido": "GB",
    "イギリス": "GB", "ロンドン": "GB",
    "英国": "GB", "伦敦": "GB",
    "великобритания": "GB",

    # ── 이탈리아 ──────────────────────────────────────────────
    "이탈리아": "IT", "로마": "IT",
    "italy": "IT", "italian": "IT", "rome": "IT",
    "italien": "IT", "italie": "IT",
    "イタリア": "IT", "ローマ": "IT",
    "意大利": "IT",

    # ── 캐나다 ────────────────────────────────────────────────
    "캐나다": "CA", "오타와": "CA",
    "canada": "CA", "canadian": "CA", "ottawa": "CA",
    "kanada": "CA",
    "カナダ": "CA", "오타와": "CA",
    "加拿大": "CA",

    # ── 호주 ──────────────────────────────────────────────────
    "호주": "AU", "시드니": "AU",
    "australia": "AU", "australian": "AU", "sydney": "AU",
    "australien": "AU",
    "オーストラリア": "AU",
    "澳大利亚": "AU",

    # ── 인도 ──────────────────────────────────────────────────
    "인도": "IN", "뉴델리": "IN",
    "india": "IN", "indian": "IN", "new delhi": "IN",
    "indien": "IN",
    "インド": "IN", "ニューデリー": "IN",
    "印度": "IN",

    # ── 브라질 ────────────────────────────────────────────────
    "브라질": "BR", "브라질리아": "BR",
    "brazil": "BR", "brazilian": "BR", "brasilia": "BR",
    "brasilien": "BR", "brésil": "BR",
    "ブラジル": "BR",
    "巴西": "BR",

    # ── 이스라엘 ──────────────────────────────────────────────
    "이스라엘": "IL", "텔아비브": "IL", "예루살렘": "IL",
    "israel": "IL", "israeli": "IL", "tel aviv": "IL", "jerusalem": "IL",
    "jérusalem": "IL",
    "イスラエル": "IL",
    "以色列": "IL",
    "израиль": "IL",

    # ── 팔레스타인 ────────────────────────────────────────────
    "팔레스타인": "PS", "가자": "PS", "서안": "PS", "요르단강서안": "PS",
    "palestine": "PS", "palestinian": "PS", "gaza": "PS",
    "west bank": "PS", "западный берег": "PS",
    # 주의: Hamas·Hezbollah는 기관명 — geo_focus 사전에서 제외
    # (org 엔티티로만 처리)

    # ── 이란 ──────────────────────────────────────────────────
    "이란": "IR", "테헤란": "IR",
    "iran": "IR", "iranian": "IR", "tehran": "IR",
    "иран": "IR",
    "イラン": "IR",

    # ── 우크라이나 ────────────────────────────────────────────
    "우크라이나": "UA", "키이우": "UA", "키예프": "UA",
    "ukraine": "UA", "ukrainian": "UA", "kyiv": "UA", "kiev": "UA",
    "украина": "UA",
    "ウクライナ": "UA", "キーウ": "UA",
    "乌克兰": "UA",

    # ── 터키 ──────────────────────────────────────────────────
    "터키": "TR", "앙카라": "TR", "이스탄불": "TR",
    "turkey": "TR", "turkish": "TR", "ankara": "TR", "istanbul": "TR",
    "türkei": "TR",
    "トルコ": "TR",
    "土耳其": "TR",

    # ── 사우디아라비아 ────────────────────────────────────────
    "사우디": "SA", "사우디아라비아": "SA", "리야드": "SA",
    "saudi arabia": "SA", "saudi": "SA", "riyadh": "SA",
    "arabie saoudite": "SA",
    "サウジアラビア": "SA",
    "沙特阿拉伯": "SA",

    # ── 대만 ──────────────────────────────────────────────────
    "대만": "TW", "타이완": "TW", "타이베이": "TW",
    "taiwan": "TW", "taiwanese": "TW", "taipei": "TW",
    "台湾": "TW", "台灣": "TW", "台北": "TW", "臺灣": "TW",
    "тайвань": "TW",

    # ── 폴란드 ────────────────────────────────────────────────
    "폴란드": "PL", "바르샤바": "PL",
    "poland": "PL", "polish": "PL", "warsaw": "PL",
    "polen": "PL", "pologne": "PL",
    "polska": "PL",
    "ポーランド": "PL",
    "польша": "PL",

    # ── 멕시코 ────────────────────────────────────────────────
    "멕시코": "MX", "멕시코시티": "MX",
    "mexico": "MX", "mexican": "MX",
    "mexique": "MX", "mexiko": "MX",
    "メキシコ": "MX",
    "墨西哥": "MX",

    # ── 스페인 ────────────────────────────────────────────────
    "스페인": "ES", "마드리드": "ES",
    "spain": "ES", "spanish": "ES", "madrid": "ES",
    "spanien": "ES", "espagne": "ES",
    "スペイン": "ES",
    "西班牙": "ES",

    # ── 아르헨티나 ────────────────────────────────────────────
    "아르헨티나": "AR", "부에노스아이레스": "AR",
    "argentina": "AR", "buenos aires": "AR",
    "argentinien": "AR",
    "アルゼンチン": "AR",

    # ── 인도네시아 ────────────────────────────────────────────
    "인도네시아": "ID", "자카르타": "ID",
    "indonesia": "ID", "jakarta": "ID",
    "インドネシア": "ID",
    "印度尼西亚": "ID",

    # ── 파키스탄 ──────────────────────────────────────────────
    "파키스탄": "PK", "이슬라마바드": "PK",
    "pakistan": "PK", "islamabad": "PK",
    "パキスタン": "PK",

    # ── 나이지리아 ────────────────────────────────────────────
    "나이지리아": "NG", "아부자": "NG", "라고스": "NG",
    "nigeria": "NG", "abuja": "NG", "lagos": "NG",

    # ── 남아공 ────────────────────────────────────────────────
    "남아공": "ZA", "남아프리카": "ZA", "요하네스버그": "ZA",
    "south africa": "ZA", "johannesburg": "ZA",

    # ── 이집트 ────────────────────────────────────────────────
    "이집트": "EG", "카이로": "EG",
    "egypt": "EG", "egyptian": "EG", "cairo": "EG",
    "ägypten": "EG",
    "埃及": "EG",

    # ── 싱가포르 ──────────────────────────────────────────────
    "싱가포르": "SG",
    "singapore": "SG",
    "シンガポール": "SG",
    "新加坡": "SG",

    # ── 베트남 ────────────────────────────────────────────────
    "베트남": "VN", "하노이": "VN",
    "vietnam": "VN", "hanoi": "VN",
    "ベトナム": "VN",
    "越南": "VN",

    # ── 태국 ──────────────────────────────────────────────────
    "태국": "TH", "방콕": "TH",
    "thailand": "TH", "bangkok": "TH",
    "タイ": "TH",
    "泰国": "TH",

    # ── 네덜란드 ──────────────────────────────────────────────
    "네덜란드": "NL", "암스테르담": "NL",
    "netherlands": "NL", "dutch": "NL", "amsterdam": "NL",
    "niederlande": "NL",
    "オランダ": "NL",

    # ── 스웨덴 ────────────────────────────────────────────────
    "스웨덴": "SE", "스톡홀름": "SE",
    "sweden": "SE", "swedish": "SE", "stockholm": "SE",
    "schweden": "SE",
    "スウェーデン": "SE",

    # ── 노르웨이 ──────────────────────────────────────────────
    "노르웨이": "NO", "오슬로": "NO",
    "norway": "NO", "oslo": "NO",
    "norwegen": "NO",

    # ── 핀란드 ────────────────────────────────────────────────
    "핀란드": "FI", "헬싱키": "FI",
    "finland": "FI", "helsinki": "FI",
    "finnland": "FI",

    # ── 스위스 ────────────────────────────────────────────────
    "스위스": "CH", "제네바": "CH", "다보스": "CH",
    "switzerland": "CH", "swiss": "CH", "geneva": "CH", "davos": "CH",
    "schweiz": "CH", "suisse": "CH",
    "スイス": "CH",

    # ── 아랍에미리트 ──────────────────────────────────────────
    "아랍에미리트": "AE", "두바이": "AE", "아부다비": "AE",
    "uae": "AE", "dubai": "AE", "abu dhabi": "AE",
    "الإمارات": "AE",

    # ── 카타르 ────────────────────────────────────────────────
    "카타르": "QA", "도하": "QA",
    "qatar": "QA", "doha": "QA",

    # ── 이라크 ────────────────────────────────────────────────
    "이라크": "IQ", "바그다드": "IQ",
    "iraq": "IQ", "baghdad": "IQ",

    # ── 시리아 ────────────────────────────────────────────────
    "시리아": "SY", "다마스쿠스": "SY",
    "syria": "SY", "damascus": "SY",

    # ── 레바논 ────────────────────────────────────────────────
    "레바논": "LB", "베이루트": "LB",
    "lebanon": "LB", "beirut": "LB",

    # ── 아프가니스탄 ──────────────────────────────────────────
    "아프가니스탄": "AF", "카불": "AF",
    "afghanistan": "AF", "kabul": "AF",

    # ── 필리핀 ────────────────────────────────────────────────
    "필리핀": "PH", "마닐라": "PH",
    "philippines": "PH", "manila": "PH",
    "フィリピン": "PH",

    # ── 말레이시아 ────────────────────────────────────────────
    "말레이시아": "MY", "쿠알라룸푸르": "MY",
    "malaysia": "MY", "kuala lumpur": "MY",
    "マレーシア": "MY",

    # ── 그리스 ────────────────────────────────────────────────
    "그리스": "GR", "아테네": "GR",
    "greece": "GR", "athens": "GR",
    "griechenland": "GR",

    # ── 체코 ──────────────────────────────────────────────────
    "체코": "CZ", "프라하": "CZ",
    "czech": "CZ", "prague": "CZ",
    "tschechien": "CZ",

    # ── 헝가리 ────────────────────────────────────────────────
    "헝가리": "HU", "부다페스트": "HU",
    "hungary": "HU", "budapest": "HU",
    "ungarn": "HU",

    # ── 세르비아 ──────────────────────────────────────────────
    "세르비아": "RS", "베오그라드": "RS",
    "serbia": "RS", "belgrade": "RS",

    # ── 보스니아 ──────────────────────────────────────────────
    "보스니아": "BA", "사라예보": "BA",
    "bosnia": "BA", "sarajevo": "BA",

    # ── 아이슬란드 ────────────────────────────────────────────
    "아이슬란드": "IS", "레이캬비크": "IS",
    "iceland": "IS", "reykjavik": "IS",
    "ísland": "IS",

    # ── 아일랜드 ──────────────────────────────────────────────
    "아일랜드": "IE", "더블린": "IE",
    "ireland": "IE", "dublin": "IE",

    # ── 포르투갈 ──────────────────────────────────────────────
    "포르투갈": "PT", "리스본": "PT",
    "portugal": "PT", "lisbon": "PT",

    # ── 콜롬비아 ──────────────────────────────────────────────
    "콜롬비아": "CO", "보고타": "CO",
    "colombia": "CO", "bogota": "CO",

    # ── 칠레 ──────────────────────────────────────────────────
    "칠레": "CL", "산티아고": "CL",
    "chile": "CL", "santiago": "CL",

    # ── 페루 ──────────────────────────────────────────────────
    "페루": "PE", "리마": "PE",
    "peru": "PE", "lima": "PE",

    # ── 쿠바 ──────────────────────────────────────────────────
    "쿠바": "CU", "하바나": "CU",
    "cuba": "CU", "havana": "CU",

    # ── 에티오피아 ────────────────────────────────────────────
    "에티오피아": "ET", "아디스아바바": "ET",
    "ethiopia": "ET", "addis ababa": "ET",

    # ── 케냐 ──────────────────────────────────────────────────
    "케냐": "KE", "나이로비": "KE",
    "kenya": "KE", "nairobi": "KE",

    # ── 몽골 ──────────────────────────────────────────────────
    "몽골": "MN", "울란바토르": "MN",
    "mongolia": "MN", "ulaanbaatar": "MN",
    "Монгол": "MN",

    # ── 미얀마 ────────────────────────────────────────────────
    "미얀마": "MM", "양곤": "MM",
    "myanmar": "MM", "yangon": "MM", "burma": "MM",

    # ── 뉴질랜드 ──────────────────────────────────────────────
    "뉴질랜드": "NZ", "오클랜드": "NZ",
    "new zealand": "NZ", "auckland": "NZ",

    # ── 아랍어 주요 국가 ──────────────────────────────────────
    "إسرائيل": "IL", "تل أبيب": "IL", "القدس": "IL",
    "السعودية": "SA", "الرياض": "SA", "المملكة العربية السعودية": "SA",
    "الولايات المتحدة": "US", "أمريكا": "US", "واشنطن": "US",
    "الصين": "CN", "بكين": "CN",
    "روسيا": "RU", "موسكو": "RU",
    "إيران": "IR", "طهران": "IR",
    "العراق": "IQ", "بغداد": "IQ",
    "سوريا": "SY", "دمشق": "SY",
    "لبنان": "LB", "بيروت": "LB",
    "فلسطين": "PS", "غزة": "PS",
    "مصر": "EG", "القاهرة": "EG",
    "تركيا": "TR", "أنقرة": "TR",
    "الأردن": "JO", "عمّان": "JO",
    "الإمارات": "AE", "أبوظبي": "AE", "دبي": "AE",
    "قطر": "QA", "الدوحة": "QA",
    "ألمانيا": "DE", "برلين": "DE",
    "فرنسا": "FR", "باريس": "FR",
    "بريطانيا": "GB", "المملكة المتحدة": "GB", "لندن": "GB",
    "الهند": "IN", "نيودلهي": "IN",
    "باكستان": "PK", "إسلام آباد": "PK",
    "اليابان": "JP", "طوكيو": "JP",
    "كوريا الجنوبية": "KR", "سيول": "KR",
    "كوريا الشمالية": "KP", "بيونغ يانغ": "KP",
    "أوكرانيا": "UA", "كييف": "UA",
    "الاتحاد الأوروبي": "EU",
    "الناتو": "NATO",

    # ─ 지역 블록 ─────────────────────────────────────────────
    "유럽연합": "EU", "유럽": "EU",
    "european union": "EU", "eu ": "EU", "brussels": "EU", "브뤼셀": "EU",
    "eurozone": "EU", "유로존": "EU",
    "나토": "NATO", "nato": "NATO",
    "아세안": "ASEAN", "asean": "ASEAN",
    "브릭스": "BRICS", "brics": "BRICS",
    "g7": "G7", "g20": "G20",
    "유엔": "UN", "united nations": "UN", "un ": "UN",
    "중동": "ME_REGION", "middle east": "ME_REGION",
    "동남아": "SE_ASIA", "southeast asia": "SE_ASIA",
    "중앙아시아": "C_ASIA", "central asia": "C_ASIA",
    "아프리카": "W_AFRICA",
    "발칸": "BALKANS", "balkans": "BALKANS",
    "코카서스": "CAUCASUS", "caucasus": "CAUCASUS",
    "스칸디나비아": "SCANDINAVIA", "scandinavia": "SCANDINAVIA",
}


# ─────────────────────────────────────────────────────────────
# 결과 데이터클래스
# ─────────────────────────────────────────────────────────────

@dataclass
class GeoFocusResult:
    """기사 Geo Focus 추출 결과.

    Attributes:
        primary: 최고 점수 국가/지역 ISO2 코드. 없으면 "UNKNOWN".
        all_codes: 임계값 초과 전체 코드 (점수 내림차순).
        scores: 코드별 가중합 점수.
        confidence: primary 상대 신뢰도 (0~1).
        method: "dict" | "ner" | "hybrid" | "none".
    """
    primary: str = "UNKNOWN"
    all_codes: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    method: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "geo_focus_primary": self.primary,
            "geo_focus_all": self.all_codes,
            "geo_confidence": round(self.confidence, 4),
            "geo_method": self.method,
        }


# ─────────────────────────────────────────────────────────────
# 정규화 유틸
# ─────────────────────────────────────────────────────────────

# 전처리: 소문자 alias 기준 키로 구축
_ALIAS_LOWER: dict[str, str] = {k.lower(): v for k, v in _ALIAS.items()}

# 단어 경계 기반 검색을 위한 패턴 컴파일
# (긴 alias부터 먼저 — greedy 방지)
_SORTED_ALIASES = sorted(
    _ALIAS_LOWER.items(),
    key=lambda x: len(x[0]),
    reverse=True,
)
# 패턴 묶음: 최대 200개씩 OR로 묶어 속도 유지
_PATTERN_CHUNK_SIZE = 200
_ALIAS_PATTERNS: list[re.Pattern[str]] = []
for i in range(0, len(_SORTED_ALIASES), _PATTERN_CHUNK_SIZE):
    chunk = _SORTED_ALIASES[i: i + _PATTERN_CHUNK_SIZE]
    escaped = [re.escape(alias) for alias, _ in chunk]
    pattern = re.compile(r"(?<![가-힣a-zA-Z])(" + "|".join(escaped) + r")(?![가-힣a-zA-Z])",
                         re.IGNORECASE)
    _ALIAS_PATTERNS.append(pattern)

# 패턴 → ISO2 역매핑 (패턴 히트 → 코드)
_PATTERN_TO_CODE: dict[str, str] = {alias: code for alias, code in _ALIAS_LOWER.items()}


def _normalize_to_iso2(text: str) -> str | None:
    """단일 텍스트를 ISO2 코드로 정규화. 없으면 None."""
    return _ALIAS_LOWER.get(text.strip().lower())


def _extract_codes_from_text(
    text: str,
    weight: float = 1.0,
) -> dict[str, float]:
    """텍스트에서 국가/지역 코드와 가중합 점수 추출."""
    scores: dict[str, float] = defaultdict(float)
    text_lower = text.lower()
    for pattern in _ALIAS_PATTERNS:
        for match in pattern.finditer(text_lower):
            alias = match.group(1).lower()
            code = _PATTERN_TO_CODE.get(alias)
            if code:
                scores[code] += weight
    return dict(scores)


# ─────────────────────────────────────────────────────────────
# 메인 추출기
# ─────────────────────────────────────────────────────────────

# 점수 임계값: 이 이상만 geo_focus_all에 포함
_MIN_SCORE_RATIO = 0.10   # primary 점수 대비 10% 이상
_MIN_ABS_SCORE = 1.0      # 절대값 최소 1점


class GeoFocusExtractor:
    """기사 Geo Focus 추출기.

    사용법:
        extractor = GeoFocusExtractor()

        # 기본 사용 (title + body만)
        result = extractor.extract(title="미국·중국 무역전쟁 재점화",
                                   body="트럼프 행정부가...",
                                   language="ko")

        # Stage 2 NER 출력과 결합 (더 정확)
        result = extractor.extract(title=..., body=..., language=...,
                                   ner_locations=["Washington", "Beijing"])

        print(result.primary)    # "US"
        print(result.all_codes)  # ["US", "CN"]
    """

    # 가중치
    _W_TITLE = 5.0      # 제목 언급
    _W_BODY_FRONT = 3.0 # 본문 앞 300자 (리드 문단)
    _W_BODY_REST = 1.0  # 본문 나머지
    _W_NER = 4.0        # Stage 2 NER 추출 위치

    def extract(
        self,
        title: str,
        body: str,
        language: str = "en",
        ner_locations: list[str] | None = None,
        source_country: str | None = None,
    ) -> GeoFocusResult:
        """Geo focus 추출.

        Args:
            title: 기사 제목.
            body: 정제된 본문 (HTML 제거 완료).
            language: ISO 639-1 언어 코드.
            ner_locations: Stage 2 entities_location 리스트 (선택).
                           제공 시 정확도 향상, NER 재실행 없음.
            source_country: 출처 국가 (ISO2). geo_focus가 없을 때
                            기본값으로 사용하지 않음 (의도적).

        Returns:
            GeoFocusResult — 항상 반환 (예외 없음).
        """
        try:
            return self._extract_safe(
                title, body, language, ner_locations, source_country,
            )
        except Exception:
            return GeoFocusResult()

    def _extract_safe(
        self,
        title: str,
        body: str,
        language: str,
        ner_locations: list[str] | None,
        source_country: str | None,
    ) -> GeoFocusResult:
        scores: dict[str, float] = defaultdict(float)
        used_ner = False

        # ── Layer 1: 제목 딕셔너리 매칭 ─────────────────────
        for code, w in _extract_codes_from_text(title, self._W_TITLE).items():
            scores[code] += w

        # ── Layer 2: 본문 딕셔너리 매칭 ─────────────────────
        if body:
            front = body[:300]
            rest = body[300:2000]  # 최대 2000자 (성능)

            for code, w in _extract_codes_from_text(front, self._W_BODY_FRONT).items():
                scores[code] += w
            if rest:
                for code, w in _extract_codes_from_text(rest, self._W_BODY_REST).items():
                    scores[code] += w

        # ── Layer 3: Stage 2 NER entities_location ──────────
        if ner_locations:
            for loc in ner_locations:
                code = _normalize_to_iso2(loc)
                if code:
                    scores[code] += self._W_NER
                    used_ner = True
                else:
                    # NER가 추출한 위치가 alias 사전에 없으면
                    # 부분 매칭 시도 (e.g. "Washington D.C." → "US")
                    code = self._fuzzy_match_location(loc)
                    if code:
                        scores[code] += self._W_NER * 0.7
                        used_ner = True

        if not scores:
            return GeoFocusResult(method="none")

        # ── 순위 결정 ────────────────────────────────────────
        total = sum(scores.values())
        primary = max(scores, key=scores.__getitem__)
        primary_score = scores[primary]
        confidence = primary_score / total if total > 0 else 0.0

        # secondary: primary 대비 10% 이상 AND 절대값 1.0 이상
        all_codes = [
            code for code, score in sorted(scores.items(), key=lambda x: -x[1])
            if score >= max(_MIN_ABS_SCORE, primary_score * _MIN_SCORE_RATIO)
        ]

        # method 결정
        if used_ner and scores:
            method = "hybrid"
        elif scores:
            method = "dict"
        else:
            method = "none"

        return GeoFocusResult(
            primary=primary,
            all_codes=all_codes,
            scores=dict(scores),
            confidence=confidence,
            method=method,
        )

    @staticmethod
    def _fuzzy_match_location(loc: str) -> str | None:
        """NER 위치 텍스트의 부분 문자열로 ISO2 탐색.

        'Washington D.C.' → 'washington' 포함 → 'US'
        """
        loc_lower = loc.lower()
        # 긴 alias부터 부분 포함 검사
        for alias, code in _SORTED_ALIASES:
            if len(alias) >= 4 and alias in loc_lower:
                return code
        return None

    def extract_batch(
        self,
        articles: list[dict[str, Any]],
    ) -> list[GeoFocusResult]:
        """배치 처리.

        Args:
            articles: 각 dict에 title, body, language,
                      ner_locations(선택), source_country(선택).

        Returns:
            GeoFocusResult 리스트 (입력 순서 보장).
        """
        return [
            self.extract(
                title=a.get("title", ""),
                body=a.get("body", ""),
                language=a.get("language", "en"),
                ner_locations=a.get("ner_locations"),
                source_country=a.get("source_country"),
            )
            for a in articles
        ]


# ─────────────────────────────────────────────────────────────
# P1 검증
# ─────────────────────────────────────────────────────────────

def validate_geo_focus_coverage(results: list[GeoFocusResult]) -> dict[str, Any]:
    """배치 처리 후 geo_focus 품질 검증.

    Rules:
        GV1 — all_codes 필드는 반드시 list (None 불가)
        GV2 — primary가 ISO2_NAMES에 없는 코드이면 경고
        GV3 — UNKNOWN 비율 > 70% 이면 경고 (alias 사전 부족 신호)
        GV4 — confidence=0 이면서 primary != UNKNOWN 이면 오류

    Returns:
        {valid: bool, warnings: list[str], stats: dict}
    """
    warnings: list[str] = []
    errors: list[str] = []

    unknown_count = sum(1 for r in results if r.primary == "UNKNOWN")
    invalid_code = [
        r.primary for r in results
        if r.primary not in ISO2_NAMES and r.primary != "UNKNOWN"
    ]
    conf_zero_not_unknown = [
        r for r in results
        if r.confidence == 0.0 and r.primary != "UNKNOWN"
    ]

    unknown_ratio = unknown_count / len(results) if results else 0

    if unknown_ratio > 0.70:
        warnings.append(
            f"GV3: UNKNOWN 비율 {unknown_ratio:.1%} > 70% — alias 사전 확장 필요"
        )
    if invalid_code:
        warnings.append(
            f"GV2: 미등록 코드 {set(invalid_code)} — ISO2_NAMES 갱신 필요"
        )
    if conf_zero_not_unknown:
        errors.append(
            f"GV4: confidence=0 이지만 primary≠UNKNOWN ({len(conf_zero_not_unknown)}건)"
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "total": len(results),
            "has_geo": len(results) - unknown_count,
            "unknown": unknown_count,
            "unknown_ratio": round(unknown_ratio, 4),
            "top_countries": _top_countries(results, n=10),
        },
    }


def _top_countries(results: list[GeoFocusResult], n: int = 10) -> list[dict[str, Any]]:
    """geo_focus_primary 빈도 상위 N개."""
    from collections import Counter
    c: Counter[str] = Counter(r.primary for r in results if r.primary != "UNKNOWN")
    return [{"code": code, "name": ISO2_NAMES.get(code, code), "count": cnt}
            for code, cnt in c.most_common(n)]
