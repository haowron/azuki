"""AG01-AG07: 修正促し(repair)の後も回答本文を捨てず、失敗と一緒に返す。

失敗の notes は ``run()`` の境界(``_adjudicate``)が一度だけ付けるため、結果は公開の ``run()`` で確かめる。
"""

from tests.fakes import ScriptedLLM, agent_call
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.llm import Call, Calls, FinalAnswer
from trackeverything.core.abc.tool import Tool, ToolResult
from trackeverything.core.builtin.domain.errors import ErrorNote

_PROBE_FAILURE = ErrorNote("probe", "probe refused")


class _Probe(Tool):
    """``fail_once=True`` の間は失敗し、その後は成功する Tool。"""

    name = "probe"
    description = "fails while armed"
    arguments = {"type": "object", "properties": {}, "additionalProperties": False}
    fail_once = True

    async def run(self, args, *, ctx=None) -> ToolResult:
        if self.fail_once:
            self.fail_once = False
            return ToolResult(ok=False, error="probe refused")
        return ToolResult(ok=True, output="probed")


class _Steady(_Probe):
    """常に成功する Tool。"""

    name = "steady"
    fail_once = False


class _Agent(BaseAgent):
    name = "repair_probe"
    fatal_tools = frozenset({"probe"})
    tools = (_Probe, _Steady)

    async def _execute(self, call):
        return await self._execute_llm_loop(call, repair_tool_failures=True)


async def test_ag01_second_final_answer_keeps_the_text_with_one_note_per_failure():
    agent = _Agent(ScriptedLLM(Calls((Call("probe", {}),)), FinalAnswer("最初の回答"), FinalAnswer("最終回答")))
    result = await agent.run(agent_call(request="依頼"))
    assert result.status == "error"
    assert result.response == "最終回答"
    assert result.errors == (_PROBE_FAILURE,)


async def test_ag02_repair_prompt_asks_once_for_the_complete_answer_again():
    llm = ScriptedLLM(Calls((Call("probe", {}),)), FinalAnswer("最初の回答"), FinalAnswer("最終回答"))
    await _Agent(llm).run(agent_call(request="依頼"))
    # 三度目の LLM 呼出しには、修正促しと失敗の本文が user message として含まれる。
    repair_messages = [m for m in llm.requests[2][0] if m.role == "user" and "Required tool operations remain unresolved" in m.content]
    assert len(repair_messages) == 1
    assert "probe refused" in repair_messages[0].content
    assert "give the complete answer again" in repair_messages[0].content


async def test_ag03_non_fatal_failure_does_not_flip_the_answer():
    class _Loose(_Agent):
        name = "loose_probe"
        fatal_tools = frozenset()

    agent = _Loose(ScriptedLLM(Calls((Call("probe", {}),)), FinalAnswer("そのままの回答")))
    result = await agent.run(agent_call(request="依頼"))
    assert result.status == "ok"
    assert result.response == "そのままの回答"


async def test_ag04_repaired_failure_returns_ok():
    agent = _Agent(ScriptedLLM(Calls((Call("probe", {}),)), Calls((Call("probe", {}),)), FinalAnswer("修復後の回答")))
    result = await agent.run(agent_call(request="依頼"))
    assert result.status == "ok"
    assert result.response == "修復後の回答"


async def test_ag05_budget_ending_after_an_answer_keeps_that_answer():
    # 1 step 目で probe が失敗し、2 step 目の回答に修正を促した後、3 step 目で予算が尽きる。
    agent = _Agent(ScriptedLLM(Calls((Call("probe", {}),)), FinalAnswer("最初の回答")))
    result = await agent.run(agent_call(request="依頼", max_steps=2))
    assert result.status == "error"
    assert result.response == "最初の回答"
    assert result.errors == (_PROBE_FAILURE,)


async def test_ag06_budget_ending_without_an_answer_reports_the_incomplete_operations():
    agent = _Agent(ScriptedLLM(Calls((Call("probe", {}),))))
    result = await agent.run(agent_call(request="依頼", max_steps=1))
    assert result.status == "error"
    assert result.response == "Required operations remain incomplete: probe: probe refused"
    assert result.errors == (_PROBE_FAILURE,)


async def test_ag07_budget_ending_without_failures_stays_max_steps():
    agent = _Agent(ScriptedLLM(Calls((Call("steady", {}),))))
    result = await agent.run(agent_call(request="依頼", max_steps=1))
    assert result.status == "max_steps"
    assert result.errors == (ErrorNote("budget", "used all 1 LLM steps"),)
