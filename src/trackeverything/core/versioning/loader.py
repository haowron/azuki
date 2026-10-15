"""版の module を予約 namespace の下へ、版の tree のソースから読み込む。

``_te_versions.v_<版 id>.trackeverything.agent.review`` のように版ごとの別名で
``sys.modules`` に載せ、checkout のファイルや canonical な
``trackeverything.agent.review`` は使わない。ソースはメモリ上の AST だけを書き換え、
版が所有する絶対 import をその版へ向ける。共有側(``core`` など)の import は
canonical のまま残し、型の同一性と lock を全ての版で保つ。
"""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import sys
import threading
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import TYPE_CHECKING, Any

import trackeverything as _canonical
from trackeverything.core.versioning.snapshot import MODULE_FOLDERS, PACKAGE_NAME, module_path

if TYPE_CHECKING:
    from trackeverything.core.versioning.repo import Version

ROOT = "_te_versions"
_OWNED_ROOTS = tuple(f"{PACKAGE_NAME}.{name}" for name in MODULE_FOLDERS)
_versions: dict[str, Version] = {}
_registry_lock = threading.Lock()
_installed = False


def is_owned(module: str) -> bool:
    """版が所有する module 名か(``trackeverything.agent`` など 4 フォルダの配下)。"""
    return any(module == root or module.startswith(root + ".") for root in _OWNED_ROOTS)


def register(version: Version) -> None:
    with _registry_lock:
        _versions[version.name] = version


def unregister(version: Version) -> None:
    with _registry_lock:
        if _versions.get(version.name) is version:
            del _versions[version.name]


def install() -> None:
    """予約 namespace の finder を一度だけ登録する。他の import には関与しない。"""
    global _installed
    with _registry_lock:
        if not _installed:
            sys.meta_path.insert(0, VersionFinder())
            _installed = True


class _EmptyPackage(importlib.abc.Loader):
    """namespace の祖先と、ソースを持たない中間 package。``__path__`` は空でディスクを探さない。

    ``create_module`` は基底の既定(None: 標準の module を作る)を使う。下の loader も同じ。
    """

    def exec_module(self, module: ModuleType) -> None:
        return None


class _Facade(importlib.abc.Loader):
    """``_te_versions.<版名>.trackeverything``: 版の部品はこの版から、それ以外は共有の package へ転送する。"""

    def __init__(self, version: Version) -> None:
        self.version = version

    def exec_module(self, module: ModuleType) -> None:
        namespace = self.version.namespace

        def forward(name: str) -> Any:
            if name in MODULE_FOLDERS:
                return importlib.import_module(f"{namespace}.{PACKAGE_NAME}.{name}")
            try:
                return getattr(_canonical, name)
            except AttributeError:
                pass
            if name.startswith("__"):
                raise AttributeError(name)
            try:
                return importlib.import_module(f"{PACKAGE_NAME}.{name}")
            except ModuleNotFoundError:
                raise AttributeError(name) from None

        module.__getattr__ = forward  # type: ignore[method-assign]


class SnapshotLoader(importlib.abc.InspectLoader):
    """版のソース辞書から 1 module を読み込む。"""

    def __init__(self, version: Version, logical: str, origin: str) -> None:
        self.version = version
        self.logical = logical
        self.origin = origin

    def get_source(self, fullname: str) -> str | None:
        # 退役後(辞書を外した後)の traceback 整形からも呼ばれる。その場合は None。
        return self.version.sources.get(self.logical)

    def exec_module(self, module: ModuleType) -> None:
        source = self.version.sources[self.logical]
        tree = ast.parse(source, filename=self.origin)
        tree = OwnedImportRewriter(self.version.namespace, self.origin).visit(tree)
        code = compile(ast.fix_missing_locations(tree), self.origin, "exec", dont_inherit=True)
        exec(code, module.__dict__)


class VersionFinder(importlib.abc.MetaPathFinder):
    """予約 namespace だけを解決する。版にない module は None を返し、ディスクへは逃がさない。"""

    def find_spec(self, fullname: str, path: Any = None, target: ModuleType | None = None) -> ModuleSpec | None:
        parts = fullname.split(".")
        if parts[0] != ROOT:
            return None
        if len(parts) == 1:
            return ModuleSpec(fullname, _EmptyPackage(), is_package=True)
        version = _versions.get(parts[1])
        if version is None:
            return None
        if len(parts) == 2:
            return ModuleSpec(fullname, _EmptyPackage(), is_package=True)
        logical = ".".join(parts[2:])
        if logical == PACKAGE_NAME:
            return ModuleSpec(fullname, _Facade(version), is_package=True)
        if logical in version.sources:
            is_package = logical in version.packages
            origin = str(version.view_path(module_path(logical, is_package)))
            spec = ModuleSpec(fullname, SnapshotLoader(version, logical, origin), origin=origin, is_package=is_package)
            spec.has_location = True
            return spec
        if logical in version.virtual_packages:
            return ModuleSpec(fullname, _EmptyPackage(), is_package=True)
        return None


class OwnedImportRewriter(ast.NodeTransformer):
    """版が所有する絶対 import を一つの版へ向ける。文字列・相対 import・共有 import はそのまま。"""

    def __init__(self, namespace: str, origin: str = "<version>") -> None:
        self.ns = namespace
        self.origin = origin

    def _bind_root(self, asname: str | None = None) -> ast.ImportFrom:
        # ``trackeverything``(または別名)を版の facade に束縛する(共有の枝は facade が転送する)。
        return ast.ImportFrom(module=self.ns, names=[ast.alias(PACKAGE_NAME, asname)], level=0)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:
        if node.level or not node.module:
            return node
        if is_owned(node.module) or node.module == PACKAGE_NAME:
            node.module = f"{self.ns}.{node.module}"
        return node

    def visit_Import(self, node: ast.Import) -> list[ast.stmt]:
        return [ast.copy_location(statement, node) for alias in node.names for statement in self._rewrite(alias)]

    def _rewrite(self, alias: ast.alias) -> list[ast.stmt]:
        """``import`` の一つの名前を書き換えた文。``import a.b`` は ``a`` を束縛するので、版の module でも root の名前を版の facade にする。"""
        name, asname = alias.name, alias.asname
        if is_owned(name):
            if asname:
                return [ast.Import(names=[ast.alias(f"{self.ns}.{name}", asname)])]
            # 版の module を読み込ませてから、``trackeverything`` の名前を facade に束縛する。
            return [ast.Import(names=[ast.alias(f"{self.ns}.{name}", "_te_loaded")]),
                    ast.Delete(targets=[ast.Name("_te_loaded", ast.Del())]),
                    self._bind_root()]
        if name == PACKAGE_NAME:
            return [self._bind_root(asname)]
        if asname is None and name.startswith(PACKAGE_NAME + "."):
            return [ast.Import(names=[alias]), self._bind_root()]
        return [ast.Import(names=[alias])]

    def visit_Call(self, node: ast.Call) -> ast.Call:
        self.generic_visit(node)
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name in ("import_module", "__import__") and node.args:
            target = node.args[0]
            if isinstance(target, ast.Constant) and isinstance(target.value, str) and is_owned(target.value):
                raise ImportError(
                    f"{self.origin}:{node.lineno}: import the version-owned module {target.value!r} with an import statement"
                )
        return node
