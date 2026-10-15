"""VR48・AS31: I/O を伴う契約は coroutine で、src は event loop を止める呼出しを持たない。版管理の旧 API は無い。"""

import ast
import dataclasses
import importlib
import importlib.util
import inspect
from contextvars import ContextVar
from pathlib import Path

from tests.fakes import ScriptedLLM
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.execution_log import LogRecorder
from trackeverything.core.abc.harness import ErrorHarness, PostHarness, PreHarness
from trackeverything.core.abc.llm import LLMClient
from trackeverything.core.abc.source import Source
from trackeverything.core.abc.tool import Tool
from trackeverything.core.builtin.channel_store import PostgresChannelStore
from trackeverything.core.builtin.codex_session_store import PostgresCodexSessionStore
from trackeverything.core.builtin.execution_log import PostgresExecutionLogger
from trackeverything.core.builtin.runtime import Runtime
from trackeverything.core.builtin.session_store import PostgresSessionStore
from trackeverything.llm.fallback import FallbackLLM
from trackeverything.llm.openai import OpenAIChatLLM
from trackeverything.llm.throttle import ThrottledLLM
from trackeverything.llm.tracking import TrackedLLM
from trackeverything.queue.consumer import QueueConsumer

SRC = Path(__file__).resolve().parents[2] / "src" / "trackeverything"

AGENT_ASYNC = ("run", "_execute", "_execute_llm_loop", "_respond_one_step", "_dispatch", "call_tool", "call_agent")
AGENT_PLAIN = (
    "_build_messages", "_build_user_message", "_build_callables", "get_prompt",
    "register", "list_tools", "list_agent_names", "has_agent", "child_meta", "extend_callables",
)
SOURCE_ASYNC = ("get_identity", "get_payload")
SOURCE_PLAIN = ("identity_for", "material_view", "from_config", "get_project", "get_source_description")
STORES = (
    PostgresSessionStore,
    PostgresChannelStore,
    PostgresCodexSessionStore,
    PostgresExecutionLogger,
)

#: 版管理の契約(``core/versioning/repo.py``・``store.py``)。
REPO_ASYNC = ("start", "refresh", "sync", "fork", "merge", "validate", "log", "tree", "close")
REPO_PLAIN = ("run", "release")
REPO_PROPERTIES = ("main", "main_version")
RUN_VERSIONS_PLAIN = ("version", "agent")
VERSION_PLAIN = ("module", "modules", "view_path")
VERSION_STORE_ASYNC = (
    "read_main", "move_main", "read_commit", "read_tree", "list_commits", "write", "keep", "fetch", "is_ancestor", "merge_tree", "push",
)

#: 版管理から削除した module と名前。
MOVED_TO_BUILTIN = ("catalog", "code_version", "compose")
REMOVED_VERSIONING_MODULES = ("manager", "watcher", *MOVED_TO_BUILTIN)
REMOVED_VERSIONING_NAMES = (
    "VersionManager", "fresh_context", "unit_of_work", "component", "current_lease", "activate", "diagnostics",
    "lease", "reader", "_LEASE", "Candidate", "capture", "owned_scan", "host_scan", "HOST_FILES",
    "deployment_lock", "deploy_lock_path", "FileWatcher",
)
REMOVED_REPO_METHODS = ("lease", "reader", "activate", "diagnostics", "commit", "adopt", "import_files")
#: repo module から削除した名前。
REMOVED_REPO_NAMES = ("AdoptConflict", "MainMoved", "PostgresVersionStore")
#: PostgreSQL の版庫とファイルの取込みとともに削除した名前。
REMOVED_SNAPSHOT_NAMES = ("commit_id", "tree_id", "read_tree")
REMOVED_STORE_NAMES = ("PostgresVersionStore",)


def _shape(owner, names, *, coroutine):
    return [
        f"{owner.__module__}.{owner.__qualname__}.{name}"
        for name in names
        if hasattr(owner, name) and inspect.iscoroutinefunction(getattr(owner, name)) is not coroutine
    ]


def _missing(owner, names):
    return [f"{owner.__module__}.{owner.__qualname__}.{name}: missing" for name in names if not hasattr(owner, name)]


def _import(name, problems):
    """計画した module を import する。無ければ問題として記録し、None を返す。"""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        problems.append(f"{name}: {type(exc).__name__}: {exc}")
        return None


def _fields(cls, label, expected):
    """dataclass ``cls`` のフィールド名が ``expected`` と一致しなければ問題を返す。"""
    if cls is None or not dataclasses.is_dataclass(cls):
        return [f"{label}: missing or not a dataclass"]
    names = {item.name for item in dataclasses.fields(cls)}
    return [] if names == expected else [f"{label} fields: {sorted(names)}"]


def _parameters(function):
    return [name for name in inspect.signature(function).parameters if name != "self"]


def test_vr48_contract_shapes():
    """I/O を伴う契約と版庫・版の保存の契約は coroutine で、取得口・解放・BaseAgent の子の参照は同期。"""
    wrong = [f"BaseAgent.{name}: missing" for name in (*AGENT_ASYNC, *AGENT_PLAIN) if not hasattr(BaseAgent, name)]
    contracts = {
        BaseAgent: AGENT_ASYNC,
        LLMClient: ("respond",),
        Tool: ("run",),
        PreHarness: ("check",),
        PostHarness: ("check",),
        ErrorHarness: ("handle",),
        Source: SOURCE_ASYNC,
        LogRecorder: ("record",),
        Runtime: ("start", "stop", "execute"),
        QueueConsumer: ("prepare", "next", "ack"),
        OpenAIChatLLM: ("respond",),
        FallbackLLM: ("respond",),
        ThrottledLLM: ("respond",),
        TrackedLLM: ("respond",),
    }
    for owner, names in contracts.items():
        wrong += _shape(owner, names, coroutine=True)
    wrong += _shape(BaseAgent, AGENT_PLAIN, coroutine=False)
    wrong += _shape(Tool, ("llm_args", "error_key"), coroutine=False)
    wrong += _shape(Source, SOURCE_PLAIN, coroutine=False)
    stores = list(STORES)

    repo_module = _import("trackeverything.core.versioning.repo", wrong)
    if repo_module is not None:
        repo = repo_module.Repo
        wrong += _missing(repo, (*REPO_ASYNC, *REPO_PLAIN, *REPO_PROPERTIES))
        wrong += _shape(repo, REPO_ASYNC, coroutine=True) + _shape(repo, REPO_PLAIN, coroutine=False)
        wrong += [f"Repo.{name}: not a property" for name in REPO_PROPERTIES if not isinstance(inspect.getattr_static(repo, name, None), property)]
        wrong += [f"Repo.{name}: removed API still present" for name in REMOVED_REPO_METHODS if hasattr(repo, name)]
        wrong += _missing(repo_module.RunVersions, RUN_VERSIONS_PLAIN) + _shape(repo_module.RunVersions, RUN_VERSIONS_PLAIN, coroutine=False)
        wrong += _missing(repo_module.Version, VERSION_PLAIN) + _shape(repo_module.Version, VERSION_PLAIN, coroutine=False)
        errors = {"UnknownVersion": LookupError, "MergeConflict": Exception, "PushRejected": Exception}
        wrong += [f"{name}: not a {base.__name__}" for name, base in errors.items() if not issubclass(getattr(repo_module, name, object), base)]
        wrong += [f"repo.{name}: removed API still present" for name in REMOVED_REPO_NAMES if hasattr(repo_module, name)]
        if inspect.iscoroutinefunction(getattr(repo_module, "describe_failure", None)) or not callable(getattr(repo_module, "describe_failure", None)):
            wrong.append("describe_failure: missing or a coroutine")
    snapshot = _import("trackeverything.core.versioning.snapshot", wrong)
    if snapshot is not None:
        wrong += [f"snapshot.{name}: removed API still present" for name in REMOVED_SNAPSHOT_NAMES if hasattr(snapshot, name)]
    store_module = _import("trackeverything.core.versioning.store", wrong)
    if store_module is not None:
        wrong += _missing(store_module.VersionStore, VERSION_STORE_ASYNC) + _shape(store_module.VersionStore, VERSION_STORE_ASYNC, coroutine=True)
        wrong += [f"store.{name}: removed API still present" for name in REMOVED_STORE_NAMES if hasattr(store_module, name)]
        git_store = getattr(store_module, "GitVersionStore", None)
        if git_store is None:
            wrong.append("store.GitVersionStore: missing")
        else:
            wrong += _missing(git_store, VERSION_STORE_ASYNC)
            stores.append(git_store)
        wrong += _fields(getattr(store_module, "Commit", None), "store.Commit", {"id", "parent", "message", "created_at"})
    for store in stores:
        public = [name for name, value in vars(store).items() if not name.startswith("_") and inspect.isfunction(value)]
        wrong += _shape(store, public, coroutine=True)

    compose = _import("trackeverything.core.builtin.compose", wrong)
    if compose is not None:
        wrong += _missing(compose.StaticAgents, ("agent",)) + _shape(compose.StaticAgents, ("agent",), coroutine=False)
        wrong += _fields(getattr(compose, "ComponentGraph", None), "compose.ComponentGraph", {"agents", "entrances", "sources"})
    if not isinstance(getattr(importlib.import_module("trackeverything.core.builtin.domain.execution_log"), "current_component_version", None), ContextVar):
        wrong.append("core.builtin.domain.execution_log.current_component_version: missing or not a ContextVar")
    lookup = getattr(importlib.import_module("trackeverything.core.abc.context"), "AgentLookup", None)
    if lookup is None:
        wrong.append("core.abc.context.AgentLookup: missing")
    else:
        wrong += _missing(lookup, ("agent",)) + _shape(lookup, ("agent",), coroutine=False)
    agents_field = {item.name: item for item in dataclasses.fields(AgentContext)}.get("agents")
    if agents_field is None or agents_field.default is not None:
        wrong.append("AgentContext.agents: missing or not defaulting to None")
    if _parameters(Runtime.__init__)[:3] != ["queue", "repo", "sessions"]:
        wrong.append(f"Runtime.__init__ parameters: {_parameters(Runtime.__init__)[:3]}")

    # 子は名前だけを持ち、実例は ctx の取得口から得る。
    wrong += [f"BaseAgent.{name}: removed API still present" for name in ("register_agent", "list_agents") if hasattr(BaseAgent, name)]
    if "_agents" in vars(BaseAgent(ScriptedLLM())):
        wrong.append("BaseAgent._agents: removed binding still present")
    if hasattr(BaseAgent, "child_meta") and _parameters(BaseAgent.child_meta) != ["name", "ctx"]:
        wrong.append(f"BaseAgent.child_meta parameters: {_parameters(BaseAgent.child_meta)}")
    if hasattr(BaseAgent, "extend_callables") and _parameters(BaseAgent.extend_callables) != ["tools", "agent_names"]:
        wrong.append(f"BaseAgent.extend_callables parameters: {_parameters(BaseAgent.extend_callables)}")

    assert wrong == [], "\n".join(sorted(set(wrong)))


def _bound_names(tree):
    """module の最上位で定義・代入・import する名前。"""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return names


def test_vr48_old_versioning_api_is_removed():
    """版リース・監視・Host の部品経路・ファイルの取込みは無く、目録・実行版・組立ては core/builtin にある。"""
    problems = []
    problems += [
        f"trackeverything.core.versioning.{name}: still importable"
        for name in REMOVED_VERSIONING_MODULES
        if importlib.util.find_spec(f"trackeverything.core.versioning.{name}") is not None
    ]
    for name in MOVED_TO_BUILTIN:
        _import(f"trackeverything.core.builtin.{name}", problems)
    for path in sorted((SRC / "core" / "versioning").rglob("*.py")):
        defined = _bound_names(ast.parse(path.read_text(encoding="utf-8")))
        problems += [f"core/versioning/{path.name}: defines {name}" for name in REMOVED_VERSIONING_NAMES if name in defined]
    loader = _import("trackeverything.core.versioning.loader", problems)
    if loader is not None and hasattr(loader, "resolve"):
        problems.append("core.versioning.loader.resolve: still present")
    if "TE_COMPONENT_WATCH" in (SRC / "core" / "builtin" / "app.py").read_text(encoding="utf-8"):
        problems.append("core/builtin/app.py: mentions TE_COMPONENT_WATCH")
    assert problems == [], "\n".join(problems)


# 呼出し(と import)だけを見る。prompt などの文字列に現れる名前は対象外。
BLOCKING_CALLS = {
    "time.sleep": "time.sleep",
    **{f"subprocess.{name}": "sync subprocess" for name in ("run", "Popen", "check_output", "check_call", "call")},
    "httpx.Client": "sync httpx client",
    **{f"httpx.{name}": "sync httpx client" for name in ("request", "get", "post", "put", "patch", "delete", "stream")},
}
BLOCKING_SUFFIXES = {"urlopen": "urlopen", "OpenAI": "sync OpenAI client", "create_engine": "sync engine"}
# ORM は async engine と AsyncSession だけを使う。同期の engine・session は import も禁止する。
BLOCKING_IMPORTS = {
    "requests": "requests",
    "time": {"sleep"},
    "subprocess": {"run", "Popen", "check_output", "check_call", "call"},
    "sqlalchemy": {"create_engine", "engine_from_config", "Session", "sessionmaker", "scoped_session"},
}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _blocking(tree):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            label = BLOCKING_CALLS.get(name) or next(
                (label for suffix, label in BLOCKING_SUFFIXES.items() if name == suffix or name.endswith("." + suffix)), None
            )
            if label is not None:
                found.append((node.lineno, label))
        elif isinstance(node, ast.Import):
            found += [(node.lineno, "requests") for alias in node.names if alias.name.split(".")[0] == "requests"]
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            banned = BLOCKING_IMPORTS.get(root)
            if banned == "requests":
                found.append((node.lineno, "requests"))
            elif isinstance(banned, set):
                found += [(node.lineno, f"{root}.{alias.name}") for alias in node.names if alias.name in banned]
    return found


def _swallows_cancellation(tree):
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        names = []
        if node.type is None:
            names.append("bare")
        else:
            types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            names += [item.id for item in types if isinstance(item, ast.Name) and item.id in {"BaseException", "CancelledError"}]
            names += [item.attr for item in types if isinstance(item, ast.Attribute) and item.attr == "CancelledError"]
        if names and not any(isinstance(inner, ast.Raise) and inner.exc is None for inner in ast.walk(node)):
            found.append(node.lineno)
    return found


def test_as31_static_guard():
    planted = ast.parse("from sqlalchemy import create_engine\nfrom sqlalchemy.orm import Session, sessionmaker\ncreate_engine('x')\n")
    assert sorted(label for _, label in _blocking(planted)) == ["sqlalchemy.Session", "sqlalchemy.create_engine", "sqlalchemy.sessionmaker", "sync engine"]
    allowed = ast.parse("from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine\ncreate_async_engine('x')\n")
    assert _blocking(allowed) == []
    violations = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if "/." in f"/{relative}":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        violations += [f"{relative}:{line}: {label}" for line, label in _blocking(tree)]
        violations += [f"{relative}:{line}: swallowed cancellation" for line in _swallows_cancellation(tree)]
    assert violations == [], "\n".join(violations)
