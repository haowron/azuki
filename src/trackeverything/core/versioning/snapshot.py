"""版の tree: 版が所有する 5 フォルダのファイル(package root からの相対パス → 内容)。

所有はフォルダで決まる。``agent/``・``tools/``・``harnesses/``・``sources/`` の ``.py`` は module、
それ以外(``skills/`` の script を含む)は資源になる。部品のリポジトリの root にある他のファイルは版に含めない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: 版が所有するフォルダ(``src/trackeverything`` からの相対)。
OWNED = ("agent", "tools", "skills", "harnesses", "sources")
#: module として読み込むフォルダ。``skills/`` の ``.py`` は Skill の script(資源)。
MODULE_FOLDERS = ("agent", "tools", "harnesses", "sources")
PACKAGE_NAME = "trackeverything"


def skipped(relative: str) -> bool:
    """版に入れないパス: ``__pycache__``・``.`` で始まる名前・``.pyc``。"""
    parts = relative.split("/")
    return any(part == "__pycache__" or part.startswith(".") for part in parts) or relative.endswith(".pyc")


def owned(relative: str) -> bool:
    """5 フォルダの中の、版に入れるパスか。"""
    parts = relative.split("/")
    return len(parts) >= 2 and parts[0] in OWNED and not skipped(relative)


def check_paths(tree: Mapping[str, bytes]) -> None:
    """5 フォルダの外・``..``・絶対パス・空の部分を持つキーを ``ValueError`` にする。"""
    for key in tree:
        parts = key.split("/")
        if PurePosixPath(key).is_absolute() or "" in parts or any(part in (".", "..") for part in parts) or "\\" in key:
            raise ValueError(f"invalid tree path: {key!r}")
        if len(parts) < 2 or parts[0] not in OWNED:
            raise ValueError(f"tree path outside the version-owned folders: {key!r}")


def package_tree(package_root: Path) -> dict[str, bytes]:
    """package の 5 フォルダの通常ファイル(symlink と ``skipped`` のパスを除く)。ローカル方式の最初の main の tree。同期関数。"""
    files: dict[str, bytes] = {}
    for path in sorted(path for name in OWNED for path in (package_root / name).rglob("*")):
        relative = path.relative_to(package_root).as_posix()
        if path.is_file() and not path.is_symlink() and not skipped(relative):
            files[relative] = path.read_bytes()
    return files


def logical_name(relative: str) -> tuple[str, bool] | None:
    """``agent/review.py`` → (``trackeverything.agent.review``, False)。module でなければ None。"""
    path = Path(relative)
    if path.suffix != ".py" or path.parts[0] not in MODULE_FOLDERS:
        return None
    parts = list(path.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    if not all(part.isidentifier() for part in parts):
        return None
    return ".".join([PACKAGE_NAME, *parts]), is_package


def module_path(logical: str, is_package: bool) -> str:
    """logical 名から版の file view での相対パスを求める(``logical_name`` の逆)。"""
    parts = logical.split(".")[1:]
    return "/".join([*parts, "__init__.py"]) if is_package else "/".join(parts) + ".py"


@dataclass(frozen=True, slots=True)
class ModuleIndex:
    """版の module の索引。``sources`` は logical 名 → ソース、``packages`` はソースを持つ package、
    ``virtual`` はソースを持たない中間 package(``__init__.py`` のないフォルダ)。"""

    sources: Mapping[str, str]
    packages: frozenset[str]
    virtual: frozenset[str]


def index_modules(tree: Mapping[str, bytes]) -> ModuleIndex:
    """tree の module を索引にする。UTF-8 でない module は ``ValueError``。"""
    sources: dict[str, str] = {}
    packages: set[str] = set()
    for relative, data in sorted(tree.items()):
        named = logical_name(relative)
        if named is None:
            continue
        logical, is_package = named
        try:
            sources[logical] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{relative}: not UTF-8") from None
        if is_package:
            packages.add(logical)
    # trackeverything.agent.a.b の祖先 trackeverything.agent と trackeverything.agent.a(package root は含めない)。
    ancestors = {name.rsplit(".", depth)[0] for name in sources for depth in range(1, name.count("."))}
    return ModuleIndex(sources=sources, packages=frozenset(packages | (ancestors & sources.keys())),
                       virtual=frozenset(ancestors - sources.keys()))
