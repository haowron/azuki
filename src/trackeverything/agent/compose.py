"""既定の部品グラフ。ローカル方式の最初の main は package 自身の 5 フォルダ(この版)から作られる。

Agent は ``dispatch``(唯一の入口)・``general``(基本 Tool を持つ LLM ループ)・``optimizer``(Host の optimizer の入口)の 3 つ。
基本 Tool(``read``・``write``・``edit``・``bash``)は Host の BaseAgent が内蔵する。mesh と Skill を束縛する。Source はない。
"""

from __future__ import annotations

from pathlib import Path

from trackeverything.agent.dispatch import DispatchAgent
from trackeverything.agent.general import GeneralAgent
from trackeverything.agent.optimizer import OptimizerAgent
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.source import Source
from trackeverything.core.builtin.callable_mesh import wire_full_mesh
from trackeverything.core.builtin.compose import ComponentGraph, HostServices, build_agents
from trackeverything.tools.skill import bind_skills

#: 版の全 Agent。名前ごとに一つの実例を作る。
AGENTS: tuple[type[BaseAgent], ...] = (DispatchAgent, GeneralAgent, OptimizerAgent)
#: Runtime が実行する入口。
ENTRANCES: tuple[str, ...] = ("dispatch",)
#: 名前指定の実行の ``source`` キー → Source。既定の部品には Source がない。
SOURCES: dict[str, type[Source]] = {}


def build(services: HostServices, *, skills_root: Path | None = None) -> ComponentGraph:
    """共有サービスからこの版の部品グラフを作る。業務処理・通信・LLM 呼出しは行わない。"""
    agents = build_agents(AGENTS, services.llms, logger=services.logger)
    if services.full_mesh:
        wire_full_mesh(agents)
    bind_skills(agents, root=skills_root)
    return ComponentGraph(agents=agents, entrances=frozenset(ENTRANCES), sources=dict(SOURCES))
