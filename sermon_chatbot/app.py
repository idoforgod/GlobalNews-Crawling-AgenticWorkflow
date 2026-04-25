"""Sermon Research Chatbot - Streamlit Web App."""

from __future__ import annotations

import uuid
from datetime import datetime

import streamlit as st

from core.bible_analyzer import analyze_passage, ask_followup, generate_outline
from storage.db_store import (
    get_all_researches,
    get_conversations,
    get_research,
    init_db,
    save_conversation,
    save_research,
    search_researches,
)
from storage.markdown_store import save_as_markdown

# --- Page Config ---
st.set_page_config(
    page_title="설교 연구 챗봇",
    page_icon="📖",
    layout="wide",
)

# --- Initialize DB ---
init_db()

# --- Session State ---
if "api_key" not in st.session_state:
    st.session_state.api_key = ""
if "current_research_id" not in st.session_state:
    st.session_state.current_research_id = None
if "analysis" not in st.session_state:
    st.session_state.analysis = ""
if "outline" not in st.session_state:
    st.session_state.outline = ""
if "reference" not in st.session_state:
    st.session_state.reference = ""
if "messages" not in st.session_state:
    st.session_state.messages = []


def _reset_research() -> None:
    st.session_state.current_research_id = None
    st.session_state.analysis = ""
    st.session_state.outline = ""
    st.session_state.reference = ""
    st.session_state.messages = []


def _load_research(research_id: str) -> None:
    """Load a previous research into session state."""
    data = get_research(research_id)
    if data:
        st.session_state.current_research_id = data["id"]
        st.session_state.reference = data["reference"]
        st.session_state.analysis = data["analysis"]
        st.session_state.outline = data["outline"]
        convos = get_conversations(research_id)
        st.session_state.messages = [
            {"role": c["role"], "content": c["content"]} for c in convos
        ]


# --- Sidebar ---
with st.sidebar:
    st.title("설교 연구 챗봇")
    st.caption("매주 설교 준비를 돕는 AI 도우미")

    st.divider()

    api_key = st.text_input(
        "Anthropic API Key",
        type="password",
        value=st.session_state.api_key,
        help="Claude API 키를 입력하세요",
    )
    if api_key:
        st.session_state.api_key = api_key

    st.divider()

    # New Research button
    if st.button("새 설교 연구 시작", use_container_width=True, type="primary"):
        _reset_research()

    st.divider()

    # Search
    search_query = st.text_input("이전 연구 검색", placeholder="키워드 입력...")
    if search_query:
        results = search_researches(search_query)
        if results:
            for r in results:
                if st.button(f"{r['reference']} ({r['created_at'][:10]})", key=f"search_{r['id']}"):
                    _load_research(r["id"])
        else:
            st.caption("검색 결과가 없습니다.")

    st.divider()

    # Recent researches
    st.subheader("최근 연구")
    researches = get_all_researches()
    for r in researches[:10]:
        if st.button(
            f"{r['reference']} ({r['created_at'][:10]})",
            key=f"hist_{r['id']}",
            use_container_width=True,
        ):
            _load_research(r["id"])


# --- Main Content ---
if not st.session_state.api_key:
    st.warning("왼쪽 사이드바에서 Anthropic API Key를 입력해주세요.")
    st.stop()

# --- New Research Flow ---
if not st.session_state.current_research_id:
    st.header("새 설교 연구")
    st.markdown("성경 구절을 입력하면 본문 분석과 설교 개요를 생성합니다.")

    col1, col2 = st.columns([3, 1])
    with col1:
        reference = st.text_input(
            "성경 구절",
            placeholder="예: 요한복음 3:16, 로마서 8:1-11, 시편 23편",
        )
    with col2:
        st.write("")  # spacing
        start_btn = st.button("연구 시작", type="primary", disabled=not reference)

    if start_btn and reference:
        research_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        st.session_state.reference = reference
        st.session_state.current_research_id = research_id

        # Step 1: Passage Analysis
        with st.status("본문 분석 중...", expanded=True) as status:
            st.write("Claude에게 성경 본문 분석을 요청하고 있습니다...")
            analysis = analyze_passage(reference, api_key=st.session_state.api_key)
            st.session_state.analysis = analysis
            status.update(label="본문 분석 완료!", state="complete")

        # Step 2: Sermon Outline
        with st.status("설교 개요 생성 중...", expanded=True) as status:
            st.write("분석을 바탕으로 설교 개요를 작성하고 있습니다...")
            outline = generate_outline(
                reference, analysis, api_key=st.session_state.api_key
            )
            st.session_state.outline = outline
            status.update(label="설교 개요 생성 완료!", state="complete")

        # Save to both storage backends
        md_path = save_as_markdown(research_id, reference, analysis, outline)
        save_research(
            research_id, reference, analysis, outline, markdown_path=str(md_path)
        )

        st.rerun()

# --- Display Research Results ---
if st.session_state.current_research_id and st.session_state.analysis:
    st.header(f"설교 연구: {st.session_state.reference}")

    tab1, tab2, tab3 = st.tabs(["본문 분석", "설교 개요", "추가 질문"])

    with tab1:
        st.markdown(st.session_state.analysis)

    with tab2:
        st.markdown(st.session_state.outline)
        # Download button
        md_content = f"# 설교 연구: {st.session_state.reference}\n\n"
        md_content += f"## 본문 분석\n\n{st.session_state.analysis}\n\n"
        md_content += f"## 설교 개요\n\n{st.session_state.outline}\n"
        st.download_button(
            "Markdown으로 다운로드",
            data=md_content,
            file_name=f"sermon_{st.session_state.reference.replace(' ', '_')}.md",
            mime="text/markdown",
        )

    with tab3:
        st.markdown("설교 준비에 대해 추가 질문을 할 수 있습니다.")

        # Display conversation history
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        # Chat input
        if prompt := st.chat_input("추가 질문을 입력하세요..."):
            # Show user message
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            # Get AI response
            with st.chat_message("assistant"):
                with st.spinner("답변 생성 중..."):
                    response = ask_followup(
                        st.session_state.reference,
                        st.session_state.analysis,
                        st.session_state.outline,
                        st.session_state.messages[:-1],  # exclude current question
                        prompt,
                        api_key=st.session_state.api_key,
                    )
                    st.markdown(response)

            st.session_state.messages.append(
                {"role": "assistant", "content": response}
            )

            # Save conversation to DB
            save_conversation(
                st.session_state.current_research_id, "user", prompt
            )
            save_conversation(
                st.session_state.current_research_id, "assistant", response
            )
