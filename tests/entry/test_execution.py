"""B05: 会話の実行記録。依頼は EventRequest で、dispatch は ``"dispatch"`` に登録した代役。"""

from unittest.mock import AsyncMock, Mock

from tests.async_helpers import make_runtime
from trackeverything.core.abc.agent import BaseAgent
from trackeverything.core.abc.call import AgentResult
from trackeverything.core.abc.submission import EventRequest


def _message(external_id, content):
    return EventRequest(provider="discord", external_id=external_id, payload={"content": content}, session_id="session-1")


async def test_b05_next_request_records_new_run_in_existing_session():
    agent = Mock(spec=BaseAgent)
    agent.run.return_value = AgentResult(status="ok", response="今回の回答")
    store = AsyncMock()
    runtime = make_runtime({"dispatch": agent}, store=store)
    await runtime.execute(_message("m-1", "最初の質問"), "1700000000000-0")
    result = await runtime.execute(_message("m-2", "続く質問"), "1700000000000-1")
    first, second = store.begin_run.await_args_list
    assert first.args[0] != second.args[0] and second.args[1] == "session-1"
    assert second.kwargs["data"] == {"event": {"provider": "discord", "external_id": "m-2", "payload": {"content": "続く質問"}}}
    assert result.response == "今回の回答"
    assert agent.run.call_args.args[0].ctx.session_id == "session-1"
    store.finish_run.assert_awaited_with(second.args[0], result)
    assert store.begin_run.await_count == store.finish_run.await_count == 2
