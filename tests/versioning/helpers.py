"""Small fixtures for the version repository cases (VR01-VR55).

Planned modules are imported inside the helpers so every case is collected
before the implementation exists and fails red instead of being skipped.

The version store is mocked per scene, never emulated: ``version_store`` answers
reads from fixed seeded data and records writes without adding them to that data.
A scene that needs a later read to differ changes ``store.read_main.return_value``
(the mirror's main), ``store.fetch.return_value`` (GitHub's main) or calls ``seed``
between its steps. The git rules themselves are checked against real git in
``test_git_store.py`` and ``test_git_repo.py``.
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import linecache
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import trackeverything.config as config
from tests.fakes import ScriptedLLM
from trackeverything.core.abc.submission import EventRequest

ROOT = "_te_versions"
#: The only entrance the Runtime starts; a test tree that the Runtime drives names its entrance this way.
DISPATCH = config.DISPATCH_AGENT
SEEDED_AT = datetime(2026, 10, 13, tzinfo=UTC)
_ticks = itertools.count()
_events = itertools.count()


def api() -> SimpleNamespace:
    """The planned modules: ``repo``, ``store``, ``snapshot``, ``loader`` and ``compose`` (core.builtin)."""
    modules = {
        "repo": "trackeverything.core.versioning.repo",
        "store": "trackeverything.core.versioning.store",
        "snapshot": "trackeverything.core.versioning.snapshot",
        "loader": "trackeverything.core.versioning.loader",
        "compose": "trackeverything.core.builtin.compose",
    }
    return SimpleNamespace(**{key: importlib.import_module(name) for key, name in modules.items()})


def entry_source(name: str, dependency: str = "vl_dep") -> str:
    """An entrance Agent class ``Entry`` whose response shows which dependency version it saw before and after its hook.

    The response is ``"<VERSION before>-><VERSION after>|<module __name__>"``. ``hook`` and ``status``
    come from the event payload when the Runtime runs it as dispatch (``ctx.data["event"]["payload"]``),
    otherwise from ``ctx.data`` (a direct run). The hook may be a plain callable or a coroutine function
    (awaited inside the run), and ``status`` sets the result status (default ``ok``). It never ACKs.
    """
    return f'''
import inspect

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.tools import {dependency} as dep


class Entry(BaseAgent):
    name = "{name}"
    description = "{name} entrance"

    async def _execute(self, call):
        event = call.ctx.data.get("event")
        options = event["payload"] if isinstance(event, dict) and isinstance(event.get("payload"), dict) else call.ctx.data
        first = dep.VERSION
        hook = options.get("hook")
        if hook is not None:
            outcome = hook()
            if inspect.isawaitable(outcome):
                await outcome
        return AgentResult(status=options.get("status", "ok"), response=f"{{first}}->{{dep.VERSION}}|{{__name__}}")
'''


def event(**payload: Any) -> EventRequest:
    """An ``internal`` EventRequest with a fresh ``external_id``; its payload may hold hooks (functions), so it is built without validation."""
    return EventRequest.model_construct(provider="internal", external_id=f"vr-event-{next(_events)}", payload=payload)


def compose_source(agents: Iterable[str], entrances: Iterable[str], sources: Mapping[str, str] | None = None) -> str:
    """Text of an ``agent/compose.py`` for a test tree.

    ``agents`` are class references relative to the package (``"agent.main.Entry"``), in ``AGENTS`` order.
    ``entrances`` become ``ENTRANCES``. ``sources`` maps a ``SOURCES`` key to a Source class reference
    (``{"s": "sources.s.S"}``). ``build(services, *, skills_root=None)`` returns
    ``ComponentGraph(agents=build_agents(AGENTS, services.llms, logger=services.logger),
    entrances=frozenset(ENTRANCES), sources=dict(SOURCES))``.
    """
    imports: list[str] = []

    def alias(reference: str, prefix: str, index: int) -> str:
        module, _, attribute = reference.rpartition(".")
        imports.append(f"from trackeverything.{module} import {attribute} as {prefix}{index}")
        return f"{prefix}{index}"

    classes = [alias(reference, "_agent", index) for index, reference in enumerate(agents)]
    keyed = {key: alias(reference, "_source", index) for index, (key, reference) in enumerate((sources or {}).items())}
    source_items = ", ".join(f"{key!r}: {name}" for key, name in keyed.items())
    return "\n".join(
        [
            "from trackeverything.core.builtin.compose import ComponentGraph, build_agents",
            *imports,
            "",
            f"AGENTS = ({', '.join(classes)}{',' if len(classes) == 1 else ''})",
            f"ENTRANCES = {tuple(entrances)!r}",
            f"SOURCES = {{{source_items}}}",
            "",
            "",
            "def build(services, *, skills_root=None):",
            "    return ComponentGraph(",
            "        agents=build_agents(AGENTS, services.llms, logger=services.logger),",
            "        entrances=frozenset(ENTRANCES),",
            "        sources=dict(SOURCES),",
            "    )",
            "",
        ]
    )


def minimal_tree(entries: Iterable[str] = ("main",), extra: Mapping[str, str | bytes | None] | None = None, *, compose: str | None = None) -> dict[str, bytes]:
    """A loadable five-folder tree (package-relative POSIX path -> bytes).

    Shape:

    - ``agent/__init__.py`` and ``tools/__init__.py``: empty.
    - ``tools/vl_dep.py``: ``VERSION = 1``.
    - ``agent/<name>.py`` for each entry: ``entry_source(name)`` (class ``Entry``, llm ``max``).
    - ``agent/compose.py``: ``compose`` when given, otherwise ``compose_source`` with every entry's
      ``Entry`` in ``AGENTS``, every entry in ``ENTRANCES`` and empty ``SOURCES``.

    ``extra`` is applied last: a ``str`` (UTF-8) or ``bytes`` value adds or replaces a path, ``None`` removes it.
    """
    names = tuple(entries)
    files: dict[str, str | bytes | None] = {
        "agent/__init__.py": "",
        "tools/__init__.py": "",
        "tools/vl_dep.py": "VERSION = 1\n",
        **{f"agent/{name}.py": entry_source(name) for name in names},
        "agent/compose.py": compose if compose is not None else compose_source([f"agent.{name}.Entry" for name in names], names),
    }
    files.update(extra or {})
    return {path: content.encode() if isinstance(content, str) else content for path, content in files.items() if content is not None}


def with_marker(files: Mapping[str, bytes], marker: str) -> dict[str, bytes]:
    """The same tree plus ``tools/<marker>.txt``, so its content differs."""
    return {**files, f"tools/{marker}.txt": marker.encode()}


def commit_id(files: Mapping[str, bytes], parent: str | None, message: str, created_at: datetime) -> str:
    """The 40-hex id ``version_store`` gives a written version.

    Like git, it depends on the content, the parent, the message and the time in whole seconds, so the
    same tree written twice in one second gets the same id and ``write`` reports it as already stored.
    """
    digest = hashlib.sha1()
    for path in sorted(files):
        digest.update(f"{path}\0".encode() + hashlib.sha1(files[path]).digest())
    stamp = int(created_at.timestamp())
    text = f"tree {digest.hexdigest()}\nparent {parent or ''}\ntime {stamp}\n\n{message}"
    return hashlib.sha1(text.encode()).hexdigest()


def make_commit(files: Mapping[str, bytes], *, parent: str | None = None, message: str = "seed", created_at: datetime | None = None) -> Any:
    """A ``store.Commit`` for ``files`` whose id is ``commit_id(files, parent, message, created_at)``.

    The id is a version id, not a content id: two commits of the same tree differ when their parent,
    message or ``created_at`` differ, so compare trees, never ids, to check content.
    Without ``created_at`` each call is one second later than the previous one, so seeded commits
    list newest first in creation order and get distinct ids. ``created_at`` keeps whole seconds, as git does.
    """
    stamp = (created_at if created_at is not None else SEEDED_AT + timedelta(seconds=next(_ticks))).replace(microsecond=0)
    return api().store.Commit(id=commit_id(files, parent, message, stamp), parent=parent, message=message, created_at=stamp)


def version_store(*, main: Any = None, versions: Iterable[tuple[Any, Mapping[str, bytes]]] = ()) -> Any:
    """``AsyncMock(spec=VersionStore)`` answering from fixed data and recording every call.

    - ``read_main`` returns ``main`` (the mirror's adopted main: a version id, or a Commit's id; None when there is none).
    - ``move_main`` returns True.
    - ``read_commit(id)`` / ``read_tree(id)`` return the seeded Commit / a copy of its tree, None when unknown.
    - ``list_commits()`` returns the seeded Commits newest first (``created_at`` descending, later seeds first on ties).
    - ``write(tree, parent, message, created_at)`` returns a Commit whose id is ``commit_id(...)`` and whose
      ``created_at`` keeps whole seconds; it returns None when that id is seeded or was already written.
    - ``keep`` returns None. ``fetch`` returns ``main`` (GitHub's main equals the mirror's). ``is_ancestor`` returns False.
      ``push`` returns True. ``merge_tree`` has no answer; a merge scene sets it.
    - ``initial_tree`` returns None (no first local main to make).

    Writes (``write``, ``keep``, ``move_main``, ``push``) are recorded only; they never change what the reads return.
    ``store.seeded`` (id -> (Commit, tree)) holds the fixed data; ``seed`` adds to it explicitly.
    ``store.written`` (id -> (Commit, tree)) holds what ``write`` returned.
    """
    modules = api()
    store: Any = AsyncMock(spec=modules.store.VersionStore)
    seeded: dict[str, tuple[Any, dict[str, bytes]]] = {}
    written: dict[str, tuple[Any, dict[str, bytes]]] = {}
    store.seeded = seeded
    store.written = written

    async def read_commit(version_id: str) -> Any:
        found = seeded.get(version_id)
        return found[0] if found is not None else None

    async def read_tree(version_id: str) -> dict[str, bytes] | None:
        found = seeded.get(version_id)
        return dict(found[1]) if found is not None else None

    async def list_commits() -> list[Any]:
        commits = [commit for commit, _ in reversed(seeded.values())]
        return sorted(commits, key=lambda commit: commit.created_at, reverse=True)

    async def write(files: Mapping[str, bytes], parent: str | None, message: str, created_at: datetime) -> Any:
        stamp = created_at.replace(microsecond=0)
        version_id = commit_id(files, parent, message, stamp)
        if version_id in seeded or version_id in written:
            return None
        commit = modules.store.Commit(id=version_id, parent=parent, message=message, created_at=stamp)
        written[version_id] = (commit, dict(files))
        return commit

    store.read_main.return_value = getattr(main, "id", main)
    store.fetch.return_value = getattr(main, "id", main)
    store.move_main.return_value = True
    store.read_commit.side_effect = read_commit
    store.read_tree.side_effect = read_tree
    store.list_commits.side_effect = list_commits
    store.write.side_effect = write
    store.keep.return_value = None
    store.is_ancestor.return_value = False
    store.push.return_value = True
    store.initial_tree.return_value = None
    seed(store, *versions)
    return store


def seed(store: Any, *versions: tuple[Any, Mapping[str, bytes]]) -> None:
    """Add ``(Commit, tree)`` pairs to a ``version_store``'s fixed data, as versions already in the mirror."""
    for commit, files in versions:
        store.seeded[commit.id] = (commit, dict(files))


def host_services(**overrides: Any) -> Any:
    """``core.builtin.compose.HostServices`` with ScriptedLLM ``max``/``high``/``low``, no logger and a Mock session store."""
    fields: dict[str, Any] = {
        "llms": {name: ScriptedLLM() for name in ("max", "high", "low")},
        "logger": None,
        "session_store": Mock(),
    }
    return api().compose.HostServices(**(fields | overrides))


def namespace_of(version: Any) -> str:
    """``_te_versions.v_<id>`` for a version id, a Commit or a loaded Version."""
    if isinstance(version, str):
        return f"{ROOT}.v_{version}"
    namespace = getattr(version, "namespace", None)
    return namespace if isinstance(namespace, str) else f"{ROOT}.v_{version.id}"


def namespace_entries(version: Any) -> list[str]:
    """The ``sys.modules`` keys of one version (a version id, a Commit or a loaded Version)."""
    namespace = namespace_of(version)
    return [name for name in list(sys.modules) if name == namespace or name.startswith(namespace + ".")]


def loaded_state(view_root: Path) -> dict[str, list[str]]:
    """What versions keep loaded, for before/after comparison.

    ``modules``: ``sys.modules`` keys under ``_te_versions.``; ``lines``: linecache keys under
    ``view_root``; ``views``: names of ``view_root``'s children.
    """
    prefixes = tuple(dict.fromkeys(f"{root}/" for root in (str(view_root), str(view_root.resolve()))))
    return {
        "modules": sorted(name for name in list(sys.modules) if name.startswith(f"{ROOT}.")),
        "lines": sorted(name for name in list(linecache.cache) if isinstance(name, str) and name.startswith(prefixes)),
        "views": sorted(child.name for child in view_root.iterdir()) if view_root.is_dir() else [],
    }


EMPTY_STATE: dict[str, list[str]] = {"modules": [], "lines": [], "views": []}
