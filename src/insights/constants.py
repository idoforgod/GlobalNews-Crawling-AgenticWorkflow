"""Insight Pipeline constants — thresholds, classification rules, keyword dictionaries.

All Type B (rule-based) classification thresholds are centralized here.
Same input + same constants = same output (fully deterministic).

Reference: research/bigdata-insight-workflow-design.md, Appendix B (Reflection #2).
"""

# =============================================================================
# M1: Cross-Lingual Thresholds
# =============================================================================

# JSD spike detection: delta above this from the rolling baseline is "significant"
JSD_SPIKE_THRESHOLD = 0.15

# JSD rolling baseline window (days)
JSD_BASELINE_WINDOW_DAYS = 14

# Attention gap threshold: topic is "systematically ignored" if attention < this
ATTENTION_GAP_EPSILON = 0.005

# Wasserstein distance threshold for "significant" sentiment divergence
SENTIMENT_DIVERGENCE_THRESHOLD = 0.15

# =============================================================================
# M2: Narrative & Framing Thresholds
# =============================================================================

# HHI thresholds for voice dominance classification
HHI_OLIGOPOLY_THRESHOLD = 0.25   # 4 or fewer entities dominate 50%+
HHI_HEALTHY_THRESHOLD = 0.10     # diverse discourse

# Shannon entropy minimum for "healthy" media diversity
SHANNON_MIN_HEALTHY = 1.5

# PELT changepoint detection parameters for frame shift detection
PELT_MODEL = "rbf"
PELT_MIN_SIZE = 3
PELT_PENALTY = 3.0

# Information flow: SBERT similarity threshold for "same story" detection
INFO_FLOW_SIMILARITY_THRESHOLD = 0.80
INFO_FLOW_TIME_WINDOW_HOURS = 24

# =============================================================================
# M3: Entity Analytics Thresholds
# =============================================================================

# Trajectory classification (slope + coefficient of variation based)
TRAJECTORY_RISING_SLOPE = 0.01       # positive slope threshold
TRAJECTORY_FADING_SLOPE = -0.01      # negative slope threshold
TRAJECTORY_PLATEAU_SLOPE = 0.005     # abs(slope) below this = plateau
TRAJECTORY_BURST_CV = 1.0            # coefficient of variation above this = burst

# Hidden connection: minimum Jaccard similarity for structural equivalence
HIDDEN_CONNECTION_JACCARD_MIN = 0.3

# Emergence index: minimum acceleration (2nd derivative) for "emerging"
EMERGENCE_ACCELERATION_MIN = 0.05

# =============================================================================
# M4: Temporal Pattern Thresholds
# =============================================================================

# Attention decay classification: R² threshold for model fit acceptance
DECAY_R2_THRESHOLD = 0.7

# Decay type classification
DECAY_FLASH_LAMBDA_MIN = 1.0      # exponential decay rate for "flash"
DECAY_SUSTAINED_ALPHA_MAX = 0.5   # power law exponent for "sustained"

# Hawkes process: minimum events for parameter estimation
HAWKES_MIN_EVENTS = 30

# Information velocity: maximum lag (hours) to consider as "propagation"
VELOCITY_MAX_LAG_HOURS = 72

# FFT periodogram: minimum power-to-noise ratio for significant period
FFT_MIN_POWER_RATIO = 3.0

# =============================================================================
# M5: Geopolitical Thresholds
# =============================================================================

# BRI z-score normalization baseline (days)
BRI_BASELINE_DAYS = 90

# Soft power component weights (sum = 1.0)
SOFT_POWER_WEIGHTS = {
    "visibility": 0.25,      # normalized coverage volume
    "sentiment": 0.30,       # positive sentiment ratio
    "frame_diversity": 0.20, # Shannon entropy of STEEPS frames
    "centrality": 0.25,      # PageRank in entity network
}

# Conflict-cooperation spectrum: emotion ratio
# ratio = (anger + fear) / (trust + anticipation)
# ratio > 1.0 = conflict-dominant, < 1.0 = cooperation-dominant

# =============================================================================
# M6: Economic Intelligence Thresholds
# =============================================================================

# EPU keywords by language (Baker-Bloom-Davis inspired)
EPU_KEYWORDS = {
    "en": {"economy", "economic", "policy", "regulation", "uncertainty",
           "uncertain", "deficit", "budget", "federal reserve", "congress",
           "legislation", "tariff", "trade war"},
    "ko": {"경제", "정책", "규제", "불확실", "불안", "재정", "적자",
            "금리", "한은", "한국은행", "국회", "입법", "관세", "무역"},
    "ja": {"経済", "政策", "規制", "不確実", "不安", "財政", "赤字",
            "金利", "日銀", "国会", "法案", "関税", "貿易"},
    "de": {"wirtschaft", "politik", "regulierung", "unsicherheit",
           "haushalt", "defizit", "zinsen", "bundestag", "gesetz", "zoll"},
    "es": {"economía", "económico", "política", "regulación", "incertidumbre",
           "déficit", "presupuesto", "legislación", "arancel", "comercio"},
    "fr": {"économie", "économique", "politique", "réglementation",
           "incertitude", "déficit", "budget", "législation", "tarif"},
    "it": {"economia", "economico", "politica", "regolamentazione",
           "incertezza", "deficit", "bilancio", "legislazione", "tariffa"},
    "pt": {"economia", "econômico", "política", "regulamentação",
           "incerteza", "déficit", "orçamento", "legislação", "tarifa"},
    "sv": {"ekonomi", "politik", "reglering", "osäkerhet", "underskott",
           "budget", "lagstiftning", "tull", "handel"},
    "no": {"økonomi", "politikk", "regulering", "usikkerhet", "underskudd",
           "budsjett", "lovgivning", "toll", "handel"},
    "cs": {"ekonomika", "politika", "regulace", "nejistota", "deficit",
           "rozpočet", "legislativa", "clo", "obchod"},
    "ru": {"экономика", "экономический", "политика", "регулирование",
           "неопределённость", "дефицит", "бюджет", "законодательство",
           "тариф", "торговля"},
}

# Sector classification keywords (deterministic keyword matching, no ML)
SECTOR_KEYWORDS = {
    "energy": {
        "en": {"oil", "gas", "solar", "renewable", "energy", "petroleum",
               "opec", "nuclear power", "wind power", "coal", "electricity"},
        "ko": {"원유", "석유", "가스", "태양광", "에너지", "신재생",
                "원자력", "전력", "석탄", "풍력", "OPEC"},
    },
    "technology": {
        "en": {"ai", "artificial intelligence", "semiconductor", "chip",
               "software", "cloud", "quantum", "robot", "5g", "blockchain",
               "data center", "cybersecurity", "startup"},
        "ko": {"인공지능", "반도체", "칩", "소프트웨어", "클라우드",
                "양자", "로봇", "5G", "블록체인", "데이터센터", "스타트업"},
    },
    "healthcare": {
        "en": {"pharmaceutical", "vaccine", "drug", "hospital", "medical",
               "health", "biotech", "clinical trial", "fda", "cancer",
               "pandemic", "therapy"},
        "ko": {"제약", "백신", "의약품", "병원", "의료", "건강",
                "바이오", "임상시험", "식약처", "암", "팬데믹", "치료"},
    },
    "financial": {
        "en": {"bank", "interest rate", "stock", "bond", "market",
               "investment", "inflation", "fed", "central bank", "forex",
               "credit", "insurance", "fintech"},
        "ko": {"은행", "금리", "주식", "채권", "시장", "투자",
                "인플레이션", "한은", "중앙은행", "환율", "보험", "핀테크"},
    },
    "manufacturing": {
        "en": {"factory", "supply chain", "manufacturing", "production",
               "automobile", "steel", "shipbuilding", "export", "import",
               "logistics", "warehouse"},
        "ko": {"공장", "공급망", "제조", "생산", "자동차", "철강",
                "조선", "수출", "수입", "물류", "창고"},
    },
}

# Hype cycle phase classification thresholds
HYPE_VOLUME_RISING = 0.10     # volume trend > this = rising
HYPE_VOLUME_FALLING = -0.05   # volume trend < this = falling
HYPE_VOLUME_STABLE = 0.02     # abs(volume trend) < this = stable
HYPE_SENTIMENT_POSITIVE = 0.20
HYPE_SENTIMENT_NEGATIVE = -0.10
HYPE_SENTIMENT_NEUTRAL = 0.10  # abs(sentiment) < this = neutral

# Narrative economics keywords
NARRATIVE_KEYWORDS = {
    "recession": {"en": {"recession", "downturn", "contraction", "slowdown"},
                  "ko": {"불황", "경기침체", "위축", "둔화"}},
    "inflation": {"en": {"inflation", "price surge", "cost of living", "cpi"},
                  "ko": {"인플레이션", "물가", "생활비", "소비자물가"}},
    "bubble":    {"en": {"bubble", "overvalued", "speculation", "crash"},
                  "ko": {"버블", "거품", "과대평가", "투기"}},
    "growth":    {"en": {"growth", "expansion", "boom", "recovery", "gdp"},
                  "ko": {"성장", "확장", "호황", "회복", "GDP"}},
    "crisis":    {"en": {"crisis", "collapse", "meltdown", "default", "bailout"},
                  "ko": {"위기", "붕괴", "부도", "구제금융"}},
}

# =============================================================================
# M7: Synthesis Constants
# =============================================================================

# Number of top findings per module to include in brief
SYNTHESIS_TOP_N = 5

# Minimum absolute change to qualify as "notable" finding
SYNTHESIS_MIN_CHANGE_THRESHOLD = 0.05

# =============================================================================
# M7 Extension: Evidence-Based Future Intelligence (FI-1 ~ FI-5)
# =============================================================================

# Evidence article selection weights (P1 deterministic scoring formula).
# D-7: referenced in m7_synthesis.py _select_evidence_articles()
EVIDENCE_SCORE_WEIGHTS = {
    "importance_score": 0.4,    # Stage 3 importance (source authority + entity density)
    "sentiment_extremity": 0.3, # abs(sentiment_score) — extreme = more evidence value
    "source_authority": 0.2,    # sources.yaml tier (inverted: Easy=0.5, Hard=0.8, Extreme=1.0)
    "body_completeness": 0.1,   # min(word_count / 1000, 1.0) — longer = more complete
}

# Same-event detection thresholds (P1 deterministic matching).
# Two articles are "same event" if ALL conditions met:
SAME_EVENT_THRESHOLDS = {
    "same_topic_id": True,           # must share BERTopic topic_id
    "same_date": True,               # must share published_at date
    "entity_jaccard_min": 0.3,       # NER entity overlap ≥ 30%
    "embedding_cosine_fallback": 0.80,  # OR: SBERT cosine similarity ≥ 0.80
}

# Alert thresholds — configurable via insights.yaml override.
# D-7: referenced in m7_synthesis.py _compute_risk_alerts()
#       and validate_intelligence.py FI4
ALERT_THRESHOLDS = {
    "crisis_sentiment": -0.40,      # entity-pair avg sentiment below this = escalation
    "epu_critical": 0.40,           # EPU index above this = economic crisis precursor
    "sector_all_negative": True,    # all sectors negative = risk-off regime
    "burst_ratio_chaos": 0.80,      # burst/(burst+plateau) above this = chaos phase
    "conflict_ratio_polarization": 0.50,  # conflict/total above this = global polarization
    "blind_spot_drop_ratio": 0.70,  # article count drops 70%+ = attention blind spot
}

# Maximum evidence articles per entity/pair
EVIDENCE_MAX_ARTICLES = 5

# Minimum articles for an entity to qualify for profiling
ENTITY_PROFILE_MIN_ARTICLES = 10
