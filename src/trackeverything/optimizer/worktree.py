"""観点が LLM に渡す作業用の tree の Tool。

作業用の tree は main の tree(パス → 内容)の写しで、LLM の書込みと削除は ``agent/``・``tools/``・``skills/`` の下に限る。
人が保守する ``agent/SYSTEM.md`` は読めるが、書込みも削除もできない。
観点は使う Tool の名前を ``callables`` で選び、LLM の呼出しを ``observe`` で tree に適用して結果を ``tool`` の発言にする。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from trackeverything.core.abc.llm import Call, Message
from trackeverything.core.builtin.compose import SYSTEM_PATH
from trackeverything.core.versioning.repo import Version
from trackeverything.core.versioning.snapshot import module_path

#: LLM が書込み・削除できる版のフォルダ。
WRITABLE = ("agent", "tools", "skills")
#: ``grep`` が返す行の上限。
GREP_LINES = 200

_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "name": "list_files",
        "description": "List the paths of the working tree that start with prefix (empty prefix lists every path).",
        "arguments": {"type": "object", "properties": {"prefix": {"type": "string"}}, "required": []},
    },
    "read_file": {
        "name": "read_file",
        "description": "Read one file of the working tree as UTF-8 text.",
        "arguments": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    "grep": {
        "name": "grep",
        "description": f"Search the UTF-8 files of the working tree whose path starts with prefix for a regular expression; "
                       f"returns at most {GREP_LINES} matching lines as 'path:line: text'.",
        "arguments": {"type": "object", "properties": {"pattern": {"type": "string"}, "prefix": {"type": "string"}},
                      "required": ["pattern"]},
    },
    "write_file": {
        "name": "write_file",
        "description": "Replace the whole content of one file under agent/, tools/ or skills/ in the working tree.",
        "arguments": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    "delete_file": {
        "name": "delete_file",
        "description": "Delete one file under agent/, tools/ or skills/ from the working tree.",
        "arguments": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
}


def callables(*names: str) -> list[dict[str, Any]]:
    """名前の順の Tool の定義(LLM に渡す callable)。"""
    return [_SCHEMAS[name] for name in names]


class ToolError(Exception):
    """作業用の tree への呼出しの失敗。LLM へ失敗の結果として返す。"""


def writable(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) >= 2
        and parts[0] in WRITABLE
        and "\\" not in path
        and all(part not in ("", ".", "..") and not part.startswith(".") and part != "__pycache__" for part in parts)
        and not path.endswith(".pyc")
        and path != SYSTEM_PATH
    )


def _text(args: Mapping[str, Any], key: str, default: str | None = None) -> str:
    value = args.get(key, default)
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string")
    return value


def _writable_path(args: Mapping[str, Any]) -> str:
    path = _text(args, "path")
    if path == SYSTEM_PATH:
        raise ToolError(f"not writable: {path} (maintained by people)")
    if not writable(path):
        raise ToolError(f"not writable: {path} (only files under {', '.join(f'{name}/' for name in WRITABLE)})")
    return path


def _grep(tree: Mapping[str, bytes], pattern: str, prefix: str) -> str:
    try:
        expression = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid pattern: {exc}") from None
    lines: list[str] = []
    for path in sorted(path for path in tree if path.startswith(prefix)):
        try:
            text = tree[path].decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if expression.search(line):
                lines.append(f"{path}:{number}: {line}")
                if len(lines) >= GREP_LINES:
                    return "\n".join(lines)
    return "\n".join(lines) or "(no matches)"


def use(tree: dict[str, bytes], call: Call, allowed: frozenset[str]) -> str:
    """作業用の tree への一回の呼出し。``allowed`` にない名前と失敗は ``ToolError``。"""
    if call.name not in allowed:
        raise ToolError(f"unknown tool: {call.name}")
    if call.name == "list_files":
        prefix = _text(call.args, "prefix", "")
        return "\n".join(sorted(path for path in tree if path.startswith(prefix))) or "(no files)"
    if call.name == "read_file":
        path = _text(call.args, "path")
        data = tree.get(path)
        if data is None:
            raise ToolError(f"no such file: {path}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"not UTF-8 text: {path}") from None
    if call.name == "grep":
        return _grep(tree, _text(call.args, "pattern"), _text(call.args, "prefix", ""))
    if call.name == "write_file":
        path, content = _writable_path(call.args), _text(call.args, "content")
        tree[path] = content.encode("utf-8")
        return f"wrote {path}"
    if call.name == "delete_file":
        path = _writable_path(call.args)
        if path not in tree:
            raise ToolError(f"no such file: {path}")
        del tree[path]
        return f"deleted {path}"
    raise ToolError(f"unknown tool: {call.name}")


def observe(tree: dict[str, bytes], call: Call, allowed: frozenset[str]) -> Message:
    """呼出しを tree に適用し、結果(``name``・``ok``・``output``・``error``)を ``tool`` の発言にする。"""
    try:
        output: str | None = use(tree, call, allowed)
        error = None
    except ToolError as exc:
        output, error = None, str(exc)
    payload = {"name": call.name, "ok": error is None, "output": output, "error": error}
    return Message("tool", json.dumps(payload, ensure_ascii=False))


def changed(tree: Mapping[str, bytes], base: Mapping[str, bytes]) -> list[str]:
    """``base`` から変わったパス(追加・変更・削除。昇順)。"""
    return sorted(path for path in set(tree) | set(base) if tree.get(path) != base.get(path))


def answer_json(text: str) -> Any:
    """最終回答の JSON(```` ``` ```` の囲みは外す)。全体が JSON でなければ、文の中の最後の JSON の object を使う。

    LLM は JSON の前後に説明の文を書くことがある。JSON の object がどこにもなければ ``ValueError``。
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    try:
        return json.loads(body)
    except ValueError:
        pass
    decoder, found, index = json.JSONDecoder(), None, 0
    while (start := body.find("{", index)) != -1:
        try:
            found, index = decoder.raw_decode(body, start)
        except ValueError:
            index = start + 1
    if found is None:
        raise ValueError("the answer holds no JSON object")
    return found


def logical_module(version: Version, cls: type) -> str:
    """類の module の logical 名(``trackeverything.agent.review``)。版の namespace の接頭辞を外す。"""
    name = cls.__module__
    prefix = version.namespace + "."
    return name[len(prefix):] if name.startswith(prefix) else name


def class_file(version: Version, cls: type) -> str | None:
    """版の tree で類を定義した module のパス。版の外の類は None。"""
    logical = logical_module(version, cls)
    for is_package in (False, True):
        path = module_path(logical, is_package)
        if path in version.tree:
            return path
    return None
