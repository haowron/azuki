"""effort の変更: Agent 類の ``llm`` の宣言だけを書き換える(LLM を使わない機械的な変更)。"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping


def _target(statement: ast.stmt) -> str | None:
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
        return statement.targets[0].id
    return None


def _value(statement: ast.stmt) -> ast.expr | None:
    return statement.value if isinstance(statement, ast.AnnAssign | ast.Assign) else None


def _named(node: ast.ClassDef, agent: str) -> bool:
    return any(_target(statement) == "name" and isinstance(value := _value(statement), ast.Constant) and value.value == agent
               for statement in node.body)


def _declares(node: ast.ClassDef, attribute: str) -> list[ast.stmt]:
    return [statement for statement in node.body if _target(statement) == attribute]


def _class(source: str, agent: str) -> ast.ClassDef | None:
    return next((node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef) and _named(node, agent)), None)


def declared_tier(source: str, agent: str) -> str | None:
    """``name`` が ``agent`` の類が自身で宣言した ``llm``(最後の代入)。宣言がない・文字列でなければ None。"""
    node = _class(source, agent)
    if node is None:
        return None
    declarations = _declares(node, "llm")
    value = _value(declarations[-1]) if declarations else None
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def class_name(source: str, agent: str) -> str | None:
    """``name`` が ``agent`` の類の名前。"""
    node = _class(source, agent)
    return node.name if node is not None else None


def inheritors(tree: Mapping[str, bytes], name: str) -> list[str]:
    """``name`` の類を継ぎ、自身で ``llm`` を宣言しない Agent 類(``path:類名``)。この類の段を変えると、これらの段も変わる。"""
    found: list[str] = []
    for path, data in sorted(tree.items()):
        if not path.endswith(".py"):
            continue
        try:
            module = ast.parse(data.decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for node in ast.walk(module):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else None
                     for base in node.bases}
            if name in bases and _declares(node, "name") and not _declares(node, "llm"):
                found.append(f"{path}:{node.name}")
    return found


def set_tier(source: str, agent: str, tier: str) -> str:
    """``name`` が ``agent`` の類の ``llm`` を ``tier`` にした module の本文。

    宣言があれば最後の代入の値の文字列だけを置き換え、なければ ``name`` の宣言の次の行に同じ形(注釈の有無)で加える。
    類が見つからない・``llm`` が一行の文字列でない場合は ``ValueError``。
    """
    node = _class(source, agent)
    if node is None:
        raise ValueError(f"no class declares name {agent!r}")
    lines = source.splitlines(keepends=True)
    name = _declares(node, "name")[0]
    declarations = _declares(node, "llm")
    if declarations:
        value = _value(declarations[-1])
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or value.lineno != value.end_lineno \
                or value.end_col_offset is None:
            raise ValueError(f"llm of {agent} is not a one-line string literal")
        raw = lines[value.lineno - 1].encode()
        lines[value.lineno - 1] = (raw[:value.col_offset] + json.dumps(tier).encode() + raw[value.end_col_offset:]).decode()
    else:
        indent = " " * name.col_offset
        annotation = ast.get_source_segment(source, name.annotation) if isinstance(name, ast.AnnAssign) else None
        declaration = f"llm: {annotation} = {json.dumps(tier)}" if annotation else f"llm = {json.dumps(tier)}"
        assert name.end_lineno is not None
        lines.insert(name.end_lineno, f"{indent}{declaration}\n")
    updated = "".join(lines)
    if declared_tier(updated, agent) != tier:
        raise ValueError(f"could not declare llm {tier!r} on {agent}")
    return updated
