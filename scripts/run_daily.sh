#!/usr/bin/env bash
# =============================================================================
# GlobalNews Crawling & Analysis System -- Daily Pipeline Runner
#
# Triggered by cron at 02:00 AM daily. Executes the full crawl + analysis
# pipeline with comprehensive error handling, lock file management, health
# checks, log rotation, and timeout protection.
#
# Usage:
#   scripts/run_daily.sh              # Normal execution
#   scripts/run_daily.sh --dry-run    # Validate without execution
#   scripts/run_daily.sh --date 2026-02-25  # Specific date
#
# CRON SETUP (add via: crontab -e):
#   0 2 * * * cd /path/to/project && .venv/bin/python scripts/run_daily.sh >> data/logs/cron.log 2>&1
#   Ensure .venv/bin/python points to Python 3.12-3.13 (NOT system 3.14+)
#
# Exit codes:
#   0 -- Pipeline completed successfully
#   1 -- Pipeline failed (see error logs)
#   2 -- Pre-run health check failed
#   3 -- Lock acquisition failed (another instance running)
#   4 -- Timeout (pipeline exceeded 4-hour limit)
#
# Reference:
#   PRD Section 12.2 -- auto-execution success rate >= 95%
#   Step 5 Architecture Blueprint, Section 8 (Operational Requirements)
# =============================================================================

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

# Auto-detect project root (directory containing main.py)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Date handling
TARGET_DATE="${2:-$(date +%Y-%m-%d)}"
DRY_RUN=false

# Timeout: 8 hours (28800 seconds) — extended from 4h to accommodate
# WF5 Newspaper daily edition (ADR-083): 14 editorial desks × Claude CLI
# adds ~1.5-2h on top of the existing ~4h pipeline.
PIPELINE_TIMEOUT=28800

# Log paths
LOG_DIR="${PROJECT_DIR}/data/logs"
DAILY_LOG="${LOG_DIR}/daily/$(date +%Y-%m-%d)-daily.log"
ERROR_LOG="${LOG_DIR}/errors.log"
ALERT_DIR="${LOG_DIR}/alerts"

# Lock name
LOCK_NAME="daily"

# Python interpreter — overridden by activate_venv() when .venv is found
PYTHON="${PYTHON:-python3}"

# =============================================================================
# Argument Parsing
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --date)
            TARGET_DATE="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--dry-run] [--date YYYY-MM-DD]" >&2
            exit 1
            ;;
    esac
done

# =============================================================================
# Utility Functions
# =============================================================================

timestamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log_info() {
    local msg="[$(timestamp)] [INFO]  $1"
    echo "$msg" | tee -a "${DAILY_LOG}"
}

log_error() {
    local msg="[$(timestamp)] [ERROR] $1"
    echo "$msg" | tee -a "${DAILY_LOG}" >&2
    echo "$msg" >> "${ERROR_LOG}"
}

log_warn() {
    local msg="[$(timestamp)] [WARN]  $1"
    echo "$msg" | tee -a "${DAILY_LOG}"
}

write_alert() {
    local alert_file="${ALERT_DIR}/$(date +%Y-%m-%d)-daily-failure.log"
    {
        echo "=========================================="
        echo "DAILY PIPELINE FAILURE ALERT"
        echo "=========================================="
        echo "Date:    ${TARGET_DATE}"
        echo "Time:    $(timestamp)"
        echo "Host:    $(hostname)"
        echo "PID:     $$"
        echo ""
        echo "Error:"
        echo "  $1"
        echo ""
        echo "Last 50 lines of daily log:"
        if [[ -f "${DAILY_LOG}" ]]; then
            tail -50 "${DAILY_LOG}"
        else
            echo "  (no daily log available)"
        fi
        echo ""
        echo "=========================================="
    } >> "${alert_file}"
    log_error "Alert written to: ${alert_file}"
}

# =============================================================================
# Virtual Environment Detection and Activation
# =============================================================================

activate_venv() {
    # Try common virtualenv locations
    local venv_candidates=(
        "${PROJECT_DIR}/.venv"
        "${PROJECT_DIR}/venv"
        "${PROJECT_DIR}/env"
    )

    for venv_path in "${venv_candidates[@]}"; do
        if [[ -f "${venv_path}/bin/activate" ]]; then
            log_info "Activating virtualenv: ${venv_path}"
            # shellcheck disable=SC1091
            source "${venv_path}/bin/activate"
            PYTHON="${venv_path}/bin/python"
            return 0
        fi
    done

    # No venv found -- check if system Python has required packages
    if "${PYTHON}" -c "import yaml; import requests" 2>/dev/null; then
        log_warn "No virtualenv found. Using system Python: $(which "${PYTHON}")"
        return 0
    fi

    log_error "No virtualenv found and system Python missing critical deps."
    log_error "Searched: ${venv_candidates[*]}"
    log_error "Install: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    return 1
}

# =============================================================================
# Lock Management (via Python self_recovery module)
# =============================================================================

acquire_lock() {
    log_info "Acquiring lock: ${LOCK_NAME}"
    local result
    result=$("${PYTHON}" -m src.utils.self_recovery \
        --project-dir "${PROJECT_DIR}" \
        --acquire-lock "${LOCK_NAME}" 2>/dev/null) || true

    if echo "${result}" | "${PYTHON}" -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('acquired') else 1)" 2>/dev/null; then
        log_info "Lock acquired successfully"
        return 0
    else
        log_error "Failed to acquire lock. Another instance may be running."
        return 1
    fi
}

release_lock() {
    log_info "Releasing lock: ${LOCK_NAME}"
    "${PYTHON}" -m src.utils.self_recovery \
        --project-dir "${PROJECT_DIR}" \
        --force-release-lock "${LOCK_NAME}" 2>/dev/null || true
}

# =============================================================================
# Health Check
# =============================================================================

run_health_check() {
    log_info "Running pre-run health checks..."
    local result
    result=$("${PYTHON}" -m src.utils.self_recovery \
        --project-dir "${PROJECT_DIR}" \
        --health-check 2>/dev/null)
    local rc=$?

    if [[ ${rc} -ne 0 ]]; then
        log_error "Health check failed:"
        echo "${result}" | tee -a "${DAILY_LOG}" >&2
        return 1
    fi

    # Log disk space
    local disk_free
    disk_free=$(echo "${result}" | "${PYTHON}" -c "import sys,json; print(json.load(sys.stdin).get('disk_free_gb', '?'))" 2>/dev/null || echo "?")
    log_info "Health check passed. Disk free: ${disk_free} GB"
    return 0
}

# =============================================================================
# Log Rotation
# =============================================================================

rotate_logs() {
    log_info "Running log rotation..."
    local daily_log_dir="${LOG_DIR}/daily"

    if [[ ! -d "${daily_log_dir}" ]]; then
        return 0
    fi

    # Calculate directory size in bytes
    local dir_size
    dir_size=$(du -sk "${daily_log_dir}" 2>/dev/null | cut -f1 || echo "0")
    local dir_size_mb=$((dir_size / 1024))

    log_info "Daily log directory size: ${dir_size_mb} MB"

    # Rotate if > 500MB
    if [[ ${dir_size_mb} -gt 500 ]]; then
        log_info "Log directory exceeds 500MB. Removing logs older than 30 days..."
        find "${daily_log_dir}" -type f -name "*.log" -mtime +30 -delete 2>/dev/null || true
        local new_size
        new_size=$(du -sk "${daily_log_dir}" 2>/dev/null | cut -f1 || echo "0")
        log_info "Log directory after rotation: $((new_size / 1024)) MB"
    fi

    # Also run the Python cleanup for data logs
    "${PYTHON}" -m src.utils.self_recovery \
        --project-dir "${PROJECT_DIR}" \
        --cleanup 2>/dev/null || true
}

# =============================================================================
# Cleanup
# =============================================================================

cleanup_on_exit() {
    local exit_code=$?
    release_lock
    if [[ ${exit_code} -ne 0 ]]; then
        log_error "Pipeline exited with code ${exit_code}"
    fi
}

# =============================================================================
# Main Pipeline Execution
# =============================================================================

run_pipeline() {
    local mode_args=("--mode" "full" "--date" "${TARGET_DATE}")

    if [[ "${DRY_RUN}" == "true" ]]; then
        mode_args+=("--dry-run")
        log_info "DRY RUN mode enabled"
    fi

    log_info "Starting pipeline: ${PYTHON} main.py ${mode_args[*]}"
    log_info "Target date: ${TARGET_DATE}"
    log_info "Project dir: ${PROJECT_DIR}"
    log_info "PID: $$"

    local start_time
    start_time=$(date +%s)

    # Execute with timeout
    local pipeline_exit_code=0
    if command -v timeout >/dev/null 2>&1; then
        # GNU timeout (Linux)
        timeout "${PIPELINE_TIMEOUT}" "${PYTHON}" "${PROJECT_DIR}/main.py" "${mode_args[@]}" \
            >> "${DAILY_LOG}" 2>&1 || pipeline_exit_code=$?
    elif command -v gtimeout >/dev/null 2>&1; then
        # GNU timeout via coreutils (macOS with brew install coreutils)
        gtimeout "${PIPELINE_TIMEOUT}" "${PYTHON}" "${PROJECT_DIR}/main.py" "${mode_args[@]}" \
            >> "${DAILY_LOG}" 2>&1 || pipeline_exit_code=$?
    else
        # Fallback: background process with manual timeout check
        "${PYTHON}" "${PROJECT_DIR}/main.py" "${mode_args[@]}" \
            >> "${DAILY_LOG}" 2>&1 &
        local bg_pid=$!

        # Wait for completion or timeout
        local elapsed=0
        while kill -0 "${bg_pid}" 2>/dev/null; do
            sleep 10
            elapsed=$((elapsed + 10))
            if [[ ${elapsed} -ge ${PIPELINE_TIMEOUT} ]]; then
                log_error "Pipeline timed out after ${PIPELINE_TIMEOUT}s. Killing PID ${bg_pid}"
                kill -TERM "${bg_pid}" 2>/dev/null || true
                sleep 5
                kill -KILL "${bg_pid}" 2>/dev/null || true
                pipeline_exit_code=124  # Matches GNU timeout exit code
                break
            fi
        done

        if [[ ${pipeline_exit_code} -eq 0 ]]; then
            wait "${bg_pid}" 2>/dev/null || pipeline_exit_code=$?
        fi
    fi

    local end_time
    end_time=$(date +%s)
    local elapsed=$((end_time - start_time))
    local elapsed_min=$((elapsed / 60))

    if [[ ${pipeline_exit_code} -eq 124 ]]; then
        log_error "Pipeline TIMED OUT after ${elapsed_min} minutes (limit: $((PIPELINE_TIMEOUT / 60)) min)"
        write_alert "Pipeline timed out after ${elapsed_min} minutes"
        return 4
    elif [[ ${pipeline_exit_code} -ne 0 ]]; then
        log_error "Pipeline FAILED with exit code ${pipeline_exit_code} after ${elapsed_min} minutes"
        write_alert "Pipeline failed with exit code ${pipeline_exit_code}"
        return 1
    fi

    log_info "Pipeline completed successfully in ${elapsed_min} minutes"
    return 0
}

# =============================================================================
# Entry Point
# =============================================================================

main() {
    # Ensure log directories exist
    mkdir -p "${LOG_DIR}/daily" "${LOG_DIR}/alerts" "${LOG_DIR}/cron"

    log_info "============================================"
    log_info "GlobalNews Daily Pipeline -- START"
    log_info "============================================"
    log_info "Date: $(date -u)"
    log_info "Target: ${TARGET_DATE}"
    log_info "Host: $(hostname)"
    log_info "Dry run: ${DRY_RUN}"

    # Set trap for cleanup on exit
    trap cleanup_on_exit EXIT

    # Change to project directory
    cd "${PROJECT_DIR}"

    # Step 1: Activate virtual environment
    if ! activate_venv; then
        write_alert "Virtual environment activation failed"
        exit 2
    fi

    # Step 2: Health check
    if ! run_health_check; then
        write_alert "Pre-run health check failed"
        exit 2
    fi

    # Step 3: Acquire lock
    if ! acquire_lock; then
        write_alert "Lock acquisition failed -- another instance running"
        # Override the trap since we do not hold the lock
        trap - EXIT
        exit 3
    fi

    # Step 4: Run the pipeline
    local pipeline_result=0
    run_pipeline || pipeline_result=$?

    # Step 5: Post-run log rotation
    rotate_logs

    # Step 6: Trigger LLM Wiki ingest (on success only)
    if [[ ${pipeline_result} -eq 0 ]]; then
        local wiki_script="/Users/cys/Desktop/CYSjavis/llm-wiki-environmentscanning/scripts/auto-wiki-ingest.sh"
        if [[ -x "${wiki_script}" ]]; then
            log_info "Triggering LLM Wiki ingest..."
            nohup bash "${wiki_script}" >> "${LOG_DIR}/daily/wiki-trigger-$(date +%Y-%m-%d).log" 2>&1 &
            log_info "LLM Wiki ingest triggered in background (PID: $!)"
        else
            log_warn "LLM Wiki ingest script not found or not executable: ${wiki_script}"
        fi
    fi

    # Step 6.3: Generate W2/W3 narrative reports via existing agents
    # (ADR-081). main.py --mode full runs the pure Python pipeline which
    # bypasses `@analysis-reporter` and `@insight-narrator`. This step wires
    # them in: parse each agent MD definition, invoke Claude CLI, write
    # output to the agent-declared path. WARNING-only on failure.
    if [[ ${pipeline_result} -eq 0 ]]; then
        local invoker="${PROJECT_DIR}/scripts/reports/invoke_claude_agent.py"
        if [[ -f "${invoker}" ]]; then
            log_info "Step 6.3: Generating W2 narrative report..."
            mkdir -p "${PROJECT_DIR}/workflows/analysis/outputs"
            local w2_parquet="${PROJECT_DIR}/data/output/${TARGET_DATE}/analysis.parquet"
            local w2_metrics="${PROJECT_DIR}/workflows/analysis/outputs/w2-metrics-${TARGET_DATE}.json"
            local w2_report="${PROJECT_DIR}/workflows/analysis/outputs/analysis-report-${TARGET_DATE}.md"
            local w2_log="${LOG_DIR}/daily/w2-report-${TARGET_DATE}.log"

            if [[ -f "${w2_parquet}" ]]; then
                "${PYTHON}" "${PROJECT_DIR}/scripts/execution/p1/w2_metrics.py" \
                    --extract --parquet "${w2_parquet}" --output "${w2_metrics}" \
                    >> "${w2_log}" 2>&1 \
                    && "${PYTHON}" "${invoker}" \
                        --agent analysis-reporter \
                        --inputs "metrics=${w2_metrics}" \
                        --output "${w2_report}" \
                        --project-dir "${PROJECT_DIR}" \
                        >> "${w2_log}" 2>&1 \
                    && log_info "W2 report → workflows/analysis/outputs/analysis-report-${TARGET_DATE}.md" \
                    || log_warn "W2 report generation failed — see ${w2_log}"
            else
                log_warn "W2 parquet not found at ${w2_parquet} — skipping W2 report"
            fi

            # Step 6.4: W3 insight-narrator refinement
            log_info "Step 6.4: Refining W3 insight report via narrator..."
            mkdir -p "${PROJECT_DIR}/workflows/master/ingest"
            # Find latest insight run (weekly/monthly/quarterly)
            local insight_run_id
            insight_run_id=$(ls -t "${PROJECT_DIR}/data/insights/" 2>/dev/null \
                | grep -E "^(weekly|monthly|quarterly)" | head -1 || true)
            if [[ -n "${insight_run_id}" ]]; then
                local w3_report="${PROJECT_DIR}/data/insights/${insight_run_id}/synthesis/insight_report.md"
                local w3_metrics="${PROJECT_DIR}/workflows/master/ingest/w3-metrics-${insight_run_id}.json"
                local w3_log="${LOG_DIR}/daily/w3-narrator-${TARGET_DATE}.log"

                if [[ -f "${w3_report}" ]]; then
                    "${PYTHON}" "${PROJECT_DIR}/scripts/execution/p1/w3_metrics.py" \
                        --extract --report "${w3_report}" --output "${w3_metrics}" \
                        >> "${w3_log}" 2>&1 \
                        && "${PYTHON}" "${invoker}" \
                            --agent insight-narrator \
                            --inputs "raw_report=${w3_report}" "metrics=${w3_metrics}" \
                            --output "${w3_report}" \
                            --project-dir "${PROJECT_DIR}" \
                            >> "${w3_log}" 2>&1 \
                        && log_info "W3 narrative refined → data/insights/${insight_run_id}/synthesis/insight_report.md" \
                        || log_warn "W3 narrator refinement failed — see ${w3_log}"
                else
                    log_warn "W3 raw report not found at ${w3_report} — skipping narrator"
                fi
            else
                log_warn "No insight run found under data/insights/ — skipping W3 narrator"
            fi
        else
            log_warn "invoke_claude_agent.py not found — skipping agent-chain reports"
        fi
    fi

    # Step 6.45: W4 Master Appendix + DCI Layer Summary (Python, idempotent)
    # ADR-081. Only runs when the corresponding final report exists.
    if [[ ${pipeline_result} -eq 0 ]]; then
        local master_final="${PROJECT_DIR}/reports/final/integrated-report-${TARGET_DATE}.md"
        if [[ -f "${master_final}" ]]; then
            log_info "Step 6.45a: W4 Master appendix..."
            "${PYTHON}" -m src.reports.w4_appendix --date "${TARGET_DATE}" \
                --project-dir "${PROJECT_DIR}" \
                >> "${LOG_DIR}/daily/w4-appendix-${TARGET_DATE}.log" 2>&1 \
                && log_info "W4 appendix appended to integrated-report-${TARGET_DATE}.md" \
                || log_warn "W4 appendix failed"
        fi

        # DCI appendix — only if a DCI run was produced today
        local dci_latest
        dci_latest=$(ls -t "${PROJECT_DIR}/data/dci/runs/" 2>/dev/null \
            | grep "${TARGET_DATE}" | head -1 || true)
        if [[ -n "${dci_latest}" ]]; then
            log_info "Step 6.45b: DCI layer summary for ${dci_latest}..."
            "${PYTHON}" -m src.reports.dci_layer_summary --run-id "${dci_latest}" \
                --project-dir "${PROJECT_DIR}" \
                >> "${LOG_DIR}/daily/dci-appendix-${TARGET_DATE}.log" 2>&1 \
                && log_info "DCI appendix appended to ${dci_latest}/final_report.md" \
                || log_warn "DCI appendix failed"
        fi
    fi

    # Step 7: WF5 Personal Newspaper — Daily edition (ADR-083)
    # Consumes W1-W4 + Public L3 + Chart Interp; writes multi-page HTML
    # edition to newspaper/daily/{date}/. 14 editorial desks × Claude CLI.
    # WARNING-only on failure (non-blocking).
    if [[ ${pipeline_result} -eq 0 ]]; then
        local np_script="${PROJECT_DIR}/scripts/reports/generate_newspaper_daily.py"
        local np_log="${LOG_DIR}/daily/newspaper-daily-${TARGET_DATE}.log"
        if [[ -f "${np_script}" ]]; then
            log_info "Step 7: Personal Newspaper daily edition..."
            mkdir -p "${LOG_DIR}/daily"
            if command -v timeout >/dev/null 2>&1; then
                timeout 10800 "${PYTHON}" "${np_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${np_log}" 2>&1 \
                    && log_info "Newspaper → newspaper/daily/${TARGET_DATE}/index.html" \
                    || log_warn "Newspaper daily failed — see ${np_log}"
            else
                "${PYTHON}" "${np_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${np_log}" 2>&1 \
                    && log_info "Newspaper daily PASS" \
                    || log_warn "Newspaper daily failed (non-blocking)"
            fi

            # Step 7a.5: Single-page merge for dashboard iframe compatibility
            # (AUDIT-newspaper-display-2026-04-15.md — Option A).
            # generate_newspaper_daily.py Phase 7b already calls this internally,
            # but as a safety net we also call it here in case Phase 7b failed
            # (e.g. import path issue). Best-effort — non-blocking.
            local sp_script="${PROJECT_DIR}/scripts/reports/build_newspaper_single_page.py"
            if [[ -f "${sp_script}" ]]; then
                "${PYTHON}" "${sp_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${np_log}" 2>&1 \
                    && log_info "Single-page merge PASS" \
                    || log_warn "Single-page merge failed (non-blocking)"
            fi

            # Step 7b: Weekly edition (only on Sunday, requires ≥4 daily)
            local dow
            dow=$(date +%u)  # 1=Mon, 7=Sun
            if [[ "${dow}" == "7" ]]; then
                local iso_week
                iso_week=$(date +%G-W%V)
                local nw_script="${PROJECT_DIR}/scripts/reports/generate_newspaper_weekly.py"
                if [[ -f "${nw_script}" ]]; then
                    log_info "Step 7b: Personal Newspaper weekly edition (${iso_week})..."
                    "${PYTHON}" "${nw_script}" \
                        --week "${iso_week}" --project-dir "${PROJECT_DIR}" \
                        >> "${LOG_DIR}/daily/newspaper-weekly-${TARGET_DATE}.log" 2>&1 \
                        && log_info "Weekly edition → newspaper/weekly/${iso_week}/" \
                        || log_warn "Weekly newspaper failed (non-blocking)"
                fi
            fi
        fi
    fi

    # Step 6.6: Chart Interpretations (ADR-082)
    # Generate 3-Layer interpretation cards for each dashboard tab
    # (해석/인사이트/미래통찰). Consumes W2 parquet + Public Narrative
    # facts_pool + W3 narrator output + M4 Temporal. WARNING-only on fail.
    if [[ ${pipeline_result} -eq 0 ]]; then
        local ci_script="${PROJECT_DIR}/scripts/reports/generate_chart_interpretations.py"
        local ci_log="${LOG_DIR}/daily/chart-interp-${TARGET_DATE}.log"
        if [[ -f "${ci_script}" ]]; then
            log_info "Step 6.6: Chart Interpretations (6 tabs)..."
            if command -v timeout >/dev/null 2>&1; then
                timeout 900 "${PYTHON}" "${ci_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${ci_log}" 2>&1 \
                    && log_info "Interpretations → data/analysis/${TARGET_DATE}/interpretations.json" \
                    || log_warn "Chart interpretations failed — see ${ci_log}"
            else
                "${PYTHON}" "${ci_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${ci_log}" 2>&1 \
                    && log_info "Interpretations → data/analysis/${TARGET_DATE}/interpretations.json" \
                    || log_warn "Chart interpretations failed — see ${ci_log}"
            fi

            # Step 6.6b: Fact Auditor review (optional, WARNING-only)
            local rv_script="${PROJECT_DIR}/scripts/reports/review_chart_interpretations.py"
            if [[ -f "${rv_script}" ]] && [[ -f "${PROJECT_DIR}/data/analysis/${TARGET_DATE}/interpretations.json" ]]; then
                log_info "Step 6.6b: interp-fact-auditor review..."
                "${PYTHON}" "${rv_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${LOG_DIR}/daily/chart-interp-review-${TARGET_DATE}.log" 2>&1 \
                    && log_info "Interp review complete" \
                    || log_warn "Interp review failed (non-blocking)"
            fi
        else
            log_warn "generate_chart_interpretations.py not found — skipping Step 6.6"
        fi
    fi

    # Step 6.5: Generate Public Narrative 3-layer (on success only)
    # Produces reports/public/{date}/{interpretation,insight,future}.md
    # so the dashboard's Run Summary tab displays interpretation-quality
    # prose immediately after each pipeline run. ADR-080.
    if [[ ${pipeline_result} -eq 0 ]]; then
        local pn_script="${PROJECT_DIR}/.claude/hooks/scripts/generate_public_layers.py"
        local pn_log="${LOG_DIR}/daily/public-layers-${TARGET_DATE}.log"
        if [[ -f "${pn_script}" ]]; then
            log_info "Generating Public Narrative 3-layer..."
            mkdir -p "${LOG_DIR}/daily"
            # Use system python3 (hook scripts run on 3.14); narrator subprocess
            # calls `claude` CLI directly. 25-min budget covers 3 layers × retries.
            if command -v timeout >/dev/null 2>&1; then
                timeout 1500 python3 "${pn_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${pn_log}" 2>&1 \
                    && log_info "Public Narrative generated → reports/public/${TARGET_DATE}/" \
                    || log_warn "Public Narrative generation failed — see ${pn_log}"
            else
                python3 "${pn_script}" \
                    --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
                    >> "${pn_log}" 2>&1 \
                    && log_info "Public Narrative generated → reports/public/${TARGET_DATE}/" \
                    || log_warn "Public Narrative generation failed — see ${pn_log}"
            fi
        else
            log_warn "Public Narrative script not found: ${pn_script}"
        fi
    fi

    # Step 6.7: BigData Engine — Enriched + 18문 + GTI + Portfolio + Weekly Map
    # Pipeline.py post-processing already runs these, but this ensures they
    # also run on partial-success days (e.g., W2 only) and produces the
    # weekly future map regardless of DCI/WF5 completion.
    if [[ ${pipeline_result} -eq 0 ]]; then
        log_info "Step 6.7: BigData Engine (Enriched · 18문 · GTI · Portfolio · WeeklyMap)..."

        local bd_log="${LOG_DIR}/daily/bigdata-engine-${TARGET_DATE}.log"
        mkdir -p "${LOG_DIR}/daily"

        # 6.7a: Enriched Assembly + 18-Question Engine (incremental — skip if done)
        "${PYTHON}" "${PROJECT_DIR}/scripts/backfill_enriched.py" \
            --date "${TARGET_DATE}" --project-dir "${PROJECT_DIR}" \
            >> "${bd_log}" 2>&1 \
            && log_info "  6.7a: Enriched + 18문 OK" \
            || log_warn "  6.7a: Enriched/18문 failed — see ${bd_log}"

        # 6.7b: GTI
        "${PYTHON}" -c "
import sys; sys.path.insert(0, '${PROJECT_DIR}')
from src.analysis.gti import run_gti
r = run_gti('${TARGET_DATE}', '${PROJECT_DIR}')
print(f'GTI {r[\"date\"]}: {r[\"gti_score\"]:.1f} ({r[\"gti_label\"]})')
" >> "${bd_log}" 2>&1 \
            && log_info "  6.7b: GTI OK" \
            || log_warn "  6.7b: GTI failed"

        # 6.7c: Signal Portfolio update
        "${PYTHON}" -c "
import sys; sys.path.insert(0, '${PROJECT_DIR}')
from pathlib import Path
from src.analysis.signal_portfolio import update_portfolio, portfolio_summary
port_path = Path('${PROJECT_DIR}') / 'data' / 'signal_portfolio.yaml'
ans_dir   = Path('${PROJECT_DIR}') / 'data' / 'answers'
stats = update_portfolio(port_path, ans_dir, lookback_days=30)
print(f'Portfolio: +{stats[\"added\"]} added, +{stats[\"promoted\"]} promoted, {stats[\"dismissed\"]} dismissed, total={stats[\"total\"]}')
" >> "${bd_log}" 2>&1 \
            && log_info "  6.7c: Signal Portfolio OK" \
            || log_warn "  6.7c: Signal Portfolio failed"

        # 6.7d: Weekly Future Map
        "${PYTHON}" -c "
import sys; sys.path.insert(0, '${PROJECT_DIR}')
from pathlib import Path
from datetime import datetime
from src.analysis.weekly_future_map import generate_weekly_future_map
meta = generate_weekly_future_map(datetime.strptime('${TARGET_DATE}', '%Y-%m-%d'), Path('${PROJECT_DIR}'), window_days=7)
print(f'Weekly map: {meta[\"week_label\"]} | {meta[\"dates_with_data\"]}/{meta[\"window_days\"]}d | GTI={meta[\"gti_avg\"]:.1f}')
" >> "${bd_log}" 2>&1 \
            && log_info "  6.7d: Weekly Future Map OK" \
            || log_warn "  6.7d: Weekly Future Map failed"

        log_info "Step 6.7 done — see ${bd_log}"
    fi

    # Step 7: Generate daily summary
    log_info "============================================"
    if [[ ${pipeline_result} -eq 0 ]]; then
        log_info "GlobalNews Daily Pipeline -- SUCCESS"
    else
        log_info "GlobalNews Daily Pipeline -- FAILED (exit: ${pipeline_result})"
    fi
    log_info "============================================"

    exit "${pipeline_result}"
}

main "$@"
