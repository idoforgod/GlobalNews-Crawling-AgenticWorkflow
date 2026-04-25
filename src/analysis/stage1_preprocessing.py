"""Stage 1: Preprocessing -- Language detection, tokenization, normalization.

Implements the first stage of the 8-stage NLP analysis pipeline.
Processes raw JSONL articles from the crawling layer into a clean
Parquet dataset ready for downstream feature extraction.

Techniques implemented:
    T01: Morphological Analysis (Korean) -- kiwipiepy.Kiwi.tokenize()
    T02: Lemmatization (English) -- spaCy en_core_web_sm
    T03: Sentence Splitting -- Kiwi split_into_sents (ko) / spaCy doc.sents (en)
    T04: Language Detection -- langdetect.detect()
    T05: Text Normalization -- Unicode NFKC + whitespace cleanup
    T06: Stopword Removal -- Custom Korean stopword list + spaCy English defaults

Input:  data/raw/YYYY-MM-DD/all_articles.jsonl (RawArticle JSON objects)
Output: data/processed/articles.parquet (ARTICLES_SCHEMA: 12 columns)

Reference: Step 7 Analysis Pipeline Design, Section 3.1.
"""

from __future__ import annotations

import gc
import html
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.config.constants import (
    ARTICLES_PARQUET_PATH,
    DATA_RAW_DIR,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
    SPACY_MODEL_NAME,
    VALID_LANGUAGES,
)
from src.storage.parquet_writer import ARTICLES_PA_SCHEMA

logger = logging.getLogger(__name__)

# =============================================================================
# Parquet Schema -- Single source: parquet_writer.ARTICLES_PA_SCHEMA
# =============================================================================

# Alias for backward compatibility within this module
ARTICLES_SCHEMA = ARTICLES_PA_SCHEMA

# =============================================================================
# Korean Stopwords
# =============================================================================

# Particles (josa), copulas, and common news-filler words.
# Particles are detected by Kiwi POS tags (JKS, JKO, JKB, JX, etc.)
# but we also keep a surface-form list for fallback whitespace tokenization.
KOREAN_STOPWORDS: frozenset[str] = frozenset({
    # Particles (josa)
    "\uc774", "\uac00", "\uc740", "\ub294", "\uc744", "\ub97c",
    "\uc5d0", "\uc5d0\uc11c", "\uc73c\ub85c", "\ub85c",
    "\uc640", "\uacfc", "\ub3c4", "\ub9cc", "\ubd80\ud130",
    "\uae4c\uc9c0", "\ubcf4\ub2e4", "\ub9c8\ub2e4", "\uc758",
    "\ub4e4", "\ub4f1", "\uc904",
    # Copulas and auxiliaries
    "\uc774\ub2e4", "\uc785\ub2c8\ub2e4", "\uc774\uc5c8\ub2e4",
    "\ub418\ub2e4", "\ud558\ub2e4", "\uc788\ub2e4", "\uc5c6\ub2e4",
    "\ub9d0\ud588\ub2e4", "\ubc1d\ud614\ub2e4", "\uc804\ud588\ub2e4",
    "\ub4e4", "\uac83", "\uc218", "\ub9ac",
    # Common news boilerplate
    "\uae30\uc790", "\ud2b9\ud30c\uc6d0", "\uc575\ucee4",
    "\ub274\uc2a4", "\ubcf4\ub3c4", "\ucde8\uc7ac",
    "\uc0ac\uc9c4", "\uc81c\uacf5", "\uc5f0\ud569\ub274\uc2a4",
    "\ub274\uc2dc\uc2a4",
    # Pronouns and demonstratives
    "\uc774\uac83", "\uc800\uac83", "\uadf8\uac83",
    "\uc5b4\ub5a4", "\ubaa8\ub4e0", "\uac01",
    # Time/quantity fillers
    "\uc624\ub298", "\uc5b4\uc81c", "\uc624\uc804", "\uc624\ud6c4",
    "\ud604\uc7ac", "\ucd5c\uadfc", "\uc62c\ud574",
    # Common verbs in news that carry little topical meaning
    "\ub9d0\ud558\ub2e4", "\uc54c\ub9ac\ub2e4", "\ud655\uc778\ud558\ub2e4",
    "\ubc1c\ud45c\ud558\ub2e4",
})

# Kiwi POS tags to keep: nouns (NNG common, NNP proper, NNB bound),
# verbs (VV), adjectives (VA). We exclude particles (J*), endings (E*),
# punctuation (S*), and affixes (XS*, XP*).
KIWI_KEEP_POS: frozenset[str] = frozenset({
    "NNG",   # Common noun
    "NNP",   # Proper noun
    "NNB",   # Bound (dependent) noun
    "VV",    # Verb
    "VA",    # Adjective
    "MAG",   # General adverb (kept for modifiers like "very", "already")
    "SL",    # Foreign letter token (preserve Latin in Korean text)
    "SH",    # Chinese character (Hanja)
})

# Kiwi POS tags that are particles -- used for stopword filtering
KIWI_PARTICLE_POS: frozenset[str] = frozenset({
    "JKS", "JKC", "JKG", "JKO", "JKB", "JKV", "JKQ",
    "JX", "JC",
    "EP", "EF", "EC", "ETN", "ETM",  # Endings
    "XSN", "XSV", "XSA",  # Suffixes
    "SF", "SP", "SS", "SE", "SO", "SW",  # Punctuation
})

# =============================================================================
# English Stopwords (supplemental news-specific)
# =============================================================================

ENGLISH_NEWS_STOPWORDS: frozenset[str] = frozenset({
    "said", "says", "say", "saying",
    "according", "reported", "reports", "reporting",
    "told", "tells", "telling",
    "added", "noted", "stated", "explained",
    "also", "would", "could", "may", "might",
    "new", "one", "two", "first", "last",
    "year", "years", "time", "day", "days",
    "people", "percent", "mr", "mrs", "ms",
    "reuters", "associated", "press", "ap",
    "copyright", "rights", "reserved",
    "photo", "image", "video", "file",
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
    "january", "february", "march", "april",
    "june", "july", "august", "september",
    "october", "november", "december",
})

# =============================================================================
# Multilingual Stopwords
# =============================================================================
# Covers European languages (es/pt/de/fr/it/nl/sv/no/da/pl/cs/ru) and
# Southeast Asian languages (id/tl). These are applied in the "else" branch of
# the fallback tokenizer where spaCy/Kiwi are not available. Source: a union of
# common function-word lists from NLTK/snowball/spaCy language stopword sets,
# pruned to high-frequency particles/articles/prepositions/conjunctions/pronouns.

_STOP_ES: frozenset[str] = frozenset({
    "de", "la", "que", "el", "en", "y", "a", "los", "del", "se",
    "las", "por", "un", "para", "con", "no", "una", "su", "al", "lo",
    "como", "mas", "pero", "sus", "le", "ya", "o", "este", "si", "porque",
    "esta", "entre", "cuando", "muy", "sin", "sobre", "tambien", "me", "hasta",
    "hay", "donde", "quien", "desde", "todo", "nos", "durante", "todos",
    "uno", "les", "ni", "contra", "otros", "ese", "eso", "ante", "ellos",
    "esto", "mi", "antes", "algunos", "que", "unos", "yo", "otro", "otras",
    "otra", "el", "tanto", "esa", "estos", "mucho", "quienes", "nada",
    "muchos", "cual", "poco", "ella", "estar", "estas", "algunas", "algo",
    "nosotros",
})

_STOP_PT: frozenset[str] = frozenset({
    "de", "a", "o", "que", "e", "do", "da", "em", "um", "para",
    "com", "nao", "uma", "os", "no", "se", "na", "por", "mais", "as",
    "dos", "como", "mas", "ao", "ele", "das", "a", "seu", "sua", "ou",
    "quando", "muito", "nos", "ja", "eu", "tambem", "so", "pelo", "pela",
    "ate", "isso", "ela", "entre", "depois", "sem", "mesmo", "aos", "seus",
    "quem", "nas", "me", "esse", "eles", "voce", "essa", "num", "nem",
    "suas", "meu", "as", "minha", "numa", "pelos", "elas", "havia", "seja",
    "qual", "sera", "tenho", "lhe", "deles", "essas", "esses", "pelas",
    "este", "dele", "tu", "te", "voces", "vos", "lhes", "meus", "minhas",
    "teu", "tua", "teus", "tuas", "nosso", "nossa", "nossos", "nossas",
    "dela", "delas",
    # Common fragments seen in topic labels
    "ao", "em", "si", "vit", "nam", "ang", "ng", "sa",
})

_STOP_DE: frozenset[str] = frozenset({
    "der", "die", "und", "in", "den", "von", "zu", "das", "mit", "sich",
    "des", "auf", "fur", "ist", "im", "dem", "nicht", "ein", "eine", "als",
    "auch", "es", "an", "werden", "aus", "er", "hat", "dass", "sie", "nach",
    "wird", "bei", "einer", "um", "am", "sind", "noch", "wie", "einem",
    "uber", "einen", "so", "zum", "war", "haben", "nur", "oder", "aber",
    "vor", "zur", "bis", "mehr", "durch", "man", "sein", "wurde", "sei",
    "in", "prozent", "hatte", "kann", "gegen", "vom", "konnen", "schon",
    "wenn", "habe", "seine", "mark", "ihre", "dann", "unter", "wir",
    "jahr", "zwei", "jahren", "diese", "dieser", "wieder", "keine", "uhr",
    "seiner", "worden", "will", "zwischen", "immer", "milliarden", "ob",
    "dieses", "leben", "dem", "du",
    # Fragments
    "fur", "zum", "zur",
})

_STOP_FR: frozenset[str] = frozenset({
    "le", "de", "un", "etre", "et", "a", "il", "avoir", "ne", "je",
    "son", "que", "se", "qui", "ce", "dans", "en", "du", "elle", "au",
    "de", "ce", "le", "pour", "pas", "que", "vous", "par", "sur", "faire",
    "plus", "dire", "me", "on", "mon", "lui", "nous", "comme", "mais", "pouvoir",
    "avec", "tout", "y", "aller", "voir", "en", "bien", "ou", "sans", "tu",
    "notre", "si", "leur", "y", "entre", "cette", "encore", "quand", "deux",
    "contre", "depuis", "apres", "trois", "pendant", "tous", "meme", "aussi",
    "sous", "tres", "avant", "peu", "trop", "toujours", "jamais", "ici",
    "la", "ces", "ses", "ma", "sa", "mes", "tes", "vos", "nos", "leurs",
    "j", "d", "l", "s", "t", "n", "c", "m",
    "du", "des", "la", "les", "au", "aux",
    # Common fragments
    "fr", "du",
})

_STOP_IT: frozenset[str] = frozenset({
    "il", "di", "che", "e", "la", "per", "in", "un", "a", "non",
    "con", "le", "una", "si", "da", "su", "i", "ma", "al", "come",
    "lo", "se", "anche", "sono", "tra", "ci", "mi", "ti", "o", "ho",
    "questo", "sua", "suo", "piu", "gli", "ha", "del", "della", "delle", "dei",
    "nel", "nella", "nei", "alle", "alla", "agli", "ad", "col", "nello", "negli",
    "sul", "sulla", "sulle", "sui", "dal", "dalla", "dalle", "dai", "ogni",
    "ecc", "cioe", "quindi", "quando", "dove", "cosi", "molto", "poco", "mai",
    "ancora", "sempre", "dopo", "prima", "contro", "senza", "fra", "verso",
    "secondo", "durante", "davanti", "dietro", "sopra", "sotto", "dentro",
    "essere", "avere", "fare", "dire", "potere", "dovere", "volere",
    # Fragments
    "di", "il", "ma",
})

_STOP_NL: frozenset[str] = frozenset({
    "de", "en", "van", "ik", "te", "dat", "die", "in", "een", "hij",
    "het", "niet", "zijn", "is", "was", "op", "aan", "met", "als", "voor",
    "had", "er", "maar", "om", "hem", "dan", "zou", "of", "wat", "mijn",
    "men", "dit", "zo", "door", "over", "ze", "zich", "bij", "ook", "tot",
    "je", "mij", "uit", "der", "daar", "haar", "naar", "heb", "hoe", "heeft",
    "hebben", "deze", "u", "want", "nog", "zal", "me", "zij", "nu", "ge",
})

_STOP_SV: frozenset[str] = frozenset({
    "och", "det", "att", "i", "en", "jag", "hon", "som", "han", "pa",
    "den", "med", "var", "sig", "for", "sa", "till", "ar", "men", "ett",
    "om", "hade", "de", "av", "icke", "mig", "du", "henne", "dem", "over",
    "honom", "ni", "efter", "man", "vi", "er", "nar", "kan", "vara", "blev",
    "innan", "under", "mellan", "genom", "utan",
})

_STOP_NO: frozenset[str] = frozenset({
    "og", "i", "jeg", "det", "at", "en", "et", "den", "til", "er",
    "som", "pa", "de", "med", "han", "av", "ikke", "ikkje", "der", "sa",
    "var", "meg", "seg", "men", "ett", "har", "om", "vi", "min", "mitt",
    "ha", "hadde", "hun", "noe", "fra", "noen", "for", "far", "blir",
    "ble", "blitt", "na", "kan", "hans", "henne", "hennes", "deres",
})

_STOP_DA: frozenset[str] = frozenset({
    "og", "i", "jeg", "det", "at", "en", "den", "til", "er", "som",
    "pa", "de", "med", "han", "af", "for", "ikke", "der", "var", "mig",
    "sig", "men", "et", "har", "om", "vi", "min", "havde", "ham", "hun",
    "nu", "over", "da", "fra", "du", "ud", "sin", "dem", "os", "op",
    "man", "hans", "hvor", "eller", "hvad", "skal", "selv", "her", "alle",
    "vil", "blev", "kunne", "ind", "nar", "ville", "jo", "sine", "noget",
    "da",
})

_STOP_PL: frozenset[str] = frozenset({
    "i", "w", "z", "na", "do", "nie", "to", "ze", "jest", "sie",
    "od", "za", "jak", "co", "po", "ale", "tak", "tez", "ma", "juz",
    "ja", "on", "ona", "ono", "my", "wy", "oni", "mnie", "mi", "cie",
    "go", "jej", "ich", "nas", "was", "dla", "bez", "pod", "przed",
    "nad", "przy", "przez", "wokol", "ktory", "ktora", "ktore", "byc",
})

_STOP_CS: frozenset[str] = frozenset({
    "a", "i", "v", "se", "na", "je", "to", "ze", "s", "o",
    "z", "jsem", "jsi", "je", "jsme", "jste", "jsou", "byl", "byla", "bylo",
    "byli", "by", "bude", "budu", "budes", "budeme", "budete", "budou",
    "jako", "pro", "nebo", "ale", "uz", "kdy", "kde", "co", "jak", "proc",
    "protoze", "pokud", "pokud", "take", "tez", "nebot", "tedy", "tak",
})

_STOP_RU: frozenset[str] = frozenset({
    "i", "v", "ne", "na", "ya", "s", "chto", "kak", "iz", "za",
    "po", "do", "k", "a", "no", "tak", "u", "bi", "on", "vse",
    "to", "my", "vy", "mne", "mi", "ti", "tam", "zdes", "eto", "tut",
    "dlya", "bez", "pri", "pod", "nad", "iz", "kto", "kogo", "chego",
})

_STOP_ID: frozenset[str] = frozenset({
    "yang", "dan", "di", "dari", "ke", "dengan", "untuk", "ini", "itu",
    "pada", "tidak", "dalam", "akan", "adalah", "saya", "kami", "mereka",
    "dia", "bagi", "bisa", "juga", "lebih", "atau", "jika", "saat",
    "bahkan", "sampai", "oleh", "tetapi", "karena", "agar", "telah",
    "sudah", "baru", "lagi", "belum", "masih", "harus", "bukan", "tak",
    "saja", "sebagai", "sejak", "secara",
})

_STOP_TL: frozenset[str] = frozenset({
    "ang", "ng", "sa", "at", "ay", "mga", "na", "ako", "siya", "kami",
    "kayo", "sila", "ko", "mo", "niya", "natin", "namin", "ninyo", "nila",
    "ito", "iyon", "iyan", "dito", "doon", "diyan", "saan", "kailan",
    "bakit", "paano", "kung", "pero", "ngunit", "subalit", "kundi",
    "dahil", "para", "laban", "tungkol", "kapag",
})

# Combined multilingual stopword set
MULTILINGUAL_STOPWORDS: frozenset[str] = frozenset().union(
    _STOP_ES, _STOP_PT, _STOP_DE, _STOP_FR, _STOP_IT, _STOP_NL,
    _STOP_SV, _STOP_NO, _STOP_DA, _STOP_PL, _STOP_CS, _STOP_RU,
    _STOP_ID, _STOP_TL,
)

# Per-language stopword lookup (for fallback tokenizer)
LANGUAGE_STOPWORDS: dict[str, frozenset[str]] = {
    "es": _STOP_ES, "pt": _STOP_PT, "de": _STOP_DE, "fr": _STOP_FR,
    "it": _STOP_IT, "nl": _STOP_NL, "sv": _STOP_SV, "no": _STOP_NO,
    "da": _STOP_DA, "pl": _STOP_PL, "cs": _STOP_CS, "ru": _STOP_RU,
    "id": _STOP_ID, "tl": _STOP_TL,
}

# =============================================================================
# Language-to-source mapping for detection verification
# =============================================================================

# Expected language by source_id prefix or explicit mapping.
# Used to verify langdetect output against known site languages.
SOURCE_LANGUAGE_MAP: dict[str, str] = {
    # Group A — Korean Major Dailies (5)
    "chosun": "ko", "joongang": "ko", "donga": "ko", "hani": "ko",
    "yna": "ko",
    # Group B — Korean Economy (4)
    "mk": "ko", "hankyung": "ko", "fnnews": "ko", "mt": "ko",
    # Group C — Korean Niche (3)
    "nocutnews": "ko", "kmib": "ko", "ohmynews": "ko",
    # Group D — Korean IT/Science (10)
    "38north": "en", "bloter": "ko", "etnews": "ko",
    "sciencetimes": "ko", "zdnet_kr": "ko", "irobotnews": "ko",
    "techneedle": "ko", "insight_kr": "ko",
    "stratechery": "en", "techmeme": "en",
    # Group E — English-Language Western (12)
    "nytimes": "en", "wsj": "en", "bloomberg": "en", "ft": "en",
    "cnn": "en", "huffpost": "en", "latimes": "en", "buzzfeed": "en",
    "marketwatch": "en", "nationalpost": "en", "voakorea": "en",
    "afmedios": "es",
    # Group F — Asia-Pacific (17)
    "globaltimes": "zh", "people": "zh", "scmp": "en",
    "taiwannews": "en", "yomiuri": "ja", "thehindu": "en",
    "asahi": "ja", "yahoo_jp": "ja", "mainichi": "en",
    "focustaiwan": "en", "taipeitimes": "en",
    "hindustantimes": "en", "indianexpress": "en", "economictimes": "en",
    "timesofindia": "en", "natureasia": "en",
    "inquirer": "en",
    # Group F — Southeast Asia
    "jakartapost": "en", "tempo_id": "en", "manilatimes": "en",
    "philstar": "en", "antaranews": "en",
    "vietnamnews": "en", "vnexpress": "en",
    # Group G — Europe/Middle East (26)
    "thesun": "en", "bild": "de", "lemonde": "fr",
    "themoscowtimes": "en", "arabnews": "en",
    "aljazeera": "en", "israelhayom": "en",
    "bbc": "en", "theguardian": "en", "telegraph": "en", "thetimes": "en",
    "wired": "en", "politico_eu": "en", "euractiv": "en", "euronews": "en",
    "spiegel": "de", "faz": "de", "welt": "de", "sueddeutsche": "de",
    "elpais": "es", "elmundo": "es", "lavanguardia": "es", "abc_es": "es",
    "lefigaro": "fr", "liberation": "fr", "ouestfrance": "fr", "france24": "fr",
    "corriere": "it", "repubblica": "it", "ansa": "it",
    "wyborcza": "pl", "pap": "pl",
    "idnes": "cs",
    "aftonbladet": "sv",
    "tv2_no": "no",
    "yle": "en",
    "icelandmonitor": "en",
    "haaretz": "en", "jpost": "en",
    "almonitor": "en", "middleeasteye": "en", "jordantimes": "en",
    "balkaninsight": "en", "centraleuropeantimes": "en", "intellinews": "en",
    "investing": "en", "qz": "en",
    # Group H — Africa (4)
    "allafrica": "en", "africanews": "en",
    "theafricareport": "en", "panapress": "en",
    # Group I — Latin America (8)
    "clarin": "es", "lanacion_ar": "es",
    "folha": "pt", "oglobo": "pt",
    "eltiempo": "es", "elcomercio_pe": "es",
    "biobiochile": "es", "elmercurio": "es",
    # Group J — Russia/Central Asia (4)
    "ria": "ru", "rg": "ru", "rbc": "ru",
    "gogo_mn": "mn",
}

# =============================================================================
# Text Normalization Patterns (compiled once at module level)
# =============================================================================

_RE_MULTI_WHITESPACE = re.compile(r"[ \t]+")
_RE_MULTI_NEWLINES = re.compile(r"\n{3,}")
_RE_URL = re.compile(
    r"https?://[^\s<>\"')\]]+", re.IGNORECASE
)
_RE_EMAIL = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_DECORATIVE_PUNCT = re.compile(r"[~\u25a0\u25cf\u25cb\u2605\u2606\u2190-\u2199\u2500-\u257f]+")
# Sentence-ending punctuation for regex-based splitting (other languages)
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")
# Chinese/Japanese sentence boundary (period variants)
_RE_CJK_SENTENCE = re.compile(r"(?<=[\u3002\uff01\uff1f\uff0e])")


# =============================================================================
# Intermediate Data Container
# =============================================================================

@dataclass
class ArticleIntermediateData:
    """In-memory intermediate data per article for downstream stages.

    Not persisted to Parquet -- passed to Stage 2 alongside the table.
    """

    article_id: str
    title_tokens: list[str] = field(default_factory=list)
    body_tokens: list[str] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    pos_tags: list[tuple[str, str]] = field(default_factory=list)


# =============================================================================
# Memory Tracking Helpers
# =============================================================================

def _get_rss_mb() -> float:
    """Return current RSS (Resident Set Size) in megabytes.

    Falls back to 0.0 if the resource module is unavailable (non-Unix).
    """
    try:
        import resource
        # maxrss is in kilobytes on Linux, bytes on macOS
        rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        import platform
        if platform.system() == "Darwin":
            return rss_raw / (1024 * 1024)  # bytes -> MB
        return rss_raw / 1024  # KB -> MB
    except ImportError:
        return 0.0


# =============================================================================
# Text Normalization
# =============================================================================

def normalize_text(text: str, language: str = "en") -> str:
    """Apply text normalization pipeline.

    Steps:
        1. Unicode normalization (NFKC for Korean/CJK, NFC for English)
        2. HTML entity decoding
        3. HTML tag removal (residual)
        4. URL removal
        5. Email removal
        6. Decorative punctuation removal
        7. Whitespace collapse
        8. Strip leading/trailing whitespace

    Args:
        text: Raw text to normalize.
        language: ISO 639-1 code; determines Unicode normalization form.

    Returns:
        Normalized text string.
    """
    if not text:
        return ""

    # Step 1: Unicode normalization
    # NFKC for Korean and CJK (decomposes compatibility chars, recomposes)
    # NFC for English (canonical composition only)
    if language in ("ko", "zh", "ja"):
        text = unicodedata.normalize("NFKC", text)
    else:
        text = unicodedata.normalize("NFC", text)

    # Step 2: HTML entity decoding (e.g., &amp; -> &, &#39; -> ')
    text = html.unescape(text)

    # Step 3: Remove residual HTML tags
    text = _RE_HTML_TAG.sub("", text)

    # Step 4: Remove URLs
    text = _RE_URL.sub("", text)

    # Step 5: Remove email addresses
    text = _RE_EMAIL.sub("", text)

    # Step 6: Remove decorative punctuation (box drawing, stars, arrows)
    text = _RE_DECORATIVE_PUNCT.sub("", text)

    # Step 7: Collapse whitespace
    text = _RE_MULTI_NEWLINES.sub("\n\n", text)
    text = _RE_MULTI_WHITESPACE.sub(" ", text)

    # Step 8: Strip
    text = text.strip()

    return text


# =============================================================================
# Charset Handling
# =============================================================================

def _try_decode(raw_bytes: bytes) -> str:
    """Attempt to decode bytes with fallback encodings.

    Order: UTF-8 -> EUC-KR -> Latin-1 (always succeeds).

    Args:
        raw_bytes: Raw byte content.

    Returns:
        Decoded string.
    """
    for encoding in ("utf-8", "euc-kr", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # latin-1 should never fail, but just in case:
    return raw_bytes.decode("utf-8", errors="replace")


# =============================================================================
# Language Detection
# =============================================================================

def detect_language(
    title: str,
    body: str,
    source_id: str,
) -> str:
    """Detect article language using langdetect with source verification.

    Strategy:
        1. Concatenate title + body[:200] for detection sample
        2. Run langdetect.detect()
        3. Verify against SOURCE_LANGUAGE_MAP
        4. Override source expectation only if langdetect confidence > 0.9

    Args:
        title: Article title.
        body: Article body text.
        source_id: Site identifier for expected-language lookup.

    Returns:
        ISO 639-1 language code.
    """
    expected = SOURCE_LANGUAGE_MAP.get(source_id, "en")

    # Build detection sample
    sample = title
    if body:
        sample = title + " " + body[:200]

    if not sample or len(sample.strip()) < 10:
        logger.warning(
            "language_detection_insufficient_text article_source=%s, "
            "defaulting to expected=%s",
            source_id, expected,
        )
        return expected

    try:
        from langdetect import detect, detect_langs
        from langdetect import DetectorFactory
        # Deterministic detection
        DetectorFactory.seed = 42

        detected = detect(sample)

        # Check confidence via detect_langs
        lang_probs = detect_langs(sample)
        confidence = 0.0
        for lp in lang_probs:
            if lp.lang == detected:
                confidence = lp.prob
                break

        # Verify against source expectation
        if detected != expected:
            if confidence > 0.9:
                logger.info(
                    "language_override source=%s expected=%s detected=%s "
                    "confidence=%.2f",
                    source_id, expected, detected, confidence,
                )
                return detected
            else:
                logger.debug(
                    "language_detection_low_confidence source=%s "
                    "expected=%s detected=%s confidence=%.2f, "
                    "keeping expected",
                    source_id, expected, detected, confidence,
                )
                return expected

        return detected

    except Exception as e:
        logger.warning(
            "language_detection_failed source=%s error=%s, "
            "defaulting to expected=%s",
            source_id, str(e), expected,
        )
        return expected


# =============================================================================
# Korean Processing (Kiwi)
# =============================================================================

def _load_kiwi():
    """Lazy-load Kiwi tokenizer singleton.

    Returns:
        kiwipiepy.Kiwi instance.
    """
    from kiwipiepy import Kiwi

    rss_before = _get_rss_mb()
    kiwi = Kiwi()
    rss_after = _get_rss_mb()
    logger.info(
        "kiwi_loaded rss_delta=%.1f MB (%.1f -> %.1f)",
        rss_after - rss_before, rss_before, rss_after,
    )
    return kiwi


def process_korean_text(
    text: str,
    kiwi,
    *,
    is_title: bool = False,
) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Process Korean text with Kiwi morphological analysis.

    Steps:
        1. Kiwi tokenize -> morpheme list with POS tags
        2. Filter by KIWI_KEEP_POS (nouns, verbs, adjectives)
        3. Remove surface forms in KOREAN_STOPWORDS
        4. Sentence split via kiwi.split_into_sents()

    Args:
        text: Normalized Korean text.
        kiwi: Loaded Kiwi instance.
        is_title: If True, skip sentence splitting (title is one sentence).

    Returns:
        Tuple of (tokens, pos_tags, sentences).
            tokens: List of surface-form tokens after filtering.
            pos_tags: List of (surface, pos_tag) tuples for all kept tokens.
            sentences: List of sentence strings.
    """
    if not text or not text.strip():
        return [], [], []

    tokens: list[str] = []
    pos_tags: list[tuple[str, str]] = []

    try:
        result = kiwi.tokenize(text)
        for morph in result:
            form = morph.form
            tag = morph.tag

            # Keep only desired POS categories
            if tag not in KIWI_KEEP_POS:
                continue

            # Skip stopwords (surface form match)
            if form in KOREAN_STOPWORDS:
                continue

            # Skip single-character non-nouns (too noisy)
            if len(form) == 1 and tag not in ("NNG", "NNP", "NNB", "SL", "SH"):
                continue

            tokens.append(form)
            pos_tags.append((form, tag))

    except Exception as e:
        logger.warning(
            "kiwi_tokenization_failed error=%s, falling back to whitespace",
            str(e),
        )
        # Fallback: simple whitespace tokenization
        words = text.split()
        tokens = [w for w in words if w not in KOREAN_STOPWORDS and len(w) > 1]
        pos_tags = [(w, "UNK") for w in tokens]

    # Sentence splitting
    sentences: list[str] = []
    if not is_title:
        try:
            sent_results = kiwi.split_into_sents(text)
            sentences = [s.text.strip() for s in sent_results if s.text.strip()]
        except Exception as e:
            logger.warning(
                "kiwi_sentence_split_failed error=%s, "
                "falling back to regex",
                str(e),
            )
            sentences = [s.strip() for s in _RE_SENTENCE_SPLIT.split(text) if s.strip()]
    else:
        sentences = [text.strip()] if text.strip() else []

    return tokens, pos_tags, sentences


# =============================================================================
# English Processing (spaCy)
# =============================================================================

def _load_spacy():
    """Lazy-load spaCy English model.

    Returns:
        spacy.Language instance, or None if spaCy is unavailable.
    """
    try:
        import spacy
    except (ImportError, Exception) as e:
        logger.warning(
            "spacy_import_failed error=%s, English will use fallback tokenizer",
            str(e),
        )
        return None

    rss_before = _get_rss_mb()
    try:
        nlp = spacy.load(SPACY_MODEL_NAME, disable=["ner"])
    except OSError:
        logger.warning(
            "spacy_model_not_found model=%s, attempting download",
            SPACY_MODEL_NAME,
        )
        try:
            from spacy.cli import download
            download(SPACY_MODEL_NAME)
            nlp = spacy.load(SPACY_MODEL_NAME, disable=["ner"])
        except Exception as e:
            logger.warning(
                "spacy_download_failed error=%s, English will use fallback",
                str(e),
            )
            return None

    rss_after = _get_rss_mb()
    logger.info(
        "spacy_loaded model=%s rss_delta=%.1f MB (%.1f -> %.1f)",
        SPACY_MODEL_NAME, rss_after - rss_before, rss_before, rss_after,
    )

    # Increase max_length for long articles
    nlp.max_length = 2_000_000

    return nlp


def process_english_text(
    text: str,
    nlp,
    *,
    is_title: bool = False,
) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Process English text with spaCy lemmatization and POS filtering.

    Steps:
        1. spaCy pipeline: tokenize + POS + lemmatize
        2. Filter tokens by POS: NOUN, VERB, ADJ, PROPN
        3. Remove stopwords (spaCy defaults + ENGLISH_NEWS_STOPWORDS)
        4. Apply lemmatization
        5. Sentence split via doc.sents

    Args:
        text: Normalized English text.
        nlp: Loaded spaCy Language instance.
        is_title: If True, skip sentence splitting (title is one sentence).

    Returns:
        Tuple of (tokens, pos_tags, sentences).
            tokens: List of lemmatized tokens after filtering.
            pos_tags: List of (token_text, pos_tag) tuples for kept tokens.
            sentences: List of sentence strings.
    """
    if not text or not text.strip():
        return [], [], []

    # Process through spaCy pipeline
    doc = nlp(text)

    keep_pos = {"NOUN", "VERB", "ADJ", "PROPN"}
    tokens: list[str] = []
    pos_tags: list[tuple[str, str]] = []

    for token in doc:
        # Skip punctuation and whitespace
        if token.is_punct or token.is_space:
            continue

        # POS filtering
        if token.pos_ not in keep_pos:
            continue

        # Lemmatize, lowercase (except proper nouns)
        lemma = token.lemma_
        if token.pos_ != "PROPN":
            lemma = lemma.lower()

        # Skip stopwords
        if token.is_stop or lemma.lower() in ENGLISH_NEWS_STOPWORDS:
            continue

        # Skip very short tokens (single characters like 's', 't')
        if len(lemma) <= 1:
            continue

        tokens.append(lemma)
        pos_tags.append((token.text, token.pos_))

    # Sentence splitting
    sentences: list[str] = []
    if not is_title:
        sentences = [sent.text.strip() for sent in doc.sents if sent.text.strip()]
    else:
        sentences = [text.strip()] if text.strip() else []

    return tokens, pos_tags, sentences


# =============================================================================
# Other Languages Processing (zh, ja, de, fr, es, ar, he)
# =============================================================================

def process_other_language_text(
    text: str,
    language: str,
    *,
    is_title: bool = False,
) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Basic processing for non-Korean, non-English languages.

    Also serves as fallback for English when spaCy is unavailable,
    and for Korean when Kiwi is unavailable. For English fallback,
    applies ENGLISH_NEWS_STOPWORDS filtering and lowercasing.

    Uses whitespace tokenization + Unicode normalization + regex sentence
    splitting. No morphological analysis performed.

    Args:
        text: Normalized text.
        language: ISO 639-1 code.
        is_title: If True, skip sentence splitting.

    Returns:
        Tuple of (tokens, pos_tags, sentences).
    """
    if not text or not text.strip():
        return [], [], []

    # Tokenize by whitespace (or character for CJK)
    if language in ("zh", "ja"):
        # For Chinese/Japanese, each character can be a token.
        # Use basic regex to split on whitespace and punctuation boundaries.
        raw_tokens = [
            t for t in re.findall(r"[\w]+", text, re.UNICODE)
            if len(t.strip()) > 0
        ]
    else:
        # Whitespace tokenization for European/Arabic/Hebrew/English fallback
        raw_tokens = [t for t in text.split() if len(t) > 1]

    # Apply language-specific stopword filtering for fallback processing
    if language == "en":
        # English fallback: lowercase + stopword removal
        tokens = [
            t.lower() for t in raw_tokens
            if t.lower() not in ENGLISH_NEWS_STOPWORDS
            and len(t) > 1
        ]
    elif language == "ko":
        # Korean fallback: surface-form stopword removal
        tokens = [
            t for t in raw_tokens
            if t not in KOREAN_STOPWORDS and len(t) > 1
        ]
    elif language in LANGUAGE_STOPWORDS:
        # Multilingual fallback (es/pt/de/fr/it/nl/sv/no/da/pl/cs/ru/id/tl):
        # lowercase + language-specific stopword removal + length filter.
        # Fragments shorter than 3 chars are dropped to suppress particles
        # (da/ms/di/fr/du/zu/il/ao/em) that leaked into previous topic labels.
        lang_stops = LANGUAGE_STOPWORDS[language]
        tokens = [
            t.lower() for t in raw_tokens
            if t.lower() not in lang_stops
            and t.lower() not in MULTILINGUAL_STOPWORDS
            and len(t) >= 3
        ]
    else:
        # Unknown language: apply universal multilingual filter + length filter
        tokens = [
            t.lower() for t in raw_tokens
            if t.lower() not in MULTILINGUAL_STOPWORDS
            and len(t) >= 3
        ]

    pos_tags = [(t, "UNK") for t in tokens]

    # Sentence splitting
    sentences: list[str] = []
    if not is_title:
        if language in ("zh", "ja"):
            # CJK sentence boundaries
            parts = _RE_CJK_SENTENCE.split(text)
            sentences = [s.strip() for s in parts if s.strip()]
        else:
            parts = _RE_SENTENCE_SPLIT.split(text)
            sentences = [s.strip() for s in parts if s.strip()]
    else:
        sentences = [text.strip()] if text.strip() else []

    return tokens, pos_tags, sentences


# =============================================================================
# Word Count
# =============================================================================

def compute_word_count(
    body_tokens: list[str],
    title_tokens: list[str],
    body: str,
    language: str,
) -> int:
    """Compute article word count based on language-specific rules.

    Korean: Count of Kiwi morphemes (NNG+NNP+VV+VA) from body.
    English: Whitespace-split count after stopword removal from body.
    Other: Whitespace-split count from body.

    If body is empty (paywall), use title tokens.

    Args:
        body_tokens: Processed body tokens.
        title_tokens: Processed title tokens.
        body: Raw body text (for fallback counting).
        language: ISO 639-1 code.

    Returns:
        Integer word count (minimum 0).
    """
    if body_tokens:
        return len(body_tokens)
    elif title_tokens:
        return len(title_tokens)
    elif body and body.strip():
        return len(body.split())
    else:
        return 0


# =============================================================================
# Timestamp Parsing
# =============================================================================

def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp value from JSONL into a timezone-aware datetime.

    Handles:
        - datetime objects (returned as-is if tz-aware, made UTC if naive)
        - ISO 8601 strings
        - None / empty

    Args:
        value: Raw timestamp value from JSONL.

    Returns:
        Timezone-aware datetime in UTC, or None.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            logger.warning("timestamp_parse_failed value=%s", value)
            return None

    return None


def _extract_category(value: Any) -> str:
    """Extract a plain string category from a raw JSONL value.

    Some sites (e.g., EU sources) emit structured category objects like
    ``{"@type": "DefinedTerm", "name": "EU and the World", ...}``
    instead of a plain string.  This function normalises those to a string.
    """
    if not value:
        return "uncategorized"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name") or value.get("termCode") or "uncategorized"
    if isinstance(value, list):
        # Take the first element's name if it's a list of dicts
        for item in value:
            if isinstance(item, dict):
                name = item.get("name") or item.get("termCode")
                if name:
                    return name
            elif isinstance(item, str):
                return item
        return "uncategorized"
    return str(value)


# =============================================================================
# Stage1Preprocessor
# =============================================================================

class Stage1Preprocessor:
    """Stage 1 preprocessing pipeline.

    Manages model lifecycle (lazy loading, singleton pattern) and provides
    the core process() method that transforms raw JSONL articles into a
    Parquet table with intermediate data.

    Usage::

        preprocessor = Stage1Preprocessor()
        table, intermediates, stats = preprocessor.process(articles_data)
        preprocessor.write_parquet(table, output_path)
        preprocessor.cleanup()  # Release spaCy, keep Kiwi singleton
    """

    def __init__(self) -> None:
        self._kiwi = None
        self._nlp = None  # spaCy
        self._models_loaded = False

    # -----------------------------------------------------------------
    # Model lifecycle
    # -----------------------------------------------------------------

    def _ensure_models(self, need_korean: bool = True, need_english: bool = True) -> None:
        """Lazy-load NLP models as needed.

        Models that fail to load are logged and set to None. The pipeline
        falls back to basic tokenization for languages whose model is
        unavailable.

        Args:
            need_korean: Whether to load Kiwi.
            need_english: Whether to load spaCy.
        """
        if need_korean and self._kiwi is None:
            logger.info("loading_kiwi_model")
            try:
                self._kiwi = _load_kiwi()
            except Exception as e:
                logger.warning("kiwi_load_failed error=%s", str(e))
                self._kiwi = None

        if need_english and self._nlp is None:
            logger.info("loading_spacy_model")
            self._nlp = _load_spacy()
            # _load_spacy returns None on failure (already logged)

        self._models_loaded = True

    def cleanup(self, keep_kiwi: bool = True) -> None:
        """Release models to free memory.

        Args:
            keep_kiwi: If True, keep Kiwi loaded (singleton for later stages).
                       If False, release everything.
        """
        if self._nlp is not None:
            del self._nlp
            self._nlp = None
            logger.info("spacy_model_released")

        if not keep_kiwi and self._kiwi is not None:
            del self._kiwi
            self._kiwi = None
            logger.info("kiwi_model_released")

        gc.collect()
        logger.info("gc_collected rss=%.1f MB", _get_rss_mb())

    @property
    def kiwi(self):
        """Access Kiwi singleton (for downstream stages)."""
        return self._kiwi

    # -----------------------------------------------------------------
    # JSONL Loading
    # -----------------------------------------------------------------

    @staticmethod
    def load_jsonl(input_path: Path) -> list[dict[str, Any]]:
        """Load articles from a JSONL file.

        Args:
            input_path: Path to the JSONL file.

        Returns:
            List of raw article dictionaries.
        """
        articles: list[dict[str, Any]] = []
        line_num = 0

        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                line_num += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    article = json.loads(line)
                    articles.append(article)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "jsonl_parse_error line=%d error=%s",
                        line_num, str(e),
                    )

        logger.info(
            "jsonl_loaded path=%s articles=%d lines_read=%d",
            input_path, len(articles), line_num,
        )
        return articles

    # -----------------------------------------------------------------
    # Core Processing
    # -----------------------------------------------------------------

    def process_article(
        self,
        raw: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, ArticleIntermediateData | None]:
        """Process a single raw article into Parquet row + intermediate data.

        Args:
            raw: Raw article dictionary from JSONL.

        Returns:
            Tuple of (parquet_row_dict, intermediate_data).
            Returns (None, None) if the article should be skipped.
        """
        # ------ Validate required fields ------
        title = raw.get("title", "")
        if not title or not title.strip():
            logger.error(
                "empty_title_skip url=%s source=%s",
                raw.get("url", "unknown"),
                raw.get("source_id", "unknown"),
            )
            return None, None

        url = raw.get("url", "")
        if not url:
            logger.error("empty_url_skip title=%s", title[:50])
            return None, None

        body = raw.get("body", "")
        source_id = raw.get("source_id", "")
        source_name = raw.get("source_name", "")
        is_paywall = raw.get("is_paywall_truncated", False)

        # ------ Language Detection (T04) ------
        language = detect_language(title, body, source_id)

        # Validate language is in our supported set
        if language not in VALID_LANGUAGES:
            logger.debug(
                "unsupported_language detected=%s source=%s, "
                "defaulting to en",
                language, source_id,
            )
            language = SOURCE_LANGUAGE_MAP.get(source_id, "en")

        # ------ Text Normalization (T05) ------
        title_normalized = normalize_text(title, language)
        body_normalized = normalize_text(body, language) if body else ""

        # If paywall-truncated, body is explicitly empty
        if is_paywall:
            body_normalized = ""

        # ------ Generate article_id ------
        article_id = str(uuid.uuid4())

        # ------ Language-specific processing ------
        # Ensure models are loaded for the languages present
        if language == "ko":
            self._ensure_models(need_korean=True, need_english=False)
        elif language == "en":
            self._ensure_models(need_korean=False, need_english=True)

        # Process title (dual-pass: title is always processed)
        title_tokens, title_pos, title_sents = self._process_text_by_language(
            title_normalized, language, is_title=True,
        )

        # Process body (dual-pass: body processed separately)
        body_tokens: list[str] = []
        body_pos: list[tuple[str, str]] = []
        body_sents: list[str] = []
        if body_normalized:
            body_tokens, body_pos, body_sents = self._process_text_by_language(
                body_normalized, language, is_title=False,
            )

        # ------ Word Count ------
        word_count = compute_word_count(
            body_tokens, title_tokens, body_normalized, language,
        )

        # ------ Timestamps ------
        published_at = _parse_timestamp(raw.get("published_at"))
        crawled_at = _parse_timestamp(raw.get("crawled_at"))

        # ------ Build Parquet row ------
        # D4 fix: preserve W1 evidence_id so the CE4 evidence chain passthrough
        # survives Stage 1 → Stage 8 without re-derivation.
        parquet_row = {
            "article_id": article_id,
            "evidence_id": raw.get("evidence_id", ""),
            "url": url,
            "title": title_normalized,
            "body": body_normalized,
            "source": source_name or source_id,
            "category": _extract_category(raw.get("category")),
            "language": language,
            "published_at": published_at,
            "crawled_at": crawled_at,
            "author": raw.get("author"),
            "word_count": word_count,
            "content_hash": raw.get("content_hash", ""),
        }

        # ------ Build Intermediate Data ------
        intermediate = ArticleIntermediateData(
            article_id=article_id,
            title_tokens=title_tokens,
            body_tokens=body_tokens,
            sentences=body_sents if body_sents else title_sents,
            pos_tags=body_pos if body_pos else title_pos,
        )

        return parquet_row, intermediate

    def _process_text_by_language(
        self,
        text: str,
        language: str,
        *,
        is_title: bool = False,
    ) -> tuple[list[str], list[tuple[str, str]], list[str]]:
        """Route text processing to the appropriate language handler.

        Falls back to basic tokenization if the language-specific model
        (spaCy for English, Kiwi for Korean) is unavailable.

        Args:
            text: Normalized text.
            language: ISO 639-1 code.
            is_title: Whether this is a title (single-sentence).

        Returns:
            Tuple of (tokens, pos_tags, sentences).
        """
        if language == "ko" and self._kiwi is not None:
            return process_korean_text(text, self._kiwi, is_title=is_title)
        elif language == "en" and self._nlp is not None:
            return process_english_text(text, self._nlp, is_title=is_title)
        else:
            # Fallback for any language when its model is unavailable
            return process_other_language_text(text, language, is_title=is_title)

    def process(
        self,
        raw_articles: list[dict[str, Any]],
        batch_size: int = 500,
    ) -> tuple[pa.Table, list[ArticleIntermediateData], dict[str, Any]]:
        """Process all articles through the Stage 1 pipeline.

        Args:
            raw_articles: List of raw article dictionaries from JSONL.
            batch_size: Number of articles to process before logging progress.

        Returns:
            Tuple of:
                - pa.Table: Parquet-ready table matching ARTICLES_SCHEMA.
                - list[ArticleIntermediateData]: Per-article intermediate data.
                - dict: Processing statistics.
        """
        start_time = time.time()
        rss_before = _get_rss_mb()

        total = len(raw_articles)
        logger.info("stage1_start total_articles=%d rss=%.1f MB", total, rss_before)

        # Detect which languages are present to pre-load models
        languages_present: set[str] = set()
        for raw in raw_articles:
            lang = raw.get("language", "en")
            source_id = raw.get("source_id", "")
            expected = SOURCE_LANGUAGE_MAP.get(source_id, lang)
            languages_present.add(expected)

        need_korean = "ko" in languages_present
        need_english = "en" in languages_present or any(
            lang not in ("ko",) for lang in languages_present
        )

        logger.info(
            "languages_detected present=%s need_korean=%s need_english=%s",
            languages_present, need_korean, need_english,
        )

        # Pre-load models
        self._ensure_models(need_korean=need_korean, need_english=need_english)
        rss_after_models = _get_rss_mb()
        logger.info(
            "models_loaded rss=%.1f MB (delta=%.1f MB)",
            rss_after_models, rss_after_models - rss_before,
        )

        # Process articles
        rows: list[dict[str, Any]] = []
        intermediates: list[ArticleIntermediateData] = []
        skipped = 0
        lang_counts: dict[str, int] = {}
        errors: list[str] = []

        for idx, raw in enumerate(raw_articles):
            try:
                row, intermediate = self.process_article(raw)

                if row is None:
                    skipped += 1
                    continue

                rows.append(row)
                intermediates.append(intermediate)

                # Track language distribution
                lang = row["language"]
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

            except Exception as e:
                skipped += 1
                url = raw.get("url", "unknown")
                errors.append(f"{url}: {str(e)}")
                logger.error(
                    "article_processing_failed url=%s error=%s",
                    url, str(e),
                    exc_info=True,
                )

            # Progress logging
            if (idx + 1) % batch_size == 0 or (idx + 1) == total:
                elapsed = time.time() - start_time
                rate = (idx + 1) / elapsed if elapsed > 0 else 0
                logger.info(
                    "stage1_progress %d/%d (%.1f%%) %.1f art/s rss=%.1f MB",
                    idx + 1, total, (idx + 1) / total * 100,
                    rate, _get_rss_mb(),
                )

        # Build PyArrow table
        table = self._build_table(rows)

        elapsed_total = time.time() - start_time
        rss_peak = _get_rss_mb()

        stats = {
            "total_input": total,
            "total_processed": len(rows),
            "total_skipped": skipped,
            "language_distribution": lang_counts,
            "elapsed_seconds": round(elapsed_total, 2),
            "articles_per_second": round(len(rows) / elapsed_total, 2) if elapsed_total > 0 else 0,
            "rss_before_mb": round(rss_before, 1),
            "rss_peak_mb": round(rss_peak, 1),
            "errors": errors[:10],  # First 10 errors
        }

        logger.info(
            "stage1_complete processed=%d skipped=%d elapsed=%.1fs "
            "rate=%.1f art/s rss_peak=%.1f MB",
            len(rows), skipped, elapsed_total,
            stats["articles_per_second"], rss_peak,
        )
        logger.info("language_distribution %s", lang_counts)

        return table, intermediates, stats

    def _build_table(self, rows: list[dict[str, Any]]) -> pa.Table:
        """Convert list of row dicts into a PyArrow Table with ARTICLES_SCHEMA.

        Args:
            rows: List of article dictionaries.

        Returns:
            PyArrow Table conforming to ARTICLES_SCHEMA.
        """
        if not rows:
            return pa.table(
                {field.name: pa.array([], type=field.type) for field in ARTICLES_SCHEMA},
                schema=ARTICLES_SCHEMA,
            )

        # Build columnar arrays
        columns: dict[str, list] = {field.name: [] for field in ARTICLES_SCHEMA}

        for row in rows:
            columns["article_id"].append(row["article_id"])
            columns["evidence_id"].append(row.get("evidence_id", ""))
            columns["url"].append(row["url"])
            columns["title"].append(row["title"])
            columns["body"].append(row["body"])
            columns["source"].append(row["source"])
            columns["category"].append(row["category"])
            columns["language"].append(row["language"])
            columns["published_at"].append(row["published_at"])
            columns["crawled_at"].append(row["crawled_at"])
            columns["author"].append(row["author"])
            columns["word_count"].append(row["word_count"])
            columns["content_hash"].append(row["content_hash"])

        # P1 Safety Net: sanitize all utf8 columns before pa.array().
        # Prevents ArrowTypeError from dict/list values that slip through
        # adapter or preprocessing layers.  _extract_category() handles
        # category-specific semantics upstream; this guard catches the rest.
        for f in ARTICLES_SCHEMA:
            if f.type == pa.utf8():
                sanitized: list[str | None] = []
                for v in columns[f.name]:
                    if v is None or isinstance(v, str):
                        sanitized.append(v)
                    elif isinstance(v, dict):
                        extracted = (
                            v.get("name") or v.get("text")
                            or v.get("href") or v.get("url") or ""
                        )
                        logger.warning(
                            "p1_dict_sanitized field=%s extracted=%s",
                            f.name, str(extracted)[:80],
                        )
                        sanitized.append(str(extracted))
                    elif isinstance(v, list):
                        first = ""
                        for item in v:
                            if isinstance(item, str):
                                first = item
                                break
                            if isinstance(item, dict):
                                first = item.get("name", "")
                                break
                        logger.warning(
                            "p1_list_sanitized field=%s len=%d", f.name, len(v),
                        )
                        sanitized.append(first)
                    else:
                        sanitized.append(str(v))
                columns[f.name] = sanitized

        # Build typed arrays
        arrays = []
        for f in ARTICLES_SCHEMA:
            if f.name in ("published_at", "crawled_at"):
                arrays.append(pa.array(columns[f.name], type=f.type))
            elif f.name == "word_count":
                arrays.append(pa.array(columns[f.name], type=pa.int32()))
            else:
                arrays.append(pa.array(columns[f.name], type=pa.utf8()))

        return pa.table(arrays, schema=ARTICLES_SCHEMA)

    # -----------------------------------------------------------------
    # Parquet I/O
    # -----------------------------------------------------------------

    @staticmethod
    def write_parquet(
        table: pa.Table,
        output_path: Path | str,
        *,
        compression: str | None = None,
        compression_level: int | None = None,
    ) -> Path:
        """Write the processed table to Parquet format.

        Args:
            table: PyArrow Table to write.
            output_path: Destination file path.
            compression: Compression codec (default: from constants).
            compression_level: Compression level (default: from constants).

        Returns:
            Path to the written Parquet file.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        comp = compression or PARQUET_COMPRESSION
        comp_level = compression_level or PARQUET_COMPRESSION_LEVEL

        pq.write_table(
            table,
            str(output_path),
            compression=comp,
            compression_level=comp_level,
            use_dictionary=True,
            write_statistics=True,
        )

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(
            "parquet_written path=%s rows=%d columns=%d "
            "size=%.2f MB compression=%s",
            output_path, table.num_rows, table.num_columns,
            file_size_mb, comp,
        )

        return output_path


# =============================================================================
# Convenience Function
# =============================================================================

def run_stage1(
    input_dir: str | Path | None = None,
    output_path: str | Path | None = None,
    date: str | None = None,
    *,
    keep_kiwi: bool = True,
) -> tuple[pa.Table, list[ArticleIntermediateData], dict[str, Any]]:
    """Run the complete Stage 1 preprocessing pipeline.

    This is the primary entry point for pipeline orchestration.

    Args:
        input_dir: Directory containing JSONL files.
                   Default: data/raw/YYYY-MM-DD/ (today's date).
        output_path: Output Parquet file path.
                     Default: data/processed/articles.parquet.
        date: Date string (YYYY-MM-DD) for input directory.
              Default: today.
        keep_kiwi: Whether to keep Kiwi loaded after processing.

    Returns:
        Tuple of (table, intermediates, stats).
    """
    # Resolve input directory
    if input_dir is None:
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        input_dir = DATA_RAW_DIR / date

    input_dir = Path(input_dir)

    # Resolve output path
    if output_path is None:
        output_path = ARTICLES_PARQUET_PATH

    output_path = Path(output_path)

    # Find JSONL input file
    jsonl_path = input_dir / "all_articles.jsonl"
    if not jsonl_path.exists():
        # Try individual source files
        jsonl_files = sorted(input_dir.glob("*.jsonl"))
        if not jsonl_files:
            raise FileNotFoundError(
                f"No JSONL files found in {input_dir}. "
                f"Expected: {jsonl_path}"
            )
        logger.info(
            "consolidated_jsonl_not_found, loading %d individual files",
            len(jsonl_files),
        )
    else:
        jsonl_files = [jsonl_path]

    # Load all articles
    all_articles: list[dict[str, Any]] = []
    for jf in jsonl_files:
        all_articles.extend(Stage1Preprocessor.load_jsonl(jf))

    logger.info("total_articles_loaded count=%d", len(all_articles))

    if not all_articles:
        logger.warning("no_articles_to_process, writing empty Parquet")
        preprocessor = Stage1Preprocessor()
        empty_table = preprocessor._build_table([])
        preprocessor.write_parquet(empty_table, output_path)
        return empty_table, [], {"total_input": 0, "total_processed": 0}

    # Process
    preprocessor = Stage1Preprocessor()
    table, intermediates, stats = preprocessor.process(all_articles)

    # Write Parquet
    preprocessor.write_parquet(table, output_path)

    # Cleanup (release spaCy, optionally keep Kiwi)
    preprocessor.cleanup(keep_kiwi=keep_kiwi)

    return table, intermediates, stats


# =============================================================================
# Schema Validation
# =============================================================================

def validate_output(parquet_path: str | Path) -> dict[str, Any]:
    """Validate the output Parquet file against ARTICLES_SCHEMA.

    Checks:
        1. File exists and is readable
        2. Column count matches (12)
        3. Column names match exactly
        4. Column types match
        5. No null article_id values

    Args:
        parquet_path: Path to the Parquet file.

    Returns:
        Dict with keys: valid (bool), errors (list[str]), stats (dict).
    """
    errors: list[str] = []
    parquet_path = Path(parquet_path)

    # Check file exists
    if not parquet_path.exists():
        return {"valid": False, "errors": ["File not found"], "stats": {}}

    try:
        table = pq.read_table(str(parquet_path))
    except Exception as e:
        return {"valid": False, "errors": [f"Read error: {e}"], "stats": {}}

    # Column count
    if table.num_columns != len(ARTICLES_SCHEMA):
        errors.append(
            f"Column count mismatch: expected {len(ARTICLES_SCHEMA)}, "
            f"got {table.num_columns}"
        )

    # Column names
    expected_names = [f.name for f in ARTICLES_SCHEMA]
    actual_names = table.column_names
    if actual_names != expected_names:
        errors.append(
            f"Column names mismatch: expected {expected_names}, "
            f"got {actual_names}"
        )

    # Column types
    for f in ARTICLES_SCHEMA:
        if f.name in actual_names:
            actual_type = table.schema.field(f.name).type
            if actual_type != f.type:
                errors.append(
                    f"Column '{f.name}' type mismatch: "
                    f"expected {f.type}, got {actual_type}"
                )

    # Null article_id check
    if "article_id" in actual_names:
        null_count = table.column("article_id").null_count
        if null_count > 0:
            errors.append(f"article_id has {null_count} null values")

    stats = {
        "rows": table.num_rows,
        "columns": table.num_columns,
        "file_size_bytes": parquet_path.stat().st_size,
    }

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "stats": stats,
    }
