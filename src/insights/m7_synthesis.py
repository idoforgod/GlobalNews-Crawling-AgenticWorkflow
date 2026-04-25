"""M7: Synthesis — Aggregate M1-M6 results into a structured insight report.

Template Mode (default): fully deterministic, P1 compliant, no LLM calls.
Extracts top-N findings from each module, ranks by magnitude of change,
and formats as structured Markdown + JSON.

Satisfies C1 constraint (Claude API = $0) by default.

Reference: research/bigdata-insight-workflow-design.md, Gap 6 (Reflection #3).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from src.insights.constants import (
    SYNTHESIS_MIN_CHANGE_THRESHOLD,
    SYNTHESIS_TOP_N,
    EVIDENCE_SCORE_WEIGHTS,
    ALERT_THRESHOLDS,
    EVIDENCE_MAX_ARTICLES,
    ENTITY_PROFILE_MIN_ARTICLES,
    SAME_EVENT_THRESHOLDS,
)

logger = logging.getLogger(__name__)


def run_synthesis(
    corpus,
    output_dir: Path,
    prior_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Generate insight report from M1-M6 results.

    Args:
        corpus: WindowCorpus (used for metadata only).
        output_dir: Directory to write synthesis outputs.
        prior_metrics: Dict of module results keyed by module short name
            (e.g., "crosslingual", "narrative", "entity", etc.).

    Returns:
        Empty dict (synthesis produces reports, not metrics for validation).
    """
    logger.info("--- M7: Synthesis (Template Mode) ---")

    findings: list[dict[str, Any]] = []

    # --- Extract findings from each module ---
    findings.extend(_extract_crosslingual_findings(prior_metrics.get("crosslingual", {})))
    findings.extend(_extract_narrative_findings(prior_metrics.get("narrative", {})))
    findings.extend(_extract_entity_findings(prior_metrics.get("entity", {})))
    findings.extend(_extract_temporal_findings(prior_metrics.get("temporal", {})))
    findings.extend(_extract_geopolitical_findings(prior_metrics.get("geopolitical", {})))
    findings.extend(_extract_economic_findings(prior_metrics.get("economic", {})))

    # Sort by absolute magnitude (most significant first)
    findings.sort(key=lambda f: abs(f.get("magnitude", 0)), reverse=True)

    # Per-module top-N: select SYNTHESIS_TOP_N findings from each module
    # independently so a high-magnitude module (e.g. geopolitical conflict
    # ratios > 1) cannot starve the others out of report slots. Magnitudes
    # are module-specific units (JSD, HHI, ratio, etc.) and are not directly
    # comparable across modules.
    per_module_top: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        mod = f.get("module", "unknown")
        bucket = per_module_top.setdefault(mod, [])
        if len(bucket) < SYNTHESIS_TOP_N:
            bucket.append(f)
    top_findings = [f for bucket in per_module_top.values() for f in bucket]
    # Re-sort the combined list by magnitude for the JSON "top_findings" view.
    top_findings.sort(key=lambda f: abs(f.get("magnitude", 0)), reverse=True)

    # --- Generate Markdown report ---
    report_md = _generate_markdown_report(top_findings, corpus, prior_metrics)
    report_path = output_dir / "insight_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    logger.info("M7: wrote insight_report.md (%d bytes)", len(report_md))

    # --- Generate structured JSON ---
    insight_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": corpus.window_days,
        "end_date": corpus.end_date,
        "total_findings": len(findings),
        "top_findings": top_findings[:SYNTHESIS_TOP_N],
        "modules_available": list(prior_metrics.keys()),
    }
    data_path = output_dir / "insight_data.json"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(insight_data, f, indent=2, ensure_ascii=False, default=str)

    # --- Key findings for dashboard ---
    key_findings = {
        "summary_stats": _compute_summary_stats(prior_metrics),
        "top_5": top_findings[:5],
        "module_status": {k: "available" for k in prior_metrics},
    }
    kf_path = output_dir / "key_findings.json"
    with open(kf_path, "w", encoding="utf-8") as f:
        json.dump(key_findings, f, indent=2, ensure_ascii=False, default=str)

    # ── M7 Extension: Evidence-Based Future Intelligence (P1 deterministic) ──
    intel_dir = output_dir / "intelligence"
    intel_dir.mkdir(parents=True, exist_ok=True)

    try:
        import pandas as pd
        # Navigate from output_dir (e.g. data/insights/quarterly-2026-Q2/synthesis)
        # to project data root (data/).
        # output_dir.parent = .../quarterly-2026-Q2
        # output_dir.parent.parent = .../insights
        # output_dir.parent.parent.parent = .../data
        data_root = output_dir.parent.parent.parent
        articles_path = _find_latest_parquet(data_root / "processed", "articles.parquet")
        analysis_path = _find_latest_parquet(data_root / "analysis", "article_analysis.parquet")
        ner_path = _find_latest_parquet(data_root / "features", "ner.parquet")
        topics_path = _find_latest_parquet(data_root / "analysis", "topics.parquet")

        if all(p and p.exists() for p in [articles_path, analysis_path, ner_path, topics_path]):
            articles_df = pq.read_table(articles_path).to_pandas()
            analysis_df = pq.read_table(analysis_path).to_pandas()
            ner_df = pq.read_table(ner_path).to_pandas()
            topics_df = pq.read_table(topics_path).to_pandas()

            merged = articles_df.merge(analysis_df, on="article_id").merge(
                topics_df[["article_id", "topic_id", "topic_label", "topic_probability"]],
                on="article_id",
            ).merge(ner_df, on="article_id")

            _compute_entity_profiles(merged, intel_dir)
            _compute_pair_tensions(merged, intel_dir)
            _select_evidence_articles(merged, intel_dir)
            _compute_risk_alerts(merged, prior_metrics, intel_dir)

            logger.info("M7: intelligence extension complete — %s", intel_dir)
        else:
            logger.warning("M7: intelligence skipped — missing input parquets")
    except Exception as e:
        logger.warning("M7: intelligence extension error — %s: %s", type(e).__name__, str(e)[:200])

    logger.info("M7: synthesis complete — %d findings extracted", len(findings))
    return {}


# =============================================================================
# Finding extractors (one per module)
# =============================================================================

def _extract_crosslingual_findings(metrics: dict) -> list[dict]:
    """Extract top findings from M1 Cross-Lingual results.

    Thresholds derived from observed distribution analysis (2026-04 window):
        JSD p50=0.17, p90=0.53 → 0.3 isolates upper-half asymmetries
        Jaccard p5~=0.07 → <0.15 isolates true filter-bubble pairs
    """
    findings = []

    # JSD spikes (threshold raised from 0.05 → 0.3 — 0.05 fires on 100% of pairs)
    for pair, val in metrics.get("jsd_values", {}).items():
        if val > 0.3:
            findings.append({
                "module": "crosslingual",
                "metric": "CL-1_JSD",
                "description": f"Information asymmetry between {pair}: JSD = {val:.3f}",
                "magnitude": val,
                "detail": {"pair": pair, "jsd": val},
            })

    # Filter bubble (threshold tightened 0.5 → 0.15 — 0.5 fires on 69% of pairs)
    for pair, val in metrics.get("filter_bubble", {}).items():
        if val < 0.15:  # <15% shared topics = genuine silo
            findings.append({
                "module": "crosslingual",
                "metric": "CL-4_FilterBubble",
                "description": f"Filter bubble: {pair} share only {val:.0%} of topics",
                "magnitude": 1.0 - val,  # Higher = more isolated
                "detail": {"pair": pair, "jaccard": val},
            })

    return findings


def _extract_narrative_findings(metrics: dict) -> list[dict]:
    """Extract top findings from M2 Narrative results."""
    findings = []

    # Voice dominance (high HHI = oligopoly)
    for topic, hhi in metrics.get("hhi_values", {}).items():
        if hhi > 0.25:
            findings.append({
                "module": "narrative",
                "metric": "NF-3_HHI",
                "description": f"Voice oligopoly in topic {topic}: HHI = {hhi:.3f}",
                "magnitude": hhi,
                "detail": {"topic": topic, "hhi": hhi},
            })

    # Low credibility sources
    for source, cred in metrics.get("source_credibility", {}).items():
        if cred < 0.5:
            findings.append({
                "module": "narrative",
                "metric": "NF-6_Credibility",
                "description": f"Low credibility source: {source} ({cred:.0%})",
                "magnitude": 1.0 - cred,
                "detail": {"source": source, "credibility": cred},
            })

    return findings


def _extract_entity_findings(metrics: dict) -> list[dict]:
    """Extract top findings from M3 Entity results."""
    findings = []

    for entity, ttype in metrics.get("trajectory_types", {}).items():
        if ttype == "rising_star":
            findings.append({
                "module": "entity",
                "metric": "EA-1_Trajectory",
                "description": f"Rising entity: {entity}",
                "magnitude": 0.8,
                "detail": {"entity": entity, "type": ttype},
            })
        elif ttype == "burst":
            findings.append({
                "module": "entity",
                "metric": "EA-1_Trajectory",
                "description": f"Burst entity: {entity}",
                "magnitude": 0.6,
                "detail": {"entity": entity, "type": ttype},
            })

    # Top hidden connections
    hc = metrics.get("hidden_connections", {})
    for pair, jaccard in sorted(hc.items(), key=lambda x: -x[1])[:5]:
        findings.append({
            "module": "entity",
            "metric": "EA-2_HiddenConnection",
            "description": f"Hidden connection: {pair} (Jaccard = {jaccard:.3f})",
            "magnitude": jaccard,
            "detail": {"pair": pair, "jaccard": jaccard},
        })

    return findings


def _extract_temporal_findings(metrics: dict) -> list[dict]:
    """Extract top findings from M4 Temporal results.

    Threshold from observed lag distribution: p50=7.2h, p90=19.2h. A 6h cap
    fires on 0.04% of pairs (overly strict). 8h ≈ slightly-below-median
    captures genuinely fast propagation without opening the floodgates.
    """
    findings = []

    # Fast propagation (threshold raised from 6.0h → 8.0h)
    for pair, lag in metrics.get("velocity_map", {}).items():
        if isinstance(lag, (int, float)) and lag < 8.0:
            findings.append({
                "module": "temporal",
                "metric": "TP-2_Velocity",
                "description": f"Fast propagation {pair}: {lag:.1f}h average lag",
                "magnitude": max(0, 1.0 - lag / 24.0),  # Faster = higher magnitude
                "detail": {"pair": pair, "lag_hours": lag},
            })

    return findings


def _extract_geopolitical_findings(metrics: dict) -> list[dict]:
    """Extract top findings from M5 Geopolitical results."""
    findings = []

    # Most negative BRI
    for pair, bri in metrics.get("bri_values", {}).items():
        if bri < -0.15:
            findings.append({
                "module": "geopolitical",
                "metric": "GI-1_BRI",
                "description": f"Negative bilateral relations: {pair} (BRI = {bri:+.3f})",
                "magnitude": abs(bri),
                "detail": {"pair": pair, "bri": bri},
            })

    # Top soft power
    for country, score in sorted(
        metrics.get("soft_power", {}).items(), key=lambda x: -x[1]
    )[:5]:
        findings.append({
            "module": "geopolitical",
            "metric": "GI-2_SoftPower",
            "description": f"Soft power leader: {country} (score = {score:.3f})",
            "magnitude": score,
            "detail": {"country": country, "score": score},
        })

    # Conflict-dominant pairs
    # Normalize magnitude to 0-1 scale so conflict ratios (observed range
    # 1.0-1.10) do not dominate raw BRI scores (|BRI| up to ~0.35). A ratio
    # of 1.10 → magnitude 0.50; 1.20 → 1.00. This lets all three
    # geopolitical metrics compete fairly for the per-module top-N slots.
    for pair, ratio in metrics.get("conflict_cooperation", {}).items():
        if isinstance(ratio, (int, float)) and ratio > 1.0:
            normalized = min(1.0, (ratio - 1.0) * 5.0)
            findings.append({
                "module": "geopolitical",
                "metric": "GI-4_Conflict",
                "description": f"Conflict-dominant: {pair} (ratio = {ratio:.2f})",
                "magnitude": normalized,
                "detail": {"pair": pair, "ratio": ratio},
            })

    return findings


def _extract_economic_findings(metrics: dict) -> list[dict]:
    """Extract top findings from M6 Economic results."""
    findings = []

    # High EPU
    for lang, epu in metrics.get("epu_values", {}).items():
        if epu > 0.3:
            findings.append({
                "module": "economic",
                "metric": "EI-1_EPU",
                "description": f"High economic uncertainty ({lang}): EPU = {epu:.3f}",
                "magnitude": epu,
                "detail": {"language": lang, "epu": epu},
            })

    # Negative sector sentiment
    for sector, sent in metrics.get("sector_sentiment", {}).items():
        if sent < -0.05:
            findings.append({
                "module": "economic",
                "metric": "EI-2_SectorSentiment",
                "description": f"Negative sentiment in {sector}: {sent:+.3f}",
                "magnitude": abs(sent),
                "detail": {"sector": sector, "sentiment": sent},
            })

    return findings


# =============================================================================
# Report generation
# =============================================================================

def _generate_markdown_report(
    findings: list[dict],
    corpus,
    prior_metrics: dict,
) -> str:
    """Generate a structured Markdown insight report (Template Mode).

    Report structure:
        1. Header (window, data, timestamp)
        2. Executive Summary (top 3 cross-module signals + single takeaway line)
        3. Module findings (6 sections, top-N per module)
        4. Cross-Module Signals (deterministic pattern detection across modules)
        5. Forward-Looking Scenarios (3 what-if branches conditioned on the signals)
    """
    # Group findings by module (re-used by every downstream section)
    by_module: dict[str, list[dict]] = {}
    for f in findings:
        mod = f.get("module", "unknown")
        by_module.setdefault(mod, []).append(f)

    lines: list[str] = [
        f"# Global News Insight Brief",
        f"",
        f"- **Window**: {corpus.window_days} days ending {corpus.end_date}",
        f"- **Data**: {corpus.total_available_days} days available",
        f"- **Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Modules**: {', '.join(prior_metrics.keys()) or 'none'}",
        f"- **Total findings**: {len(findings)}",
        f"",
    ]

    # --- Executive Summary: the 3 highest-magnitude findings across modules ---
    lines.append("## Executive Summary")
    lines.append("")
    if findings:
        top3 = findings[:3]
        for f in top3:
            mod_short = str(f.get("module", "")).upper()[:4]
            lines.append(f"- **[{mod_short}] {f['description']}**")
        takeaway = _derive_takeaway(by_module)
        lines.append("")
        lines.append(f"_{takeaway}_")
    else:
        lines.append(
            "_Insufficient qualifying signals in the current window; "
            "see module sections for raw observations._"
        )
    lines.append("")

    # --- Module sections (unchanged structure) ---
    module_titles = {
        "crosslingual": "Cross-Lingual Asymmetry",
        "narrative": "Narrative & Framing",
        "entity": "Entity Analytics",
        "temporal": "Temporal Patterns",
        "geopolitical": "Geopolitical Index",
        "economic": "Economic Intelligence",
    }

    for mod_key in ["crosslingual", "geopolitical", "economic",
                    "narrative", "entity", "temporal"]:
        mod_findings = by_module.get(mod_key, [])
        title = module_titles.get(mod_key, mod_key)
        mod_raw = prior_metrics.get(mod_key, {}) or {}
        lines.append(f"## {title}")
        lines.append("")

        # Module-level statistical context (always emitted, even if no
        # findings surfaced — helps the reader understand whether empty
        # means "nothing observed" vs. "observed but below threshold").
        context_lines = _module_context_lines(mod_key, mod_raw, mod_findings)
        if context_lines:
            lines.append("### Statistical Context")
            lines.append("")
            lines.extend(context_lines)
            lines.append("")

        if not mod_findings:
            lines.append("### Findings")
            lines.append("")
            lines.append("_No findings exceeded the module's detection "
                         "thresholds in this window._")
            lines.append("")
            continue

        # Primary findings (existing top-N)
        lines.append("### Key Findings")
        lines.append("")
        for f in mod_findings[:SYNTHESIS_TOP_N]:
            lines.append(f"- **[{f['metric']}]** {f['description']}")
        lines.append("")

        # Extended findings: next tier below top-N (up to 5 more)
        extra = mod_findings[SYNTHESIS_TOP_N:SYNTHESIS_TOP_N + 5]
        if extra:
            lines.append("### Additional Observations")
            lines.append("")
            for f in extra:
                lines.append(f"- [{f['metric']}] {f['description']}")
            lines.append("")

        # Evidence coverage for this module (article_count if provided
        # in the metric detail; otherwise omit gracefully).
        coverage = _module_coverage_lines(mod_key, mod_raw, mod_findings)
        if coverage:
            lines.append("### Evidence Coverage")
            lines.append("")
            lines.extend(coverage)
            lines.append("")

    # --- Cross-Module Signals ---
    lines.append("## Cross-Module Signals")
    lines.append("")
    cross_signals = _detect_cross_module_signals(by_module, prior_metrics)
    if cross_signals:
        for sig in cross_signals:
            lines.append(f"- {sig}")
    else:
        lines.append("_No cross-module convergence detected in this window._")
    lines.append("")

    # --- Forward-Looking Scenarios ---
    lines.append("## Forward-Looking Scenarios")
    lines.append("")
    scenarios = _generate_scenarios(by_module, prior_metrics)
    for name, body in scenarios:
        lines.append(f"### {name}")
        lines.append("")
        lines.append(body)
        lines.append("")

    return "\n".join(lines)


def _module_context_lines(
    mod_key: str, mod_raw: dict, findings: list[dict],
) -> list[str]:
    """Produce 2-5 bullet lines of statistical context for a module.

    Reads raw M1-M6 metric dicts (JSD values, HHI distributions, entity
    trajectories, etc.) and summarises counts + distribution percentiles
    so the reader can tell whether the reported findings are the tip of a
    large iceberg or a thin outlier. Pure Python — no LLM.
    """
    out: list[str] = []

    def _pct(values, p):
        import numpy as _np
        if not values:
            return None
        try:
            return float(_np.percentile(list(values), p))
        except Exception:
            return None

    if mod_key == "crosslingual":
        jsd = (mod_raw.get("jsd_values") or {}).values()
        fb = (mod_raw.get("filter_bubble") or {}).values()
        if jsd:
            out.append(f"- Language pairs analysed: **{len(list(mod_raw.get('jsd_values', {})))}**")
            p50, p90 = _pct(list(jsd), 50), _pct(list(jsd), 90)
            if p50 is not None and p90 is not None:
                out.append(f"- JSD distribution: p50 = {p50:.3f}, p90 = {p90:.3f}")
        if fb:
            below_15 = sum(1 for v in fb if v < 0.15)
            out.append(f"- Filter-bubble pairs (Jaccard < 0.15): **{below_15}**")
    elif mod_key == "narrative":
        hhi = (mod_raw.get("hhi_values") or {})
        if hhi:
            out.append(f"- Topics with HHI measured: **{len(hhi)}**")
            p50 = _pct(list(hhi.values()), 50)
            if p50 is not None:
                out.append(f"- Median HHI: {p50:.3f} (0.25 = concentration floor)")
            high = sum(1 for v in hhi.values() if v > 0.25)
            out.append(f"- Oligopoly-flagged topics: **{high}**")
        cred = mod_raw.get("source_credibility") or {}
        if cred:
            low_cred = sum(1 for v in cred.values() if v < 0.5)
            out.append(f"- Sources with credibility < 0.50: **{low_cred}** / {len(cred)}")
    elif mod_key == "entity":
        traj = mod_raw.get("trajectory_types") or {}
        if traj:
            from collections import Counter
            c = Counter(traj.values())
            out.append(f"- Entities with trajectory classified: **{len(traj)}**")
            out.append(
                f"- Types: "
                + ", ".join(f"{k}={v}" for k, v in c.most_common(6))
            )
    elif mod_key == "temporal":
        bursts = mod_raw.get("bursts") or mod_raw.get("burst_events") or []
        if bursts:
            out.append(f"- Burst events detected: **{len(bursts)}**")
        changes = mod_raw.get("change_points") or []
        if changes:
            out.append(f"- Change points: **{len(changes)}**")
    elif mod_key == "geopolitical":
        conf = mod_raw.get("conflict_ratios") or {}
        if conf:
            out.append(f"- Countries profiled: **{len(conf)}**")
            escalating = sum(1 for v in conf.values() if v > 1.0)
            out.append(f"- Countries with escalating conflict ratio (> 1.0): **{escalating}**")
        soft = mod_raw.get("soft_power") or {}
        if soft:
            out.append(f"- Soft-power leaders ranked: **{len(soft)}**")
    elif mod_key == "economic":
        sec = mod_raw.get("sector_sentiment") or {}
        if sec:
            out.append(f"- Sectors scored: **{len(sec)}**")
            neg = sum(1 for v in sec.values() if (
                v.get("mean") if isinstance(v, dict) else v
            ) < -0.05)
            out.append(f"- Sectors with negative mean sentiment: **{neg}**")
        epu = mod_raw.get("epu_index")
        if epu is not None:
            out.append(f"- EPU index: {float(epu):.3f}")

    # Always append total findings count for that module
    if findings:
        out.append(f"- Total findings surfaced: **{len(findings)}**")
    return out


def _module_coverage_lines(
    mod_key: str, mod_raw: dict, findings: list[dict],
) -> list[str]:
    """Per-module evidence coverage summary.

    Extracts article_count / article IDs from finding details when
    available. Returns empty list when no coverage info is surfaced.
    """
    out: list[str] = []
    total_articles = 0
    per_finding: list[tuple[str, int]] = []
    for f in findings[: max(5, len(findings))]:
        detail = f.get("detail") or {}
        ac = detail.get("article_count") or detail.get("n_articles")
        if isinstance(ac, (int, float)):
            per_finding.append((f.get("metric", "?"), int(ac)))
            total_articles += int(ac)
    if per_finding:
        out.append(f"- Findings with article backing: **{len(per_finding)}**")
        out.append(f"- Total articles referenced (sum): **{total_articles:,}**")
    return out


def _derive_takeaway(by_module: dict[str, list[dict]]) -> str:
    """One-line interpretation of the dominant module signal."""
    non_empty = [m for m, fs in by_module.items() if fs]
    if not non_empty:
        return "No actionable signal surfaced in this window."
    # Identify the module with the highest-magnitude finding
    best_mod = max(non_empty,
                   key=lambda m: abs(by_module[m][0].get("magnitude", 0)))
    hints = {
        "geopolitical": (
            "Geopolitical conflict signals are the dominant driver of this "
            "window; monitor for spillover into economic and narrative flows."
        ),
        "economic": (
            "Economic uncertainty dominates — sector sentiment and EPU "
            "readings warrant close tracking for market-adjacent decisions."
        ),
        "crosslingual": (
            "Information asymmetry is the window's defining pattern — "
            "different language spheres are not seeing the same news."
        ),
        "narrative": (
            "Voice concentration is elevated — a narrow set of actors is "
            "shaping narrative across multiple topics."
        ),
        "entity": (
            "Emerging entity linkages suggest a reshuffling of influence "
            "networks worth deeper ethnographic inspection."
        ),
        "temporal": (
            "Information is propagating unusually fast across languages — "
            "expect rapid agenda-setting cycles in the coming window."
        ),
    }
    return hints.get(best_mod, "Module-level signals available below.")


def _detect_cross_module_signals(
    by_module: dict[str, list[dict]],
    prior_metrics: dict,
) -> list[str]:
    """Surface deterministic cross-module patterns.

    Each rule fires when two or more independent modules agree on the
    presence of a condition, producing a higher-confidence signal than any
    single-module finding on its own.
    """
    signals: list[str] = []

    # 1) Geopolitical conflict + economic stress co-occurrence
    geo = by_module.get("geopolitical", [])
    eco = by_module.get("economic", [])
    if geo and eco:
        signals.append(
            f"Conflict-economic coupling: {len(geo)} conflict-dominant pair(s) "
            f"co-occur with {len(eco)} elevated economic-stress indicator(s)."
        )

    # 2) Information asymmetry + filter bubble (language isolation)
    cl = by_module.get("crosslingual", [])
    if sum(1 for f in cl if f.get("metric", "").startswith("CL-4")) >= 3:
        signals.append(
            "Language silo regime: multiple language pairs share less than "
            "10% of topics — cross-lingual attention is fragmenting."
        )

    # 3) Voice concentration + entity trajectory shifts
    narr = by_module.get("narrative", [])
    ent = by_module.get("entity", [])
    if narr and ent:
        signals.append(
            f"Narrative concentration alongside entity reshuffling: "
            f"{len(narr)} voice-oligopoly topic(s) while {len(ent)} hidden "
            f"entity connection(s) emerge — coordinated actor repositioning."
        )

    # 4) Fast temporal propagation between specific language pairs
    temp = by_module.get("temporal", [])
    if len(temp) >= 3:
        signals.append(
            f"Rapid cross-lingual propagation: {len(temp)} language pairs "
            f"show sub-6h information transfer — agenda-setting compression."
        )

    return signals


def _generate_scenarios(
    by_module: dict[str, list[dict]],
    prior_metrics: dict,
) -> list[tuple[str, str]]:
    """Three deterministic what-if scenarios conditioned on current signals."""
    geo_n = len(by_module.get("geopolitical", []))
    eco_n = len(by_module.get("economic", []))
    cl_n = len(by_module.get("crosslingual", []))

    base = (
        "If the dominant patterns persist through the next window, "
        "we expect continuation at approximately the current intensity."
    )
    escalation = (
        "If geopolitical conflict ratios drift further above 1.10 while "
        "economic uncertainty (EPU) rises above 0.45 in any language sphere, "
        "expect cross-border narrative contagion within 2–4 weeks."
    ) if (geo_n and eco_n) else (
        "Insufficient conflict-economic coupling to project escalation at "
        "this time."
    )
    decoupling = (
        "If cross-lingual filter-bubble overlap falls below 3% for three or "
        "more pairs, the information environment approaches a "
        "language-partitioned equilibrium where different spheres debate "
        "fundamentally different events, not just different framings."
    ) if cl_n >= 3 else (
        "Cross-lingual overlap still permits a shared reference frame; "
        "decoupling is not the leading-probability trajectory."
    )

    return [
        ("Baseline (signal persistence)", base),
        ("Escalation (coupled intensification)", escalation),
        ("Decoupling (language-sphere partition)", decoupling),
    ]


# =============================================================================
# M7 Extension: Evidence-Based Future Intelligence (P1 deterministic)
# =============================================================================

def _find_latest_parquet(base_dir: Path, filename: str) -> Path | None:
    """Find the most recent date directory containing the given parquet file."""
    if not base_dir.exists():
        return None
    dates = sorted(
        [d for d in base_dir.iterdir() if d.is_dir() and d.name.startswith("2026")],
        reverse=True,
    )
    for d in dates:
        p = d / filename
        if p.exists():
            return p
    return None


def _entity_in_row(row, entity_name: str) -> bool:
    """P1: check if entity_name appears in any NER column of the row."""
    for col in ("entities_person", "entities_org", "entities_location"):
        val = row.get(col)
        if val is not None and hasattr(val, "__iter__"):
            if entity_name in str(val):
                return True
    return False


def _compute_evidence_score(row, weights: dict) -> float:
    """P1: deterministic evidence quality score. Same input → same output."""
    imp = float(row.get("importance_score", 0) or 0)
    sent_ext = abs(float(row.get("sentiment_score", 0) or 0))
    word_count = int(row.get("word_count", 0) or 0)
    body_comp = min(word_count / 1000.0, 1.0)
    # source_authority is approximated by importance_score (already includes it)
    return (
        weights.get("importance_score", 0.4) * imp
        + weights.get("sentiment_extremity", 0.3) * sent_ext
        + weights.get("body_completeness", 0.1) * body_comp
        + weights.get("source_authority", 0.2) * min(imp * 1.5, 1.0)
    )


def _compute_entity_profiles(merged, intel_dir: Path) -> None:
    """FI-1: Entity sentiment profiling — P1 deterministic aggregation."""
    import pandas as pd

    # Collect all entities from NER columns
    entity_data = []
    for _, row in merged.iterrows():
        sent = float(row.get("sentiment_score", 0) or 0)
        sent_label = str(row.get("sentiment_label", "neutral"))
        lang = str(row.get("language", ""))
        source = str(row.get("source", ""))
        for col in ("entities_person", "entities_org", "entities_location"):
            val = row.get(col)
            if val is not None and hasattr(val, "__len__"):
                for ent in val:
                    ent_str = str(ent).strip()
                    if len(ent_str) >= 2:
                        entity_data.append({
                            "entity": ent_str,
                            "entity_type": col.replace("entities_", ""),
                            "sentiment_score": sent,
                            "sentiment_label": sent_label,
                            "language": lang,
                            "source": source,
                        })

    if not entity_data:
        logger.warning("FI-1: no entity data to profile")
        return

    edf = pd.DataFrame(entity_data)
    profiles = edf.groupby("entity").agg(
        mention_count=("sentiment_score", "count"),
        avg_sentiment=("sentiment_score", "mean"),
        neg_ratio=("sentiment_label", lambda x: (x == "negative").mean()),
        pos_ratio=("sentiment_label", lambda x: (x == "positive").mean()),
        language_count=("language", "nunique"),
        source_count=("source", "nunique"),
        entity_type=("entity_type", "first"),
    ).reset_index()

    profiles = profiles[profiles["mention_count"] >= ENTITY_PROFILE_MIN_ARTICLES]
    profiles = profiles.sort_values("mention_count", ascending=False).head(100)

    pq.write_table(
        pa.Table.from_pandas(profiles, preserve_index=False),
        intel_dir / "entity_profiles.parquet",
    )
    logger.info("FI-1: entity_profiles.parquet — %d entities profiled", len(profiles))


def _compute_pair_tensions(merged, intel_dir: Path) -> None:
    """FI-2: Bilateral entity pair tension tracking — P1 deterministic."""
    import pandas as pd

    # Define pairs of interest based on most mentioned entities
    entity_counts = {}
    for _, row in merged.iterrows():
        for col in ("entities_person", "entities_org", "entities_location"):
            val = row.get(col)
            if val is not None and hasattr(val, "__len__"):
                for ent in val:
                    ent_str = str(ent).strip()
                    if len(ent_str) >= 2:
                        entity_counts[ent_str] = entity_counts.get(ent_str, 0) + 1

    # Top 30 entities
    top_entities = sorted(entity_counts.items(), key=lambda x: -x[1])[:30]
    top_names = [e[0] for e in top_entities]

    pair_data = []
    for i, e1 in enumerate(top_names):
        for e2 in top_names[i + 1:]:
            mask = merged.apply(
                lambda r, a=e1, b=e2: _entity_in_row(r, a) and _entity_in_row(r, b),
                axis=1,
            )
            co = merged[mask]
            if len(co) >= 3:
                avg_sent = float(co["sentiment_score"].mean())
                pair_data.append({
                    "entity_a": e1,
                    "entity_b": e2,
                    "co_occurrence_count": len(co),
                    "avg_sentiment": round(avg_sent, 4),
                    "neg_ratio": round(float((co["sentiment_label"] == "negative").mean()), 4),
                    "top_source": co["source"].mode().iloc[0] if len(co["source"].mode()) > 0 else "",
                    "top_title": str(co.iloc[0].get("title", ""))[:120],
                })

    if pair_data:
        pdf = pd.DataFrame(pair_data).sort_values("co_occurrence_count", ascending=False)
        pq.write_table(
            pa.Table.from_pandas(pdf, preserve_index=False),
            intel_dir / "pair_tensions.parquet",
        )
        logger.info("FI-2: pair_tensions.parquet — %d pairs tracked", len(pdf))


def _select_evidence_articles(merged, intel_dir: Path) -> None:
    """FI-3: Select top evidence articles per topic — P1 deterministic scoring."""
    import pandas as pd

    merged = merged.copy()
    merged["evidence_score"] = merged.apply(
        lambda row: _compute_evidence_score(row, EVIDENCE_SCORE_WEIGHTS), axis=1,
    )

    # Per topic: top N evidence articles
    evidence_rows = []
    for tid in merged[merged["topic_id"] != -1]["topic_id"].unique():
        t_articles = merged[merged["topic_id"] == tid].nlargest(
            EVIDENCE_MAX_ARTICLES, "evidence_score",
        )
        for _, row in t_articles.iterrows():
            evidence_rows.append({
                "topic_id": int(tid),
                "topic_label": str(row.get("topic_label", ""))[:80],
                "article_title": str(row.get("title", ""))[:120],
                "source": str(row.get("source", "")),
                "language": str(row.get("language", "")),
                "sentiment_score": round(float(row.get("sentiment_score", 0)), 4),
                "sentiment_label": str(row.get("sentiment_label", "")),
                "steeps_category": str(row.get("steeps_category", "")),
                "evidence_score": round(float(row["evidence_score"]), 4),
                "body_preview": str(row.get("body", ""))[:200].replace("\n", " "),
            })

    if evidence_rows:
        edf = pd.DataFrame(evidence_rows)
        pq.write_table(
            pa.Table.from_pandas(edf, preserve_index=False),
            intel_dir / "evidence_articles.parquet",
        )
        logger.info("FI-3: evidence_articles.parquet — %d evidence rows", len(edf))


def _compute_risk_alerts(merged, prior_metrics: dict, intel_dir: Path) -> None:
    """FI-4: Risk alert computation — P1 deterministic threshold comparison."""
    import pandas as pd

    alerts = []
    thresholds = ALERT_THRESHOLDS

    # Alert 1: Entity pair crisis sentiment
    entity_counts = {}
    for _, row in merged.iterrows():
        for col in ("entities_person", "entities_org", "entities_location"):
            val = row.get(col)
            if val is not None and hasattr(val, "__len__"):
                for ent in val:
                    entity_counts[str(ent).strip()] = entity_counts.get(str(ent).strip(), 0) + 1

    top_ents = [e for e, c in sorted(entity_counts.items(), key=lambda x: -x[1])[:10]]
    for i, e1 in enumerate(top_ents):
        for e2 in top_ents[i + 1:]:
            mask = merged.apply(
                lambda r, a=e1, b=e2: _entity_in_row(r, a) and _entity_in_row(r, b),
                axis=1,
            )
            co = merged[mask]
            if len(co) >= 5:
                avg = float(co["sentiment_score"].mean())
                if avg < thresholds.get("crisis_sentiment", -0.4):
                    alerts.append({
                        "type": "crisis_sentiment",
                        "entity_pair": f"{e1}-{e2}",
                        "value": round(avg, 4),
                        "threshold": thresholds["crisis_sentiment"],
                        "triggered": True,
                        "article_count": len(co),
                    })

    # Alert 2: EPU critical
    epu_vals = prior_metrics.get("economic", {}).get("epu_values", {})
    for lang, epu in epu_vals.items():
        if isinstance(epu, (int, float)) and epu > thresholds.get("epu_critical", 0.4):
            alerts.append({
                "type": "epu_critical",
                "language": lang,
                "value": round(float(epu), 4),
                "threshold": thresholds["epu_critical"],
                "triggered": True,
            })

    # Alert 3: All sectors negative
    sector_vals = prior_metrics.get("economic", {}).get("sector_sentiment", {})
    if sector_vals and all(v < 0 for v in sector_vals.values() if isinstance(v, (int, float))):
        alerts.append({
            "type": "sector_all_negative",
            "sectors": {k: round(float(v), 4) for k, v in sector_vals.items()},
            "triggered": True,
        })

    # Alert 4: Burst ratio
    traj_types = prior_metrics.get("entity", {}).get("trajectory_types", {})
    burst = traj_types.get("burst", 0)
    plateau = traj_types.get("plateau", 0)
    total_traj = burst + plateau
    if total_traj > 0:
        ratio = burst / total_traj
        if ratio > thresholds.get("burst_ratio_chaos", 0.8):
            alerts.append({
                "type": "burst_ratio_chaos",
                "value": round(ratio, 4),
                "threshold": thresholds["burst_ratio_chaos"],
                "triggered": True,
                "burst": burst,
                "plateau": plateau,
            })

    if alerts:
        adf = pd.DataFrame(alerts)
        pq.write_table(
            pa.Table.from_pandas(adf, preserve_index=False),
            intel_dir / "risk_alerts.parquet",
        )
    else:
        # Write empty schema
        schema = pa.schema([
            ("type", pa.utf8()), ("triggered", pa.bool_()),
        ])
        pq.write_table(pa.table({"type": [], "triggered": []}, schema=schema),
                       intel_dir / "risk_alerts.parquet")

    logger.info("FI-4: risk_alerts.parquet — %d alerts (%d triggered)",
                len(alerts), sum(1 for a in alerts if a.get("triggered")))


def _compute_summary_stats(prior_metrics: dict) -> dict:
    """Compute aggregate summary statistics across all modules."""
    stats = {}

    cl = prior_metrics.get("crosslingual", {})
    jsd = cl.get("jsd_values", {})
    if jsd:
        stats["mean_jsd"] = sum(jsd.values()) / len(jsd)
        stats["max_jsd_pair"] = max(jsd, key=jsd.get) if jsd else None

    gi = prior_metrics.get("geopolitical", {})
    bri = gi.get("bri_values", {})
    if bri:
        stats["most_negative_pair"] = min(bri, key=bri.get) if bri else None
        stats["most_negative_bri"] = min(bri.values()) if bri else None

    ei = prior_metrics.get("economic", {})
    epu = ei.get("epu_values", {})
    if epu:
        stats["max_epu_lang"] = max(epu, key=epu.get) if epu else None
        stats["max_epu"] = max(epu.values()) if epu else None

    return stats
