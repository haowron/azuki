"""既定の general。依頼を基本 Tool(``read``・``write``・``edit``・``bash``)で実行し、結果を返す LLM ループ。

返信の Tool は持たない。結果は run の記録に残る。
"""

from __future__ import annotations

from typing import Any, ClassVar

from trackeverything.core.abc.agent import BaseAgent


class GeneralAgent(BaseAgent):
    """依頼を基本 Tool で実行して結果を返す。"""

    name: ClassVar[str] = "general"
    description: ClassVar[str] = "Carries out a request with the base tools (read, write, edit, bash) and returns the result."
    arguments: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"request": {"type": "string"}},
        "required": ["request"],
        "additionalProperties": False,
    }

    def get_prompt(self) -> str:
        return (
            "You are a general-purpose agent. Carry out the request with the tools you have: read, write, and edit for files, "
            "and bash for commands (including deleting files). Paths may be absolute, '~'-based, or relative to the workspace root. "
            "Check real outputs and exit codes, and never report a failure as a success. "
            "Reference credentials through environment variable names and never print their values. "
            "When the request is an incoming event, decide whether anything needs to be done and say what you did. "
            "Answer with the result; nothing is posted anywhere unless the request asks for it and a tool can do it."
        )
