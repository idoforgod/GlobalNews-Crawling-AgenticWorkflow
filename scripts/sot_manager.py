#!/usr/bin/env python3
"""SOT Manager — Deterministic SOT read/write/validate for state.yaml.

P1 Hallucination Prevention: All SOT mutations go through this script.
The Orchestrator (LLM) MUST NOT directly edit state.yaml.

Usage:
    python3 scripts/sot_manager.py --read --project-dir .
    python3 scripts/sot_manager.py --advance-step 3 --project-dir .
    python3 scripts/sot_manager.py --record-output 3 research/output.md --project-dir .
    python3 scripts/sot_manager.py --update-pacs 3 --F 85 --C 78 --L 80 --project-dir .
    python3 scripts/sot_manager.py --update-team '{"name":"team-x","status":"partial","tasks_completed":[],"tasks_pending":["t1"]}' --project-dir .
    python3 scripts/sot_manager.py --init --workflow-name "GlobalNews Auto-Build" --total-steps 20 --project-dir .
    python3 scripts/sot_manager.py --set-autopilot true --project-dir .
    python3 scripts/sot_manager.py --add-auto-approved 8 --project-dir .

All output is JSON to stdout. Exit code 0 always (errors in JSON).
"""

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile

# Phase 0.1 RW3: validate_execution_section integration.
# _execution_lib lives in .claude/hooks/scripts/ — add to sys.path for import.
_SOT_MANAGER_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SOT_MANAGER_DIR)
_HOOKS_SCRIPTS_DIR = os.path.join(
    _PROJECT_ROOT, ".claude", "hooks", "scripts"
)
if _HOOKS_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_SCRIPTS_DIR)

try:
    from _execution_lib import validate_execution_section as _validate_execution_section
    _HAS_EXECUTION_LIB = True
except ImportError:
    _validate_execution_section = None  # type: ignore
    _HAS_EXECUTION_LIB = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# D-7 intentional duplication — must match _context_lib.py:SOT_FILENAMES
SOT_FILENAMES = ("state.yaml", "state.yml", "state.json")
MIN_OUTPUT_SIZE = 100  # bytes — L0 Anti-Skip Guard threshold
# D-7 intentional duplication — must match _context_lib.py:validate_sot_schema() valid_statuses
VALID_STATUSES = {"in_progress", "completed", "failed", "running", "error", "paused"}
VALID_TEAM_STATUSES = {"partial", "all_completed"}
# D-7 intentional duplication — must match run_quality_gates.py:HUMAN_STEPS,
# validate_step_transition.py:HUMAN_STEPS, _context_lib.py:HUMAN_STEPS_SET,
# and prompt/workflow.md "Steps 4, 8, 18"
HUMAN_STEPS = frozenset({4, 8, 18})

# Phase 0.1 additions — execution layer actor authorization
# Valid actor names for --atomic-write / --init-execution.
VALID_ACTORS = frozenset({"meta", "w1", "w2", "w3", "master", "dci", "newspaper"})
# Current execution schema version (D-7: must match _execution_lib.SCHEMA_VERSION)
EXECUTION_SCHEMA_VERSION = 1
# Workflow names owned by W1/W2/W3/master/dci actors (D-7:
#   must match _execution_lib.REQUIRED_WORKFLOW_KEYS)
#
# "dci" is the standalone Deep Content Intelligence workflow (v1.0+).
# Independent of W1-W3 chain; owns its own top-level `workflows.dci.*` slot.
# See prompt/execution-workflows/dci.md and DECISION-LOG ADR-073.
WORKFLOW_ACTOR_MAP = {
    "w1": "crawling",
    "w2": "analysis",
    "w3": "insight",
    "master": "master",
    "dci": "dci",
    "newspaper": "newspaper",  # ADR-083 WF5
}

# D-7 intentional duplication — must match _context_lib.py:_PACS_WITH_MIN_RE, _PACS_SIMPLE_RE
# SM5c uses these to parse pACS scores from log files.
# 2-stage parsing: (1) explicit min formula, (2) simple fallback.
# Changing regex here requires sync with _context_lib.py PA7 logic.
_PACS_WITH_MIN_RE = re.compile(
    r"pACS\s*=\s*min\s*\([^)]+\)\s*=\s*(\d{1,3})", re.IGNORECASE
)
_PACS_SIMPLE_RE = re.compile(r"pACS\s*=\s*(\d{1,3})\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# YAML helpers (PyYAML preferred, regex fallback)
# ---------------------------------------------------------------------------

def _load_yaml(text):
    """Parse YAML text. Returns dict or raises."""
    try:
        import yaml
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("YAML root is not a mapping")
        return data
    except ImportError:
        raise ImportError("PyYAML is required for sot_manager.py")


def _dump_yaml(data):
    """Serialize dict to YAML string."""
    import yaml
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# SOT path resolution
# ---------------------------------------------------------------------------

def _sot_path(project_dir):
    """Find existing SOT file or return default path."""
    for fn in SOT_FILENAMES:
        p = os.path.join(project_dir, ".claude", fn)
        if os.path.exists(p):
            return p
    # Default: state.yaml
    return os.path.join(project_dir, ".claude", "state.yaml")


def _read_sot(project_dir):
    """Read and parse SOT with shared lock. Returns (data_dict, file_path) or raises.

    For read-only commands (--read). Mutating commands use _read_sot_unlocked()
    inside an exclusive _SOTLock context.
    """
    sot = _sot_path(project_dir)
    with _SOTLock(sot, exclusive=False):
        return _read_sot_unlocked(project_dir)


def _extract_wf(data):
    """Extract workflow dict from SOT data (handles nesting)."""
    wf = data.get("workflow")
    if isinstance(wf, dict):
        return wf
    # Flat schema — data itself is the workflow
    return data


def _write_sot_atomic(sot_path, data):
    """Atomic write: temp file → rename."""
    content = _dump_yaml(data)
    dir_path = os.path.dirname(sot_path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, sot_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# Concurrent access safety (fcntl — Unix/macOS)
# ---------------------------------------------------------------------------

class _SOTLock:
    """File-based lock for SOT concurrent access safety.

    Prevents Lost Update when multiple teammates call --update-team
    simultaneously during (team) steps. Uses fcntl.flock() (Unix only).

    Usage:
        with _SOTLock(sot_path, exclusive=True):
            data, sot = _read_sot_unlocked(...)
            ... modify ...
            _write_sot_atomic(sot, data)
    """

    def __init__(self, sot_path, exclusive=False):
        self._lock_path = sot_path + ".lock"
        self._exclusive = exclusive
        self._fd = None

    def __enter__(self):
        self._fd = open(self._lock_path, "w")
        mode = fcntl.LOCK_EX if self._exclusive else fcntl.LOCK_SH
        fcntl.flock(self._fd, mode)
        return self

    def __exit__(self, *args):
        if self._fd:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()


def _read_sot_unlocked(project_dir):
    """Read and parse SOT WITHOUT locking. Internal use only."""
    sot = _sot_path(project_dir)
    if not os.path.exists(sot):
        raise FileNotFoundError(f"SOT not found: {sot}")
    with open(sot, "r", encoding="utf-8") as f:
        content = f.read()
    data = _load_yaml(content)
    return data, sot


# ---------------------------------------------------------------------------
# Step number validation helper
# ---------------------------------------------------------------------------

def _validate_step_num(wf, step_num, context="operation"):
    """Validate step_num is within [1, total_steps]. Returns error dict or None.

    CR-1: Prevents negative, zero, or out-of-range step numbers from
    corrupting SOT state.
    """
    if not isinstance(step_num, int) or step_num < 1:
        return {
            "valid": False,
            "error": f"SM-R1: step_num={step_num} must be int >= 1 (context: {context})",
        }
    total = wf.get("total_steps")
    if total is not None and isinstance(total, int) and step_num > total:
        return {
            "valid": False,
            "error": f"SM-R1: step_num={step_num} exceeds total_steps={total} (context: {context})",
        }
    return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_schema(wf):
    """Validate SOT schema. Returns list of warnings."""
    warnings = []

    # SM1: current_step must be int >= 0
    cs = wf.get("current_step")
    if cs is not None:
        if not isinstance(cs, int):
            warnings.append(f"SM1: current_step is {type(cs).__name__}, expected int")
        elif cs < 0:
            warnings.append(f"SM1: current_step is {cs}, must be >= 0")

    # SM2: outputs must be dict
    outputs = wf.get("outputs")
    if outputs is not None and not isinstance(outputs, dict):
        warnings.append(f"SM2: outputs is {type(outputs).__name__}, expected dict")

    # SM3: status must be valid
    status = wf.get("status", "")
    if status and status not in VALID_STATUSES:
        warnings.append(f"SM3: status '{status}' not in {VALID_STATUSES}")

    # SM-AP1: autopilot.enabled must be bool (if autopilot section exists)
    ap = wf.get("autopilot")
    if ap is not None:
        if not isinstance(ap, dict):
            warnings.append(f"SM-AP1: autopilot is {type(ap).__name__}, expected dict")
        else:
            enabled = ap.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                warnings.append(f"SM-AP2: autopilot.enabled is {type(enabled).__name__}, expected bool")

            # SM-AP3: auto_approved_steps must be list of ints in HUMAN_STEPS
            aas = ap.get("auto_approved_steps")
            if aas is not None:
                if not isinstance(aas, list):
                    warnings.append(f"SM-AP3: auto_approved_steps is {type(aas).__name__}, expected list")
                else:
                    for item in aas:
                        if not isinstance(item, int):
                            warnings.append(f"SM-AP3: auto_approved_steps contains non-int: {item}")
                        elif item not in HUMAN_STEPS:
                            warnings.append(f"SM-AP4: auto_approved_steps contains non-human step: {item}")

    return warnings


# ---------------------------------------------------------------------------
# Phase 0.1 — Workflow freeze + actor authorization + atomic-write helpers
# ---------------------------------------------------------------------------

def _has_execution_layer(data):
    """Return True if state.yaml has an execution: root section (freeze trigger).

    One-way ratchet: once execution: is initialized, workflow.* becomes frozen.
    Legacy states without execution section remain mutable for backward compat.

    D-7: mirrors _execution_lib.has_execution_layer()
    """
    if not isinstance(data, dict):
        return False
    return isinstance(data.get("execution"), dict)


def _check_workflow_freeze(data, context="operation"):
    """Refuse any mutation targeting workflow.* when execution layer is active.

    SM-WFR1: workflow.* hard freeze. Returns error dict if blocked, else None.
    Callers pre-check this inside the exclusive lock before committing writes.

    Args:
        data: the full SOT root dict
        context: operation name for error message

    Returns:
        {"valid": False, "error": ...} if frozen, None if write allowed
    """
    if _has_execution_layer(data):
        return {
            "valid": False,
            "error": (
                f"SM-WFR1: workflow.* section is frozen (execution layer "
                f"active). '{context}' would mutate workflow.*, which is "
                f"forbidden. The 20-step build workflow is historical and "
                f"must not be modified. Writes must target execution.* "
                f"via --atomic-write with an authorized --actor."
            ),
        }
    return None


def _validate_actor(actor, context="operation"):
    """SM-AUTH1/SM-AUTH2: validate the --actor flag value.

    Returns error dict or None.
    """
    if actor is None or actor == "":
        return {
            "valid": False,
            "error": (
                f"SM-AUTH1: --actor flag is required for '{context}'. "
                f"Valid actors: {sorted(VALID_ACTORS)}"
            ),
        }
    if actor not in VALID_ACTORS:
        return {
            "valid": False,
            "error": (
                f"SM-AUTH2: invalid --actor '{actor}'. "
                f"Valid actors: {sorted(VALID_ACTORS)}"
            ),
        }
    return None


def _check_write_authorization(actor, path):
    """SM-AUTH3: verify that `actor` may write to dotted section `path`.

    Authorization matrix (from FINAL-DESIGN-triple-execution.md §4.1):

        workflow.*                                  → ❌ HARD FREEZE (handled separately)
        execution.schema_version                    → meta
        execution.current_run_id                    → meta
        execution.runs.{id}.status                  → meta
        execution.runs.{id}.current_workflow        → meta
        execution.runs.{id}.transition_log          → meta
        execution.runs.{id}.trace                   → any actor (append-only)
        execution.runs.{id}.workflows.crawling.*    → w1
        execution.runs.{id}.workflows.analysis.*    → w2
        execution.runs.{id}.workflows.insight.*     → w3
        execution.runs.{id}.workflows.master.*      → master
        execution.runs.{id}.meta_decisions          → meta
        execution.runs.{id}.retry_budgets.*         → meta
        execution.history                           → meta
        execution.longitudinal_index                → meta
        execution.retention                         → meta

    Returns error dict or None.
    """
    if not isinstance(path, str) or not path.strip():
        return {
            "valid": False,
            "error": "SM-AW2: --path must be a non-empty dotted path",
        }

    # Root-scope guard: atomic-write only operates on execution.*
    if not path.startswith("execution") and not path.startswith("workflow"):
        return {
            "valid": False,
            "error": (
                f"SM-AW2: --path '{path}' must start with 'execution.*'. "
                f"Other root sections are not writable via atomic-write."
            ),
        }

    # workflow.* paths are handled by workflow freeze check, not authorization.
    # (If execution layer is active, freeze check triggers first.)
    if path.startswith("workflow"):
        return None  # defer to freeze check

    # Trace append is permitted from any actor
    if _is_trace_path(path):
        return None

    # Determine which actor owns this path
    owner = _resolve_path_owner(path)
    if owner is None:
        return {
            "valid": False,
            "error": f"SM-AUTH3: no actor owns path '{path}'",
        }
    if owner == "meta" and actor != "meta":
        return {
            "valid": False,
            "error": (
                f"SM-AUTH3: actor '{actor}' cannot write to '{path}' "
                f"(owner: meta)"
            ),
        }
    if owner != actor:
        return {
            "valid": False,
            "error": (
                f"SM-AUTH3: actor '{actor}' cannot write to '{path}' "
                f"(owner: {owner})"
            ),
        }
    return None


def _is_trace_path(path):
    """Return True if path targets a run's trace list ROOT (any-actor append).

    CE2 (Phase 0.1 L2 review fix): only matches the exact trace list root,
    NOT sub-indexed paths like execution.runs.<id>.trace.0.status. Trace is
    append-only; mutating existing entries would break the E9 invariant.
    """
    if not path.startswith("execution.runs."):
        return False
    parts = path.split(".")
    # Exact match: execution.runs.<id>.trace (4 segments, no sub-indexing)
    return len(parts) == 4 and parts[3] == "trace"


# Path component regex: must be a word (letters/digits/underscore/hyphen).
# Exec run IDs (exec-YYYY-MM-DD) and workflow names match this.
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


def _validate_path_components(path):
    """SOT path component validator.

    CE3 (Phase 0.1 L2 review fix): reject path traversal, empty segments,
    dunder attacks, and any non-word characters. Every component must match
    [A-Za-z_][A-Za-z0-9_\\-]*.

    Returns error dict or None.
    """
    if not isinstance(path, str) or not path.strip():
        return {
            "valid": False,
            "error": "SM-AW2: --path must be a non-empty dotted string",
        }
    parts = path.split(".")
    for i, part in enumerate(parts):
        if part == "":
            return {
                "valid": False,
                "error": f"SM-AW5: empty path segment at index {i} in '{path}'",
            }
        if part in ("..", ".", "__proto__", "constructor", "prototype"):
            return {
                "valid": False,
                "error": f"SM-AW5: forbidden segment '{part}' in path",
            }
        if not _PATH_COMPONENT_RE.match(part):
            return {
                "valid": False,
                "error": (
                    f"SM-AW5: segment '{part}' contains invalid characters. "
                    f"Must match [A-Za-z_][A-Za-z0-9_-]*"
                ),
            }
    return None


def _resolve_path_owner(path):
    """Determine which actor owns a given dotted path under execution.*.

    Returns one of: 'meta', 'w1', 'w2', 'w3', 'master', or None.
    """
    # Top-level execution fields
    if path == "execution.schema_version":
        return "meta"
    if path == "execution.current_run_id":
        return "meta"
    if path == "execution.history" or path.startswith("execution.history."):
        return "meta"
    if path == "execution.longitudinal_index" or path.startswith("execution.longitudinal_index."):
        return "meta"
    if path == "execution.retention" or path.startswith("execution.retention."):
        return "meta"
    if path == "execution.schema_migrations" or path.startswith("execution.schema_migrations."):
        return "meta"

    # Per-run paths: execution.runs.<id>.<field>...
    if not path.startswith("execution.runs."):
        return None
    parts = path.split(".")
    if len(parts) < 4:
        # execution.runs or execution.runs.<id> — meta-level
        return "meta"

    run_field = parts[3]

    # Meta-only run-level fields
    if run_field in ("status", "started_at", "completed_at", "current_workflow",
                     "transition_log", "meta_decisions"):
        return "meta"

    # retry_budgets.meta.* → meta; retry_budgets.workflow.{name}.* → meta too
    # (Meta owns all retry budgets to enforce global limits.)
    if run_field == "retry_budgets":
        return "meta"

    # Trace is any-actor append (handled by _is_trace_path)
    if run_field == "trace":
        return None  # caller uses _is_trace_path first

    # Workflows section: execution.runs.<id>.workflows.<name>.<field>...
    if run_field == "workflows":
        if len(parts) < 5:
            return "meta"  # whole workflows dict → meta
        wf_name = parts[4]
        # Reverse map workflow name → actor
        for actor, owned_wf in WORKFLOW_ACTOR_MAP.items():
            if wf_name == owned_wf:
                return actor
        return None  # unknown workflow name

    return None


def _set_dotted_path(root, dotted, value):
    """Set a nested dict value by dotted path, creating intermediate dicts.

    Used by atomic-write. `dotted` must already be authorized by caller.
    Returns True on success, False if path traversal hit a non-dict.
    """
    parts = dotted.split(".")
    cursor = root
    for key in parts[:-1]:
        if not isinstance(cursor, dict):
            return False
        if key not in cursor or not isinstance(cursor.get(key), dict):
            cursor[key] = {}
        cursor = cursor[key]
    if not isinstance(cursor, dict):
        return False
    cursor[parts[-1]] = value
    return True


def _get_dotted_path(root, dotted, default=None):
    """Get a nested value by dotted path, or default."""
    parts = dotted.split(".")
    cursor = root
    for key in parts:
        if not isinstance(cursor, dict) or key not in cursor:
            return default
        cursor = cursor[key]
    return cursor


def _append_to_dotted_list(root, dotted, value):
    """Append `value` to the list at `dotted` path; create list if absent."""
    current = _get_dotted_path(root, dotted)
    if current is None:
        # Create empty list
        if not _set_dotted_path(root, dotted, []):
            return False
        current = _get_dotted_path(root, dotted)
    if not isinstance(current, list):
        return False
    current.append(value)
    return True


def _evaluate_guard(root, guard_expr):
    """Evaluate a simple guard expression of the form `path==value`.

    Supports:
        execution.runs.<id>.current_workflow==crawling
        execution.current_run_id==exec-2026-04-09
        path!=value

    Returns (ok: bool, reason: str).
    """
    if not guard_expr:
        return True, "no guard"
    # Find operator (order matters: != before ==)
    for op in ("!=", "=="):
        if op in guard_expr:
            left, right = guard_expr.split(op, 1)
            left = left.strip()
            right = right.strip()
            # Unwrap quoted string
            if len(right) >= 2 and right[0] in ('"', "'") and right[-1] == right[0]:
                right = right[1:-1]
            # Handle null keyword
            if right == "null":
                expected = None
            elif right.isdigit():
                expected = int(right)
            else:
                expected = right
            actual = _get_dotted_path(root, left)
            if op == "==":
                return actual == expected, f"{left}={actual!r} vs {expected!r}"
            else:
                return actual != expected, f"{left}={actual!r} vs {expected!r}"
    return False, f"SM-AW1: unsupported guard expression: {guard_expr}"


def cmd_init_execution(project_dir, actor):
    """Initialize the execution: root section in state.yaml.

    Idempotent: second call on an already-initialized SOT preserves existing
    runs/history. Creates default scaffolding only for missing keys.

    Requires --actor meta (only Meta may initialize the execution layer).
    """
    auth_err = _validate_actor(actor, "init-execution")
    if auth_err:
        return auth_err
    if actor != "meta":
        return {
            "valid": False,
            "error": (
                f"SM-AUTH3: only 'meta' actor may run --init-execution, "
                f"got '{actor}'"
            ),
        }

    # CE4 (Phase 0.1 L2 review fix): hard-fail if schema validation unavailable
    if not _HAS_EXECUTION_LIB or _validate_execution_section is None:
        return {
            "valid": False,
            "error": (
                "SM-AW6: schema validation unavailable. "
                "_execution_lib.py could not be imported. "
                "Refusing to initialize execution section without validation."
            ),
        }

    try:
        sot = _sot_path(project_dir)
        # State file must already exist; init-execution is an extension, not
        # a fresh workflow bootstrap.
        if not os.path.exists(sot):
            return {
                "valid": False,
                "error": (
                    f"SOT not found: {sot}. Use --init first to create the "
                    f"workflow section."
                ),
            }

        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            existing = data.get("execution") if isinstance(data, dict) else None
            if existing is None or not isinstance(existing, dict):
                # Create fresh execution section
                data["execution"] = {
                    "schema_version": EXECUTION_SCHEMA_VERSION,
                    "schema_migrations": [],
                    "current_run_id": None,
                    "runs": {},
                    "history": [],
                    "longitudinal_index": {
                        "daily": {}, "weekly": {}, "monthly": {},
                    },
                    "retention": {"hot_runs": 30, "warm_runs": 365},
                }
                action = "initialized"
            else:
                # Idempotent: fill in missing keys without wiping existing data
                defaults = {
                    "schema_version": EXECUTION_SCHEMA_VERSION,
                    "schema_migrations": [],
                    "current_run_id": None,
                    "runs": {},
                    "history": [],
                    "longitudinal_index": {
                        "daily": {}, "weekly": {}, "monthly": {},
                    },
                    "retention": {"hot_runs": 30, "warm_runs": 365},
                }
                for k, v in defaults.items():
                    if k not in existing:
                        existing[k] = v
                action = "reconciled"

            _write_sot_atomic(sot, data)

            # Phase 0.1 RW3: post-write E1-E15 schema validation
            result = {
                "valid": True,
                "action": f"execution_section_{action}",
                "schema_version": data["execution"]["schema_version"],
            }
            if _HAS_EXECUTION_LIB and _validate_execution_section is not None:
                warnings = _validate_execution_section(data["execution"])
                if warnings:
                    result["schema_warnings"] = warnings
            return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


def cmd_atomic_write(project_dir, actor, path, value_str, guard=None,
                     append_list=False):
    """Atomic check-and-write to a dotted path under execution.*.

    P1 Hallucination Prevention: TOCTOU-safe section writes. Guard and write
    occur inside the exclusive fcntl lock, so no intermediate state can
    change between guard evaluation and commit.

    Args:
        project_dir: project root
        actor: one of VALID_ACTORS
        path: dotted path (must start with execution.*)
        value_str: JSON-encoded value (string, int, bool, dict, list, null)
        guard: optional "path==value" guard expression
        append_list: if True, append value to list at path (else set)

    Returns JSON result dict.
    """
    # SM-AUTH1/2: Validate actor flag
    auth_err = _validate_actor(actor, "atomic-write")
    if auth_err:
        return auth_err

    # SM-AW5 (CE3 fix): validate path components before ANY other processing.
    path_err = _validate_path_components(path)
    if path_err:
        return path_err

    # CE4 (Phase 0.1 L2 review fix): schema validation MUST be available for
    # atomic-write. Hard-fail if _execution_lib import failed — silent
    # degradation would defeat P1 Supremacy.
    if not _HAS_EXECUTION_LIB or _validate_execution_section is None:
        return {
            "valid": False,
            "error": (
                "SM-AW6: schema validation unavailable. "
                "_execution_lib.py could not be imported. "
                "Atomic writes are refused to prevent silent P1 bypass."
            ),
        }

    # Parse the value
    try:
        value = json.loads(value_str) if value_str is not None else None
    except (json.JSONDecodeError, TypeError) as e:
        return {
            "valid": False,
            "error": f"SM-AW3: --value is not valid JSON: {e}",
        }

    # CE2 trace append-only enforcement: if target is a trace list, force
    # --append-list to prevent index-based mutation.
    if _is_trace_path(path) and not append_list:
        return {
            "valid": False,
            "error": (
                "SM-AW7: trace is append-only. Use --append-list when "
                "writing to execution.runs.<id>.trace"
            ),
        }

    try:
        sot = _sot_path(project_dir)
        if not os.path.exists(sot):
            return {"valid": False, "error": f"SOT not found: {sot}"}

        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: Freeze workflow.* (must come before authorization
            # because a user can target workflow.* intentionally)
            if path.startswith("workflow"):
                freeze_err = _check_workflow_freeze(data, context="atomic-write")
                if freeze_err:
                    return freeze_err
                # Unfrozen workflow writes are not supported via atomic-write
                return {
                    "valid": False,
                    "error": (
                        "SM-AW2: atomic-write targets execution.* only. "
                        "Use specific commands (--advance-step etc.) for "
                        "legacy workflow writes when execution layer is "
                        "not active."
                    ),
                }

            # SM-AUTH3: Section ownership
            authz_err = _check_write_authorization(actor, path)
            if authz_err:
                return authz_err

            # SM-AW1: Guard evaluation (TOCTOU protection)
            if guard:
                ok, reason = _evaluate_guard(data, guard)
                if not ok:
                    return {
                        "valid": False,
                        "error": f"SM-AW1: guard failed: {reason}",
                    }

            # Commit: set or append
            if append_list:
                if not _append_to_dotted_list(data, path, value):
                    return {
                        "valid": False,
                        "error": (
                            f"SM-AW4: append failed for path '{path}' "
                            f"(not a list or invalid traversal)"
                        ),
                    }
            else:
                if not _set_dotted_path(data, path, value):
                    return {
                        "valid": False,
                        "error": (
                            f"SM-AW4: set failed for path '{path}' "
                            f"(traversal hit non-dict)"
                        ),
                    }

            _write_sot_atomic(sot, data)

            # Phase 0.1 RW3: post-write E1-E15 schema validation
            result = {
                "valid": True,
                "action": "atomic_write",
                "actor": actor,
                "path": path,
                "mode": "append" if append_list else "set",
            }
            if _HAS_EXECUTION_LIB and _validate_execution_section is not None:
                warnings = _validate_execution_section(data.get("execution"))
                if warnings:
                    result["schema_warnings"] = warnings
            return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_read(project_dir):
    """Read SOT and return as JSON."""
    try:
        data, sot = _read_sot(project_dir)
        wf = _extract_wf(data)
        schema_warnings = _validate_schema(wf)
        return {
            "valid": True,
            "sot_path": sot,
            "workflow": wf,
            "schema_warnings": schema_warnings,
        }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def cmd_init(project_dir, workflow_name, total_steps):
    """Initialize a new SOT file."""
    sot = _sot_path(project_dir)
    if os.path.exists(sot):
        return {"valid": False, "error": f"SOT already exists: {sot}. Delete first to re-initialize."}

    data = {
        "workflow": {
            "name": workflow_name,
            "current_step": 1,
            "status": "in_progress",
            "total_steps": total_steps,
            "parent_genome": {
                "source": "AgenticWorkflow",
                "version": "2026-02-25",
                "inherited_dna": [
                    "absolute-criteria", "sot-pattern", "3-phase-structure",
                    "4-layer-qa", "safety-hooks", "adversarial-review",
                    "decision-log", "context-preservation",
                    "cross-step-traceability",
                ],
            },
            "outputs": {},
            "autopilot": {
                "enabled": False,
                "auto_approved_steps": [],
                "auto_approved_details": {},
            },
            "pending_human_action": {"step": None, "options": []},
            "verification": {"last_verified_step": 0, "retries": {}},
            "pacs": {
                "current_step_score": None,
                "dimensions": {"F": None, "C": None, "L": None},
                "weak_dimension": None,
                "pre_mortem_flag": None,
                "history": {},
            },
        }
    }

    os.makedirs(os.path.dirname(sot), exist_ok=True)
    _write_sot_atomic(sot, data)
    return {"valid": True, "sot_path": sot, "action": "initialized", "workflow_name": workflow_name}


def _check_gate_evidence(project_dir, step_num):
    """SM5: Check quality gate evidence files before allowing step advancement.

    P1 Hallucination Prevention: Makes it physically impossible to advance
    a step without quality gate evidence, regardless of LLM behavior.
    This is the single most critical anti-hallucination guard in the system.

    Checks:
        SM5a: verification-logs/step-N-verify.md must exist
        SM5b: pacs-logs/step-N-pacs.md must exist
        SM5c: pACS score must be >= 50 (RED zone blocks advancement)
        SM5d: review-logs/step-N-review.md must not have FAIL verdict (if exists)

    Returns:
        tuple: (block_dict_or_None, warnings_list)
        - block_dict: {"valid": False, "error": ...} if blocked, None if passed
        - warnings: list of non-blocking warning strings (e.g., parse failures)
    """
    warnings = []

    # SM5a: Verification log must exist
    verify_path = os.path.join(
        project_dir, "verification-logs", f"step-{step_num}-verify.md"
    )
    if not os.path.exists(verify_path):
        return {
            "valid": False,
            "error": (
                f"SM5a: verification log missing: verification-logs/step-{step_num}-verify.md. "
                f"Run Verification Gate before advancing. Use --force to override."
            ),
        }, warnings

    # SM5b: pACS log must exist
    pacs_path = os.path.join(
        project_dir, "pacs-logs", f"step-{step_num}-pacs.md"
    )
    if not os.path.exists(pacs_path):
        return {
            "valid": False,
            "error": (
                f"SM5b: pACS log missing: pacs-logs/step-{step_num}-pacs.md. "
                f"Run pACS Self-Rating before advancing. Use --force to override."
            ),
        }, warnings

    # SM5c: pACS score must not be RED (< 50)
    # D-7: 2-stage parsing matching _context_lib.py PA7 logic exactly.
    # Stage 1: "pACS = min(F, C, L) = 75" — explicit min formula (preferred)
    # Stage 2: "pACS = 75" — simple fallback (no min formula)
    try:
        with open(pacs_path, "r", encoding="utf-8") as f:
            pacs_content = f.read()
        reported_pacs = None
        min_match = _PACS_WITH_MIN_RE.search(pacs_content)
        if min_match:
            reported_pacs = int(min_match.group(1))
        else:
            simple_matches = _PACS_SIMPLE_RE.findall(pacs_content)
            if len(simple_matches) == 1:
                reported_pacs = int(simple_matches[0])
        if reported_pacs is not None and reported_pacs < 50:
            return {
                "valid": False,
                "error": (
                    f"SM5c: pACS score is {reported_pacs} (RED zone < 50). "
                    f"Rework required before advancing. Use --force to override."
                ),
            }, warnings
        if reported_pacs is None:
            # Non-blocking: ambiguous parse is not the same as confirmed RED.
            # Multiple pACS= matches or no matches both yield None
            # (same logic as _context_lib.py PA7).
            warnings.append(
                f"SM5c-warn: Could not extract pACS score from "
                f"pacs-logs/step-{step_num}-pacs.md. RED zone check skipped."
            )
    except (IOError, UnicodeDecodeError):
        warnings.append(
            f"SM5c-warn: Could not read pacs-logs/step-{step_num}-pacs.md. "
            f"RED zone check skipped."
        )

    # SM5d: Review verdict must not be FAIL (if review log exists)
    review_path = os.path.join(
        project_dir, "review-logs", f"step-{step_num}-review.md"
    )
    if os.path.exists(review_path):
        try:
            with open(review_path, "r", encoding="utf-8") as f:
                review_content = f.read()
            verdict_match = re.search(
                r'Verdict\s*:\s*\*?\*?\s*(PASS|FAIL)', review_content, re.IGNORECASE
            )
            if verdict_match and verdict_match.group(1).upper() == "FAIL":
                return {
                    "valid": False,
                    "error": (
                        f"SM5d: Review verdict is FAIL in review-logs/step-{step_num}-review.md. "
                        f"Address review issues before advancing. Use --force to override."
                    ),
                }, warnings
        except (IOError, UnicodeDecodeError):
            warnings.append(
                f"SM5d-warn: Could not read review-logs/step-{step_num}-review.md. "
                f"Review verdict check skipped."
            )

    return None, warnings  # All quality gate evidence checks passed


def _log_force_audit(project_dir, step_num):
    """H3: Write audit record when --force bypasses SM5 quality gates.

    Creates an append-only audit trail in autopilot-logs/ so every
    --force usage is traceable. Best-effort — failure does not block advance.
    """
    import datetime
    try:
        log_dir = os.path.join(project_dir, "autopilot-logs")
        os.makedirs(log_dir, exist_ok=True)
        audit_path = os.path.join(log_dir, "sm5-force-audit.jsonl")
        entry = json.dumps({
            "step": step_num,
            "timestamp": datetime.datetime.now().isoformat(),
            "action": "SM5 gate bypass via --force",
        }, ensure_ascii=False)
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass  # Audit failure must not block step advancement


def cmd_advance_step(project_dir, step_num, force=False):
    """Advance current_step from step_num to step_num+1.

    P1 Hallucination Prevention (SM5): For non-human steps, quality gate
    evidence files must exist before advancement is allowed. This makes it
    physically impossible for the LLM to skip quality gates.

    Use force=True only when the user explicitly instructs override.

    Check ordering: CR-1 → MD-1 → SM3 → SM4 → SM4b → SM5 → advance.
    SM5 runs inside lock after SOT validation to ensure correct error messages
    (e.g., invalid step_num yields CR-1 error, not SM5 file-not-found).
    """
    try:
        sot = _sot_path(project_dir)
        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: workflow.* hard freeze (Phase 0.1)
            freeze_err = _check_workflow_freeze(data, "advance-step")
            if freeze_err:
                return freeze_err

            wf = _extract_wf(data)

            # CR-1: Step range validation
            range_err = _validate_step_num(wf, step_num, "advance-step")
            if range_err:
                return range_err

            # MD-1: Cannot advance past total_steps
            total = wf.get("total_steps")
            if total is not None and isinstance(total, int) and step_num + 1 > total + 1:
                return {
                    "valid": False,
                    "error": f"SM-R2: advance would set current_step={step_num + 1}, but total_steps={total}. Workflow already complete.",
                }

            # SM3: Pre-condition — current_step must equal step_num
            cs = wf.get("current_step", 0)
            if cs != step_num:
                return {
                    "valid": False,
                    "error": f"SM3: current_step is {cs}, expected {step_num}. Cannot advance.",
                }

            # SM4: Pre-condition — step-N output must exist in outputs
            step_key = f"step-{step_num}"
            outputs = wf.get("outputs", {})
            if step_key not in outputs:
                return {
                    "valid": False,
                    "error": f"SM4: No output recorded for {step_key}. Record output first.",
                }

            # SM4b: Output file must exist on disk
            output_path = outputs[step_key]
            full_path = os.path.join(project_dir, output_path) if not os.path.isabs(output_path) else output_path
            if not os.path.exists(full_path):
                return {
                    "valid": False,
                    "error": f"SM4b: Output file does not exist: {output_path}",
                }

            # SM5: Quality gate evidence (non-human steps only)
            # P1 hallucination prevention: LLM cannot advance without gate evidence.
            # Runs inside lock after CR-1/SM3/SM4 to ensure correct error ordering.
            # Local file I/O only (~100μs) — lock duration impact is negligible.
            sm5_warnings = []
            if step_num not in HUMAN_STEPS and not force:
                gate_block, sm5_warnings = _check_gate_evidence(project_dir, step_num)
                if gate_block:
                    return gate_block

            # Advance
            wf["current_step"] = step_num + 1
            _write_sot_atomic(sot, data)

            result = {
                "valid": True,
                "action": "advanced",
                "previous_step": step_num,
                "current_step": step_num + 1,
            }
            if force and step_num not in HUMAN_STEPS:
                result["warning"] = "SM5 quality gate checks bypassed via --force"
                _log_force_audit(project_dir, step_num)
            if sm5_warnings:
                result["sm5_warnings"] = sm5_warnings
            return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


def cmd_record_output(project_dir, step_num, output_path):
    """Record output path for step-N in SOT."""
    try:
        sot = _sot_path(project_dir)
        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: workflow.* hard freeze (Phase 0.1)
            freeze_err = _check_workflow_freeze(data, "record-output")
            if freeze_err:
                return freeze_err

            wf = _extract_wf(data)

            # CR-1: Step range validation
            range_err = _validate_step_num(wf, step_num, "record-output")
            if range_err:
                return range_err

            # HI-1: Cannot record output for future steps
            cs = wf.get("current_step", 1)
            if step_num > cs:
                return {
                    "valid": False,
                    "error": f"SM-R3: Cannot record output for step-{step_num} when current_step={cs}. Future step outputs are forbidden.",
                }

            # SM6: File must exist and meet minimum size
            full_path = os.path.join(project_dir, output_path) if not os.path.isabs(output_path) else output_path
            if not os.path.exists(full_path):
                return {
                    "valid": False,
                    "error": f"SM6: Output file does not exist: {output_path}",
                }
            file_size = os.path.getsize(full_path)
            if file_size < MIN_OUTPUT_SIZE:
                return {
                    "valid": False,
                    "error": f"SM6: Output file too small: {file_size} bytes (min {MIN_OUTPUT_SIZE})",
                }

            # SM7: Path format validation
            if not isinstance(output_path, str) or not output_path.strip():
                return {"valid": False, "error": "SM7: Output path must be non-empty string"}

            # Record
            step_key = f"step-{step_num}"
            if "outputs" not in wf:
                wf["outputs"] = {}
            wf["outputs"][step_key] = output_path
            _write_sot_atomic(sot, data)

            return {
                "valid": True,
                "action": "output_recorded",
                "step": step_num,
                "output_path": output_path,
                "file_size": file_size,
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def cmd_update_pacs(project_dir, step_num, f_score, c_score, l_score):
    """Update pACS scores for step-N in SOT."""
    try:
        sot = _sot_path(project_dir)
        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: workflow.* hard freeze (Phase 0.1)
            freeze_err = _check_workflow_freeze(data, "update-pacs")
            if freeze_err:
                return freeze_err

            wf = _extract_wf(data)

            # CR-1: Step range validation
            range_err = _validate_step_num(wf, step_num, "update-pacs")
            if range_err:
                return range_err

            # SM10: Validate dimension ranges
            for name, val in [("F", f_score), ("C", c_score), ("L", l_score)]:
                if not isinstance(val, (int, float)) or not (0 <= val <= 100):
                    return {
                        "valid": False,
                        "error": f"SM10: {name}={val} must be int/float in [0,100]",
                    }

            # SM11: Compute min
            pacs_score = min(f_score, c_score, l_score)
            weak = "F" if f_score == pacs_score else ("C" if c_score == pacs_score else "L")

            # Determine zone
            if pacs_score < 50:
                zone = "RED"
            elif pacs_score < 70:
                zone = "YELLOW"
            else:
                zone = "GREEN"

            # Update
            if "pacs" not in wf:
                wf["pacs"] = {}
            wf["pacs"]["current_step_score"] = pacs_score
            wf["pacs"]["dimensions"] = {"F": f_score, "C": c_score, "L": l_score}
            wf["pacs"]["weak_dimension"] = weak

            if "history" not in wf["pacs"]:
                wf["pacs"]["history"] = {}
            wf["pacs"]["history"][f"step-{step_num}"] = {
                "score": pacs_score,
                "weak": weak,
            }

            _write_sot_atomic(sot, data)

            return {
                "valid": True,
                "action": "pacs_updated",
                "step": step_num,
                "pacs_score": pacs_score,
                "dimensions": {"F": f_score, "C": c_score, "L": l_score},
                "weak_dimension": weak,
                "zone": zone,
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def cmd_update_team(project_dir, team_json):
    """Update active_team state in SOT."""
    try:
        team_data = json.loads(team_json)
    except json.JSONDecodeError as e:
        return {"valid": False, "error": f"Invalid JSON: {e}"}

    try:
        sot = _sot_path(project_dir)
        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: workflow.* hard freeze (Phase 0.1)
            freeze_err = _check_workflow_freeze(data, "update-team")
            if freeze_err:
                return freeze_err

            wf = _extract_wf(data)

            # SM8: Validate required fields
            name = team_data.get("name")
            if not name or not isinstance(name, str):
                return {"valid": False, "error": "SM8: active_team.name must be non-empty string"}

            status = team_data.get("status", "partial")
            if status not in VALID_TEAM_STATUSES:
                return {"valid": False, "error": f"SM8: status '{status}' not in {VALID_TEAM_STATUSES}"}

            tc = team_data.get("tasks_completed", [])
            tp = team_data.get("tasks_pending", [])
            cs = team_data.get("completed_summaries", {})

            # SM9: tasks_completed must be subset of all tasks
            if not isinstance(tc, list) or not isinstance(tp, list):
                return {"valid": False, "error": "SM9: tasks_completed and tasks_pending must be lists"}

            # CR-3: Task overlap check — no task can be both completed AND pending
            overlap = set(tc) & set(tp)
            if overlap:
                return {
                    "valid": False,
                    "error": f"SM-R4: Tasks appear in both completed and pending: {sorted(overlap)}. Logical contradiction.",
                }

            # SM9b: completed_summaries keys must be subset of tasks_completed
            if isinstance(cs, dict):
                for k in cs:
                    if k not in tc:
                        return {
                            "valid": False,
                            "error": f"SM9b: completed_summaries key '{k}' not in tasks_completed",
                        }

            # Update
            wf["active_team"] = {
                "name": name,
                "status": status,
                "tasks_completed": tc,
                "tasks_pending": tp,
                "completed_summaries": cs,
            }

            # If all_completed, move to completed_teams
            if status == "all_completed":
                if "completed_teams" not in wf:
                    wf["completed_teams"] = []
                wf["completed_teams"].append(wf["active_team"].copy())

            _write_sot_atomic(sot, data)

            return {
                "valid": True,
                "action": "team_updated",
                "team_name": name,
                "status": status,
                "tasks_completed_count": len(tc),
                "tasks_pending_count": len(tp),
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def cmd_set_autopilot(project_dir, enabled_str):
    """Set autopilot.enabled to true or false."""
    # SM-AP1: Validate input value
    if enabled_str not in ("true", "false"):
        return {"valid": False, "error": f"SM-AP1: enabled must be 'true' or 'false', got '{enabled_str}'"}
    enabled = enabled_str == "true"
    try:
        sot = _sot_path(project_dir)
        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: workflow.* hard freeze (Phase 0.1)
            freeze_err = _check_workflow_freeze(data, "set-autopilot")
            if freeze_err:
                return freeze_err

            wf = _extract_wf(data)

            # Ensure autopilot section exists
            if "autopilot" not in wf or not isinstance(wf.get("autopilot"), dict):
                wf["autopilot"] = {"enabled": False, "auto_approved_steps": []}

            old_val = wf["autopilot"].get("enabled", False)
            wf["autopilot"]["enabled"] = enabled
            _write_sot_atomic(sot, data)

            # SM-AP2: Generate activation Decision Log on first enable (best-effort)
            if enabled and not old_val:
                _generate_activation_log(project_dir, wf)
                # SM-AP3: Reset stall tracker to prevent false positives on re-activation
                tracker_path = os.path.join(project_dir, "autopilot-logs", ".progress-tracker")
                try:
                    if os.path.exists(tracker_path):
                        os.remove(tracker_path)
                except Exception:
                    pass  # Non-blocking

            return {
                "valid": True,
                "action": "autopilot_set",
                "previous": old_val,
                "enabled": enabled,
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def _generate_activation_log(project_dir, wf):
    """Generate autopilot-logs/activation-decision.md on first activation (best-effort).

    P1 Compliance: Deterministic file write.
    Non-blocking: Swallows all exceptions.
    """
    import datetime
    logs_dir = os.path.join(project_dir, "autopilot-logs")
    log_path = os.path.join(logs_dir, "activation-decision.md")
    if os.path.exists(log_path):
        return  # Already exists — don't overwrite
    try:
        os.makedirs(logs_dir, exist_ok=True)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        workflow_name = wf.get("name", "N/A")
        current_step = wf.get("current_step", "?")
        total_steps = wf.get("total_steps", "?")
        content = (
            f"# Autopilot Activation Decision Log\n\n"
            f"- **Activation Time**: {now}\n"
            f"- **Workflow**: {workflow_name}\n"
            f"- **Current Step at Activation**: Step {current_step} / {total_steps}\n"
            f"- **Decision**: Enable autopilot mode for automated (human) step approval\n"
            f"- **Rationale**: User explicitly requested autopilot/auto mode. "
            f"All (human) steps will be auto-approved with quality-maximizing defaults "
            f"per Absolute Criterion 1. Hook exit code 2 blocking remains enforced.\n"
            f"- **Constraints**: (hook) exit code 2 still blocks. "
            f"Quality gates (L0/L1/L1.5/L2) are not bypassed.\n"
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass  # Non-blocking


def cmd_add_auto_approved(project_dir, step_num):
    """Record a human step as auto-approved in autopilot.auto_approved_steps."""
    # SM-AA1: Must be a human step
    if step_num not in HUMAN_STEPS:
        return {
            "valid": False,
            "error": f"SM-AA1: step {step_num} is not a human step. HUMAN_STEPS = {sorted(HUMAN_STEPS)}",
        }
    try:
        sot = _sot_path(project_dir)
        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: workflow.* hard freeze (Phase 0.1)
            freeze_err = _check_workflow_freeze(data, "add-auto-approved")
            if freeze_err:
                return freeze_err

            wf = _extract_wf(data)

            # SM-AA2: autopilot must be enabled
            ap = wf.get("autopilot")
            if not isinstance(ap, dict) or not ap.get("enabled"):
                return {
                    "valid": False,
                    "error": "SM-AA2: autopilot is not enabled. Enable with --set-autopilot true first.",
                }

            # SM-AA4: Cannot approve future steps
            cs = wf.get("current_step", 0)
            if step_num > cs:
                return {
                    "valid": False,
                    "error": f"SM-AA4: cannot approve future step {step_num} (current_step={cs})",
                }

            # SM-AA3: Idempotent — already present is success
            aas = ap.get("auto_approved_steps", [])
            if not isinstance(aas, list):
                aas = []
            if step_num in aas:
                return {
                    "valid": True,
                    "action": "already_approved",
                    "step": step_num,
                    "auto_approved_steps": sorted(aas),
                }

            aas.append(step_num)
            aas.sort()
            ap["auto_approved_steps"] = aas

            # SM-AA5: Record approval details for audit trail
            import datetime
            details = ap.get("auto_approved_details", {})
            if not isinstance(details, dict):
                details = {}
            details[str(step_num)] = {
                "timestamp": datetime.datetime.now().isoformat(),
                "decision_log": f"autopilot-logs/step-{step_num}-decision.md",
            }
            ap["auto_approved_details"] = details

            _write_sot_atomic(sot, data)

            return {
                "valid": True,
                "action": "auto_approved",
                "step": step_num,
                "auto_approved_steps": aas,
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


def cmd_set_status(project_dir, new_status):
    """Set workflow status (e.g., in_progress → completed).

    SM-ST1: "completed" requires current_step >= total_steps to prevent
    premature completion marking that would mis-route /start to /run.
    """
    if new_status not in VALID_STATUSES:
        return {"valid": False, "error": f"Invalid status '{new_status}'. Must be one of: {sorted(VALID_STATUSES)}"}
    try:
        sot = _sot_path(project_dir)
        with _SOTLock(sot, exclusive=True):
            data, sot = _read_sot_unlocked(project_dir)

            # SM-WFR1: workflow.* hard freeze (Phase 0.1)
            freeze_err = _check_workflow_freeze(data, "set-status")
            if freeze_err:
                return freeze_err

            wf = _extract_wf(data)
            old_status = wf.get("status", "unknown")

            # SM-ST1: Cross-validate "completed" against current_step/total_steps
            if new_status == "completed":
                cs = wf.get("current_step", 0)
                ts = wf.get("total_steps")
                if isinstance(ts, int) and isinstance(cs, int) and cs < ts:
                    return {
                        "valid": False,
                        "error": (
                            f"SM-ST1: Cannot set status to 'completed' — "
                            f"current_step={cs} < total_steps={ts}. "
                            f"Complete remaining steps first."
                        ),
                    }

            wf["status"] = new_status
            _write_sot_atomic(sot, data)
            return {
                "valid": True,
                "action": "status_updated",
                "previous_status": old_status,
                "new_status": new_status,
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


# ---------------------------------------------------------------------------
# DCI (Deep Content Intelligence) — independent workflow helpers (v1.0+)
# ---------------------------------------------------------------------------
#
# Canonical SOT path (v1.0+): execution.runs.{id}.workflows.dci.*
# Legacy SOT path (read-only): execution.runs.{id}.workflows.master.phases.dci.*
#
# DCI is a standalone independent workflow (promoted from WF4-Master Phase 4
# in ADR-073). The "dci" actor owns workflows.dci.* per WORKFLOW_ACTOR_MAP.
# Historical runs at the legacy path remain readable by _context_lib.py and
# validate_sot_schema() S10 (dual-location support). These helpers now write
# ONLY to the canonical path.
#
# D-7 intentional duplication — DCI_LAYERS below must match
# src/config/constants.py:DCI_LAYERS and
# prompt/execution-workflows/dci.md §Phases. Changing one requires syncing all.

_DCI_LAYERS = frozenset({
    "L-1_external",
    "L0_discourse",
    "L1_semantic",
    "L1.5_meaning",
    "L2_relations",
    "L3_kg_hypergraph",
    "L4_cross_document",
    "L5_psycho_style",
    "L6_triadic_synthesis",
    "L7_graph_of_thought",
    "L8_monte_carlo",
    "L9_metacognitive",
    "L10_final_report",
    "L11_dashboard",
})
_DCI_VALID_STATUSES = frozenset({"pending", "running", "completed", "failed", "skipped"})


def cmd_dci_set_layer(project_dir, run_id, layer_id, status, elapsed=None,
                      article_count=None, notes=None):
    """Write DCI layer state to the canonical SOT path (v1.0+).

    Path: ``execution.runs.<run_id>.workflows.dci.layers.<layer_id>``

    Invariants enforced before the atomic write:
        SM-DCI1: layer_id must be in _DCI_LAYERS (14 valid IDs)
        SM-DCI2: status must be in _DCI_VALID_STATUSES
        SM-DCI3: elapsed (if provided) must be non-negative number
        SM-DCI4: article_count (if provided) must be non-negative int

    Semantic validation (SG-Superhuman thresholds) is src/dci/'s job, not
    this helper — we only check structural shape. Write is serialized via
    the existing cmd_atomic_write path, so exclusive fcntl lock applies.

    Args:
        project_dir: project root
        run_id: execution run identifier
        layer_id: one of 14 DCI layer IDs
        status: pending | running | completed | failed | skipped
        elapsed: seconds spent in this layer (optional)
        article_count: articles processed (optional)
        notes: free-form note string (optional)

    Returns JSON result dict.
    """
    # SM-DCI1
    if layer_id not in _DCI_LAYERS:
        return {
            "valid": False,
            "error": (
                f"SM-DCI1: unknown DCI layer '{layer_id}'. "
                f"Must be one of: {sorted(_DCI_LAYERS)}"
            ),
        }
    # SM-DCI2
    if status not in _DCI_VALID_STATUSES:
        return {
            "valid": False,
            "error": (
                f"SM-DCI2: invalid status '{status}'. "
                f"Must be one of: {sorted(_DCI_VALID_STATUSES)}"
            ),
        }
    # SM-DCI3 / SM-DCI4
    if elapsed is not None:
        if not isinstance(elapsed, (int, float)) or elapsed < 0:
            return {"valid": False, "error": "SM-DCI3: elapsed must be non-negative"}
    if article_count is not None:
        if not isinstance(article_count, int) or article_count < 0:
            return {
                "valid": False,
                "error": "SM-DCI4: article_count must be non-negative int",
            }
    if not isinstance(run_id, str) or not run_id.strip():
        return {"valid": False, "error": "SM-DCI5: run_id must be non-empty string"}

    # Build the layer entry — only include provided fields (caller can
    # incrementally update; absent fields don't overwrite).
    entry = {"status": status}
    if elapsed is not None:
        entry["elapsed_seconds"] = round(float(elapsed), 3)
    if article_count is not None:
        entry["article_count"] = article_count
    if notes is not None:
        entry["notes"] = str(notes)[:512]  # cap length

    # SOT path segments cannot contain dots (SM-AW5 forbids '.' in segments
    # because '.' is the delimiter itself). Canonical layer_id "L1.5_meaning"
    # is encoded as "L1_5_meaning" for SOT writes; readers round-trip back.
    safe_layer_id = layer_id.replace(".", "_")
    path = (
        f"execution.runs.{run_id}.workflows.dci."
        f"layers.{safe_layer_id}"
    )
    value_str = json.dumps(entry, ensure_ascii=False)

    # Delegate to atomic-write with dci actor (v1.0+ independent workflow)
    return cmd_atomic_write(
        project_dir,
        actor="dci",
        path=path,
        value_str=value_str,
        guard=None,
        append_list=False,
    )


def cmd_dci_set_gate(project_dir, run_id, gate_name, gate_value):
    """Write SG-Superhuman gate result to canonical SOT path (v1.0+).

    Path: ``execution.runs.<run_id>.workflows.dci.semantic_gates.<gate_name>``

    No value shape is enforced here — src/dci/ validators compute the
    structured gate dict (pass/fail, score, threshold, evidence). This
    helper is the thin write path. DCI actor authorization applies.

    Args:
        project_dir: project root
        run_id: execution run id
        gate_name: SG identifier (e.g., "SG-Superhuman", "char_coverage")
        gate_value: dict or scalar — serialized as JSON

    Returns JSON result dict.
    """
    if not isinstance(gate_name, str) or not gate_name.strip():
        return {"valid": False, "error": "SM-DCI6: gate_name must be non-empty string"}
    if not isinstance(run_id, str) or not run_id.strip():
        return {"valid": False, "error": "SM-DCI5: run_id must be non-empty string"}

    path = (
        f"execution.runs.{run_id}.workflows.dci."
        f"semantic_gates.{gate_name}"
    )
    try:
        value_str = json.dumps(gate_value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return {"valid": False, "error": f"SM-DCI7: gate_value not JSON-serializable: {e}"}

    return cmd_atomic_write(
        project_dir,
        actor="dci",
        path=path,
        value_str=value_str,
        guard=None,
        append_list=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="SOT Manager — P1 deterministic SOT operations")
    parser.add_argument("--project-dir", required=True, help="Project root directory")
    parser.add_argument("--read", action="store_true", help="Read and validate SOT")
    parser.add_argument("--init", action="store_true", help="Initialize new SOT")
    parser.add_argument("--workflow-name", default="", help="Workflow name (for --init)")
    parser.add_argument("--total-steps", type=int, default=20, help="Total steps (for --init)")
    parser.add_argument("--advance-step", type=int, help="Advance from step N to N+1")
    parser.add_argument("--record-output", nargs=2, metavar=("STEP", "PATH"), help="Record output for step")
    parser.add_argument("--update-pacs", type=int, metavar="STEP", help="Update pACS for step")
    parser.add_argument("--F", type=float, help="F dimension score")
    parser.add_argument("--C", type=float, help="C dimension score")
    parser.add_argument("--L", type=float, help="L dimension score")
    parser.add_argument("--update-team", metavar="JSON", help="Update active_team (JSON string)")
    parser.add_argument("--set-status", metavar="STATUS", help="Set workflow status (in_progress, completed, failed, etc.)")
    parser.add_argument("--set-autopilot", metavar="BOOL", help="Set autopilot enabled (true/false)")
    parser.add_argument("--add-auto-approved", type=int, metavar="STEP", help="Record a human step as auto-approved")
    parser.add_argument("--force", action="store_true",
                        help="Force advance step — bypass SM5 quality gate evidence checks. "
                             "Use ONLY when the user explicitly instructs override.")

    # Phase 0.1 — execution layer extensions
    parser.add_argument("--atomic-write", action="store_true",
                        help="Atomic check-and-write to a dotted path under execution.*")
    parser.add_argument("--init-execution", action="store_true",
                        help="Initialize the execution: root section (requires --actor meta)")
    parser.add_argument("--actor", default=None,
                        help="Actor for write authorization: meta|w1|w2|w3|master")
    parser.add_argument("--path", default=None,
                        help="Dotted path for --atomic-write (e.g., execution.current_run_id)")
    parser.add_argument("--value", default=None,
                        help="JSON-encoded value for --atomic-write")
    parser.add_argument("--guard", default=None,
                        help="Optional guard expression for --atomic-write (path==value)")
    parser.add_argument("--append-list", action="store_true",
                        help="For --atomic-write: append value to list at path")

    # WF4-DCI v0.5 — master-actor helpers for Phase 4 state writes
    parser.add_argument("--dci-set-layer", action="store_true",
                        help="Write DCI layer state to workflows.master.phases.dci.layers.<id>")
    parser.add_argument("--dci-set-gate", action="store_true",
                        help="Write SG-Superhuman gate result to workflows.master.phases.dci.semantic_gates.<name>")
    parser.add_argument("--run-id", default=None,
                        help="Execution run id (for --dci-set-layer / --dci-set-gate)")
    parser.add_argument("--layer-id", default=None,
                        help="DCI layer id (for --dci-set-layer); one of 14 IDs")
    parser.add_argument("--status", default=None,
                        help="Status (for --dci-set-layer): pending|running|completed|failed|skipped")
    parser.add_argument("--elapsed", type=float, default=None,
                        help="Optional elapsed seconds (for --dci-set-layer)")
    parser.add_argument("--article-count", type=int, default=None,
                        help="Optional article count (for --dci-set-layer)")
    parser.add_argument("--notes", default=None,
                        help="Optional notes (for --dci-set-layer)")
    parser.add_argument("--gate-name", default=None,
                        help="Gate name (for --dci-set-gate)")
    parser.add_argument("--gate-value", default=None,
                        help="JSON-encoded gate value (for --dci-set-gate)")

    args = parser.parse_args()

    # Phase 0.1 command dispatch (execution layer) — take precedence
    if args.init_execution:
        result = cmd_init_execution(args.project_dir, args.actor)
    elif args.dci_set_layer:
        if not args.run_id or not args.layer_id or not args.status:
            result = {
                "valid": False,
                "error": "--dci-set-layer requires --run-id, --layer-id, --status",
            }
        else:
            result = cmd_dci_set_layer(
                args.project_dir,
                run_id=args.run_id,
                layer_id=args.layer_id,
                status=args.status,
                elapsed=args.elapsed,
                article_count=args.article_count,
                notes=args.notes,
            )
    elif args.dci_set_gate:
        if not args.run_id or not args.gate_name or args.gate_value is None:
            result = {
                "valid": False,
                "error": "--dci-set-gate requires --run-id, --gate-name, --gate-value",
            }
        else:
            try:
                gate_value = json.loads(args.gate_value)
            except (json.JSONDecodeError, TypeError) as e:
                result = {"valid": False, "error": f"--gate-value not valid JSON: {e}"}
            else:
                result = cmd_dci_set_gate(
                    args.project_dir,
                    run_id=args.run_id,
                    gate_name=args.gate_name,
                    gate_value=gate_value,
                )
    elif args.atomic_write:
        result = cmd_atomic_write(
            args.project_dir,
            actor=args.actor,
            path=args.path,
            value_str=args.value,
            guard=args.guard,
            append_list=args.append_list,
        )
    elif args.read:
        result = cmd_read(args.project_dir)
    elif args.init:
        result = cmd_init(args.project_dir, args.workflow_name, args.total_steps)
    elif args.advance_step is not None:
        result = cmd_advance_step(args.project_dir, args.advance_step, force=args.force)
    elif args.record_output:
        step = int(args.record_output[0])
        path = args.record_output[1]
        result = cmd_record_output(args.project_dir, step, path)
    elif args.update_pacs is not None:
        if args.F is None or args.C is None or args.L is None:
            result = {"valid": False, "error": "pACS update requires --F, --C, --L"}
        else:
            result = cmd_update_pacs(args.project_dir, args.update_pacs, args.F, args.C, args.L)
    elif args.update_team:
        result = cmd_update_team(args.project_dir, args.update_team)
    elif args.set_status:
        result = cmd_set_status(args.project_dir, args.set_status)
    elif args.set_autopilot:
        result = cmd_set_autopilot(args.project_dir, args.set_autopilot)
    elif args.add_auto_approved is not None:
        result = cmd_add_auto_approved(args.project_dir, args.add_auto_approved)
    else:
        result = {"valid": False, "error": "No command specified. Use --read, --init, --init-execution, --atomic-write, --advance-step, --record-output, --update-pacs, --update-team, --set-status, --set-autopilot, or --add-auto-approved"}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
