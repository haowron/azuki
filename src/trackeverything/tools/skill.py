"""宣言済み Skill の段階的読取りと、呼出し内だけの function 登録。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from yaml.nodes import MappingNode

import trackeverything.config as config
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.context import AgentContext
from trackeverything.core.abc.tool import Tool, ToolResult
from trackeverything.core.builtin.base_tools import stop_process_group
from trackeverything.core.builtin.process_registry import spawn_exec

MAIN_LIMIT = 64 * 1024
RESOURCE_LIMIT = 256 * 1024
OUTPUT_LIMIT = 1024 * 1024
#: Skill の script に渡す環境変数。資格情報は渡さない。
SAFE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "REPOS_DIR", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_FUNCTION = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
_RULES = """利用可能な Skill の名前と説明から今回の依頼に必要なものを選び、read_skill で本文を読んでください。
成功結果が示す function は次の呼出しから利用できます。手順に従って適切な Tool を選択してください。
references 等は必要なときだけ読み、未読資料や未実行 Tool を利用済みと報告しないでください。
現在の依頼、system の制約、各 Tool の権限検査と Harness を守ってください。
available_skills:
"""


class _UniqueLoader(yaml.SafeLoader):
    """safe loader に重複 key の拒否を追加する。"""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate YAML key")
        return super().construct_mapping(node, deep=deep)


def _metadata(content: str, name: str) -> tuple[str, str | None]:
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md requires YAML frontmatter")
    try:
        end = lines.index("---", 1)
        meta = yaml.load("\n".join(lines[1:end]), Loader=_UniqueLoader)
    except (ValueError, TypeError, yaml.YAMLError, RecursionError):
        raise ValueError("invalid SKILL.md frontmatter") from None
    if not isinstance(meta, dict) or meta.get("name") != name:
        raise ValueError("Skill name must match the package directory")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("description must contain 1-1024 characters")
    extra = meta.get("metadata", {})
    if not isinstance(extra, dict):
        raise ValueError("metadata must be a mapping")
    manifest = extra.get("trackeverything-tools")
    if manifest is not None and (not isinstance(manifest, str) or not manifest.strip()):
        raise ValueError("trackeverything-tools must be a relative file path")
    return description, manifest


def _relative(path: str) -> str:
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts or "\\" in path or "\x00" in path:
        raise ValueError("resource path must stay inside the Skill package")
    return str(candidate)


@dataclass(frozen=True)
class SkillPackage:
    """構成で固定した配置場所、正本と目録の説明。"""

    name: str
    location: Path
    base: Path
    description: str

    def resource(self, path: str) -> Path:
        relative = _relative(path)
        if self.location.resolve(strict=True) != self.base:
            raise ValueError("Skill root changed; restart required")
        target = (self.base / relative).resolve(strict=True)
        if not target.is_relative_to(self.base) or not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("resource must be a regular file inside the Skill package")
        return target

    def read(self, path: str) -> bytes:
        target = self.resource(path)
        limit = MAIN_LIMIT if _relative(path) == "SKILL.md" else RESOURCE_LIMIT
        with target.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Skill resource exceeds the byte limit")
        _text(data)
        return data

    def main(self) -> tuple[bytes, str | None]:
        content = self.read("SKILL.md")
        description, manifest = _metadata(_text(content), self.name)
        if description != self.description:
            raise ValueError("Skill metadata changed; restart required")
        return content, manifest


def _text(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ValueError("Skill resource must be UTF-8 text") from None
    if "\x00" in text:
        raise ValueError("Skill resource contains NUL")
    return text


def _json(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value: str) -> Any:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid JSON") from None


def _definitions(package: SkillPackage, manifest: str | None) -> tuple[list[dict[str, Any]], str]:
    """ロード・実行時に宣言と全入口を検証し、同じ定義の hash を返す。"""
    digest = hashlib.sha256()
    if manifest is None:
        return [], digest.hexdigest()
    contents = package.read(manifest)
    digest.update(manifest.encode())
    digest.update(contents)
    value = _json(contents)
    if not isinstance(value, dict) or set(value) != {"tools"} or not isinstance(value["tools"], list):
        raise ValueError("tools.json requires a tools array")
    definitions: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value["tools"]:
        if not isinstance(item, dict):
            raise ValueError("Tool declaration must be an object")
        if set(item) == {"ref"}:
            name = item["ref"]
        else:
            if set(item) != {"name", "description", "arguments", "script"}:
                raise ValueError("native Tool requires name, description, arguments and script")
            local_name, description, schema, script = (item[key] for key in ("name", "description", "arguments", "script"))
            if not isinstance(local_name, str) or not _FUNCTION.fullmatch(local_name):
                raise ValueError("invalid Tool name")
            name = f"skill__{package.name}__{local_name}"
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Tool description must be non-empty")
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("Tool arguments must be an object schema")
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                raise ValueError("invalid Tool argument schema") from None
            if not isinstance(script, str) or PurePosixPath(script).suffix != ".py":
                raise ValueError("Tool script must be a package Python file")
            digest.update(script.encode())
            digest.update(package.read(script))
        if not isinstance(name, str) or not _FUNCTION.fullmatch(name) or name in names:
            raise ValueError("invalid or duplicate function name")
        names.add(name)
        definitions.append(item)
    return definitions, digest.hexdigest()


class SkillScriptTool(Tool):
    """公開 metadata は宣言から作る型に置き、固定した Python 入口を実行する。"""

    def __init__(self, owner: BaseAgent, package: SkillPackage, script: str, fingerprint: str) -> None:
        self._owner, self._package = owner, package
        self._script, self._fingerprint = script, fingerprint

    async def run(self, args: dict[str, Any], *, ctx: AgentContext | None = None) -> ToolResult:
        try:
            scope = self._owner.tool_scope(ctx)
            if scope.tools.get(self.name) is not self:
                raise ValueError("Skill Tool is not registered in this execution")
            _, manifest = self._package.main()
            _, fingerprint = _definitions(self._package, manifest)
            if fingerprint != self._fingerprint:
                raise ValueError("Skill tools changed; start a new run")
            # 空 registry は外部 $ref の自動 HTTP 取得を許可しない。
            Draft202012Validator(self.arguments, registry=Registry()).validate(args)
            payload = json.dumps(args, ensure_ascii=False, allow_nan=False).encode()
            with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                stdin.write(payload)
                stdin.seek(0)
                proc = await spawn_exec(
                    sys.executable,
                    str(self._package.resource(self._script)),
                    cwd=self._package.base,
                    env={key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ},
                    stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True,
                )
                try:
                    async with asyncio.timeout(config.COMMAND_TIMEOUT):
                        await proc.wait()
                finally:
                    await stop_process_group(proc)
                stdout.seek(0)
                stderr.seek(0)
                out, err = stdout.read(OUTPUT_LIMIT + 1), stderr.read(OUTPUT_LIMIT + 1)
            if len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT:
                raise ValueError("Skill Tool output exceeds the byte limit")
            if proc.returncode != 0:
                return ToolResult(ok=False, error=f"Skill Tool exited {proc.returncode}", output={"stderr": err.decode("utf-8", errors="replace")})
            return ToolResult(ok=True, output=_json(out))
        except ValidationError:
            return ToolResult(ok=False, error="arguments do not match the Tool schema")
        except TimeoutError:
            return ToolResult(ok=False, error="Skill Tool timed out")
        except (ValueError, OSError, RuntimeError) as exc:
            return ToolResult(ok=False, error=_failure(exc))
        except Exception:
            # schema の未解決 ref なども引数や本文を error へ複写しない。
            return ToolResult(ok=False, error="Skill Tool validation or execution failed")


def _failure(exc: Exception) -> str:
    # OS のエラーは絶対パスを含むため、種別だけ返す。
    return str(exc) if isinstance(exc, ValueError) else f"Skill resource unavailable ({type(exc).__name__})"


def _check_definition(package: SkillPackage, fingerprint: str) -> None:
    """公共 Tool の再利用でも、採用した Skill 宣言の差替えを検出する。"""
    try:
        _, manifest = package.main()
        if _definitions(package, manifest)[1] != fingerprint:
            raise ValueError("Skill tools changed; start a new run")
    except (ValueError, OSError, RuntimeError) as exc:
        raise ValueError(_failure(exc)) from None


class ReadSkillTool(Tool):
    """許可された Skill の本文や package 内のテキストを必要時に読み、主文の読取りに成功すると Skill の Tool を次の LLM 呼出しから使えるようにする。"""

    name: ClassVar[str] = "read_skill"
    description: ClassVar[str] = "許可された Skill の本文、または package 内の指定テキストを必要時に読む。主文の成功で自前・公共 Tool を次の LLM 呼出しから利用できる。"
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "path": {"type": "string", "default": "SKILL.md"}},
        "required": ["name"], "additionalProperties": False,
    }

    def __init__(self, owner: BaseAgent, packages: dict[str, SkillPackage], factories: Mapping[str, Callable[[], Tool]]) -> None:
        self._owner, self._packages, self._factories = owner, packages, dict(factories)

    def error_key(self, args: dict[str, Any]) -> str | None:
        path = args.get("path", "SKILL.md")
        return json.dumps([args.get("name"), str(PurePosixPath(path)) if isinstance(path, str) else None], ensure_ascii=False)

    async def run(self, args: dict[str, Any], *, ctx: AgentContext | None = None) -> ToolResult:
        try:
            scope = self._owner.tool_scope(ctx)
            name, path = args.get("name"), args.get("path", "SKILL.md")
            if set(args) - {"name", "path"} or not isinstance(name, str) or name not in self._packages or not isinstance(path, str):
                raise ValueError("Skill or arguments are not allowed")
            package = self._packages[name]
            path = _relative(path)
            main, manifest = package.main()
            registered: list[dict[str, str]] = []
            previous = scope.loaded_skills.get(name)
            if path == "SKILL.md" or previous is not None:
                definitions, fingerprint = _definitions(package, manifest)
                if previous is not None and previous[0] != fingerprint:
                    raise ValueError("Skill tools changed; start a new run")
            if path == "SKILL.md":
                tools = previous[1] if previous is not None else tuple(self._tool(package, item, fingerprint) for item in definitions)
                self._owner.register_run_tools(scope.ctx, name, fingerprint, tools, validate=lambda: _check_definition(package, fingerprint))
                registered = [{"name": item["ref"] if "ref" in item else item["name"], "callable": tool.name} for item, tool in zip(definitions, tools, strict=True)]
                data = main
            else:
                data = package.read(path)
            return ToolResult(ok=True, output={
                "name": name, "path": path, "base_dir": str(package.base), "content": _text(data),
                "sha256": hashlib.sha256(data).hexdigest(), "registered_tools": registered,
            })
        except (ValueError, OSError, RuntimeError) as exc:
            return ToolResult(ok=False, error=_failure(exc))

    def _tool(self, package: SkillPackage, item: dict[str, Any], fingerprint: str) -> Tool:
        if "ref" in item:
            factory = self._factories.get(item["ref"])
            if factory is None:
                raise ValueError("unknown public Tool reference")
            candidate = factory()
            if candidate.name != item["ref"]:
                raise ValueError("public Tool factory name mismatch")
            existing = next((tool for tool in self._owner.list_tools() if tool.name == candidate.name), None)
            if existing is not None:
                if existing is not candidate and (factory is not type(existing) or existing.arguments != candidate.arguments or existing.description != candidate.description):
                    raise ValueError("public Tool implementation collision")
                return existing
            return candidate
        adapter: type[SkillScriptTool] = type("DeclaredSkillTool", (SkillScriptTool,), {
            "name": f"skill__{package.name}__{item['name']}", "description": item["description"], "arguments": item["arguments"],
        })
        return adapter(self._owner, package, item["script"], fingerprint)


def bind_skills(
    agents: Mapping[str, BaseAgent], *, root: str | Path | None = None, tool_factories: Mapping[str, Callable[[], Tool]] | None = None,
) -> None:
    """mesh 後の版の全 Agent に目録と専用 reader を束縛する。未宣言 package は読まない。"""
    for agent in agents.values():
        if not agent.skills:
            continue
        if ReadSkillTool.name in agent.list_agent_names():
            raise ValueError("read_skill conflicts with a child Agent")
        packages: dict[str, SkillPackage] = {}
        if root is None and not config.SKILLS_DIR:
            raise ValueError("Skill root is not set: pass root or set SKILLS_DIR")
        directory = Path(config.SKILLS_DIR if root is None else root).expanduser().absolute()
        for name in agent.skills:
            if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name) or name in packages:
                raise ValueError("invalid or duplicate declared Skill name")
            try:
                location = directory / name
                base = location.resolve(strict=True)
                temporary = SkillPackage(name, location, base, "")
                description, _ = _metadata(_text(temporary.read("SKILL.md")), name)
                packages[name] = SkillPackage(name, location, base, description)
            except (ValueError, OSError, RuntimeError) as exc:
                raise ValueError(f"Skill {name}: {_failure(exc)}") from None
        catalog = [{"name": package.name, "description": package.description} for _, package in sorted(packages.items())]
        agent.register(ReadSkillTool(agent, packages, tool_factories or {}))
        agent.configure_skills(_RULES + json.dumps(catalog, ensure_ascii=False))
