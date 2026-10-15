"""Generate the core code guide docs/core/code-guide.html.

Descriptions come from docs/core/code-guide.json; files, line counts, symbol lines, import relations,
and public symbols come from the AST of src/trackeverything/core. ``--check`` reports modules and public
symbols without a description, described symbols missing from the code, excerpts that no longer match,
and differences between the Figure 1 arrows and the actual imports between packages, and exits with
status 1 when there are any.
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src/trackeverything"
CORE = SRC / "core"
DATA = ROOT / "docs/core/code-guide.json"
OUTPUT = ROOT / "docs/core/code-guide.html"

GROUPS = [
    ("abc-agent", "abc · BaseAgent", "One base class fixes the execution rules of Agents"),
    ("abc-contracts", "abc · shared contracts", "Types for Agents, Tools, LLMs, Sources, and inputs"),
    ("builtin-exec", "builtin · execution and assembly", "Assembles the runtime process and executes messages one at a time"),
    ("builtin-stores", "builtin · stores", "Saves sessions, channels, and more to PostgreSQL"),
    ("builtin-log", "builtin · logs and errors", "Values for execution records and errors"),
    ("orm-shared", "orm · shared_state", "Tables and connections, and state shared across versions"),
    ("versioning", "versioning · component versions", "Reads versions from the component repository, adopts main, and resolves Agents by name"),
]
PROCESS = {"runtime": ("runtime", "runtime"), "queue": ("queue", "queue"), "both": ("both", "both"), "cli": ("CLI", "none"),
           "import-only": ("import only", "none")}
KIND = {"class": "class", "abc": "ABC", "protocol": "Protocol", "dataclass": "dataclass", "pydantic": "pydantic", "function": "def",
        "coroutine": "async def", "constant": "constant", "type": "type", "contextvar": "ContextVar"}

# Mermaid diagrams render at natural size; diagram-scroll scrolls them horizontally (shrinking makes the text unreadable).
MERMAID_INIT = ('%%{init: {"flowchart": {"useMaxWidth": false, "nodeSpacing": 25, "rankSpacing": 35}, '
                '"sequence": {"useMaxWidth": false, "wrap": true, "actorMargin": 20, "width": 110, "messageMargin": 20}}}%%\n')

# Package layout and arrows of Figure 1 (direction of runtime imports). --check compares them with the actual imports.
BOXES = {
    "versioning": (40, 30, 240, "Version store and main"),
    "builtin": (360, 30, 300, "Execute, store, log, assemble"),
    "abc": (40, 200, 240, "Contracts and BaseAgent"),
    "shared_state": (400, 200, 200, "Shared state, locks"),
    "builtin/domain": (40, 370, 240, "Pure values, run state"),
    "orm": (660, 370, 240, "Tables, connection, schema"),
}
EDGES = {
    ("builtin", "versioning"): ("M360 62 H280", None),
    ("versioning", "builtin"): ("M280 100 H360", None),
    ("versioning", "orm"): ("M40 110 H20 V480 H780 V466", None),
    ("builtin", "abc"): ("M400 126 V160 H250 V200", None),
    ("builtin", "builtin/domain"): ("M640 126 V418 H280", None),
    ("builtin", "orm"): ("M660 62 H940 V418 H900", None),
    ("abc", "builtin/domain"): ("M160 296 V370", None),
    ("abc", "builtin"): ("M220 200 V145 H460 V126", None),
}


@dataclass
class ModuleFacts:
    path: str
    module: str
    lines: int
    symbols: list[dict[str, Any]]
    defined: dict[str, int]
    deps: set[str] = field(default_factory=set)
    runtime_deps: set[str] = field(default_factory=set)
    importers: set[str] = field(default_factory=set)


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT / "src").with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _package(path: str) -> str:
    if path.startswith("builtin/domain/"):
        return "builtin/domain"
    return path.split("/")[0] if "/" in path else path.removesuffix(".py")


def definitions(node: ast.AST, prefix: str = "", attributes: bool = True) -> dict[str, int]:
    """Map each qualified name (``Class.method``, ``function.inner``) to the line of its first definition.

    Classes and functions count at any depth; assignments and imports only where they bind a module or class attribute.
    """
    found: dict[str, int] = {}
    for child in ast.iter_child_nodes(node):
        bound: list[tuple[str, int]] = []
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bound = [(child.name, child.lineno)]
            nested = definitions(child, f"{prefix}{child.name}.", isinstance(child, ast.ClassDef))
        else:
            if attributes and isinstance(child, (ast.Import, ast.ImportFrom)):
                bound = [((a.asname or a.name).split(".")[0], a.lineno) for a in child.names]
            elif attributes and isinstance(child, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.name if isinstance(child, ast.TypeAlias) else child.target]
                bound = [(t.id, child.lineno) for t in targets if isinstance(t, ast.Name)]
            nested = definitions(child, prefix, attributes)
        for name, line in [*((prefix + n, ln) for n, ln in bound), *nested.items()]:
            found.setdefault(name, line)
    return found


def scan() -> dict[str, ModuleFacts]:
    """Read the public symbols of each core module and the import relations across src from the AST."""
    names = {_module_name(p): p for p in (ROOT / "src").rglob("*.py")}
    facts: dict[str, ModuleFacts] = {}
    imports: dict[str, set[str]] = defaultdict(set)
    for name, path in names.items():
        tree = ast.parse(path.read_text())
        typing_only = {id(n) for node in ast.walk(tree) if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test) for n in ast.walk(node)}
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trackeverything"):
                targets = [f"{node.module}.{a.name}" if f"{node.module}.{a.name}" in names else node.module for a in node.names]
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names if a.name.startswith("trackeverything")]
            for target in targets:
                imports[name].add(target)
                if id(node) not in typing_only and path.is_relative_to(CORE):
                    imports[name + "#runtime"].add(target)
        if path.is_relative_to(CORE):
            rel = path.relative_to(CORE).as_posix()
            symbols = []
            for node in tree.body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({"name": node.name, "kind": "class" if isinstance(node, ast.ClassDef) else "function", "line": node.lineno})
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
                    targets_ = node.targets if isinstance(node, ast.Assign) else [node.name if isinstance(node, ast.TypeAlias) else node.target]
                    symbols += [{"name": t.id, "kind": "constant", "line": node.lineno} for t in targets_ if isinstance(t, ast.Name) and t.id[:1].isupper()]
            facts[name] = ModuleFacts(rel, name, len(path.read_text().splitlines()), symbols, definitions(tree))
    for name, item in facts.items():
        item.deps = {d for d in imports[name] if d != name}
        item.runtime_deps = {d for d in imports[name + "#runtime"] if d != name}
        item.importers = {m for m, ds in imports.items() if "#" not in m and m != name and name in ds}
    return {item.path: item for item in facts.values()}


def package_edges(facts: dict[str, ModuleFacts]) -> set[tuple[str, str]]:
    by_module = {item.module: item.path for item in facts.values()}
    edges = set()
    for item in facts.values():
        for dep in item.runtime_deps:
            if dep in by_module and _package(by_module[dep]) != _package(item.path) and _package(item.path) != "__init__":
                edges.add((_package(item.path), _package(by_module[dep])))
    return edges


def check(facts: dict[str, ModuleFacts], data: dict[str, Any]) -> list[str]:
    documented = {m["path"]: m for group in data["groups"] for m in group["modules"]}
    problems = [f"module without a description: {path}" for path in sorted(set(facts) - set(documented))]
    problems += [f"module not in the code: {path}" for path in sorted(set(documented) - set(facts))]
    for path, item in sorted(facts.items()):
        names = {s["name"] for s in documented.get(path, {}).get("key_symbols", [])}
        problems += [f"public symbol without a description: {path}:{s['line']} {s['name']}" for s in item.symbols if not s["name"].startswith("_") and s["name"] not in names]
        problems += [f"described symbol not in the code: {path} {name}" for name in sorted(names - set(item.defined))]
    actual = package_edges(facts)
    problems += [f"import missing from Figure 1: {a} -> {b}" for a, b in sorted(actual - set(EDGES))]
    problems += [f"no import for the Figure 1 arrow: {a} -> {b}" for a, b in sorted(set(EDGES) - actual)]
    for mechanism in data.get("mechanisms", []):
        for excerpt in mechanism["excerpts"]:
            if excerpt_line(excerpt) is None:
                problems += [f"excerpt does not match the code: {mechanism['id']} {excerpt['path']}"]
    return problems


def excerpt_line(excerpt: dict[str, Any]) -> int | None:
    """Return the start line when the excerpt still appears verbatim in the source, otherwise None."""
    path = SRC / excerpt["path"]
    text = path.read_text() if path.is_file() else ""
    index = text.find(excerpt["code"])
    return None if index < 0 else text.count("\n", 0, index) + 1


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def md(text: str) -> str:
    """Escape HTML and turn `code` into <code>."""
    return "".join(f"<code>{esc(p[1:-1])}</code>" if len(p) > 1 and p[0] == p[-1] == "`" else esc(p) for p in re.split(r"(`[^`]+`)", text or ""))


def anchor(path: str) -> str:
    return "m-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")


def short(module: str) -> str:
    return module.removeprefix("trackeverything.").removeprefix("core.")


def layered(text: str) -> str:
    """Turn heading lines and item lines starting with ``- `` into bullet lists under headings."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("- "):
            out.append(f"<li>{md(line[2:])}</li>")
        else:
            out.append(f'</ul><h4 class="layer">{md(line)}</h4><ul class="layer">')
    return ("<ul>" + "".join(out) + "</ul>").replace("<ul></ul>", "")


def render(facts: dict[str, ModuleFacts], data: dict[str, Any]) -> str:
    groups = {g["key"]: g for g in data["groups"]}
    documented = {m["path"]: m for g in data["groups"] for m in g["modules"]}
    cross = data["cross"]
    public = [(path, s) for path, item in facts.items() for s in item.symbols if not s["name"].startswith("_")]
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for path, item in facts.items():
        stats[_package(path)][0] += 1
        stats[_package(path)][1] += item.lines

    def link(path: str) -> str:
        if path.startswith("../"):
            return f"<code>{esc(path.removeprefix('../'))}</code>"
        return f'<a href="#{anchor(path)}"><code>{esc(path)}</code></a>' if path in documented else f"<code>{esc(path)}</code>"

    def diagram(d: dict[str, Any] | None) -> str:
        if not d:
            return ""
        return (f'<figure class="mmd"><div class="diagram-scroll" tabindex="0" role="region" aria-label="{esc(d["caption"])}">'
                f'<pre class="mermaid">{esc(MERMAID_INIT + d["mermaid"])}</pre></div><figcaption>{md(d["caption"])}</figcaption></figure>')

    def badge(process: str) -> str:
        label, cls = PROCESS.get(process, (process, "none"))
        return f'<span class="badge {cls}">{esc(label)}</span>'

    def names(items: list[str], limit: int = 12) -> str:
        if not items:
            return "none"
        shown = ", ".join(f"<code>{esc(short(i))}</code>" for i in items[:limit])
        return shown + (f" and {len(items) - limit} more modules" if len(items) > limit else "")

    def boxes() -> str:
        out = []
        for name, (x, y, w, role) in BOXES.items():
            files, lines = stats[name]
            label = name if name == "shared_state" else name + "/"
            out.append(f'<rect x="{x}" y="{y}" width="{w}" height="96" rx="8" class="box{" accent" if name == "builtin" else " "}"/>'
                       f'<text x="{x + 20}" y="{y + 32}" class="node-title mono">{esc(label)}</text>'
                       f'<text x="{x + 20}" y="{y + 59}" class="sub">{esc(role)}</text>'
                       f'<text x="{x + 20}" y="{y + 83}" class="mono sub">{files} file{"" if files == 1 else "s"} · {lines:,} LOC</text>')
        for d, label in EDGES.values():
            out.append(f'<path d="{d}" class="edge" marker-end="url(#arrow)"/>')
            if label:
                out.append(f'<text x="320" y="52" class="edge-label" text-anchor="middle">{label}</text>')
        return "".join(out)

    def module_block(m: dict[str, Any]) -> str:
        item = facts[m["path"]]
        rows = "".join(f"<tr><td><code>{esc(s['name'])}</code></td><td>{esc(KIND.get(s['kind'], s['kind']))}</td><td class=\"num\">{item.defined.get(s['name'], '')}</td><td>{md(s['explain'])}</td></tr>"
                       for s in m["key_symbols"])
        table = (f'<div class="table-scroll" tabindex="0" role="region" aria-label="Symbols in {esc(m["path"])}"><table class="symbols"><thead><tr>'
                 f"<th>Name</th><th>Kind</th><th>Line</th><th>Description</th></tr></thead><tbody>{rows}</tbody></table></div>") if rows else ""
        rules = "".join(f"<li>{md(x)}</li>" for x in m["invariants"])
        pitfalls = "".join(f"<li>{md(x)}</li>" for x in m["pitfalls"])
        flow = f'<p class="flow"><strong>Usage</strong>{md(m["flow"])}</p>' if m["flow"] else ""
        search = " ".join([m["path"], m["role"], *(s["name"] for s in m["key_symbols"])]).lower()
        return f"""<details class="contract module" id="{anchor(m['path'])}" data-search="{esc(search)}">
<summary><code>{esc(m['path'])}</code><span class="sum-role">{md(m['role'])}</span></summary>
<div class="contract-body">
<p class="meta">{badge(m['process'])}<span>{item.lines} lines</span><a href="../../src/trackeverything/core/{esc(m['path'])}">View source</a></p>
<p>{md(m['summary'])}</p>
{flow}{table}
{f'<h4>Rules it follows</h4><ul>{rules}</ul>' if rules else ''}
{f'<div class="planned"><strong>Pitfalls</strong><ul>{pitfalls}</ul></div>' if pitfalls else ''}
<dl class="deps"><dt>Imports in core</dt><dd>{names(sorted(d for d in item.deps if d.startswith('trackeverything.core')))}</dd>
<dt>Imports outside core</dt><dd>{names(sorted(d for d in item.deps if not d.startswith('trackeverything.core')))}</dd>
<dt>Used by</dt><dd>{names(sorted(item.importers, key=lambda x: (not x.startswith('trackeverything.core'), x)))}</dd></dl>
</div>
</details>"""

    sections = []
    for key, title, lead in GROUPS:
        group = groups[key]
        rows = "".join(f'<tr><td><a href="#{anchor(m["path"])}"><code>{esc(m["path"])}</code></a></td><td class="num">{facts[m["path"]].lines}</td>'
                       f"<td>{badge(m['process'])}</td><td>{md(m['role'])}</td></tr>" for m in group["modules"])
        sections.append(f"""<section id="g-{key}">
<h2>{esc(title)}</h2>
<p class="lead">{esc(lead)}. {md(group['overview'])}</p>
{diagram(group.get('diagram'))}
<div class="table-scroll" tabindex="0" role="region" aria-label="Files in {esc(title)}"><table><thead><tr><th>File</th><th>Lines</th><th>process</th><th>Responsibility</th></tr></thead><tbody>{rows}</tbody></table></div>
{chr(10).join(module_block(m) for m in group['modules'])}
</section>""")

    def steps(items: list[dict[str, Any]]) -> str:
        return "<ol>" + "".join(f"<li><strong>{md(s['title'])}</strong><small>{md(s['detail'])}"
                                + (f'<span class="mods">{", ".join(link(p) for p in s["modules"])}</span>' if s["modules"] else "") + "</small></li>" for s in items) + "</ol>"

    explained = {(m["path"], s["name"]): s for m in documented.values() for s in m["key_symbols"]}
    index = "".join(
        f'<tr data-search="{esc((s["name"] + " " + path).lower())}"><td><code>{esc(s["name"])}</code></td>'
        f'<td>{esc(KIND.get(explained[(path, s["name"])]["kind"], "") if (path, s["name"]) in explained else s["kind"])}</td>'
        f'<td><a href="#{anchor(path)}"><code>{esc(path)}</code></a>:{s["line"]}</td><td>{md(explained[(path, s["name"])]["explain"]) if (path, s["name"]) in explained else ""}</td></tr>'
        for path, s in sorted(public, key=lambda x: (x[1]["name"].lower(), x[0])))
    mechanisms = data.get("mechanisms", [])
    parts = data.get("parts", [])
    numbers = {m["id"]: n for n, m in enumerate(mechanisms, 1)}

    def code_block(excerpt: dict[str, Any]) -> str:
        start = excerpt_line(excerpt) or 0
        lines = excerpt["code"].split("\n")
        body = "\n".join(f'<span class="ln">{start + i}</span>{esc(line)}' for i, line in enumerate(lines))
        return (f'<figure class="code"><figcaption><a href="../../src/trackeverything/{esc(excerpt["path"])}"><code>{esc(excerpt["path"])}:{start}</code></a>'
                f'<span>{md(excerpt["caption"])}</span></figcaption><pre><code>{body}</code></pre></figure>')

    def mechanism_block(m: dict[str, Any]) -> str:
        how = "".join(f"<li>{md(step)}</li>" for step in m["steps"])
        notes = "".join(f"<li>{md(note)}</li>" for note in m["notes"])
        return (f'<article class="mech" id="mech-{m["id"]}">\n<h3><span class="mech-num">{numbers[m["id"]]}</span>{md(m["title"])}</h3>\n'
                f'<p class="design"><strong>Design</strong>{md(m["design"])}</p>\n{diagram(m.get("diagram"))}\n<h4>How the code implements it</h4>\n<ol class="how">{how}</ol>\n'
                + "\n".join(code_block(e) for e in m["excerpts"])
                + (f'\n<div class="planned"><strong>Notes</strong><ul>{notes}</ul></div>' if notes else "") + "\n</article>")

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    toc = "".join(f'<li><a href="#mech-{m["id"]}">{md(m["title"])}</a></li>' for m in mechanisms)
    impl = [f"""<section id="impl">
<h2>How the design is implemented</h2>
<p class="lead">The sections below follow the path of a message and explain which functions implement each design and how.
Each section first states the design (one line), then explains the code in execution order, and ends with verbatim code excerpts (line numbers refer to commit {esc(head)}).</p>
<ol class="impl-toc">{toc}</ol>
</section>"""] if mechanisms else []
    for part in parts:
        items = [m for m in mechanisms if m["part"] == part["id"]]
        impl.append(f'<section id="{part["id"]}">\n<h2>{esc(part["title"])}</h2>\n<p class="lead">{md(part["lead"])}</p>\n'
                    + "\n".join(mechanism_block(m) for m in items) + "\n</section>")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    day = subprocess.run(["git", "log", "-1", "--format=%cs"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    style = re.search(r"<style>(.*?)</style>", (ROOT / "docs/ARCHITECTURE.html").read_text(), re.S)
    nav = [("overview", "Overview"), *((("impl", "How the design is implemented"),) if mechanisms else ()), *((p["id"], p["title"]) for p in parts), ("reading", "Reading order"), ("flow", "Message flow"), ("lifecycle", "Startup and shutdown"),
           *((f"g-{key}", title) for key, title, _ in GROUPS), ("index", "Symbol index")]
    nav_html = "\n".join(f'<a href="#{i}"{" aria-current=\"location\"" if n == 0 else ""}>{esc(t)}</a>' for n, (i, t) in enumerate(nav))
    flow_rows = "".join(f"<tr><td><strong>{md(s['title'])}</strong></td><td>{md(s['detail'])}</td><td>{'<br>'.join(link(p) for p in s['modules'])}</td></tr>"
                        for s in cross["message_path"])
    return TEMPLATE.format(
        style=(style.group(1) if style else "") + EXTRA_CSS, icon=ICON, nav=nav_html, commit=esc(commit), day=esc(day),
        files=len(facts), lines=f"{sum(i.lines for i in facts.values()):,}", symbols=len(public), packages=len(BOXES),
        svg=boxes(), layers=layered(cross["layers"]), rules="".join(f"<li>{md(r)}</li>" for r in cross["layer_rules"]),
        reading="".join(f"<li>{link(r['path'])}<small>{md(r['why'])}</small></li>" for r in cross["reading_order"]),
        flow=flow_rows, startup=steps(cross["startup"]), shutdown=steps(cross["shutdown"]),
        queue="".join(f"<tr><td>{link(q['path'])}</td><td>{md(q['why'])}</td></tr>" for q in cross["queue_uses"]),
        groups="\n".join(sections), index=index, impl="\n".join(impl),
        flow_diagram=diagram(cross.get("message_path_diagram")), lifecycle_diagram=diagram(cross.get("lifecycle_diagram")),
    )


ICON = (
    "data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 40 40%27%3"
    "E%3Crect width=%2740%27 height=%2740%27 rx=%278%27 fill=%27%23086b64%27/%3E%3Cpath d=%27M1"
    "0 12h20M20 12v18%27 stroke=%27white%27 stroke-width=%274%27/%3E%3C/svg%3E"
)

EXTRA_CSS = """
.mmd{margin:1.25rem 0 1.75rem}.mmd figcaption{margin-top:.6rem}
.mmd pre.mermaid{margin:0;padding:1rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;
color:var(--text);font-family:var(--sans);font-size:.85rem;white-space:pre;min-width:min-content;text-align:center}
.mmd pre.mermaid svg{display:inline-block;width:auto;min-width:0;max-width:none;height:auto}
.lead{color:var(--dim);max-width:76ch}
.stats{display:flex;flex-wrap:wrap;gap:.6rem 1.6rem;margin:1rem 0 0;padding:0;list-style:none;font-size:.86rem;color:var(--dim)}
.stats li{margin:0}
.stats strong{font:600 1.35rem var(--mono);color:var(--text);margin-right:.35rem}
.badge{display:inline-block;font:600 .72rem var(--mono);padding:.12rem .5rem;border-radius:999px;border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.badge.runtime{border-color:var(--accent);color:var(--accent);background:var(--tint)}
.badge.queue{border-color:var(--warning);color:var(--warning);background:var(--warningbg)}
.badge.both{border-color:var(--text);color:var(--text)}
details.module summary{display:flex;flex-wrap:wrap;align-items:baseline;gap:.25rem 1rem}
details.module summary code{font-size:.9rem}
.sum-role{font-weight:400;color:var(--dim);font-size:.86rem}
.meta{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem 1rem;font-size:.8rem;color:var(--dim)}
.flow{font-size:.9rem}
.flow strong{color:var(--accent);margin-right:.6rem}
.contract-body h4{font-size:.92rem;margin:1.5rem 0 .4rem}
.deps{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.35rem 1rem;font-size:.82rem;margin:1.25rem 0 0;color:var(--dim)}
.deps dt{font-weight:600;color:var(--text)}
.deps dd{margin:0}
table.symbols td:first-child{white-space:nowrap}
td.num{font-family:var(--mono);text-align:right;width:3.5rem;min-width:0}
.mods{display:block;margin-top:.3rem}
.reading small,.lifecycle li small{display:block;color:var(--dim);font-size:.84rem;line-height:1.65}
.lifecycle li small code{font-size:.94em}
.search{display:flex;gap:.6rem;align-items:center;margin:1rem 0}
.search input{flex:1;max-width:28rem;font:inherit;font-size:.9rem;padding:.55rem .75rem;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--text)}
.search small{color:var(--dim)}
[data-search].hidden{display:none}
.layer{margin:.4rem 0}h4.layer{font-size:.95rem;margin:1.2rem 0 .2rem}
.impl-toc{columns:2;column-gap:2.5rem;font-size:.9rem;padding-left:1.4rem}
.impl-toc li{break-inside:avoid}
.mech{margin:2.5rem 0 0;padding-top:2rem;border-top:1px solid var(--border)}
.mech:first-of-type{border-top:0;padding-top:0;margin-top:1.5rem}
.mech h3{display:flex;gap:.75rem;align-items:baseline;margin:0 0 .8rem;font-size:1.2rem}
.mech-num{font:600 .85rem var(--mono);color:var(--accent);border:1px solid var(--accent);border-radius:999px;min-width:1.9rem;text-align:center;padding:.05rem .45rem;flex:none}
.design{background:var(--tint);border-left:3px solid var(--accent);padding:.7rem 1rem;max-width:none;font-size:.92rem}
.design strong{color:var(--accent);margin-right:.7rem}
.mech h4{font-size:.95rem;margin:1.4rem 0 .4rem}
.how{max-width:80ch;font-size:.93rem}
.how li+li{margin-top:.55rem}
figure.code{margin:1.1rem 0}
figure.code figcaption{display:flex;flex-wrap:wrap;gap:.3rem .9rem;align-items:baseline;margin:0 0 .35rem;font-size:.82rem}
figure.code figcaption code{font-size:.8rem}
figure.code pre{margin:0;padding:.9rem 1rem;font-size:.78rem;line-height:1.6;border-radius:8px;background:var(--surface)}
.ln{display:inline-block;width:2.6rem;margin-right:1rem;color:var(--line);text-align:right;user-select:none}
@media(max-width:600px){.impl-toc{columns:1}}
.planned ul{margin:.3rem 0 0}
@media(max-width:600px){.deps{grid-template-columns:minmax(0,1fr)}
.deps dd{margin-bottom:.4rem}
}
"""

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="The responsibility, usage, and rules of every module and public symbol in the TrackEverything core (src/trackeverything/core).">
<title>TrackEverything — core code guide</title>
<link rel="icon" href="{icon}">
<!-- Generated by scripts/render_core_guide.py from docs/core/code-guide.json and the code. Do not edit directly. -->
<style>{style}</style>
</head>
<body>
<a class="skip" href="#overview">Skip to content</a>
<svg width="0" height="0" style="position:absolute;min-width:0;width:0;height:0" aria-hidden="true"><defs>
<marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 Z" class="arrow-head"/></marker>
</defs></svg>
<div class="shell">
<aside class="sidebar">
<a href="#overview" class="brand">TrackEverything</a>
<p class="subline">core code guide</p>
<nav class="nav" aria-label="Page sections">
{nav}
</nav>
<div class="side-footer">
<a href="../ARCHITECTURE.html">Architecture</a>
<a href="README.md">core README</a>
<a href="../dependencies.html">Code dependency graph</a>
<span>Generated from the implementation at commit {commit}.</span>
</div>
</aside>
<main class="content" id="main">
<section id="overview">
<header class="intro">
<h1>core code guide</h1>
<p>Covers every module in <code>src/trackeverything/core/</code>: what it is responsible for, where it is used, and which rules it follows.
The source of truth for the shared contracts is <a href="../ARCHITECTURE.html">Architecture</a>; this page is a map for reading the code.</p>
<ul class="stats"><li><strong>{files}</strong>files</li><li><strong>{lines}</strong>lines</li><li><strong>{symbols}</strong>public symbols</li><li><strong>{packages}</strong>packages</li></ul>
<span class="state">commit {commit} · {day}</span>
</header>
<figure>
<div class="diagram-scroll" tabindex="0" role="region" aria-label="Figure 1. Scrolls horizontally on narrow screens">
<svg viewBox="0 0 960 490" role="img" aria-labelledby="fig-pkg-title"><title id="fig-pkg-title">The packages of core and the direction of runtime imports.
builtin composes the other packages, abc depends only on the pure values in builtin/domain, and versioning saves versions to the orm tables.</title>{svg}</svg>
</div>
<figcaption><span>Fig. 1</span> Arrows show the direction of runtime imports (imports used only for type annotations are excluded).
builtin composes the other packages, and abc depends only on the pure values in builtin/domain.
builtin's <code>runtime.py</code> and <code>app.py</code> get the Agents of the main version through the versioning version store; versioning's <code>store.py</code> reads versions from the component git repository's mirror,
and only the check CLI <code>versioning/__main__.py</code> imports builtin's assembly functions in the reverse direction.</figcaption>
</figure>
{layers}
<h3>Dependency rules</h3>
<ul>{rules}</ul>
<p class="section-note">The process of each module is the process in which the module actually does its work.
Modules that the queue process loads only through an import chain are marked runtime, and the reasons are listed in the "core loaded by the queue process" table.
<span class="badge runtime">runtime</span> means the runtime process, <span class="badge queue">queue</span> the queue process,
<span class="badge both">both</span> both processes, and <span class="badge none">CLI</span> a command run by hand or by the component repository's pre-push hook (the check CLI).</p>
</section>
{impl}
<section id="reading">
<h2>Where to start reading</h2>
<ol class="reading">{reading}</ol>
</section>
<section id="flow">
<h2>core in the message flow</h2>
<p>Each step of a message, from intake and saving to Redis through execution and ACK in the runtime process, with the modules responsible for it.
See <a href="../ARCHITECTURE.html#message-flow">Message flow</a> for the full flow.</p>
{flow_diagram}
<div class="table-scroll" tabindex="0" role="region" aria-label="Steps and responsible modules"><table><thead><tr><th>Step</th><th>What happens</th><th>Owner</th></tr></thead><tbody>{flow}</tbody></table></div>
</section>
<section id="lifecycle">
<h2>Startup and shutdown of the runtime process</h2>
{lifecycle_diagram}
<div class="lifecycle" role="group" aria-label="Startup and shutdown order">
<div><h3>Startup <code>serve()</code></h3>{startup}</div>
<div><h3>Shutdown</h3>{shutdown}</div>
</div>
<h3>core loaded by the queue process</h3>
<p>The queue process runs no Agents but uses the following core modules.</p>
<div class="table-scroll" tabindex="0" role="region" aria-label="core used by the queue process"><table><thead><tr><th>Module</th><th>Reason</th></tr></thead><tbody>{queue}</tbody></table></div>
</section>
{groups}
<section id="index">
<h2>Symbol index</h2>
<p>All public symbols of core, listed from the AST (top-level names that do not start with <code>_</code>).</p>
<div class="search"><input id="search" type="search" placeholder="Filter by name or file (e.g. Runtime, lease, store)" aria-label="Filter modules and symbols"><small id="search-count"></small></div>
<div class="table-scroll" tabindex="0" role="region" aria-label="Symbol index"><table class="symbols"><thead><tr><th>Name</th><th>Kind</th><th>Location</th><th>Description</th></tr></thead><tbody>{index}</tbody></table></div>
<p class="footnote">Files, line counts, symbol line numbers, import relations, and public symbols come from the AST of the code at commit {commit},
and the descriptions have been checked against the code. Line numbers are the lines where the definitions start.
To change a description, edit <code>docs/core/code-guide.json</code> and regenerate the page with <code>uv run python scripts/render_core_guide.py</code>.</p>
</section>
</main>
</div>
<script id="mermaid-lib" src="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"></script>
<script id="mermaid-init">mermaid.initialize({{startOnLoad:true,securityLevel:'strict',theme:matchMedia('(prefers-color-scheme: dark)').matches?'dark':'default'}});</script>
<script>
const navLinks=[...document.querySelectorAll('.nav a')];
const observer=new IntersectionObserver(entries=>{{const c=entries.filter(e=>e.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top)[0];
if(c)navLinks.forEach(a=>{{if(a.hash==='#'+c.target.id)a.setAttribute('aria-current','location');
else a.removeAttribute('aria-current');
}});
}},{{rootMargin:'-12% 0px -60% 0px',threshold:0}});
document.querySelectorAll('main>section').forEach(s=>observer.observe(s));
function reveal(){{let h;
try{{h=decodeURIComponent(location.hash.slice(1));
}}catch{{return;
}}const t=document.getElementById(h);
if(t&&t.matches('details')){{t.open=true;
requestAnimationFrame(()=>t.scrollIntoView({{block:'start'}}));
}}}}
addEventListener('hashchange',reveal);
document.querySelectorAll('a[href^="#m-"]').forEach(a=>a.addEventListener('click',()=>{{const d=document.getElementById(a.hash.slice(1));
if(d)d.open=true;
}}));
reveal();
const input=document.getElementById('search'),count=document.getElementById('search-count');
input.addEventListener('input',()=>{{const q=input.value.trim().toLowerCase();let shown=0;
document.querySelectorAll('#index tbody tr').forEach(r=>{{const hit=!q||r.dataset.search.includes(q);r.classList.toggle('hidden',!hit);if(hit)shown++;}});
document.querySelectorAll('details.module').forEach(d=>{{if(q)d.open=d.dataset.search.includes(q);}});
count.textContent=q?shown+' shown':'';}});
let preprint=[];
const all=[...document.querySelectorAll('details')];
addEventListener('beforeprint',()=>{{preprint=all.map(d=>d.open);
all.forEach(d=>d.open=true);
}});
addEventListener('afterprint',()=>all.forEach((d,i)=>d.open=preprint[i]));
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report missing or stale descriptions and excerpts and differences from the Figure 1 arrows (does not generate the page)")
    args = parser.parse_args()
    facts = scan()
    data = json.loads(DATA.read_text())
    problems = check(facts, data)
    for problem in problems:
        print(problem, file=sys.stderr)
    if args.check:
        sys.exit(1 if problems else 0)
    OUTPUT.write_text(render(facts, data))
    print(f"{OUTPUT.relative_to(ROOT)}: {len(facts)} files, {sum(1 for _ in problems)} problem(s)")


if __name__ == "__main__":
    main()
