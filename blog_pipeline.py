"""The blog-generation graph, extracted from notebooks/blog_research_with_image_generation.ipynb
so a GUI (or any other caller) can import it.

    router -> (researcher | skip) -> orchestrator -> worker x N -> reducer -> refiner

`reducer` runs an image subgraph: art_director -> illustrator x N+1 -> assembler.
"""

from typing import TypedDict, Annotated, List, Optional
from pathlib import Path
import operator
import base64
import os
import re

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.tools.tavily_search import TavilySearchResults
import requests

# Loads GOOGLE_API_KEY, TAVILY_API_KEY and the LANGSMITH_* vars. LangChain and
# LangGraph pick tracing up from the environment, so importing this module is all
# it takes to trace every node, LLM call and tool call.
load_dotenv(Path(__file__).parent / ".env")

TEXT_MODEL = "gemini-3.5-flash-lite"
IMAGE_MODEL = "gemini-3.1-flash-image"
IMAGE_DIR = Path("images")
OUTPUT_DIR = Path("output")


def tracing_status() -> dict:
    """Whether LangSmith tracing is on, for display in a UI."""
    enabled = os.getenv("LANGSMITH_TRACING", "").lower() == "true"
    project = os.getenv("LANGSMITH_PROJECT") or "default"
    return {
        "enabled": enabled and bool(os.getenv("LANGSMITH_API_KEY")),
        "project": project,
        "url": f"https://smith.langchain.com/o/me/projects/p/{project}",
    }


# --------------------------------------------------------------------------- schemas


class ResearchDecision(BaseModel):
    needs_research: bool = Field(
        ..., description="True if the blog needs current facts, data or citations from the web"
    )
    reason: str = Field(..., description="One short sentence explaining the decision")


class SearchQueries(BaseModel):
    queries: List[str] = Field(..., description="3-4 focused web search queries")


class Task(BaseModel):
    id: int
    title: str
    brief: str = Field(..., description="What to cover")


class Plan(BaseModel):
    blog_title: str
    tasks: List[Task]


class ImageIdea(BaseModel):
    alt: str = Field(..., description="Accessible alt text, one short sentence")
    prompt: str = Field(
        ..., description="Image generation prompt: subject, style, palette, composition"
    )


class ImagePlan(BaseModel):
    hero: ImageIdea = Field(..., description="The banner image for the top of the blog")
    per_section: List[ImageIdea] = Field(
        ..., description="One idea per section, in the same order as the sections given"
    )


class ImageSpec(BaseModel):
    """An ImageIdea with its slot assigned by us, not by the model."""

    slot: int
    alt: str
    prompt: str


class State(TypedDict):
    topic: str
    force_research: Optional[bool]
    needs_research: bool
    research_reason: str
    research: str
    sources: List[dict]
    plan: Plan
    sections: Annotated[List[str], operator.add]
    images: List[dict]
    draft: str
    final: str


class ImageState(TypedDict):
    """Own schema. Invoked explicitly by the `reducer` node rather than registered as
    a subgraph node: sharing the `sections` key would echo sections back into the
    parent, where operator.add would append them a second time."""

    plan: Plan
    sections: List[str]
    image_specs: List[ImageSpec]
    images: Annotated[List[dict], operator.add]
    draft: str


# --------------------------------------------------------------------------- clients

llm = ChatGoogleGenerativeAI(model=TEXT_MODEL)


def make_search(max_results: int = 4):
    return TavilySearchResults(max_results=max_results)


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug or "blog"


# ----------------------------------------------------------------------- image calls


def generate_image(prompt: str, filename: str) -> dict:
    """Call the Gemini image model over REST. Returns {"path", "error"}; never raises.

    The key goes in a header, not the query string: an HTTPError message echoes the
    request URL, which would leak the key into logs and notebook output.
    """
    IMAGE_DIR.mkdir(exist_ok=True)

    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGE_MODEL}:generateContent",
            headers={"x-goog-api-key": os.environ["GOOGLE_API_KEY"]},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=180,
        )
        response.raise_for_status()

        for part in response.json()["candidates"][0]["content"]["parts"]:
            if "inlineData" in part:
                path = IMAGE_DIR / filename
                path.write_bytes(base64.b64decode(part["inlineData"]["data"]))
                return {"path": str(path), "error": None}

        return {"path": None, "error": "response contained no image part"}

    except Exception as e:
        return {"path": None, "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ----------------------------------------------------------------------------- nodes


def router(state: State) -> dict:
    override = state.get("force_research")
    if override is not None:
        return {
            "needs_research": override,
            "research_reason": f"forced by caller (force_research={override})",
        }

    decision = llm.with_structured_output(ResearchDecision).invoke(
        [
            SystemMessage(
                content=(
                    "Decide whether writing a blog on this topic needs live web research.\n\n"
                    "Ask: would a good blog on this topic state any figure, date, "
                    "version, price, or claim about the CURRENT state of the world?\n"
                    "  yes -> needs_research = true. Err on the side of true; stale "
                    "numbers are worse than a wasted search.\n"
                    "  no  -> needs_research = false. Reserve this for genuinely "
                    "timeless pieces: conceptual explainers, opinion, how-to, "
                    "creative writing, or advice with no factual claims to date."
                )
            ),
            HumanMessage(content=f"Topic: {state['topic']}"),
        ]
    )

    return {
        "needs_research": decision.needs_research,
        "research_reason": decision.reason,
    }


def route_research(state: State) -> str:
    return "researcher" if state["needs_research"] else "orchestrator"


def make_researcher(max_results: int):
    search = make_search(max_results)

    def researcher(state: State) -> dict:
        queries = llm.with_structured_output(SearchQueries).invoke(
            [
                SystemMessage(
                    content="Write 3-4 focused web search queries to research this blog topic."
                ),
                HumanMessage(content=f"Topic: {state['topic']}"),
            ]
        )

        seen, sources = set(), []
        for q in queries.queries:
            for hit in search.invoke({"query": q}):
                if hit["url"] in seen:
                    continue
                seen.add(hit["url"])
                sources.append(hit)

        notes = "\n\n".join(
            f"[{i}] {s['title']}\n{s['url']}\n{s['content']}"
            for i, s in enumerate(sources, start=1)
        )

        return {"research": notes, "sources": sources}

    return researcher


def orchestrator(state: State) -> dict:
    research = state.get("research", "")

    plan = llm.with_structured_output(Plan).invoke(
        [
            SystemMessage(
                content=(
                    "Create a Blog Plan with 3-5 sections on the following topic. "
                    + (
                        "Ground the plan in the supplied research notes."
                        if research
                        else "Plan from general knowledge; no research was gathered."
                    )
                )
            ),
            HumanMessage(
                content=f"Topic: {state['topic']}"
                + (f"\n\nResearch notes:\n{research}" if research else "")
            ),
        ]
    )

    return {"plan": plan}


def distributer(state: State):
    return [
        Send(
            "worker",
            {
                "task": task,
                "topic": state["topic"],
                "plan": state["plan"],
                "research": state.get("research", ""),
            },
        )
        for task in state["plan"].tasks
    ]


def worker(payload: dict) -> dict:
    task = payload["task"]
    plan = payload["plan"]
    research = payload["research"]

    if research:
        instruction = (
            "Write one clean Markdown section. Use only facts supported by the "
            "research notes. Cite sources inline as [1], [2] matching the notes."
        )
    else:
        instruction = (
            "Write one clean Markdown section from general knowledge. "
            "Do not fabricate statistics or citations."
        )

    section_md = llm.invoke(
        [
            SystemMessage(content=instruction),
            HumanMessage(
                content=(
                    f"Blog: {plan.blog_title}\n"
                    f"Topic: {payload['topic']}\n\n"
                    f"Section: {task.title}\n"
                    f"Brief: {task.brief}\n\n"
                    + (f"Research notes:\n{research}\n\n" if research else "")
                    + "Return only the section content in Markdown."
                )
            ),
        ]
    ).content.strip()

    return {"sections": [section_md]}


# ------------------------------------------------------------------- image subgraph


def art_director(state: ImageState) -> dict:
    headings = [s.strip().split("\n")[0].lstrip("# ").strip() for s in state["sections"]]
    listed = "\n".join(f"{i}. {h}" for i, h in enumerate(headings, start=1))

    plan = llm.with_structured_output(ImagePlan).invoke(
        [
            SystemMessage(
                content=(
                    "You are the art director for a blog post. Plan one hero image, plus "
                    f"exactly {len(headings)} section images -- one per section, in the "
                    "same order the sections are listed.\n"
                    "Every prompt must describe a clean editorial illustration: name the "
                    "subject, style, palette and composition. No text or lettering in the "
                    "image, no logos, no real identifiable people."
                )
            ),
            HumanMessage(content=f"Blog: {state['plan'].blog_title}\n\nSections:\n{listed}"),
        ]
    )

    # slots assigned by position, so a short or long list can never mis-place an image
    specs = [ImageSpec(slot=0, alt=plan.hero.alt, prompt=plan.hero.prompt)]
    for i, idea in enumerate(plan.per_section[: len(headings)], start=1):
        specs.append(ImageSpec(slot=i, alt=idea.alt, prompt=idea.prompt))

    return {"image_specs": specs}


def distribute_images(state: ImageState):
    return [Send("illustrator", {"spec": spec}) for spec in state["image_specs"]]


def illustrator(payload: dict) -> dict:
    spec = payload["spec"]
    result = generate_image(spec.prompt, f"slot_{spec.slot}.png")

    return {
        "images": [
            {
                "slot": spec.slot,
                "alt": spec.alt,
                "prompt": spec.prompt,
                "path": result["path"],
                "error": result["error"],
            }
        ]
    }


def embed(image: dict) -> str:
    """Markdown for one image, or a visible placeholder if generation failed."""
    if image["path"]:
        return f"![{image['alt']}]({image['path']})"
    return f"> **[image unavailable]** {image['alt']}\n>\n> _prompt: {image['prompt']}_"


def assembler(state: ImageState) -> dict:
    by_slot = {img["slot"]: img for img in state["images"]}
    parts = [f"# {state['plan'].blog_title}"]

    if 0 in by_slot:
        parts.append(embed(by_slot[0]))

    for i, section in enumerate(state["sections"], start=1):
        parts.append(section.strip())
        if i in by_slot:
            parts.append(embed(by_slot[i]))

    return {"draft": "\n\n".join(parts) + "\n"}


def build_image_subgraph():
    g = StateGraph(ImageState)
    g.add_node("art_director", art_director)
    g.add_node("illustrator", illustrator)
    g.add_node("assembler", assembler)

    g.add_edge(START, "art_director")
    g.add_conditional_edges("art_director", distribute_images, ["illustrator"])
    g.add_edge("illustrator", "assembler")
    g.add_edge("assembler", END)
    return g.compile()


# --------------------------------------------------------------- reducer and refiner


def make_reducer(enable_images: bool):
    subgraph = build_image_subgraph() if enable_images else None

    def reducer(state: State) -> dict:
        if subgraph is None:
            body = "\n\n".join(state["sections"]).strip()
            return {
                "draft": f"# {state['plan'].blog_title}\n\n{body}\n",
                "images": [],
            }

        result = subgraph.invoke(
            {"plan": state["plan"], "sections": state["sections"]}
        )
        return {"draft": result["draft"], "images": result["images"]}

    return reducer


def refiner(state: State) -> dict:
    polished = llm.invoke(
        [
            SystemMessage(
                content=(
                    "Refine this blog draft. Remove repetition across sections, smooth the "
                    "transitions, and do not invent new facts.\n"
                    "Reproduce EXACTLY, character for character and in the same positions: "
                    "every inline [n] citation, every ![alt](path) image embed, and every "
                    "'> **[image unavailable]**' placeholder block. Never drop, reword, "
                    "reorder or renumber them.\n"
                    "Return the full blog in Markdown."
                )
            ),
            HumanMessage(content=state["draft"]),
        ]
    ).content.strip()

    sources = state.get("sources", [])
    if sources:
        refs = "\n".join(
            f"{i}. [{s['title']}]({s['url']})" for i, s in enumerate(sources, start=1)
        )
        final_md = f"{polished}\n\n## Sources\n\n{refs}\n"
    else:
        final_md = f"{polished}\n"

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / (slugify(state["plan"].blog_title) + ".md")).write_text(
        final_md, encoding="utf-8"
    )

    return {"final": final_md}


# ----------------------------------------------------------------------------- graph


def build_workflow(enable_images: bool = True, max_results: int = 4):
    g = StateGraph(State)
    g.add_node("router", router)
    g.add_node("researcher", make_researcher(max_results))
    g.add_node("orchestrator", orchestrator)
    g.add_node("worker", worker)
    g.add_node("reducer", make_reducer(enable_images))
    g.add_node("refiner", refiner)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", route_research, ["researcher", "orchestrator"])
    g.add_edge("researcher", "orchestrator")
    g.add_conditional_edges("orchestrator", distributer, ["worker"])
    g.add_edge("worker", "reducer")
    g.add_edge("reducer", "refiner")
    g.add_edge("refiner", END)
    return g.compile()


def stream_blog(
    topic: str,
    enable_images: bool = True,
    force_research: Optional[bool] = None,
    max_results: int = 4,
):
    """Run the graph, yielding (namespace, node_name, payload) as each node finishes.

    Namespace is () for parent-graph nodes and ("reducer:<id>",) for image-subgraph
    nodes. The last item yielded is ((), "__final__", accumulated_state).
    """
    workflow = build_workflow(enable_images=enable_images, max_results=max_results)
    state: dict = {"topic": topic, "sections": [], "images": []}

    # names/tags/metadata land on the LangSmith trace, so runs are searchable by
    # topic and comparable across settings instead of all being called "LangGraph"
    config = {
        "run_name": f"blog: {topic[:60]}",
        "tags": [
            "blog-generator",
            f"images:{'on' if enable_images else 'off'}",
            f"research:{'auto' if force_research is None else force_research}",
        ],
        "metadata": {
            "topic": topic,
            "text_model": TEXT_MODEL,
            "image_model": IMAGE_MODEL if enable_images else None,
            "enable_images": enable_images,
            "force_research": force_research,
            "max_results": max_results,
        },
    }

    for namespace, update in workflow.stream(
        {"topic": topic, "sections": [], "force_research": force_research},
        config=config,
        subgraphs=True,
    ):
        for node, payload in update.items():
            yield namespace, node, payload

            # only parent-graph writes count; subgraph updates are progress-only,
            # since the reducer already returns their final values
            if namespace or not payload:
                continue
            for key, value in payload.items():
                if key == "sections":
                    state["sections"] = state["sections"] + value
                else:
                    state[key] = value

    yield (), "__final__", state
