"""AS29: LLM の同時呼出し上限は、層の違う client を含む全 task に共通で効く。"""

import asyncio

from tests.async_helpers import WAIT, settle, until
from trackeverything.core.abc.llm import FinalAnswer, LLMClient
from trackeverything.llm.throttle import ThrottledLLM


class _Gauge:
    def __init__(self):
        self.in_flight = 0
        self.peak = 0
        self.started = 0
        self.gates = [asyncio.Event() for _ in range(3)]


class _Inner(LLMClient):
    def __init__(self, gauge, index):
        self.gauge, self.index = gauge, index

    async def respond(self, messages, callables):
        gauge = self.gauge
        gauge.in_flight += 1
        gauge.started += 1
        gauge.peak = max(gauge.peak, gauge.in_flight)
        try:
            await gauge.gates[self.index].wait()
        finally:
            gauge.in_flight -= 1
        return FinalAnswer(str(self.index))


async def test_as29_llm_concurrency_limit():
    gauge = _Gauge()
    semaphore = asyncio.Semaphore(2)
    tiers = [ThrottledLLM(_Inner(gauge, index), semaphore) for index in range(3)]
    tasks = [asyncio.create_task(tier.respond([], [])) for tier in tiers]
    await until(lambda: gauge.started == 2)
    await settle()
    assert gauge.started == 2 and gauge.in_flight == 2
    gauge.gates[0].set()
    await until(lambda: gauge.started == 3)
    for gate in gauge.gates:
        gate.set()
    answers = await asyncio.wait_for(asyncio.gather(*tasks), WAIT)
    assert [answer.text for answer in answers] == ["0", "1", "2"]
    assert gauge.peak == 2
