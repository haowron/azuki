"""Experimental full-mesh wiring: every agent can call every tool and agent.

The composition root calls :func:`wire_full_mesh` when ``AGENT_FULL_MESH`` is
enabled. Declared configurations stay untouched; only what an agent cannot
reach by declaration is added — shared tool instances and agent names — so the
LLM itself can pick the best agent/tool combination from one flat callable
list. Child agents are resolved by name at run time through the context's
lookup, like declared children. The default (flag off) keeps each agent at its
declared configuration.
"""

from __future__ import annotations

from collections.abc import Mapping

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.tool import Tool


def wire_full_mesh(agents: Mapping[str, BaseAgent]) -> tuple[int, int]:
    """Expose every tool and every agent name of one version to every agent of it.

    Collect one shared instance per tool name over all agents (first seen
    wins), then add the whole universe to every agent with
    ``extend_callables`` — declared names win and each agent excludes itself.

    Circular delegation this creates is bounded twice: the shared step
    budget ends the run, and ``BaseAgent.MAX_AGENT_DEPTH`` converts
    over-deep nesting into a structured error result before the Python
    stack is at risk. Returns ``(tool_count, agent_count)``.
    """
    tools: dict[str, Tool] = {}
    for agent in agents.values():
        for tool in agent.list_tools():
            tools.setdefault(tool.name, tool)
    names = [name for name in agents if name]
    for agent in agents.values():
        agent.extend_callables(tools, names)
    return len(tools), len(names)
