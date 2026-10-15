"""設定から LLM の Provider と名前付きの client(max・high・low・flash・flash_low)を作る。全 client は同じ並行上限を共有する。"""

from __future__ import annotations

import asyncio
import logging
import os

import trackeverything.config as config
from trackeverything.core.abc.execution_log import LogRecorder
from trackeverything.core.abc.llm import LLMClient
from trackeverything.llm.fallback import FallbackLLM
from trackeverything.llm.openai import OpenAIChatLLM
from trackeverything.llm.throttle import ThrottledLLM
from trackeverything.llm.tracking import TrackedLLM

log = logging.getLogger(__name__)


def build_default_llm() -> LLMClient:
    """Construct the LLM from env, preferring Z.AI with a DeepSeek fallback.

    Priority:
      1. ``ZAI_API_KEY`` set  → primary = Z.AI. If ``DEEPSEEK_API_KEY`` is also
         set, wrap in :class:`FallbackLLM` (Z.AI → DeepSeek on transient
         failure); otherwise Z.AI alone.
      2. else ``DEEPSEEK_API_KEY`` set → DeepSeek alone.
      3. else ``OPENAI_API_KEY`` set → OpenAI alone (backward compatibility).
      4. else raise — no credential available.

    Credentials are environment-owned secrets. Provider endpoints, models,
    timeout, and concurrency are application policy defined in ``config.py``;
    Environment variables do not override them.

    DeepSeek V4 (``deepseek-v4-flash`` / ``deepseek-v4-pro``) is a hybrid
    reasoning model: thinking mode is controlled by the non-standard
    ``thinking: {"type": "enabled"}`` request field. We send it explicitly so
    CoT + function calling are both on regardless of the provider's default.

    The z.ai primary is GLM-5.3 at max reasoning effort (see
    :func:`build_zai_reasoning_llm`); ``build_llms`` names it ``max`` and every
    agent declaring ``llm = "max"`` (the default) shares the one chain.
    """
    timeout = config.LLM_TIMEOUT
    zai_key = os.environ.get("ZAI_API_KEY", "").strip()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

    def _deepseek() -> OpenAIChatLLM:
        return _deepseek_client(deepseek_key)

    if zai_key:
        # GLM-5.3 at max reasoning effort is the z.ai primary (the ``max``
        # client); agents declaring ``llm = "max"`` (the default) share it.
        # See build_llms for the ``high`` / ``low`` variants.
        primary = build_zai_reasoning_llm()
        if deepseek_key:
            log.info("LLM: z.ai (model=%s, reasoning_effort=max) with deepseek fallback", config.ZAI_REASONING_MODEL)
            return FallbackLLM(primary, _deepseek())
        log.info("LLM: z.ai (model=%s, reasoning_effort=max), no fallback configured", config.ZAI_REASONING_MODEL)
        return primary

    if deepseek_key:
        deepseek_model = config.DEEPSEEK_DEFAULT_MODEL
        log.info("LLM: deepseek (model=%s, thinking=enabled)", deepseek_model)
        return _deepseek()

    if openai_key:
        openai_model = config.OPENAI_DEFAULT_MODEL
        llm = OpenAIChatLLM(
            api_key=openai_key,
            base_url=config.OPENAI_BASE_URL,
            model=openai_model,
            timeout=timeout,
            provider="openai",
        )
        log.info("LLM: openai (model=%s)", openai_model)
        return llm

    raise RuntimeError("no LLM credential set; set ZAI_API_KEY (preferred), DEEPSEEK_API_KEY, or OPENAI_API_KEY")


def _deepseek_client(api_key: str) -> OpenAIChatLLM:
    """Build a DeepSeek client with thinking mode explicitly enabled."""
    return OpenAIChatLLM(
        api_key=api_key,
        base_url=config.DEEPSEEK_BASE_URL,
        model=config.DEEPSEEK_DEFAULT_MODEL,
        timeout=config.LLM_TIMEOUT,
        extra_body={"thinking": {"type": "enabled"}},
        provider="deepseek",
    )


def build_zai_reasoning_llm(effort: str = "max", model: str | None = None) -> OpenAIChatLLM:
    """Build a GLM-5.3 family client pinned to the requested reasoning effort.

    Same Coding Plan endpoint and ``ZAI_API_KEY`` as the primary LLM, so it
    bills the same subscription quota. GLM-5.3 and GLM-5.3-Flash are
    forced-reasoning models (``thinking.type: "disabled"`` is rejected) whose
    depth comes from the non-standard ``reasoning_effort`` field (``low`` /
    ``high`` / ``max``). ``model`` defaults to ``config.ZAI_REASONING_MODEL``.
    ``build_default_llm`` uses the ``max`` default as the z.ai primary;
    ``build_llms`` derives the ``high``, ``low``, ``flash`` and ``flash_low`` clients from this
    same constructor. Returned unwrapped — ``build_llms`` wraps the chain in
    ThrottledLLM/TrackedLLM. Constructing the client makes no network call.
    """
    api_key = os.environ.get("ZAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ZAI_API_KEY not set; the GLM-5.3 client reuses the z.ai primary's credential")
    return OpenAIChatLLM(
        api_key=api_key,
        base_url=config.ZAI_BASE_URL,
        model=model or config.ZAI_REASONING_MODEL,
        timeout=config.LLM_TIMEOUT,
        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": effort},
        provider="zai",
    )



def build_llms(logger: LogRecorder) -> dict[str, LLMClient]:
    """``max``(主 client)・``high``・``low``・``flash``・``flash_low`` の client を同じ recorder と Semaphore で包んで名前で返す。

    Agent は類の ``llm`` で名前を宣言し、構成時にその client を一つ受け取る。
    """
    # 全 tier の LLM client が共有する同時実行の上限(Host の loop の全 task にまたがる)。
    sem = asyncio.Semaphore(config.LLM_CONCURRENCY)

    def _wrapped(inner: LLMClient) -> LLMClient:
        """全 client 共通の包装: 同一 Semaphore の throttle + 実行記録。"""
        return TrackedLLM(ThrottledLLM(inner, sem), logger)

    llm = _wrapped(build_default_llm())

    def _zai_variant(effort: str, model: str = config.ZAI_REASONING_MODEL) -> LLMClient:
        """z.ai 主構成の間だけ model・effort 違いの同構成 client を作る。未構成なら主 client に退化。"""
        if not os.environ.get("ZAI_API_KEY", "").strip():
            return llm
        variant: LLMClient = build_zai_reasoning_llm(effort, model)
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if deepseek_key:
            variant = FallbackLLM(variant, _deepseek_client(deepseek_key))
        log.info("LLM: z.ai variant (model=%s, reasoning_effort=%s)", model, effort)
        return _wrapped(variant)

    return {
        "max": llm,
        "high": _zai_variant("high"),
        "low": _zai_variant("low"),
        "flash": _zai_variant("high", config.ZAI_FLASH_MODEL),
        "flash_low": _zai_variant("low", config.ZAI_FLASH_MODEL),
    }
