"""Execution layer library — runtime triple-execution workflow helpers.

This module is kept SEPARATE from _context_lib.py (7,554 LOC) to prevent
monolithic bloat (CE8 from 4th reflection). _context_lib.py handles the
build workflow; _execution_lib.py handles the runtime execution layer.

Responsibilities:
    - validate_execution_section(): schema validation for execution: root section
    - read_execution_state(): read-only access to execution section
    - get_active_workflow(): determine which of W1/W2/W3/master is active
    - extract_execution_progress(): snapshot-ready progress summary
    - has_execution_layer(): one-way ratchet freeze trigger

P1 Compliance: All functions are deterministic. No LLM judgment.
SOT Compliance: Read-only. Writes go through sot_manager.py atomic-write.

Architecture (from FINAL-DESIGN-triple-execution.md):

    .claude/state.yaml
    ├── workflow:          # frozen when execution: is present
    └── execution:         # managed by this module
        ├── schema_version: 1
        ├── current_run_id: "exec-YYYY-MM-DD"
        ├── runs:
        │   └── exec-YYYY-MM-DD:
        │       ├── status: pending|in_progress|completed|failed
        │       ├── current_workflow: crawling|analysis|insight|master|null
        │       ├── transition_log: []
        │       ├── trace: []
        │       ├── workflows:
        │       │   ├── crawling: {...}
        │       │   ├── analysis: {...}
        │       │   ├── insight: {...}
        │       │   └── master: {...}
        │       ├── meta_decisions: []
        │       └── retry_budgets: {...}
        ├── history: []
        ├── longitudinal_index: {...}
        └── retention: {...}
"""

from __future__ import annotations

import os
import re
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Schema version (bump when making backward-incompatible changes).
# Current value must match sot_manager.py:cmd_init_execution default.
SCHEMA_VERSION = 1

# Valid status enums
VALID_RUN_STATUS = frozenset({"pending", "in_progress", "completed", "failed"})
VALID_WORKFLOW_STATUS = frozenset({"pending", "in_progress", "completed", "failed"})
VALID_CURRENT_WORKFLOW = frozenset({"crawling", "analysis", "insight", "master"})

# Expected workflow keys in execution.runs.{id}.workflows
REQUIRED_WORKFLOW_KEYS = frozenset({"crawling", "analysis", "insight", "master"})

# Required fields in execution.runs.{id}
REQUIRED_RUN_FIELDS = (
    "status", "started_at", "current_workflow", "transition_log",
    "trace", "workflows", "meta_decisions", "retry_budgets",
)

# Required fields in execution.runs.{id}.workflows.{name}
REQUIRED_WORKFLOW_FIELDS = (
    "status", "phase", "pacs", "outputs", "tdd",
    "active_team", "semantic_gates",
)

# Required keys in transition_log entries
TRANSITION_LOG_FIELDS = ("ts", "from", "to", "status")

# Run ID pattern: exec-YYYY-MM-DD (Meta-level run_id)
RUN_ID_PATTERN = re.compile(r"^exec-\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# E1-E15 schema validation
# ---------------------------------------------------------------------------

def validate_execution_section(execution: Any) -> list:
    """Validate the execution: root section of state.yaml (E1-E15 rules).

    P1 Compliance: Deterministic. No file I/O. No LLM judgment.

    Returns:
        list of warning strings. Empty list = all checks passed.

    Rules:
        E1  execution field is dict (if present)
        E2  schema_version is int == SCHEMA_VERSION
        E3  current_run_id is string or None
        E4  runs is dict
        E5  run keys match exec-YYYY-MM-DD
        E6  each run has required fields
        E7  run.status in valid enum
        E8  run.current_workflow in valid enum or None
        E9  transition_log is list with structured entries
        E10 run.workflows has exactly 4 keys
        E11 each workflow has required fields
        E12 workflow.status in valid enum
        E13 workflow.active_team is None or dict
        E14 workflow.semantic_gates is dict
        E15 retry_budgets has meta + workflow subfields with integer types
    """
    # E1: None or missing → OK (forward-compat: execution section is optional)
    if execution is None:
        return []

    warnings: list[str] = []

    # E1: must be dict
    if not isinstance(execution, dict):
        warnings.append(
            f"E1: execution is {type(execution).__name__}, expected dict"
        )
        return warnings

    # E2: schema_version
    if "schema_version" not in execution:
        warnings.append("E2: execution.schema_version is missing")
    else:
        sv = execution["schema_version"]
        if not isinstance(sv, int) or isinstance(sv, bool):
            warnings.append(
                f"E2: execution.schema_version is "
                f"{type(sv).__name__}, expected int"
            )
        elif sv != SCHEMA_VERSION:
            warnings.append(
                f"E2: execution.schema_version is {sv}, "
                f"expected {SCHEMA_VERSION}"
            )

    # E3: current_run_id
    crid = execution.get("current_run_id")
    if crid is not None and not isinstance(crid, str):
        warnings.append(
            f"E3: execution.current_run_id is "
            f"{type(crid).__name__}, expected string or null"
        )

    # E4: runs is dict
    runs = execution.get("runs")
    if runs is None:
        warnings.append("E4: execution.runs is None, expected dict (may be empty)")
    elif not isinstance(runs, dict):
        warnings.append(
            f"E4: execution.runs is {type(runs).__name__}, expected dict"
        )
    else:
        # E5-E15: validate each run
        for run_key, run_value in runs.items():
            warnings.extend(_validate_run(run_key, run_value))

    return warnings


def _validate_run(run_key: Any, run: Any) -> list:
    """Validate a single execution.runs.{id} entry."""
    warnings: list[str] = []

    # E5: run key format
    if not isinstance(run_key, str):
        warnings.append(
            f"E5: run key {run_key!r} is "
            f"{type(run_key).__name__}, expected string matching exec-YYYY-MM-DD"
        )
        return warnings  # skip rest if key is malformed
    if not RUN_ID_PATTERN.match(run_key):
        warnings.append(
            f"E5: run key '{run_key}' does not match pattern exec-YYYY-MM-DD"
        )

    if not isinstance(run, dict):
        warnings.append(
            f"E6: run '{run_key}' is {type(run).__name__}, expected dict"
        )
        return warnings

    # E6: required fields
    for field in REQUIRED_RUN_FIELDS:
        if field not in run:
            warnings.append(
                f"E6: run '{run_key}' missing required field '{field}'"
            )

    # E7: run.status
    status = run.get("status")
    if status is not None and status not in VALID_RUN_STATUS:
        warnings.append(
            f"E7: run '{run_key}'.status = '{status}', "
            f"expected one of {sorted(VALID_RUN_STATUS)}"
        )

    # E8: run.current_workflow
    cwf = run.get("current_workflow")
    if cwf is not None and cwf not in VALID_CURRENT_WORKFLOW:
        warnings.append(
            f"E8: run '{run_key}'.current_workflow = '{cwf}', "
            f"expected one of {sorted(VALID_CURRENT_WORKFLOW)} or null"
        )

    # E9: transition_log
    tlog = run.get("transition_log")
    if tlog is not None:
        if not isinstance(tlog, list):
            warnings.append(
                f"E9: run '{run_key}'.transition_log is "
                f"{type(tlog).__name__}, expected list"
            )
        else:
            for i, entry in enumerate(tlog):
                if not isinstance(entry, dict):
                    warnings.append(
                        f"E9: run '{run_key}'.transition_log[{i}] is "
                        f"{type(entry).__name__}, expected dict"
                    )
                    continue
                for req in TRANSITION_LOG_FIELDS:
                    if req not in entry:
                        warnings.append(
                            f"E9: run '{run_key}'.transition_log[{i}] "
                            f"missing field '{req}'"
                        )

    # E10: run.workflows must be dict with exactly 4 keys
    workflows = run.get("workflows")
    if workflows is not None:
        if not isinstance(workflows, dict):
            warnings.append(
                f"E10: run '{run_key}'.workflows is "
                f"{type(workflows).__name__}, expected dict"
            )
        else:
            present = set(workflows.keys())
            missing = REQUIRED_WORKFLOW_KEYS - present
            extra = present - REQUIRED_WORKFLOW_KEYS
            if missing:
                warnings.append(
                    f"E10: run '{run_key}'.workflows missing keys: "
                    f"{sorted(missing)}"
                )
            if extra:
                warnings.append(
                    f"E10: run '{run_key}'.workflows has unexpected keys: "
                    f"{sorted(extra)}"
                )
            # E11-E14: validate each workflow subsection
            for wf_name, wf_value in workflows.items():
                if wf_name in REQUIRED_WORKFLOW_KEYS:
                    warnings.extend(
                        _validate_workflow_section(run_key, wf_name, wf_value)
                    )

    # E15: retry_budgets
    rb = run.get("retry_budgets")
    if rb is not None:
        warnings.extend(_validate_retry_budgets(run_key, rb))

    return warnings


def _validate_workflow_section(run_key: str, wf_name: str, wf: Any) -> list:
    """Validate a single execution.runs.{id}.workflows.{name} entry."""
    warnings: list[str] = []

    if not isinstance(wf, dict):
        warnings.append(
            f"E11: run '{run_key}'.workflows.{wf_name} is "
            f"{type(wf).__name__}, expected dict"
        )
        return warnings

    # E11: required fields
    for field in REQUIRED_WORKFLOW_FIELDS:
        if field not in wf:
            warnings.append(
                f"E11: run '{run_key}'.workflows.{wf_name} "
                f"missing required field '{field}'"
            )

    # E12: workflow.status
    status = wf.get("status")
    if status is not None and status not in VALID_WORKFLOW_STATUS:
        warnings.append(
            f"E12: run '{run_key}'.workflows.{wf_name}.status = "
            f"'{status}', expected one of {sorted(VALID_WORKFLOW_STATUS)}"
        )

    # E13: active_team
    at = wf.get("active_team")
    if at is not None and not isinstance(at, dict):
        warnings.append(
            f"E13: run '{run_key}'.workflows.{wf_name}.active_team is "
            f"{type(at).__name__}, expected dict or null"
        )

    # E14: semantic_gates
    sg = wf.get("semantic_gates")
    if sg is not None and not isinstance(sg, dict):
        warnings.append(
            f"E14: run '{run_key}'.workflows.{wf_name}.semantic_gates is "
            f"{type(sg).__name__}, expected dict"
        )

    return warnings


def _validate_retry_budgets(run_key: str, rb: Any) -> list:
    """Validate run.retry_budgets structure (E15)."""
    warnings: list[str] = []

    if not isinstance(rb, dict):
        warnings.append(
            f"E15: run '{run_key}'.retry_budgets is "
            f"{type(rb).__name__}, expected dict"
        )
        return warnings

    # E15: meta subsection
    if "meta" not in rb:
        warnings.append(f"E15: run '{run_key}'.retry_budgets missing 'meta'")
    else:
        meta = rb["meta"]
        if not isinstance(meta, dict):
            warnings.append(
                f"E15: run '{run_key}'.retry_budgets.meta is "
                f"{type(meta).__name__}, expected dict"
            )
        else:
            for int_field in ("run_retries", "max"):
                if int_field in meta:
                    v = meta[int_field]
                    if not isinstance(v, int) or isinstance(v, bool):
                        warnings.append(
                            f"E15: run '{run_key}'.retry_budgets.meta."
                            f"{int_field} is {type(v).__name__}, expected int"
                        )

    # E15: workflow subsection
    if "workflow" not in rb:
        warnings.append(
            f"E15: run '{run_key}'.retry_budgets missing 'workflow'"
        )
    else:
        wrb = rb["workflow"]
        if not isinstance(wrb, dict):
            warnings.append(
                f"E15: run '{run_key}'.retry_budgets.workflow is "
                f"{type(wrb).__name__}, expected dict"
            )
        else:
            for required_wf in REQUIRED_WORKFLOW_KEYS:
                if required_wf not in wrb:
                    warnings.append(
                        f"E15: run '{run_key}'.retry_budgets.workflow "
                        f"missing '{required_wf}'"
                    )
                else:
                    wf_budget = wrb[required_wf]
                    if not isinstance(wf_budget, dict):
                        warnings.append(
                            f"E15: run '{run_key}'.retry_budgets.workflow."
                            f"{required_wf} is {type(wf_budget).__name__}, "
                            f"expected dict"
                        )
                        continue
                    for gate in ("verification", "pacs", "review"):
                        if gate in wf_budget:
                            v = wf_budget[gate]
                            if not isinstance(v, int) or isinstance(v, bool):
                                warnings.append(
                                    f"E15: run '{run_key}'.retry_budgets."
                                    f"workflow.{required_wf}.{gate} is "
                                    f"{type(v).__name__}, expected int"
                                )

    return warnings


# ---------------------------------------------------------------------------
# Execution state accessors (read-only)
# ---------------------------------------------------------------------------

def has_execution_layer(sot_data: dict | None) -> bool:
    """Return True if state.yaml has an execution: section.

    This is the freeze trigger: one-way ratchet for workflow.* hard freeze.
    Once execution: exists, workflow.* becomes immutable.

    Args:
        sot_data: parsed state.yaml dict (full root, not just workflow:)

    Returns:
        True if execution section is present (and is a dict)
    """
    if not isinstance(sot_data, dict):
        return False
    exec_section = sot_data.get("execution")
    return isinstance(exec_section, dict)


def read_execution_state(sot_data: dict | None) -> dict | None:
    """Extract execution section from full SOT data.

    Returns:
        The execution dict, or None if not present.
    """
    if not isinstance(sot_data, dict):
        return None
    exec_section = sot_data.get("execution")
    return exec_section if isinstance(exec_section, dict) else None


def get_active_workflow(sot_data: dict | None) -> str | None:
    """Return the currently active workflow name (crawling|analysis|insight|master).

    Returns None if no active run or current_workflow is null.
    """
    exec_section = read_execution_state(sot_data)
    if not exec_section:
        return None
    current_run_id = exec_section.get("current_run_id")
    if not current_run_id:
        return None
    runs = exec_section.get("runs", {})
    run = runs.get(current_run_id) if isinstance(runs, dict) else None
    if not isinstance(run, dict):
        return None
    cwf = run.get("current_workflow")
    return cwf if cwf in VALID_CURRENT_WORKFLOW else None


def get_current_run(sot_data: dict | None) -> dict | None:
    """Return the current run dict, or None."""
    exec_section = read_execution_state(sot_data)
    if not exec_section:
        return None
    current_run_id = exec_section.get("current_run_id")
    if not current_run_id:
        return None
    runs = exec_section.get("runs", {})
    run = runs.get(current_run_id) if isinstance(runs, dict) else None
    return run if isinstance(run, dict) else None


def extract_execution_progress(sot_data: dict | None) -> dict | None:
    """Extract execution progress summary for snapshot/restoration.

    Returns None if no execution layer is active.
    Returns a dict suitable for inclusion in context snapshots (IMMORTAL).
    """
    exec_section = read_execution_state(sot_data)
    if not exec_section:
        return None

    summary = {
        "schema_version": exec_section.get("schema_version"),
        "current_run_id": exec_section.get("current_run_id"),
        "runs_count": 0,
        "active_workflow": None,
        "workflow_states": {},
    }

    runs = exec_section.get("runs", {})
    if isinstance(runs, dict):
        summary["runs_count"] = len(runs)
        current_run_id = exec_section.get("current_run_id")
        if current_run_id and current_run_id in runs:
            run = runs[current_run_id]
            if isinstance(run, dict):
                summary["active_workflow"] = run.get("current_workflow")
                workflows = run.get("workflows", {})
                if isinstance(workflows, dict):
                    for wf_name, wf in workflows.items():
                        if isinstance(wf, dict):
                            summary["workflow_states"][wf_name] = {
                                "status": wf.get("status"),
                                "phase": wf.get("phase"),
                                "pacs_score":
                                    wf.get("pacs", {}).get("current_step_score")
                                    if isinstance(wf.get("pacs"), dict) else None,
                            }
    return summary


# ---------------------------------------------------------------------------
# Path helpers (SOT file location)
# ---------------------------------------------------------------------------

# D-7 intentional duplication: must match _context_lib.py:SOT_FILENAMES
# and scripts/sot_manager.py:SOT_FILENAMES
SOT_FILENAMES = ("state.yaml", "state.yml", "state.json")


def find_sot_path(project_dir: str) -> str | None:
    """Return absolute path to existing SOT file, or None."""
    for fn in SOT_FILENAMES:
        p = os.path.join(project_dir, ".claude", fn)
        if os.path.exists(p):
            return p
    return None


def read_sot_file(project_dir: str) -> dict | None:
    """Read and parse SOT YAML. Returns full root dict or None.

    Read-only. No fcntl lock (for simple read access from hooks).
    For mutating operations, use sot_manager.py atomic-write subcommand.
    """
    sot_path = find_sot_path(project_dir)
    if not sot_path:
        return None
    try:
        import yaml
        with open(sot_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
