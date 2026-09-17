from __future__ import annotations

import os
from typing import Any

_runtime: Any = None
_refs = 0

_HEADLESS_TRUE = {"1", "true", "yes", "on", "headless"}
_HEADLESS_FALSE = {"0", "false", "no", "off", "headed", "head"}


def headless_from_env(default: bool = True) -> bool:
    """由 `BROWSER_HEADLESS` 决定浏览器是否无头，未设置时用 `default`。

    **管线默认是无头（`default=True`）**：有头会真的弹窗、而且**会抢焦点**，
    批量跑起来就没法干别的活了（用户实测反馈）。要看过程时显式开：
    `BROWSER_HEADLESS=0`（或 false/no/off/headed）或 `--headed`。

    历史上默认值来回改过两次（无头 → 2026-09-16 改有头 → 2026-09-17 改回无头），
    教训是：**模式不该由"方便观察"来定默认，而该由"会不会打断使用者的正常工作"来定**；
    另外有头还会让 PDF 内联渲染（见 `browser._looks_like_blank_document_view`），
    观察用途完全可以按需临时开。

    **取值无法识别时直接报错**，不静默退回默认：否则 `BROWSER_HEADLESS=Flase` 这种笔误
    会让人以为在看有头窗口，其实又跑了一遍无头。
    """
    raw = os.getenv("BROWSER_HEADLESS")
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _HEADLESS_TRUE:
        return True
    if value in _HEADLESS_FALSE:
        return False
    raise ValueError(
        f"BROWSER_HEADLESS 取值无法识别：{raw!r}（可用 1/0、true/false、headless/headed）"
    )


def chromium_launch_args(headless: bool) -> list[str]:
    """Chromium 的启动参数。**有头时显式加 `--incognito`**。

    要说清楚它改变了什么、没改变什么：

    - **隔离性没有变化**：Playwright 的 `BrowserContext` 本来就是临时且隔离的
      （不落 user-data-dir、跑完即丢、上下文之间不共享 cookie/storage），等价于无痕。
      全仓库没有 `launch_persistent_context`，所以"每次都是干净会话"本来就成立；
      `--incognito` 只是把它**写在明面上**：窗口外观是无痕、看日志和调试时
      一眼能确认这次跑的是干净会话。
    - **它不解决兜底页**：README 第 40 节已用"带 cookies vs 全新隐身上下"做过对照，
      结果一致；兜底页是引擎按查询/时间窗口的行为，与会话无关。
    - **只在有头时加**：无头 shell 对 `--incognito` 的支持不保证，而那里也没有窗口要标明，
      所以不冒这个风险（`headless=True` 时返回空列表，行为与改动前完全一致）。
    """
    return [] if headless else ["--incognito"]


def acquire_playwright():
    """让搜索引擎与页面浏览器共用同一个 Playwright 实例。

    Playwright 的同步 API 会在当前线程里驱动一个 asyncio 事件循环，同一线程中
    启动第二个实例会直接抛 "using Playwright Sync API inside the asyncio loop"。
    因此这里用引用计数做全局共享：谁先需要谁负责启动，最后一个释放者负责停止。
    """
    global _runtime, _refs
    if _runtime is None:
        from playwright.sync_api import sync_playwright

        _runtime = sync_playwright().start()
    _refs += 1
    return _runtime


def release_playwright() -> None:
    global _runtime, _refs
    if _refs > 0:
        _refs -= 1
    if _refs == 0 and _runtime is not None:
        runtime, _runtime = _runtime, None
        try:
            runtime.stop()
        except Exception:
            pass
