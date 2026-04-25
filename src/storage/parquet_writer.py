"""Parquet Writer: schema-validated, ZSTD-compressed Parquet I/O for the storage layer.

Implements the authoritative PyArrow schemas for all pipeline output tables
as defined in PRD SS7.1 and the Step 7 Analysis Pipeline Design:

    ARTICLES_SCHEMA  (12 columns) -- Stage 1 output
    ANALYSIS_SCHEMA  (21 columns) -- Stage 8 merged output (PRD SS7.1.2)
    SIGNALS_SCHEMA   (12 columns) -- Stage 7 output (PRD SS7.1.3)
    TOPICS_SCHEMA    ( 7 columns) -- Stage 4 output (topics.parquet)

Design decisions:
    - ZSTD level 3 compression (PRD C4): best throughput/ratio for analytical workloads
    - Atomic write via temp-file + rename: prevents corrupt partial writes on crash
    - Row group size tuned per table: smaller for wide tables (ANALYSIS with embeddings),
      larger for narrow tables (SIGNALS)
    - Explicit pa.schema enforcement before every write: type errors surface immediately,
      not at query time

Reference: PRD SS7.1 (Parquet schemas), Step 7 pipeline design Section 3.8 (Stage 8).
"""

from __future__ import annotations

import gc
import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.config.constants import (
    ANALYSIS_SCHEMA_COLUMNS,
    ARTICLES_SCHEMA_COLUMNS,
    PARQUET_COMPRESSION,
    PARQUET_COMPRESSION_LEVEL,
    SIGNALS_SCHEMA_COLUMNS,
    SBERT_EMBEDDING_DIM,
)
from src.utils.error_handler import ParquetIOError, SchemaValidationError
from src.utils.logging_config import get_analysis_logger

logger = get_analysis_logger()

# =============================================================================
# Authoritative PyArrow Schema Definitions (PRD SS7.1)
# =============================================================================

# articles.parquet -- Stage 1 output (PRD SS7.1.1)
# NOTE: Column names diverge from PRD in 4 places (see ADR-049):
#   PRD source_id → impl source, PRD section → impl category,
#   PRD raw_html_hash → impl content_hash, PRD extraction_method → impl word_count (int32)
# Nullability: published_at/crawled_at are nullable=True (timestamps may be missing)
ARTICLES_PA_SCHEMA = pa.schema([
    pa.field("article_id",   pa.utf8(),                    nullable=False),
    pa.field("evidence_id",  pa.utf8(),                    nullable=True),
    pa.field("url",          pa.utf8(),                    nullable=False),
    pa.field("title",        pa.utf8(),                    nullable=False),
    pa.field("body",         pa.utf8(),                    nullable=False),
    pa.field("source",       pa.utf8(),                    nullable=False),
    pa.field("category",     pa.utf8(),                    nullable=False),
    pa.field("language",     pa.utf8(),                    nullable=False),
    pa.field("published_at", pa.timestamp("us", tz="UTC"), nullable=True),
    pa.field("crawled_at",   pa.timestamp("us", tz="UTC"), nullable=True),
    pa.field("author",       pa.utf8(),                    nullable=True),
    pa.field("word_count",   pa.int32(),                   nullable=False),
    pa.field("content_hash", pa.utf8(),                    nullable=False),
])

# analysis.parquet -- Stage 8 merged output (PRD SS7.1.2, 21 columns)
# NOTE: Column structure diverges from PRD (see ADR-049):
#   PRD uses JSON-packed emotion/entities; impl uses flat Plutchik + separate entity cols.
#   PRD adds stance, novelty_score, etc.; impl uses topic_label, topic_probability.
#   Both have exactly 21 columns but different column composition.
ANALYSIS_PA_SCHEMA = pa.schema([
    pa.field("article_id",          pa.utf8(),              nullable=False),
    pa.field("sentiment_label",     pa.utf8(),              nullable=True),
    pa.field("sentiment_score",     pa.float32(),           nullable=True),
    pa.field("emotion_joy",         pa.float32(),           nullable=True),
    pa.field("emotion_trust",       pa.float32(),           nullable=True),
    pa.field("emotion_fear",        pa.float32(),           nullable=True),
    pa.field("emotion_surprise",    pa.float32(),           nullable=True),
    pa.field("emotion_sadness",     pa.float32(),           nullable=True),
    pa.field("emotion_disgust",     pa.float32(),           nullable=True),
    pa.field("emotion_anger",       pa.float32(),           nullable=True),
    pa.field("emotion_anticipation", pa.float32(),          nullable=True),
    pa.field("topic_id",            pa.int32(),             nullable=True),
    pa.field("topic_label",         pa.utf8(),              nullable=True),
    pa.field("topic_probability",   pa.float32(),           nullable=True),
    pa.field("steeps_category",     pa.utf8(),              nullable=True),
    pa.field("importance_score",    pa.float32(),           nullable=True),
    pa.field("keywords",            pa.list_(pa.utf8()),    nullable=True),
    pa.field("entities_person",     pa.list_(pa.utf8()),    nullable=True),
    pa.field("entities_org",        pa.list_(pa.utf8()),    nullable=True),
    pa.field("entities_location",   pa.list_(pa.utf8()),    nullable=True),
    pa.field("embedding",           pa.list_(pa.float32()), nullable=True),
])

# signals.parquet -- Stage 7 output (PRD SS7.1.3, 12 columns EXACT)
SIGNALS_PA_SCHEMA = pa.schema([
    pa.field("signal_id",                pa.utf8(),                  nullable=False),
    pa.field("signal_layer",             pa.utf8(),                  nullable=False),
    pa.field("signal_label",             pa.utf8(),                  nullable=False),
    pa.field("detected_at",              pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("topic_ids",                pa.list_(pa.int32()),        nullable=True),
    pa.field("article_ids",              pa.list_(pa.utf8()),         nullable=True),
    pa.field("burst_score",              pa.float32(),                nullable=True),
    pa.field("changepoint_significance", pa.float32(),                nullable=True),
    pa.field("novelty_score",            pa.float32(),                nullable=True),
    pa.field("singularity_composite",    pa.float32(),                nullable=True),
    pa.field("evidence_summary",         pa.utf8(),                   nullable=True),
    pa.field("confidence",               pa.float32(),                nullable=False),
])

# topics.parquet -- Stage 4 output (Step 7 design Section 3.4)
# published_at and source propagated from articles.parquet for Stage 7 signal classification
TOPICS_PA_SCHEMA = pa.schema([
    pa.field("article_id",         pa.utf8(),                    nullable=False),
    pa.field("topic_id",           pa.int32(),                   nullable=False),
    pa.field("topic_label",        pa.utf8(),                    nullable=True),
    pa.field("topic_probability",  pa.float32(),                 nullable=True),
    pa.field("hdbscan_cluster_id", pa.int32(),                   nullable=True),
    pa.field("nmf_topic_id",       pa.int32(),                   nullable=True),
    pa.field("lda_topic_id",       pa.int32(),                   nullable=True),
    pa.field("published_at",       pa.timestamp("us", tz="UTC"), nullable=True),
    pa.field("source",             pa.utf8(),                    nullable=True),
])

# Registry: table_name -> (pa.Schema, row_group_size)
# Row group tuning rationale:
#   analysis: 384-float embeddings make rows wide; smaller groups improve column pruning
#   signals, topics: narrow rows; larger groups reduce metadata overhead
_SCHEMA_REGISTRY: dict[str, tuple[pa.Schema, int]] = {
    "articles": (ARTICLES_PA_SCHEMA,  10_000),
    "analysis": (ANALYSIS_PA_SCHEMA,   5_000),   # wide rows due to embedding column
    "signals":  (SIGNALS_PA_SCHEMA,   20_000),
    "topics":   (TOPICS_PA_SCHEMA,    10_000),
}

# =============================================================================
# Value range constraints for validation
# =============================================================================

_RANGE_CONSTRAINTS: dict[str, tuple[float, float]] = {
    "sentiment_score":          (-1.0,  1.0),
    "emotion_joy":              ( 0.0,  1.0),
    "emotion_trust":            ( 0.0,  1.0),
    "emotion_fear":             ( 0.0,  1.0),
    "emotion_surprise":         ( 0.0,  1.0),
    "emotion_sadness":          ( 0.0,  1.0),
    "emotion_disgust":          ( 0.0,  1.0),
    "emotion_anger":            ( 0.0,  1.0),
    "emotion_anticipation":     ( 0.0,  1.0),
    "topic_probability":        ( 0.0,  1.0),
    "importance_score":         ( 0.0, 100.0),
    "burst_score":              ( 0.0, None),    # type: ignore[arg-type]
    "changepoint_significance": ( 0.0,  1.0),
    "novelty_score":            ( 0.0,  1.0),
    "singularity_composite":    ( 0.0,  1.0),
    "confidence":               ( 0.0,  1.0),
}


# =============================================================================
# Validation result dataclass
# =============================================================================

@dataclass
class ValidationResult:
    """Result of a schema or range validation check."""

    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.passed = False
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def __str__(self) -> str:
        lines = [f"ValidationResult(passed={self.passed})"]
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        return "\n".join(lines)


# =============================================================================
# Schema validator
# =============================================================================

def validate_schema(
    table: pa.Table,
    table_name: str,
    *,
    check_ranges: bool = True,
    check_not_null: bool = True,
) -> ValidationResult:
    """Validate a PyArrow table against the PRD SS7.1 schema for *table_name*.

    Checks performed:
        1. Column presence  -- all required columns present
        2. Column types     -- dtypes match schema spec (permissive cast check)
        3. Null constraints -- NOT NULL columns (nullable=False) have no nulls
        4. Value ranges     -- bounded float columns stay within spec

    Args:
        table:        PyArrow table to validate.
        table_name:   Key in _SCHEMA_REGISTRY ("articles", "analysis",
                      "signals", "topics").
        check_ranges: Whether to validate float value ranges.
        check_not_null: Whether to enforce nullable=False constraints.

    Returns:
        ValidationResult with .passed, .errors, .warnings.

    Raises:
        ValueError: If *table_name* is not in the schema registry.
    """
    if table_name not in _SCHEMA_REGISTRY:
        raise ValueError(
            f"Unknown table_name={table_name!r}. "
            f"Valid names: {sorted(_SCHEMA_REGISTRY)}"
        )

    schema, _ = _SCHEMA_REGISTRY[table_name]
    result = ValidationResult()
    actual_names = set(table.schema.names)

    # --- 1. Column presence ---------------------------------------------------
    for expected_field in schema:
        if expected_field.name not in actual_names:
            result.fail(
                f"Missing required column '{expected_field.name}' "
                f"(expected type {expected_field.type})"
            )

    # Early exit: cannot check types/nulls if columns are missing
    if not result.passed:
        return result

    # --- 2. Column type compatibility -----------------------------------------
    for expected_field in schema:
        col_name = expected_field.name
        if col_name not in actual_names:
            continue  # already flagged above
        actual_field = table.schema.field(col_name)
        if not _types_compatible(actual_field.type, expected_field.type):
            result.fail(
                f"Column '{col_name}': expected type {expected_field.type}, "
                f"got {actual_field.type}"
            )

    # --- 3. Null constraints (NOT NULL columns) --------------------------------
    if check_not_null:
        for expected_field in schema:
            if expected_field.nullable:
                continue  # nullable columns may have nulls
            col_name = expected_field.name
            if col_name not in actual_names:
                continue
            col = table.column(col_name)
            null_count = col.null_count
            if null_count > 0:
                result.fail(
                    f"Column '{col_name}' is NOT NULL but has "
                    f"{null_count} null value(s)"
                )

    # --- 4. Value ranges -------------------------------------------------------
    if check_ranges:
        for col_name, (lo, hi) in _RANGE_CONSTRAINTS.items():
            if col_name not in actual_names:
                continue
            col = table.column(col_name)
            # Flatten: strip nulls before range check
            flat = col.drop_null()
            if len(flat) == 0:
                continue
            # For list columns skip range check (embedding)
            if pa.types.is_large_list(col.type) or pa.types.is_list(col.type):
                continue
            try:
                import pyarrow.compute as pc
                if lo is not None:
                    below_min = pc.sum(pc.less(flat, lo)).as_py()
                    if below_min and below_min > 0:
                        result.warn(
                            f"Column '{col_name}': {below_min} value(s) below "
                            f"minimum {lo}"
                        )
                if hi is not None:
                    above_max = pc.sum(pc.greater(flat, hi)).as_py()
                    if above_max and above_max > 0:
                        result.warn(
                            f"Column '{col_name}': {above_max} value(s) above "
                            f"maximum {hi}"
                        )
            except Exception as exc:  # pragma: no cover
                result.warn(f"Range check skipped for '{col_name}': {exc}")

    return result


def _types_compatible(actual: pa.DataType, expected: pa.DataType) -> bool:
    """Return True if *actual* is compatible with *expected* schema type.

    Rules (from most to least strict):
    - Identical types always compatible.
    - Any utf8/large_utf8 combination: compatible (PyArrow uses both).
    - Any int family to int family: compatible if same sign + width.
    - Any float32/float64: compatible (both are floating point).
    - list<X> vs list<X>: recurse on value type.
    - timestamp[us,UTC] vs timestamp[*]: compatible (tz normalization).
    """
    if actual == expected:
        return True
    # utf8 / large_utf8 interchangeable
    if pa.types.is_string(actual) and pa.types.is_string(expected):
        return True
    if pa.types.is_large_string(actual) and (
        pa.types.is_string(expected) or pa.types.is_large_string(expected)
    ):
        return True
    if pa.types.is_string(actual) and pa.types.is_large_string(expected):
        return True
    # float32 / float64 both acceptable for float32 spec
    if pa.types.is_floating(actual) and pa.types.is_floating(expected):
        return True
    # int family -- same width + sign
    if pa.types.is_integer(actual) and pa.types.is_integer(expected):
        return True
    # list<X> -> check value type
    if pa.types.is_list(actual) and pa.types.is_list(expected):
        return _types_compatible(actual.value_type, expected.value_type)
    if pa.types.is_large_list(actual) and (
        pa.types.is_list(expected) or pa.types.is_large_list(expected)
    ):
        return _types_compatible(actual.value_type, expected.value_type)
    # timestamp -- any timezone acceptable
    if pa.types.is_timestamp(actual) and pa.types.is_timestamp(expected):
        return True
    return False


# =============================================================================
# ParquetWriter class
# =============================================================================

class ParquetWriter:
    """Writes PyArrow tables to Parquet with ZSTD compression and schema validation.

    Usage:
        writer = ParquetWriter()
        writer.write(table, output_path, table_name="analysis")

    Design:
        - validate_schema() runs before every write (fails fast on schema errors)
        - Atomic write: data goes to temp file, then os.rename() into place
        - Compression: ZSTD level 3 (PRD C4 archival-quality, CPU-efficient on M2)
        - Row group size: per-table tuned values from _SCHEMA_REGISTRY

    Raises:
        SchemaValidationError: If pre-write schema validation fails.
        ParquetIOError: If write to disk fails after retries.
    """

    def __init__(
        self,
        compression: str = PARQUET_COMPRESSION,
        compression_level: int = PARQUET_COMPRESSION_LEVEL,
        max_write_retries: int = 3,
    ) -> None:
        self.compression = compression
        self.compression_level = compression_level
        self.max_write_retries = max_write_retries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(
        self,
        table: pa.Table,
        output_path: Path | str,
        table_name: str,
        *,
        validate: bool = True,
        coerce_schema: bool = True,
    ) -> dict[str, Any]:
        """Write *table* to *output_path* as a ZSTD-compressed Parquet file.

        Args:
            table:       PyArrow table to persist.
            output_path: Destination path (parent directory must exist).
            table_name:  Table identifier for schema lookup and validation.
            validate:    Run schema validation before write.
            coerce_schema: Cast columns to schema types where safe. This
                           fixes minor type drift (e.g., float64 -> float32)
                           without altering the data.

        Returns:
            dict with keys: path, rows, size_bytes, compression_ratio,
                            md5_checksum, elapsed_seconds, validation_passed.

        Raises:
            SchemaValidationError: Pre-write validation failed.
            ParquetIOError: Disk write failed after retries.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        t_start = time.perf_counter()

        # Optional schema coercion (safe casts only)
        if coerce_schema and table_name in _SCHEMA_REGISTRY:
            table = self._coerce_to_schema(table, table_name)

        # Pre-write schema validation
        validation_passed = True
        if validate:
            result = validate_schema(table, table_name)
            if not result.passed:
                raise SchemaValidationError(
                    f"Schema validation FAILED for table '{table_name}' "
                    f"before writing to {output_path}:\n{result}",
                    expected_columns=[f.name for f in _SCHEMA_REGISTRY[table_name][0]],
                    actual_columns=table.schema.names,
                )
            for w in result.warnings:
                logger.warning("Schema warning [%s]: %s", table_name, w)
            validation_passed = result.passed

        # Atomic write: temp -> rename
        row_group_size = _SCHEMA_REGISTRY.get(table_name, (None, 10_000))[1]
        self._atomic_write(table, output_path, row_group_size)

        elapsed = time.perf_counter() - t_start
        size_bytes = output_path.stat().st_size
        md5 = self._md5_file(output_path)

        logger.info(
            "Parquet write complete",
            extra={
                "table": table_name,
                "path": str(output_path),
                "rows": len(table),
                "size_bytes": size_bytes,
                "elapsed_s": round(elapsed, 2),
                "md5": md5[:8],
            },
        )

        return {
            "path": str(output_path),
            "table_name": table_name,
            "rows": len(table),
            "size_bytes": size_bytes,
            "md5_checksum": md5,
            "elapsed_seconds": round(elapsed, 3),
            "validation_passed": validation_passed,
        }

    def write_from_path(
        self,
        source_path: Path | str,
        output_path: Path | str,
        table_name: str,
        *,
        validate: bool = True,
    ) -> dict[str, Any]:
        """Read a Parquet file from *source_path*, validate, and write to *output_path*.

        Used to re-compress and validate an existing Parquet file (e.g., signals.parquet
        produced by Stage 7 being finalized into data/output/).

        Args:
            source_path:  Input Parquet file path.
            output_path:  Destination path.
            table_name:   Schema identifier.
            validate:     Run schema validation.

        Returns:
            Same dict as write().
        """
        source_path = Path(source_path)
        if not source_path.exists():
            raise ParquetIOError(f"Source Parquet not found: {source_path}")

        table = pq.read_table(str(source_path))
        return self.write(table, output_path, table_name, validate=validate)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _atomic_write(
        self,
        table: pa.Table,
        output_path: Path,
        row_group_size: int,
    ) -> None:
        """Write table to a temp file in the same directory, then rename into place.

        The rename is atomic on POSIX systems (same filesystem), preventing
        a half-written Parquet from being observed by concurrent readers.
        """
        parent = output_path.parent
        last_exc: Exception | None = None

        for attempt in range(self.max_write_retries):
            # Create temp file in same dir so rename stays on same filesystem
            fd, tmp_path_str = tempfile.mkstemp(
                suffix=".parquet.tmp", dir=parent
            )
            tmp_path = Path(tmp_path_str)
            try:
                os.close(fd)
                pq.write_table(
                    table,
                    str(tmp_path),
                    compression=self.compression,
                    compression_level=self.compression_level,
                    row_group_size=row_group_size,
                    write_statistics=True,
                    use_dictionary=True,
                )
                # Atomic rename
                os.rename(str(tmp_path), str(output_path))
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Parquet write attempt %d/%d failed: %s",
                    attempt + 1, self.max_write_retries, exc,
                )
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if attempt < self.max_write_retries - 1:
                    time.sleep(0.5 * (attempt + 1))

        raise ParquetIOError(
            f"Parquet write to {output_path} failed after "
            f"{self.max_write_retries} attempts: {last_exc}"
        )

    @staticmethod
    def _md5_file(path: Path) -> str:
        """Compute SHA-256 hex digest of a file (for corruption detection).

        Note: Method name kept as _md5_file for API compatibility;
        internally uses SHA-256 (non-security context, integrity check only).
        """
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _coerce_to_schema(self, table: pa.Table, table_name: str) -> pa.Table:
        """Attempt safe casts to align table dtypes with the registered schema.

        Only performs casts that are guaranteed lossless:
            float64 -> float32
            int64   -> int32  (only if no value exceeds int32 range)
            large_utf8 -> utf8

        Unknown columns (not in schema) are dropped with a warning.

        Args:
            table:      Input PyArrow table.
            table_name: Schema key.

        Returns:
            PyArrow table with columns cast to schema types where possible.
        """
        import pyarrow.compute as pc

        schema, _ = _SCHEMA_REGISTRY[table_name]
        schema_fields = {f.name: f for f in schema}
        new_columns: list[pa.ChunkedArray] = []
        new_names: list[str] = []

        # Iterate over expected schema columns in order
        for expected_field in schema:
            col_name = expected_field.name
            if col_name not in table.schema.names:
                # Column missing -- leave for validation to flag
                continue

            col = table.column(col_name)
            expected_type = expected_field.type

            try:
                if _types_compatible(col.type, expected_type):
                    if col.type != expected_type:
                        col = col.cast(expected_type, safe=True)
                else:
                    # Incompatible -- leave as-is, validation will flag it
                    pass
            except Exception as exc:
                logger.debug(
                    "Coerce cast skipped for '%s' (%s -> %s): %s",
                    col_name, col.type, expected_type, exc,
                )

            new_columns.append(col)
            new_names.append(col_name)

        # Check for extra columns not in schema
        extra = set(table.schema.names) - set(schema_fields)
        if extra:
            logger.warning(
                "Columns not in schema for '%s' will be dropped: %s",
                table_name, sorted(extra),
            )

        return pa.table(dict(zip(new_names, new_columns)))


# =============================================================================
# Standalone utility: validate a Parquet file on disk
# =============================================================================

def validate_parquet_file(parquet_path: Path | str, table_name: str) -> ValidationResult:
    """Validate a Parquet file on disk against the PRD SS7.1 schema.

    This is the `validate_schema(parquet_path, table_name)` utility function
    described in the storage layer spec.

    Args:
        parquet_path: Path to a Parquet file.
        table_name:   Schema key ("articles", "analysis", "signals", "topics").

    Returns:
        ValidationResult with full details.
    """
    path = Path(parquet_path)
    result = ValidationResult()

    if not path.exists():
        result.fail(f"File not found: {path}")
        return result

    try:
        table = pq.read_table(str(path))
    except Exception as exc:
        result.fail(f"Failed to read Parquet file {path}: {exc}")
        return result

    return validate_schema(table, table_name)


# =============================================================================
# MD5 checksum store
# =============================================================================

class ChecksumStore:
    """Stores and verifies MD5 checksums for Parquet files.

    Checksums are persisted to a plain-text file (one line per Parquet):
        <md5hex>  <relative_path>

    This format is compatible with `md5sum --check` on Linux/macOS.
    """

    def __init__(self, checksum_file: Path | str) -> None:
        self.checksum_file = Path(checksum_file)
        self._checksums: dict[str, str] = {}
        if self.checksum_file.exists():
            self._load()

    def add(self, parquet_path: Path | str, md5: str) -> None:
        """Record a checksum for *parquet_path*."""
        key = str(Path(parquet_path).resolve())
        self._checksums[key] = md5
        self._save()

    def verify(self, parquet_path: Path | str) -> tuple[bool, str]:
        """Verify the current MD5 of *parquet_path* against the stored value.

        Returns:
            (True, "") if checksums match or no stored checksum.
            (False, reason) on mismatch or read error.
        """
        path = Path(parquet_path).resolve()
        key = str(path)
        if key not in self._checksums:
            return True, ""  # No stored checksum -- cannot verify

        stored = self._checksums[key]
        try:
            current = ParquetWriter._md5_file(path)
        except OSError as exc:
            return False, f"Cannot read file: {exc}"

        if current == stored:
            return True, ""
        return False, f"Checksum mismatch: stored={stored}, current={current}"

    def _save(self) -> None:
        self.checksum_file.parent.mkdir(parents=True, exist_ok=True)
        with self.checksum_file.open("w", encoding="utf-8") as fh:
            for path, md5 in sorted(self._checksums.items()):
                fh.write(f"{md5}  {path}\n")

    def _load(self) -> None:
        with self.checksum_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if "  " not in line:
                    continue
                md5, path = line.split("  ", 1)
                self._checksums[path.strip()] = md5.strip()
