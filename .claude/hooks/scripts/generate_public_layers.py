#!/usr/bin/env python3
"""
Public Narrative Orchestrator — generate_public_layers.py

Run the full 3-layer Public Narrative pipeline for a given date:
    1. facts_extractor.build_facts_pool  → facts_pool.json
    2. L1 narration (retry×5 against PUB2-PUB7)
    3. L2 narration (retry×5, sees L1)
    4. L3 narration (retry×5, sees L1+L2)
    5. Korean translation (reuses @translator convention — subprocess)
    6. PUB8 EN↔KO parity re-verify

Usage:
    python3 .claude/hooks/scripts/generate_public_layers.py \\
        --date 2026-04-14 --project-dir .

    # Limit to L1 only (testing)
    python3 .claude/hooks/scripts/generate_public_layers.py \\
        --date 2026-04-14 --only L1 --project-dir .

    # Skip KO translation (EN only)
    python3 .claude/hooks/scripts/generate_public_layers.py \\
        --date 2026-04-14 --skip-translation --project-dir .

Exit codes:
    0 — all requested layers PASS
    1 — at least one FAIL after retries
    2 — script error

Output: JSON summary to stdout; layer files to reports/public/{date}/.

Requires .venv Python — spaCy/textstat/pandas used downstream in the
narrator pipeline. Hook scripts normally run on system python3, so this
script's entrypoint is intentionally compatible with either interpreter
(it dispatches to .venv for the heavy work).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


LAYER_ORDER = ("L1", "L2", "L3")
LAYER_FILENAME = {
    "L1": "interpretation.md",
    "L2": "insight.md",
    "L3": "future.md",
}
LAYER_TEMPLATE = {
    "L1": "interpretation.prompt.md",
    "L2": "insight.prompt.md",
    "L3": "future.prompt.md",
}


def _emit(result: dict[str, Any], exit_code: int) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(exit_code)


def _run_validator(
    project_dir: Path, layer: str, date: str, report_path: Path,
    facts_path: Path, skip_parity: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Call validate_public_readability.py and interpret exit code."""
    cmd = [
        sys.executable,
        str(project_dir / ".claude" / "hooks" / "scripts"
            / "validate_public_readability.py"),
        "--layer", layer, "--date", date,
        "--project-dir", str(project_dir),
        "--report", str(report_path),
        "--facts-pool", str(facts_path),
    ]
    if skip_parity:
        cmd.append("--skip-parity")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, {"reason": "validator timeout"}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return False, {"reason": f"validator stdout parse: {exc}",
                       "raw": proc.stdout[:500]}
    return proc.returncode == 0, data


def _translate_via_claude(
    en_text: str, ko_path: Path, glossary_path: Path,
) -> bool:
    """Thin translation path — uses Claude CLI directly with glossary."""
    import shutil
    binary = shutil.which("claude")
    if binary is None:
        return False
    prompt = (
        "아래 영어 마크다운을 한국어로 번역하십시오.\n"
        "- 모든 [ev:xxx] 마커는 그대로 유지\n"
        "- 모든 숫자는 변경하지 말 것\n"
        "- 코드 블록(```)은 원문 유지\n"
        "- 헤딩 수를 동일하게 유지\n"
        "- 공식적이되 자연스러운 한국어 문체\n\n"
        "---\n\n"
        f"{en_text}\n"
    )
    try:
        proc = subprocess.run(
            [binary, "--print", "--model", "claude-sonnet-4-6"],
            input=prompt, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    ko_path.write_text(proc.stdout.strip(), encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Public Narrative orchestrator")
    ap.add_argument("--date", required=True)
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--only", default=None, choices=["L1", "L2", "L3"],
                    help="Generate only the specified layer (testing)")
    ap.add_argument("--skip-translation", action="store_true")
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    args = ap.parse_args()

    project_dir = Path(args.project_dir).resolve()
    public_dir = project_dir / "reports" / "public" / args.date
    public_dir.mkdir(parents=True, exist_ok=True)
    templates_dir = project_dir / "src" / "public_narrative" / "templates"

    # Import lazily after sys.path boost
    sys.path.insert(0, str(project_dir))
    try:
        from src.public_narrative.facts_extractor import (
            build_facts_pool, save_facts_pool,
        )
        from src.public_narrative.narrator import (
            NarrationInput, NarrationError, narrate_with_retry,
        )
    except Exception as exc:
        _emit({"valid": False,
               "error": f"import public_narrative failed: {exc}"}, 2)

    # ---- Step 1: facts pool ----
    t0 = time.time()
    pool = build_facts_pool(project_dir, args.date)
    facts_path = save_facts_pool(pool, public_dir)
    if not pool["w1"].get("available"):
        _emit({"valid": False, "reason": "W1 corpus missing — cannot build pool",
               "facts_pool": str(facts_path)}, 1)

    # ---- Step 2-4: L1 → L2 → L3 ----
    layer_texts: dict[str, str] = {}
    per_layer: list[dict[str, Any]] = []
    layers_to_run = [args.only] if args.only else list(LAYER_ORDER)

    for layer in layers_to_run:
        template_path = templates_dir / LAYER_TEMPLATE[layer]
        if not template_path.exists():
            _emit({"valid": False,
                   "reason": f"template missing: {template_path}"}, 2)

        report_path = public_dir / LAYER_FILENAME[layer]

        ni = NarrationInput(
            layer=layer,
            template_path=template_path,
            facts_pool=pool,
            l1_text=layer_texts.get("L1"),
            l2_text=layer_texts.get("L2"),
            model=args.model,
        )

        def make_validator(layer=layer, report_path=report_path):
            def _validate(text: str) -> tuple[bool, dict[str, Any]]:
                # Write candidate to disk so validator sees it
                report_path.write_text(text, encoding="utf-8")
                ok, details = _run_validator(
                    project_dir, layer, args.date, report_path,
                    facts_path, skip_parity=True,
                )
                if not ok:
                    # Surface only FAIL reasons for the retry prompt
                    fail_reasons = {
                        c.get("code"): c.get("details")
                        for c in details.get("checks", [])
                        if c.get("status") == "FAIL"
                    }
                    return False, fail_reasons
                return True, details
            return _validate

        layer_started = time.time()
        try:
            out = narrate_with_retry(ni, make_validator(),
                                     max_attempts=args.max_attempts)
        except NarrationError as exc:
            per_layer.append({
                "layer": layer, "status": "FAIL",
                "error": str(exc),
                "elapsed_seconds": round(time.time() - layer_started, 2),
            })
            # L3 failure is non-blocking; L1/L2 failure aborts subsequent layers
            if layer in {"L1", "L2"}:
                _emit({
                    "valid": False,
                    "facts_pool": str(facts_path),
                    "layers": per_layer,
                    "total_elapsed_seconds": round(time.time() - t0, 2),
                }, 1)
            continue

        # Persist & record
        report_path.write_text(out.text, encoding="utf-8")
        layer_texts[layer] = out.text
        per_layer.append({
            "layer": layer,
            "status": "PASS",
            "path": str(report_path.relative_to(project_dir)),
            "attempts": out.attempts,
            "elapsed_seconds": round(out.elapsed_seconds, 2),
        })

    # ---- Step 5: KO sidecar (prose is Korean-native per templates) ----
    # The narrator templates instruct Korean-first output, so the .md IS
    # already Korean. We skip translation-to-Korean (meaningless) and create
    # a .ko.md alias so the dashboard's language toggle remains wired. A
    # future English-first mode can reverse this.
    if not args.skip_translation:
        import re as _re
        for entry in per_layer:
            if entry["status"] != "PASS":
                continue
            layer = entry["layer"]
            src_path = public_dir / LAYER_FILENAME[layer]
            ko_name = LAYER_FILENAME[layer].replace(".md", ".ko.md")
            ko_path = public_dir / ko_name
            src_text = src_path.read_text(encoding="utf-8")
            hangul = sum(1 for ch in src_text if "\uac00" <= ch <= "\ud7a3")
            letters = sum(1 for ch in src_text if ch.isalnum())
            hangul_ratio = hangul / max(1, letters)
            if hangul_ratio >= 0.30:
                # Already Korean → create alias (dashboard toggle-compatible)
                ko_path.write_text(src_text, encoding="utf-8")
                entry["ko_translation"] = (
                    f"{ko_path.relative_to(project_dir)} (alias — "
                    f"prose is Korean-native, hangul={hangul_ratio:.2%})"
                )
                entry["ko_parity"] = "PASS"
            else:
                # English-source → translate via Claude CLI
                glossary_path = (
                    project_dir / "src" / "public_narrative"
                    / "glossary_simple.yaml"
                )
                ok = _translate_via_claude(src_text, ko_path, glossary_path)
                entry["ko_translation"] = (
                    str(ko_path.relative_to(project_dir)) if ok else "FAILED"
                )
                if ok:
                    passed, detail = _run_validator(
                        project_dir, layer, args.date, src_path, facts_path,
                        skip_parity=False,
                    )
                    entry["ko_parity"] = "PASS" if passed else "FAIL"
                    if not passed:
                        entry.setdefault("warnings", []).append(
                            {"PUB8": detail.get("checks", [])[-1]
                                     if detail else {}}
                        )

    # ---- Step 6: emit summary ----
    total_elapsed = round(time.time() - t0, 2)
    overall = all(e["status"] == "PASS" for e in per_layer)
    # L3 failure degrades to "partial" — L1/L2 still valid
    if not overall and all(
        e["status"] == "PASS" for e in per_layer if e["layer"] != "L3"
    ):
        overall_label = "partial_pass_l3_failed"
    else:
        overall_label = "full_pass" if overall else "failed"

    meta_path = public_dir / "generation_metadata.json"
    meta = {
        "date": args.date,
        "status": overall_label,
        "total_elapsed_seconds": total_elapsed,
        "layers": per_layer,
        "facts_pool": str(facts_path.relative_to(project_dir)),
        "model": args.model,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    _emit({
        "valid": overall,
        **meta,
    }, 0 if overall else 1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "valid": False,
            "error": f"script failure: {type(exc).__name__}: {exc}",
        }))
        sys.exit(2)
