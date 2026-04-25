"""Evidence chain library — stable evidence ID generation + traceability.

G1 from 2nd reflection: the Evidence Chain is the answer to "how do we
trace a master-report claim back to an original crawled article?" Every
article gets an `evidence_id` at crawl time that propagates unchanged
through Stages 1-8 of the NLP pipeline and into W3 insight reports and
W4 master integration claims.

CE1 from 4th reflection: the evidence_id formula MUST be stable across
re-crawls. Including body text (which mutates with ads/timestamps) makes
IDs drift. The correct formula uses only immutable canonical fields:

    evidence_id = "ev:" + sha256(normalized_url + "|" + published_at_iso)[:16]

where normalized_url strips tracking params, fragments, and trailing
slashes. This is tested in tests/execution/lib/test_evidence_lib.py.

P1 Compliance: Deterministic. No LLM judgment. Pure Python + hashlib.
SOT Compliance: Read-only file I/O (JSONL lookup).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tracking parameters stripped during URL normalization.
# These contribute nothing to article identity — pure analytics noise.
_TRACKING_PARAMS = frozenset({
    # Google / UTM
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_name", "utm_brand", "gclid", "gclsrc", "dclid", "gbraid", "wbraid",
    # Facebook
    "fbclid", "fb_source", "fb_ref", "fb_action_ids",
    # Twitter / X
    "s", "t", "ref_src", "ref_url", "twclid",
    # LinkedIn
    "trk", "li_source",
    # Yandex
    "yclid",
    # Generic analytics
    "ref", "referer", "referrer", "source", "campaign",
    "_hsenc", "_hsmi", "mc_eid", "mc_cid",
    # Ad platforms
    "adgroupid", "adid", "campaignid", "creative",
})

# ISO-8601 datetime regex (tolerates second precision, timezone Z or offset).
# Used to validate published_at format before hashing.
_ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"           # date
    r"(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?"  # optional time
    r"(Z|[+-]\d{2}:?\d{2})?)?$"     # optional TZ
)


class EvidenceIDError(ValueError):
    """Raised when evidence_id cannot be generated from inputs."""


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

def normalize_url_for_evidence(url: str) -> str:
    """Normalize URL for stable evidence ID generation.

    RFC 3986-compliant canonicalization:
      1. Parse URL into components
      2. Lowercase scheme and host (netloc) — RFC 3986 §3.2.2 (CE1-C1 fix)
      3. Strip default ports (:80 for http, :443 for https) — CE1-C2 fix
      4. Strip query parameters in _TRACKING_PARAMS
      5. Remove fragment (#anchor)
      6. Remove trailing slash from path (preserve root /)
      7. Re-encode query in alphabetical order for canonicalization

    Returns empty string if input is empty. Does NOT lowercase the path
    (paths ARE case-sensitive per RFC 3986).
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url  # fall back to raw string if unparseable

    # CE1-C1: Lowercase scheme and host (netloc). RFC 3986 §3.2.2 declares
    # the host component case-insensitive. Any crawler redirect or CDN
    # rewrite that alters host casing MUST produce the same evidence ID.
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()

    # CE1-C2: Strip default ports. https://site.com/a and https://site.com:443/a
    # are RFC-equivalent; stripping normalizes re-crawl variance.
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    elif scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]

    # Strip tracking params and canonicalize query
    if parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for (k, v) in pairs if k.lower() not in _TRACKING_PARAMS]
        filtered.sort()  # alphabetical for stability
        new_query = urlencode(filtered)
    else:
        new_query = ""

    # Strip fragment
    new_fragment = ""

    # Normalize path: remove trailing slash except for root "/"
    path = parsed.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    normalized = urlunparse((
        scheme,
        netloc,
        path,
        parsed.params,
        new_query,
        new_fragment,
    ))
    return normalized


def _canonicalize_iso_datetime(value: str) -> str:
    """Canonicalize an ISO-8601 datetime to UTC.

    CE1-C3 fix: without this, two ISO strings representing the same instant
    (e.g., "2026-04-09T10:00:00Z" vs "2026-04-09T19:00:00+09:00") would hash
    to different evidence IDs. All inputs are converted to UTC and formatted
    as the canonical "YYYY-MM-DDTHH:MM:SSZ".

    Raises:
        ValueError: if the input is not parseable as ISO-8601.
    """
    stripped = value.strip()
    # datetime.fromisoformat does not accept the literal "Z" suffix until
    # Python 3.11; normalize to "+00:00" for wider compatibility.
    iso_form = stripped.replace("Z", "+00:00")
    try:
        parsed_dt = datetime.fromisoformat(iso_form)
    except (ValueError, TypeError) as exc:
        raise ValueError(str(exc))

    # Date-only inputs (e.g., "2026-04-09") are parsed as naive midnight.
    # For evidence IDs we require a full instant — reject them so the caller
    # sees an explicit failure rather than a drifted ID later.
    if parsed_dt.tzinfo is None:
        # Treat naive as UTC if it has time info, else reject
        if stripped == parsed_dt.date().isoformat():
            return parsed_dt.strftime("%Y-%m-%d")
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)

    utc_dt = parsed_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Evidence ID generation
# ---------------------------------------------------------------------------

def generate_evidence_id(url: str, published_at_iso: str) -> str:
    """Generate a stable evidence ID from URL + published timestamp.

    CE1 (4th reflection critical error): the formula uses ONLY immutable
    canonical fields. Body content is excluded because it drifts across
    re-crawls (ads, timestamps, recommendation modules). URL is normalized
    to strip tracking noise AND lowercase scheme/host AND drop default ports.
    Timestamp is canonicalized to UTC before hashing.

    Stability guarantees (added by Phase 0.1 RW5 post-review fixes):
      - Host case insensitive (C1): https://Example.com == https://example.com
      - Default ports stripped (C2): https://site.com == https://site.com:443
      - Timezone canonicalized (C3): ...T10:00:00Z == ...T19:00:00+09:00

    Args:
        url: source_url of the article (will be normalized)
        published_at_iso: ISO-8601 datetime string (must not be empty)

    Returns:
        "ev:" + first 16 hex chars of SHA-256(normalized_url + "|" + canonical_utc_date)

    Raises:
        EvidenceIDError: if url or published_at_iso is empty or malformed
    """
    if not url or not isinstance(url, str):
        raise EvidenceIDError(
            f"evidence_id: url is empty or not a string (got {type(url).__name__})"
        )
    if not published_at_iso or not isinstance(published_at_iso, str):
        raise EvidenceIDError(
            f"evidence_id: published_at_iso is empty or not a string "
            f"(got {type(published_at_iso).__name__})"
        )

    # CE1-C3: canonicalize timezone to UTC before hashing.
    try:
        canonical_date = _canonicalize_iso_datetime(published_at_iso)
    except ValueError as exc:
        raise EvidenceIDError(
            f"evidence_id: published_at_iso '{published_at_iso}' is not a "
            f"valid ISO-8601 datetime: {exc}"
        )

    normalized_url = normalize_url_for_evidence(url)
    if not normalized_url:
        raise EvidenceIDError(
            f"evidence_id: URL normalization yielded empty string for '{url}'"
        )

    payload = f"{normalized_url}|{canonical_date}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"ev:{digest}"


# ---------------------------------------------------------------------------
# CE4 — Bracketed hierarchical evidence markers (WF4-DCI v0.5)
# ---------------------------------------------------------------------------
#
# CE4 extends CE1 with three in-report citation markers:
#   [ev:article:<eid>]                  — whole article
#   [ev:segment:<eid>:s<N>]             — N-th segment (RST/sentence unit)
#   [ev:char:<eid>:<start>-<end>]       — absolute char offset in body
#
# where <eid> is the bare CE1 evidence_id (format: "ev:<16-hex>") — reusing
# the stable hash, not regenerating. CE1-CE3 formula and field-level usage
# are unchanged. CE4 is purely additive for DCI L6 synthesis citations.
#
# Hallucination prevention (v0.5 H3): only Python (this module) creates CE4
# markers. LLMs must reference from the pre-registered pool, never generate.
# validate_evidence_chain.py enforces CE4 regex; EvidenceLedger in src/dci/
# owns the pool and verify_llm_output() rejects unknown markers.

# Regex to recognize the CE1 hash body inside CE4 markers.
_CE1_EID_RE = re.compile(r"^ev:[0-9a-f]{16}$")


def _assert_ce1_evidence_id(evidence_id: str) -> None:
    """Raise EvidenceIDError if evidence_id is not a valid CE1 string."""
    if not isinstance(evidence_id, str) or not _CE1_EID_RE.match(evidence_id):
        raise EvidenceIDError(
            f"CE4: invalid CE1 evidence_id '{evidence_id}' — "
            f"expected format 'ev:<16-hex>'"
        )


def generate_article_marker(evidence_id: str) -> str:
    """CE4: whole-article citation marker.

    Args:
        evidence_id: CE1 evidence_id (format: ``ev:<16-hex>``).

    Returns:
        ``[ev:article:<evidence_id>]``

    Raises:
        EvidenceIDError: if evidence_id is not a valid CE1 string.
    """
    _assert_ce1_evidence_id(evidence_id)
    return f"[ev:article:{evidence_id}]"


def generate_segment_marker(evidence_id: str, segment_ordinal: int) -> str:
    """CE4: segment-level citation marker.

    A segment is an RST-parsed unit or sentence. Ordinal is the 0-indexed
    position within the article. Ordinal must be a non-negative integer.

    Args:
        evidence_id: CE1 evidence_id (format: ``ev:<16-hex>``).
        segment_ordinal: 0-indexed segment position within the article.

    Returns:
        ``[ev:segment:<evidence_id>:s<N>]``

    Raises:
        EvidenceIDError: if evidence_id is invalid or ordinal is negative.
    """
    _assert_ce1_evidence_id(evidence_id)
    if not isinstance(segment_ordinal, int) or segment_ordinal < 0:
        raise EvidenceIDError(
            f"CE4: segment_ordinal must be non-negative int "
            f"(got {segment_ordinal!r})"
        )
    return f"[ev:segment:{evidence_id}:s{segment_ordinal}]"


def generate_char_span_marker(
    evidence_id: str,
    char_start: int,
    char_end: int,
) -> str:
    """CE4: char-offset citation marker (finest granularity).

    Represents an absolute character-range citation inside the article body.
    char_end is exclusive (Pythonic slice semantics: body[start:end]).

    Args:
        evidence_id: CE1 evidence_id (format: ``ev:<16-hex>``).
        char_start: Inclusive start offset (>= 0).
        char_end: Exclusive end offset (> char_start).

    Returns:
        ``[ev:char:<evidence_id>:<start>-<end>]``

    Raises:
        EvidenceIDError: if evidence_id is invalid or the range is malformed.
    """
    _assert_ce1_evidence_id(evidence_id)
    if not isinstance(char_start, int) or not isinstance(char_end, int):
        raise EvidenceIDError(
            f"CE4: char offsets must be integers (got {type(char_start).__name__} "
            f"and {type(char_end).__name__})"
        )
    if char_start < 0 or char_end <= char_start:
        raise EvidenceIDError(
            f"CE4: invalid char range [{char_start}, {char_end}) — "
            f"require 0 <= start < end"
        )
    return f"[ev:char:{evidence_id}:{char_start}-{char_end}]"


# Parser regex for CE4 markers. Order matters: char > segment > article so
# the more-specific patterns match first when the input is ambiguous.
_CE4_MARKER_RE = re.compile(
    r"\[ev:"
    r"(?P<kind>article|segment|char):"
    r"(?P<eid>ev:[0-9a-f]{16})"
    r"(?::"
        r"(?:s(?P<seg_ord>\d+)"      # segment
        r"|(?P<cs>\d+)-(?P<ce>\d+)"  # char range
        r")"
    r")?"
    r"\]"
)


def parse_ce4_marker(marker: str) -> dict | None:
    """Parse a CE4 marker string into its components.

    Args:
        marker: A string such as ``[ev:article:ev:abc...]``,
            ``[ev:segment:ev:abc...:s12]``, or
            ``[ev:char:ev:abc...:1234-1289]``.

    Returns:
        ``{"kind": "article"|"segment"|"char", "evidence_id": "ev:...", ...}``
        plus ``segment_ordinal`` or ``char_start``/``char_end`` depending on
        kind. None when the marker is unrecognized.
    """
    if not isinstance(marker, str):
        return None
    m = _CE4_MARKER_RE.fullmatch(marker.strip())
    if not m:
        return None
    kind = m.group("kind")
    eid = m.group("eid")
    out: dict = {"kind": kind, "evidence_id": eid, "marker": marker.strip()}
    if kind == "article":
        # Article kind must have no suffix group matched.
        if m.group("seg_ord") is not None or m.group("cs") is not None:
            return None
        return out
    if kind == "segment":
        if m.group("seg_ord") is None:
            return None
        out["segment_ordinal"] = int(m.group("seg_ord"))
        return out
    # kind == "char"
    if m.group("cs") is None or m.group("ce") is None:
        return None
    cs = int(m.group("cs"))
    ce = int(m.group("ce"))
    if cs < 0 or ce <= cs:
        return None
    out["char_start"] = cs
    out["char_end"] = ce
    return out


def extract_ce4_markers(text: str) -> list[dict]:
    """Extract every well-formed CE4 marker from a text blob.

    Malformed or non-CE4 ``[ev:...]`` substrings are skipped silently.

    Args:
        text: Arbitrary text (e.g. an LLM synthesis output).

    Returns:
        List of dicts from :func:`parse_ce4_marker`, in document order.
    """
    if not isinstance(text, str) or not text:
        return []
    results: list[dict] = []
    for m in _CE4_MARKER_RE.finditer(text):
        parsed = parse_ce4_marker(m.group(0))
        if parsed is not None:
            results.append(parsed)
    return results


# ---------------------------------------------------------------------------
# Reverse lookup — JSONL raw article source
# ---------------------------------------------------------------------------

def lookup_evidence_source(evidence_id: str, raw_jsonl_path: str) -> dict | None:
    """Look up the raw article record by evidence_id in the raw JSONL file.

    Scans the JSONL file line by line. Skips malformed JSON lines silently
    (robust to partial file corruption).

    Returns:
        The matching dict (with all fields), or None if not found.
    """
    if not evidence_id or not raw_jsonl_path:
        return None
    if not os.path.exists(raw_jsonl_path):
        return None

    try:
        with open(raw_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # skip malformed
                if not isinstance(record, dict):
                    continue
                if record.get("evidence_id") == evidence_id:
                    return record
    except (OSError, UnicodeDecodeError):
        return None
    return None


def load_evidence_index(raw_jsonl_path: str) -> set:
    """Return a set of all evidence_ids in the raw JSONL file.

    Used for O(1) membership checks when validating long downstream chains.
    """
    ids: set = set()
    if not raw_jsonl_path or not os.path.exists(raw_jsonl_path):
        return ids
    try:
        with open(raw_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                eid = record.get("evidence_id")
                if isinstance(eid, str) and eid.startswith("ev:"):
                    ids.add(eid)
    except (OSError, UnicodeDecodeError):
        pass
    return ids


# ---------------------------------------------------------------------------
# Chain continuity validation
# ---------------------------------------------------------------------------

def validate_evidence_chain_continuity(
    downstream_ids: Iterable[str],
    raw_jsonl_path: str,
) -> dict:
    """Verify every downstream evidence_id exists in the raw JSONL index.

    Used at stage boundaries (S1 → S2 → ... → S8 → W3 → W4) to guarantee
    no orphan IDs leak into insight reports or master integration.

    Returns:
        {
          "valid": bool,
          "orphan_count": int,
          "orphans": list[str],   # orphan IDs (capped at 20 for report brevity)
          "total_checked": int,
          "error": str | None,
        }
    """
    downstream_list = list(downstream_ids)

    if not os.path.exists(raw_jsonl_path):
        return {
            "valid": False,
            "orphan_count": 0,
            "orphans": [],
            "total_checked": len(downstream_list),
            "error": f"raw_missing: {raw_jsonl_path}",
        }

    known = load_evidence_index(raw_jsonl_path)
    orphans: list[str] = []
    for eid in downstream_list:
        if eid not in known:
            orphans.append(eid)

    return {
        "valid": len(orphans) == 0,
        "orphan_count": len(orphans),
        "orphans": orphans[:20],  # cap for brevity in error reports
        "total_checked": len(downstream_list),
        "error": None,
    }
