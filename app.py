"""Streamlit GUI for the agentic blog generator.

    streamlit run app.py
"""

import re
import time
from pathlib import Path

import streamlit as st

import blog_pipeline as bp

st.set_page_config(page_title="Agentic Blog Generator", page_icon="📝", layout="wide")

IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

NODE_LABELS = {
    "router": "🧭 router — deciding whether to research",
    "researcher": "🔎 researcher — searching the web",
    "orchestrator": "🗂️ orchestrator — planning sections",
    "worker": "✍️ worker — writing a section",
    "art_director": "🎨 art_director — planning images",
    "illustrator": "🖼️ illustrator — generating an image",
    "assembler": "🧩 assembler — placing images",
    "reducer": "📎 reducer — assembling the draft",
    "refiner": "✨ refiner — polishing",
}


def describe(node: str, payload: dict) -> str:
    """One-line summary of what a node produced."""
    if not payload:
        return ""
    if node == "router":
        flag = "yes" if payload.get("needs_research") else "no"
        return f"research: **{flag}** — {payload.get('research_reason', '')}"
    if node == "researcher":
        return f"found **{len(payload.get('sources', []))}** unique sources"
    if node == "orchestrator":
        plan = payload.get("plan")
        return f"**{plan.blog_title}** — {len(plan.tasks)} sections" if plan else ""
    if node == "worker":
        section = (payload.get("sections") or [""])[0]
        heading = section.strip().split("\n")[0].lstrip("# ").strip()
        return f"wrote **{heading}** ({len(section):,} chars)"
    if node == "art_director":
        return f"planned **{len(payload.get('image_specs', []))}** images"
    if node == "illustrator":
        img = (payload.get("images") or [{}])[0]
        if img.get("path"):
            return f"slot {img.get('slot')} → `{img['path']}`"
        return f"slot {img.get('slot')} failed — {img.get('error', '')}"
    if node == "reducer":
        ok = sum(1 for i in payload.get("images", []) if i.get("path"))
        total = len(payload.get("images", []))
        return f"draft {len(payload.get('draft', '')):,} chars, images {ok}/{total} generated"
    if node == "refiner":
        return f"final **{len(payload.get('final', '')):,}** chars"
    return ""


def render_blog(md: str) -> None:
    """st.markdown will not load local image files, so split the markdown on image
    embeds and hand the local paths to st.image."""
    pos = 0
    for match in IMG_RE.finditer(md):
        chunk = md[pos : match.start()]
        if chunk.strip():
            st.markdown(chunk)

        alt, path = match.group(1), match.group(2)
        if Path(path).exists():
            st.image(path, caption=alt, width="stretch")
        else:
            st.warning(f"image not on disk: `{path}`")
        pos = match.end()

    tail = md[pos:]
    if tail.strip():
        st.markdown(tail)


# ------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("📝 Blog Generator")
    st.caption("LangGraph · Gemini · Tavily")

    topic = st.text_area("Topic", value="Global Warming", height=90)

    research_mode = st.radio(
        "Web research",
        ["Let the router decide", "Always research", "Never research"],
        help="The router node normally chooses. Override it to force either branch.",
    )
    force_research = {
        "Let the router decide": None,
        "Always research": True,
        "Never research": False,
    }[research_mode]

    max_results = st.slider("Tavily results per query", 2, 8, 4)
    enable_images = st.checkbox("Generate images", value=True)

    if enable_images:
        st.caption(
            f"Image model `{bp.IMAGE_MODEL}` needs billing enabled on your Google API "
            "key. Without it each image fails and the blog renders a placeholder."
        )

    run = st.button("Generate blog", type="primary", width="stretch")

    trace = bp.tracing_status()
    if trace["enabled"]:
        st.caption(
            f"🔍 LangSmith tracing on → [{trace['project']}]({trace['url']})"
        )
    else:
        st.caption("🔍 LangSmith tracing off (set `LANGSMITH_TRACING=true`)")

    st.divider()
    st.caption("Graph")
    st.code(
        "router\n"
        "  ├─ researcher ─┐\n"
        "  └──────────────┤\n"
        "           orchestrator\n"
        "                 │ fan-out\n"
        "              worker × N\n"
        "                 │\n"
        "              reducer ── art_director\n"
        "                 │       illustrator × N+1\n"
        "                 │       assembler\n"
        "              refiner",
        language=None,
    )

# ---------------------------------------------------------------------------- main

if "result" not in st.session_state:
    st.session_state.result = None
    st.session_state.trace = []

if run:
    if not topic.strip():
        st.error("Give me a topic first.")
        st.stop()

    st.session_state.result = None
    st.session_state.trace = []
    started = time.time()

    progress = st.container()
    try:
        with progress:
            st.subheader("Run")
            for namespace, node, payload in bp.stream_blog(
                topic.strip(),
                enable_images=enable_images,
                force_research=force_research,
                max_results=max_results,
            ):
                if node == "__final__":
                    st.session_state.result = payload
                    break

                where = "reducer ▸ " if namespace else ""
                label = NODE_LABELS.get(node, node)
                note = describe(node, payload)

                st.session_state.trace.append(
                    {"node": f"{where}{node}", "detail": note}
                )
                with st.status(f"{where}{label}", state="complete", expanded=False):
                    st.markdown(note or "_no output_")

        st.success(f"Done in {time.time() - started:.1f}s")

    except Exception as e:
        st.error(f"Run failed: {type(e).__name__}: {e}")
        st.exception(e)

result = st.session_state.result

if result is None:
    st.title("Agentic Blog Generator")
    st.markdown(
        "Set a topic in the sidebar and hit **Generate blog**. The router decides "
        "whether the topic needs live web research, an orchestrator plans the "
        "sections, workers write them in parallel, an image subgraph illustrates "
        "them, and a refiner polishes the result."
    )
    st.stop()

blog_tab, images_tab, sources_tab, raw_tab, trace_tab = st.tabs(
    ["Blog", "Images", "Sources", "Markdown", "Trace"]
)

with blog_tab:
    render_blog(result.get("final", ""))

with images_tab:
    images = sorted(result.get("images", []), key=lambda i: i["slot"])
    if not images:
        st.info("Image generation was disabled for this run.")
    else:
        ok = [i for i in images if i["path"]]
        st.caption(f"{len(ok)}/{len(images)} generated")
        for img in images:
            label = "Hero" if img["slot"] == 0 else f"Section {img['slot']}"
            with st.expander(f"{label} — {img['alt']}", expanded=bool(img["path"])):
                if img["path"] and Path(img["path"]).exists():
                    st.image(img["path"], width="stretch")
                else:
                    st.warning(img["error"] or "no image produced")
                st.caption("Prompt")
                st.code(img["prompt"], language=None)

with sources_tab:
    sources = result.get("sources", [])
    if not sources:
        st.info("This run took the no-research branch.")
    else:
        for i, s in enumerate(sources, start=1):
            st.markdown(f"**[{i}]** [{s['title']}]({s['url']})")
            st.caption(s["content"][:300] + "…")

with raw_tab:
    plan = result.get("plan")
    if plan:
        st.download_button(
            "Download .md",
            data=result.get("final", ""),
            file_name=bp.slugify(plan.blog_title) + ".md",
            mime="text/markdown",
        )
        st.caption(f"Also saved to `{bp.OUTPUT_DIR}/{bp.slugify(plan.blog_title)}.md`")
    st.code(result.get("final", ""), language="markdown")

with trace_tab:
    st.caption(
        f"router: {'research' if result.get('needs_research') else 'no research'}"
        f" — {result.get('research_reason', '')}"
    )
    st.dataframe(st.session_state.trace, width="stretch", hide_index=True)
