"""Tool が子 process を起動する入口。記録・回収はせず、asyncio の起動をそのまま呼ぶ。

終了・期限切れ・取消しの回収は呼出元が ``core/builtin/base_tools.py`` の ``stop_process_group`` で行う。
"""

from __future__ import annotations

import asyncio
from typing import Any


async def spawn_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*args, **kwargs)


async def spawn_shell(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_shell(*args, **kwargs)
