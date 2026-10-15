"""Host が版ごとの合成モジュールへ渡す共有サービスと、合成の結果。

Host は具体的な Agent・Tool・Source を知らない。版の ``agent/compose.py`` が
共有サービスを受け取り、``build_agents`` に Agent 類の一覧を渡して部品グラフを組み立て、
``ComponentGraph`` を返す。子 Agent の実例は束縛せず、実行時に Context の取得口から名前で得る。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.llm import LLMClient
from trackeverything.core.abc.source import Source

if TYPE_CHECKING:
    from trackeverything.core.versioning.repo import Version

#: 版の合成モジュール(版が所有する)。
COMPOSE_MODULE = "trackeverything.agent.compose"
#: 版の全 Agent の LLM 呼出しの先頭に置く文(版が所有する)と、前後の空白を除いた文字数の上限。
SYSTEM_PATH = "agent/SYSTEM.md"
SYSTEM_LIMIT = 8000


@dataclass(frozen=True)
class HostServices:
    """全ての版が共有する Host のサービス。"""

    #: LLM client の名前(``max``・``high``・``low``・``flash``)→ client。Agent は ``llm`` の名前で一つを受け取る。
    llms: Mapping[str, LLMClient]
    logger: Any
    session_store: Any
    full_mesh: bool = False
    #: ``SKILLS_DIR`` で checkout 外を指す場合の固定の Skill 配置先。None なら版の file view。
    skills_root: Path | None = None


@dataclass(frozen=True)
class ComponentGraph:
    """一つの版の部品グラフ。

    ``agents`` は版の全 Agent(名前 → 実例)、``entrances`` は queue の依頼が名前で起動できる入口、
    ``sources`` は ``source`` キー → Source クラス(目録が読む)。``agents`` にない入口は ``ValueError``。
    """

    agents: Mapping[str, BaseAgent]
    entrances: frozenset[str]
    sources: Mapping[str, type[Source]]

    def __post_init__(self) -> None:
        if unknown := sorted(set(self.entrances) - set(self.agents)):
            raise ValueError(f"unknown entrances: {', '.join(unknown)}")


class StaticAgents:
    """固定の対応表から名前で Agent を返す取得口(版を持たない script・テスト用)。"""

    def __init__(self, agents: Mapping[str, BaseAgent]) -> None:
        self._agents = dict(agents)

    def agent(self, name: str) -> BaseAgent | None:
        return self._agents.get(name)


def build_agents(
    classes: Iterable[type[BaseAgent]],
    llms: Mapping[str, LLMClient],
    *,
    logger: Any = None,
    dependencies: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, BaseAgent]:
    """Agent 類の一覧から名前ごとに一つの実例を作り、各類が宣言した子の名前を検証する。

    実例は ``cls(llms[cls.llm], logger=logger, **dependencies[cls.name])`` で一度だけ作る。子は束縛しない。
    名前のない類・重複する名前・``llms`` にない LLM 名・一覧にない子の名前・一覧にない名前への依存は
    ``ValueError`` にする(版の構成の失敗として保存されない)。
    """
    extra = dependencies or {}
    built: dict[str, BaseAgent] = {}
    for cls in classes:
        if not cls.name:
            raise ValueError(f"agent class {cls.__qualname__} has no name")
        if cls.name in built:
            raise ValueError(f"agent name {cls.name!r} is declared by {type(built[cls.name]).__qualname__} and {cls.__qualname__}")
        if cls.llm not in llms:
            raise ValueError(f"agent {cls.name!r} uses unknown llm {cls.llm!r}")
        built[cls.name] = cls(llms[cls.llm], logger=logger, **extra.get(cls.name, {}))
    if unknown := sorted(set(extra) - set(built)):
        raise ValueError(f"dependencies for unknown agents: {', '.join(unknown)}")
    for parent in built.values():
        for child in parent.list_agent_names():
            if child not in built:
                raise ValueError(f"agent {parent.name!r} declares unknown agent {child!r}")
    return built


def prepare_components(services: HostServices) -> Callable[[Version], ComponentGraph]:
    """全 module の読込み後に、版の合成モジュールで部品グラフを作り、版の ``agent/SYSTEM.md`` を全 Agent に束縛する関数。

    SYSTEM.md が UTF-8 でないか、前後の空白を除いて ``SYSTEM_LIMIT`` 文字を超えれば ``ValueError``(版の構成の失敗)。
    """

    def prepare(version: Version) -> ComponentGraph:
        compose = version.module(COMPOSE_MODULE)
        skills_root = services.skills_root or version.view_path("skills")
        graph = compose.build(services, skills_root=skills_root)
        if not isinstance(graph, ComponentGraph):
            raise TypeError(f"{COMPOSE_MODULE}.build returned {type(graph).__name__}, not ComponentGraph")
        try:
            system = version.tree.get(SYSTEM_PATH, b"").decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ValueError(f"{SYSTEM_PATH}: not UTF-8") from None
        if len(system) > SYSTEM_LIMIT:
            raise ValueError(f"{SYSTEM_PATH}: {len(system)} characters exceed the limit of {SYSTEM_LIMIT}")
        for agent in graph.agents.values():
            agent.configure_system(system)
        return graph

    return prepare
