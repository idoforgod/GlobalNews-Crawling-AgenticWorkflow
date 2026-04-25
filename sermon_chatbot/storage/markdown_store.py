"""Markdown file storage for sermon research."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

SERMONS_DIR = Path(__file__).parent.parent / "sermons"


def save_as_markdown(
    research_id: str,
    reference: str,
    analysis: str,
    outline: str,
    additional_notes: str = "",
) -> Path:
    """Save sermon research as a Markdown file. Returns the file path."""
    SERMONS_DIR.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_ref = reference.replace(":", "_").replace(" ", "_")
    filename = f"{date_str}_{safe_ref}.md"
    filepath = SERMONS_DIR / filename

    content = f"""# 설교 연구: {reference}

- **날짜**: {datetime.now().strftime("%Y년 %m월 %d일")}
- **연구 ID**: {research_id}

---

## 본문 분석

{analysis}

---

## 설교 개요

{outline}
"""

    if additional_notes.strip():
        content += f"""
---

## 추가 메모

{additional_notes}
"""

    filepath.write_text(content, encoding="utf-8")
    return filepath
