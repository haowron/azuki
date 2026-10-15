"""一つの版の部品グラフと宣言一覧を catalog の値へ変換する。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import ModuleType
from typing import Any, Literal, cast

from pydantic import BaseModel

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.harness import PostHarness, PreHarness
from trackeverything.core.abc.source import Source
from trackeverything.core.abc.tool import Tool


class ToolView(BaseModel):
    """One tool: its LLM-facing contract plus which live agents use it."""

    name: str
    description: str
    # JSON schema shape handed to the LLM (``arguments`` ClassVar).
    arguments: dict[str, Any]
    module: str
    cls: str
    # Agent names whose live graph registers this tool; empty = 未接続.
    used_by: list[str]


class HarnessView(BaseModel):
    """One harness gate: phase and which live agents it guards."""

    name: str
    description: str
    phase: Literal["pre", "post"]
    module: str
    cls: str
    # Agent names declaring this harness; empty = 未接続.
    guards: list[str]


class AgentView(BaseModel):
    """One agent: callable metadata plus its position in the live graph."""

    name: str
    description: str
    arguments: dict[str, Any]
    module: str
    cls: str
    # The agent's own name when the version lists it as an entrance; empty = 子専用 / 未接続.
    entrances: list[str]
    # Agents delegating to this one through ``agents`` declarations.
    parents: list[str]
    children: list[str]
    tools: list[str]
    pre: list[str]
    post: list[str]


class SourceView(BaseModel):
    """One registered source class: ``source`` key plus provenance."""

    key: str
    description: str
    module: str
    cls: str


class CatalogView(BaseModel):
    """Envelope for ``GET /api/catalog``."""

    agents: list[AgentView]
    tools: list[ToolView]
    harnesses: list[HarnessView]
    sources: list[SourceView]
    # Entrances that receive external event payloads directly (the dispatch Agent).
    webhook_entrances: list[str]


def _doc_summary(text: str | None) -> str:
    """First paragraph of a docstring, joined to one line."""
    if not text:
        return ""
    paragraph = text.strip().split("\n\n", 1)[0]
    return " ".join(line.strip() for line in paragraph.splitlines()).strip()


def _meta_name(obj: object, fallback: str) -> str:
    name = str(getattr(obj, "name", "") or "")
    return name or fallback


def _attr(obj: object, key: str, default: Any) -> Any:
    value = getattr(obj, key, default)
    return default if value is None else value


def _module_name(cls: type) -> str:
    """表示用の module 名。版の予約 namespace の接頭辞(``_te_versions.<版名>.``)は外す。"""
    name = cls.__module__
    if name.startswith("_te_versions."):
        parts = name.split(".", 2)
        return parts[2] if len(parts) == 3 else name
    return name


def _provenance(cls: type) -> tuple[str, str]:
    return _module_name(cls), cls.__qualname__


def _declared_in[T: type](modules: Mapping[str, ModuleType], package: str, bases: tuple[T, ...]) -> list[T]:
    """版の読込み済み module から宣言クラスを集める。新たな import はしない。"""
    found: set[type] = set()
    for logical, module in modules.items():
        if logical != package and not logical.startswith(package + "."):
            continue
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, bases)
                and value not in bases
                and value.__module__ == module.__name__
                and not value.__name__.startswith("_")
            ):
                found.add(value)
    return cast("list[T]", sorted(found, key=lambda cls: (cls.__name__, cls.__module__)))


def build_catalog(
    graph_agents: Mapping[str, BaseAgent],
    sources: Mapping[str, type[Source]],
    entrances: Iterable[str] = (),
    *,
    webhook_entrances: Sequence[str] = (),
    declared_agents: Sequence[type[BaseAgent]] = (),
    declared_tools: Sequence[type[Tool]] = (),
    declared_harnesses: Sequence[type[PreHarness | PostHarness]] = (),
) -> CatalogView:
    """版の全 Agent と各 Agent の子の名前から接続関係を作り、同じ版の未接続の宣言も一覧に含める。"""

    entrances_by_name: dict[str, list[str]] = {name: [name] for name in entrances}

    agents: dict[str, AgentView] = {}
    tools: dict[str, ToolView] = {}
    harnesses: dict[tuple[str, Literal["pre", "post"]], HarnessView] = {}
    live_agent_classes: set[type[BaseAgent]] = set()
    live_tool_classes: set[type[Tool]] = set()
    live_harness_classes: set[type[PreHarness] | type[PostHarness]] = set()

    def tool_entry(tool: Tool) -> ToolView:
        name = _meta_name(tool, type(tool).__name__)
        live_tool_classes.add(type(tool))
        existing = tools.get(name)
        if existing is None:
            module, cls = _provenance(type(tool))
            existing = ToolView(
                name=name,
                description=str(getattr(tool, "description", "") or ""),
                arguments=_attr(tool, "arguments", {}),
                module=module,
                cls=cls,
                used_by=[],
            )
            tools[name] = existing
        return existing

    def harness_entry(harness: PreHarness | PostHarness, phase: Literal["pre", "post"]) -> HarnessView:
        name = _meta_name(harness, type(harness).__name__)
        live_harness_classes.add(type(harness))
        key = (name, phase)
        existing = harnesses.get(key)
        if existing is None:
            module, cls = _provenance(type(harness))
            existing = HarnessView(
                name=name,
                description=str(getattr(harness, "description", "") or ""),
                phase=phase,
                module=module,
                cls=cls,
                guards=[],
            )
            harnesses[key] = existing
        return existing

    def agent_entry(agent: BaseAgent) -> AgentView:
        name = _meta_name(agent, type(agent).__name__)
        live_agent_classes.add(type(agent))
        existing = agents.get(name)
        if existing is None:
            module, cls = _provenance(type(agent))
            existing = AgentView(
                name=name,
                description=str(getattr(agent, "description", "") or ""),
                arguments=_attr(agent, "arguments", {}),
                module=module,
                cls=cls,
                entrances=entrances_by_name.get(name, []),
                parents=[],
                children=[],
                tools=[],
                pre=[],
                post=[],
            )
            agents[name] = existing
        return existing

    def wire_tools(agent: BaseAgent, entry: AgentView) -> None:
        for tool in agent.list_tools():
            tool_name = _meta_name(tool, type(tool).__name__)
            if tool_name not in entry.tools:
                entry.tools.append(tool_name)
            used = tool_entry(tool)
            if entry.name not in used.used_by:
                used.used_by.append(entry.name)

    def wire_harnesses(agent: BaseAgent, entry: AgentView) -> None:
        for pre_harness in agent.list_pre_harnesses():
            name = _meta_name(pre_harness, type(pre_harness).__name__)
            if name not in entry.pre:
                entry.pre.append(name)
            guarded = harness_entry(pre_harness, "pre")
            if entry.name not in guarded.guards:
                guarded.guards.append(entry.name)
        for post_harness in agent.list_post_harnesses():
            name = _meta_name(post_harness, type(post_harness).__name__)
            if name not in entry.post:
                entry.post.append(name)
            guarded = harness_entry(post_harness, "post")
            if entry.name not in guarded.guards:
                guarded.guards.append(entry.name)

    for agent in graph_agents.values():
        entry = agent_entry(agent)
        wire_tools(agent, entry)
        wire_harnesses(agent, entry)
        for child_name in agent.list_agent_names():
            child = graph_agents.get(child_name)
            if child is None:
                continue
            child_entry = agent_entry(child)
            if entry.name not in child_entry.parents:
                child_entry.parents.append(entry.name)
            if child_entry.name not in entry.children:
                entry.children.append(child_entry.name)

    for agent_cls in declared_agents:
        if agent_cls in live_agent_classes:
            continue
        name = _meta_name(agent_cls, agent_cls.__name__)
        if name in agents:
            continue
        module, qualname = _provenance(agent_cls)
        agents[name] = AgentView(
            name=name,
            description=str(getattr(agent_cls, "description", "") or ""),
            arguments=_attr(agent_cls, "arguments", {}),
            module=module,
            cls=qualname,
            entrances=[],
            parents=[],
            children=[str(child) for child in getattr(agent_cls, "agents", ())],
            tools=[_meta_name(tool, tool.__name__) for tool in getattr(agent_cls, "tools", ())],
            pre=[_meta_name(h, h.__name__) for h in getattr(agent_cls, "pre", ())],
            post=[_meta_name(h, h.__name__) for h in getattr(agent_cls, "post", ())],
        )
    for tool_cls in declared_tools:
        name = _meta_name(tool_cls, tool_cls.__name__)
        if tool_cls in live_tool_classes or name in tools:
            continue
        module, qualname = _provenance(tool_cls)
        tools[name] = ToolView(
            name=name,
            description=str(getattr(tool_cls, "description", "") or ""),
            arguments=_attr(tool_cls, "arguments", {}),
            module=module,
            cls=qualname,
            used_by=[],
        )
    for harness_cls in declared_harnesses:
        name = _meta_name(harness_cls, harness_cls.__name__)
        phase: Literal["pre", "post"] = "pre" if issubclass(harness_cls, PreHarness) else "post"
        if harness_cls in live_harness_classes or (name, phase) in harnesses:
            continue
        module, qualname = _provenance(harness_cls)
        harnesses[(name, phase)] = HarnessView(
            name=name,
            description=str(getattr(harness_cls, "description", "") or ""),
            phase=phase,
            module=module,
            cls=qualname,
            guards=[],
        )

    agent_items = sorted(agents.values(), key=lambda view: (not view.entrances, view.name))
    tool_items = sorted(tools.values(), key=lambda view: view.name)
    harness_items = sorted(harnesses.values(), key=lambda view: (view.phase, view.name))
    source_items = sorted(
        (
            SourceView(
                key=key,
                description=_doc_summary(cls.__doc__),
                module=_module_name(cls),
                cls=cls.__qualname__,
            )
            for key, cls in sources.items()
        ),
        key=lambda view: view.key,
    )
    return CatalogView(
        agents=agent_items,
        tools=tool_items,
        harnesses=harness_items,
        sources=source_items,
        webhook_entrances=sorted(set(webhook_entrances)),
    )
