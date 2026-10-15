"""Structured failure notes and per-agent failure scopes in a shared context.

``ErrorNote`` is the single shape an orchestration-level failure takes when
it crosses a run boundary: which stage failed (a machine-readable
identifier) and the verbatim cause. Rendering keeps the original text so a
delivered notification stays greppable against the source — stage labels
and causes are never translated.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from trackeverything.core.builtin.domain.execution_log import current_agent_execution_id


@dataclass(frozen=True)
class ErrorNote:
    """One failure: a machine-readable stage and the verbatim cause."""

    stage: str
    cause: str


def render(notes: Iterable[ErrorNote]) -> str:
    """Render notes as ``stage: cause`` pairs joined with ``'; '``."""
    return "; ".join(f"{note.stage}: {note.cause}" for note in notes)


class ToolFailures:
    """Agent-execution-scoped tool failures inside the shared run context.

    A failed call is recorded under the tool's declared target key. A later
    successful call resolves the same key, and also the tool's unaddressed
    slot — the failures whose arguments carried no usable target, which no
    key could ever match. The map therefore holds, at any moment, exactly
    the failures that never recovered. Each run boundary consumes it
    (``take``) so failures never leak past the agent that owns them.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str | None, dict[str, ErrorNote]] = {}

    def _current(self) -> dict[str, ErrorNote]:
        return self._entries.setdefault(current_agent_execution_id.get(), {})

    def record(self, tool: str, key: str | None, note: ErrorNote) -> None:
        """Record a failure under ``(tool, key)``; key None means unaddressed."""
        self._current()[self._slot(tool, key)] = note

    def resolve(self, tool: str, key: str | None) -> None:
        """Resolve a recovered target and the tool's unaddressed failures."""
        self._current().pop(self._slot(tool, key), None)
        self._current().pop(self._slot(tool, None), None)

    def unresolved(self, *tools: str) -> tuple[ErrorNote, ...]:
        """Unresolved notes, optionally restricted to the given tools."""
        if not tools:
            return tuple(self._current().values())
        wanted = set(tools)
        return tuple(
            note for slot, note in self._current().items() if slot.split("\x00", 1)[0] in wanted
        )

    def take(self) -> tuple[ErrorNote, ...]:
        """Consume and return every unresolved note (run-boundary ownership)."""
        notes = tuple(self._entries.pop(current_agent_execution_id.get(), {}).values())
        return notes

    @staticmethod
    def _slot(tool: str, key: str | None) -> str:
        return f"{tool}\x00{key if key is not None else '*'}"
