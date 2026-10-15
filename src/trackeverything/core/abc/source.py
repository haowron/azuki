"""Source contract consumed by the dispatch Agent and the fixed ``fetch_source`` tool."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Source(ABC):
    """A source-defined entity snapshot.

    Identity and payload formats belong to each concrete source. The optional
    project tag groups related entities; entrance routing stays with the
    dispatch Agent rather than leaking into source implementations.
    """

    _project: str | None = None

    @abstractmethod
    async def get_identity(self) -> str:
        """Return a stable, source-unique correlation key."""
        ...

    @abstractmethod
    async def get_payload(self) -> dict[str, Any]:
        """Return the current source-defined entity snapshot."""
        ...

    def get_project(self) -> str | None:
        """Return the optional project grouping tag."""
        return self._project

    @abstractmethod
    def get_source_description(self) -> str:
        """Describe this source and the semantics of its payload."""
        ...

    @classmethod
    @abstractmethod
    def from_config(cls, **kwargs: Any) -> Source:
        """Construct one source instance from ``source_kwargs``."""
        ...

    @classmethod
    def material_view(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Reduce a payload to what can change project material, **without I/O**.

        Project sync compares this view (not the raw payload) with the last
        processed input and shows it to the LLM, so fields that only churn —
        counters, URLs, timestamps, nested metadata — neither trigger a sync
        nor fill the prompt. The default keeps the payload unchanged. An
        override must be pure and idempotent (``material_view`` of its own
        output returns the same dict), because stored records written before
        the override, or by it, go through the same function.
        """
        return payload

    @classmethod
    def identity_for(cls, **kwargs: Any) -> str | None:
        """Derive :meth:`get_identity` from ``source_kwargs`` **without I/O**.

        The default returns ``None``: a source whose identity needs network
        resolution (e.g. a name→id lookup) opts out, so the dispatch Agent's
        routing (GitHub events and named runs with a Source) never triggers a fetch.
        A source whose identity is pure formatting overrides this and must
        return the same string :meth:`get_identity` would for the same
        locator. :meth:`get_identity` is a coroutine, so share a pure
        formatting helper between the two instead of calling it here.
        """
        return None
