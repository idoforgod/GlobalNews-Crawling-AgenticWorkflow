"""Bible passage analysis and sermon outline generation using Claude API."""

from __future__ import annotations

import anthropic

from .prompts import FOLLOWUP_PROMPT, PASSAGE_ANALYSIS_PROMPT, SERMON_OUTLINE_PROMPT

MODEL = "claude-sonnet-4-20250514"


def analyze_passage(reference: str, *, api_key: str) -> str:
    """Analyze a Bible passage and return structured analysis."""
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=PASSAGE_ANALYSIS_PROMPT,
        messages=[
            {"role": "user", "content": f"다음 성경 구절을 분석해주세요: {reference}"}
        ],
    )
    return message.content[0].text


def generate_outline(reference: str, analysis: str, *, api_key: str) -> str:
    """Generate a sermon outline based on passage analysis."""
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SERMON_OUTLINE_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"다음 본문 분석을 바탕으로 설교 개요를 작성해주세요.\n\n"
                    f"**본문**: {reference}\n\n"
                    f"**분석 내용**:\n{analysis}"
                ),
            }
        ],
    )
    return message.content[0].text


def ask_followup(
    reference: str,
    analysis: str,
    outline: str,
    conversation_history: list[dict],
    question: str,
    *,
    api_key: str,
) -> str:
    """Handle follow-up questions about the sermon research."""
    client = anthropic.Anthropic(api_key=api_key)

    context_message = (
        f"현재 연구 중인 본문: {reference}\n\n"
        f"--- 본문 분석 ---\n{analysis}\n\n"
        f"--- 설교 개요 ---\n{outline}"
    )

    messages = [{"role": "user", "content": context_message}]
    messages.append({"role": "assistant", "content": "네, 위 설교 연구 내용을 바탕으로 추가 질문에 답변하겠습니다."})

    for msg in conversation_history:
        messages.append(msg)

    messages.append({"role": "user", "content": question})

    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=FOLLOWUP_PROMPT,
        messages=messages,
    )
    return message.content[0].text
