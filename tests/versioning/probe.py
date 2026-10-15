"""Trees whose entrance ``main`` reports what it sees, shared by the real-git cases (VG04-VG15, VG19-VG28).

``values(a, b)`` is a ``minimal_tree`` whose entrance reports the ``VALUE`` of ``tools/a.py`` and ``tools/b.py``;
``changed`` writes only the changed files, as a person's commit does; ``seen`` runs ``main`` on a new run.
"""

import json
from typing import Any

from tests.fakes import agent_call
from tests.versioning.helpers import minimal_tree

PROBE = '''
import json

from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.tools import a, b


class Entry(BaseAgent):
    name = "main"
    description = "reports the values it sees"

    async def _execute(self, call):
        return AgentResult(status="ok", response=json.dumps({"a": a.VALUE, "b": b.VALUE, "module": __name__}))
'''

#: Files that make a tree fail to load: ``tools/vl_dep.py`` imports a module that divides by zero.
BROKEN = {"tools/vl_dep.py": "from trackeverything.tools import boom  # noqa: F401\nVERSION = 1\n", "tools/boom.py": "RESULT = 1 / 0\n"}


def values(a: Any = 1, b: Any = 1, **extra: Any) -> dict[str, bytes]:
    """A tree whose entrance reports ``tools/a.py`` and ``tools/b.py``; ``extra`` adds or replaces files."""
    return minimal_tree(extra={"agent/main.py": PROBE, "tools/a.py": f"VALUE = {a!r}\n", "tools/b.py": f"VALUE = {b!r}\n", **extra})


def changed(**given: Any) -> dict[str, str]:
    """Only the changed ``tools/<name>.py`` files, as a person's commit writes them."""
    return {f"tools/{name}.py": f"VALUE = {value!r}\n" for name, value in given.items()}


async def seen(repo: Any) -> dict[str, Any]:
    """What a new run's entrance ``main`` reports: ``{"a": ..., "b": ..., "module": ...}``."""
    run = repo.run()
    try:
        agent = run.agent("main")
        result = await agent.run(agent_call(agents=run))
    finally:
        repo.release(run)
    assert result.status == "ok", result.response
    loaded: dict[str, Any] = json.loads(result.response)
    return loaded


def chain(error: BaseException | None) -> list[BaseException]:
    """An exception with its causes and contexts, outermost first."""
    found: list[BaseException] = []
    while error is not None and not any(item is error for item in found):
        found.append(error)
        error = error.__cause__ or error.__context__
    return found
