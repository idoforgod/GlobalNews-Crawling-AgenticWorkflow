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
    top_findings = findings[:SYNTHESIS_TOP_N * 3]  # Keep more for the report

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
    """Extract top findings from M1 Cross-Lingual results."""
    findings = []

    # JSD spikes
    for pair, val in metrics.get("jsd_values", {}).items():
        if val > SYNTHESIS_MIN_CHANGE_THRESHOLD:
            findings.append({
                "module": "crosslingual",
                "metric": "CL-1_JSD",
                "description": f"Information asymmetry between {pair}: JSD = {val:.3f}",
                "magnitude": val,
                "detail": {"pair": pair, "jsd": val},
            })

    # Filter bubble (low Jaccard = high isolation)
    for pair, val in metrics.get("filter_bubble", {}).items():
        if val < 0.5:  # Low overlap = notable
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
    """Extract top findings from M4 Temporal results."""
    findings = []

    # Fast propagation
    for pair, lag in metrics.get("velocity_map", {}).items():
        if isinstance(lag, (int, float)) and lag < 6.0:  # < 6 hours = fast
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
    for pair, ratio in metrics.get("conflict_cooperation", {}).items():
        if isinstance(ratio, (int, float)) and ratio > 1.0:
            findings.append({
                "module": "geopolitical",
                "metric": "GI-4_Conflict",
                "description": f"Conflict-dominant: {pair} (ratio = {ratio:.2f})",
                "magnitude": ratio,
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
    """Generate a structured Markdown insight report (Template Mode)."""
    lines = [
        f"# Global News Insight Brief",
        f"",
        f"- **Window**: {corpus.window_days} days ending {corpus.end_date}",
        f"- **Data**: {corpus.total_available_days} days available",
        f"- **Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Modules**: {', '.join(prior_metrics.keys()) or 'none'}",
        f"- **Total findings**: {len(findings)}",
        f"",
    ]

    # Group findings by module
    by_module: dict[str, list[dict]] = {}
    for f in findings:
        mod = f.get("module", "unknown")
        by_module.setdefault(mod, []).append(f)

    module_titles = {
        "crosslingual": "Cross-Lingual Asymmetry",
        "narrative": "Narrative & Framing",
        "entity": "Entity Analytics",
        "temporal": "Temporal Patterns",
        "geopolitical": "Geopolitical Index",
        "economic": "Economic Intelligence",
    }

    for mod_key in ["crosslingual", "geopolitical", "economic", "narrative", "entity", "temporal"]:
        mod_findings = by_module.get(mod_key, [])
        title = module_titles.get(mod_key, mod_key)
        lines.append(f"## {title}")
        lines.append("")

        if not mod_findings:
            lines.append("_No notable findings in this module._")
            lines.append("")
            continue

        for f in mod_findings[:SYNTHESIS_TOP_N]:
            lines.append(f"- **[{f['metric']}]** {f['description']}")

        lines.append("")

    return "\n".join(lines)


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
