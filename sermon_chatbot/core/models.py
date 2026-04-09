"""Data models for sermon research."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PassageAnalysis:
    """Result of analyzing a Bible passage."""

    reference: str  # e.g., "John 3:16"
    text_kr: str  # Korean Bible text
    text_en: str  # English Bible text
    original_language: str  # Hebrew/Greek analysis
    historical_context: str
    theological_meaning: str
    key_words: list[str] = field(default_factory=list)
    cross_references: list[str] = field(default_factory=list)
    application_points: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class SermonOutline:
    """Generated sermon outline."""

    title: str
    reference: str
    theme: str
    introduction: str
    main_points: list[dict[str, str]] = field(default_factory=list)  # [{point, explanation, illustration}]
    conclusion: str = ""
    prayer_points: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class SermonResearch:
    """Complete sermon research result."""

    id: str
    reference: str
    analysis: PassageAnalysis
    outline: SermonOutline
    additional_notes: str = ""
    created_at: datetime = field(default_factory=datetime.now)
