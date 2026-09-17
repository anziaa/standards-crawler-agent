from __future__ import annotations

import os
import random
import re
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import SearchResult
from .runtime import acquire_playwright, chromium_launch_args, headless_from_env, release_playwright


def normalize_filename(name: str) -> tuple[str, list[str]]:
    value = name.strip().replace("　", " ")
    value = re.sub(r"\.(pdf|docx?|xlsx?|zip)$", "", value, flags=re.I)
    value = re.sub(r"[\s_—–-]+", " ", value)
    value = re.sub(r"[（(]\s*(最终版|最新版|扫描件|附件)\s*[）)]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    tokens = [t for t in re.split(r"[\s,，;；/]+", value) if t]
    return value, tokens


DOC_TYPE_SUFFIXES = (
    "技术要求",
    "技术条件",
    "技术规程",
    "技术规范",
    "技术标准",
    "规范",
    "规程",
    "标准",
    "规定",
    "办法",
    "通知",
    "细则",
    "导则",
    "指南",
    "规则",
)


def core_name(name: str) -> str:
    """去掉文种词 / 拆掉“印发通知”外壳，得到用于“纠错检索”的核心名称。

    两种形态分别处理：

    1. **编制依据表的笔误**：把《高层民用建筑钢结构技术规程》写成“技术规范”。
       只按原名做精确短语检索会完全找不到官网，因此额外用去掉文种词的核心名称检索一次
       ——去掉“规范”之后，“规程 / 规范 / 标准”都能匹配上同一份文件。
    2. **“关于印发《X》的通知”**：真正的文件是书名号里的 X。
       旧实现只会砍掉结尾的“通知”，得到「关于印发《…》的」这种词——**实测返回 0 条**
       （而完整名称返回 10 条相关结果），等于白白浪费 3 个检索词里的 1 个。
    """
    value = normalize_filename(name)[0]
    inner = re.search(r"[《<]([^》>]+)[》>]", value)
    if inner:
        # 书名号里的就是正式名称，不再按文种词截断：它自带“实施细则/办法”这类词，
        # 砍掉反而变成“…安全管理实施”这种不完整的短语。
        return inner.group(1).strip()
    for suffix in DOC_TYPE_SUFFIXES:
        if value.endswith(suffix) and len(value) - len(suffix) >= 6:
            return value[: -len(suffix)]
    return ""


def build_queries(name: str, max_queries: int = 4) -> list[str]:
    """按信息量排序检索词：精确短语 → **裸词** → PDF 直链 → 核心名（纠错）→ 现行状态。

    **2026-09-16 调整（第 2 条：核心名 → 裸词）**。依据是一次四路对照实验
    （`temp/probe_bing_submit.py`，56 次读取）：**同一条名称的"带引号"与"裸词"两种形态，
    可以一条返回头名词兜底页、另一条拿到真文件**，而且这种差异在同一分钟内就能出现：

        「建设工程质量检测管理条例」带引号 → "中国建设银行-网上银行" 兜底页（最长命中 2 字）
                                    裸词   → 住建部令第57号 + 《建设工程质量检测管理办法》，政府站 9 条
        「济南市建筑施工高处作业吊篮安全管理暂行规定」带引号 → 百度百科兜底页
                                    裸词   → 带文号「济建质安监字[2012]4号」的目标文件

    这与第 40 节的结论一致（"兜底与否"是按**这一条查询这一次**决定的），所以第一轮发出的
    3 条检索词应当**形态互不相同**（精确短语 / 裸词 / 限定 filetype），而不是三条都是带引号的
    近似形态——多发一种独立形态就多一次命中机会，且**不增加请求次数**（同样是 3 条）。

    原先占第 2 位的是"核心名（截掉文种词的带引号形态）"，它与第 1 条只差一个后缀、
    形态重复；裸形态的核心名仍由 `build_retry_queries` 在第 2 轮使用，没有丢失。

    风险提示：裸词更松，可能带进"标题像但不是同一份文件"的页面。相关性闸门
    （`relevance_report`）、页面自述身份核对（`declared_identity`）与内容校验仍然逐层拦，
    但这条改动确实把更多判断交给了后面几层。

    旧版实测记录（保留）：`"…的通知"` 10 条 / 4 条官方站 ✅；`"…的通知" filetype:pdf` ✅；
    `"…的通知" 最新 现行` 无匹配兜底页 ❌；`"关于印发《…》的"` 0 条 ❌。
    """
    normalized, tokens = normalize_filename(name)
    core = core_name(name)
    queries = [
        f'"{name.strip()}"',
        # 裸词：与上一条**形态不同**，命中窗口独立（见上面的实验）。
        normalized,
        f'"{normalized}" filetype:pdf',
    ]
    if core and core != normalized.strip('"'):
        queries.append(f'"{core}"')
    queries.append(f'"{normalized}" 最新 现行')
    if len(tokens) > 1:
        queries.append(" ".join(f'"{token}"' for token in tokens[:8]))
    return list(dict.fromkeys(queries))[:max_queries]


# 引号形态的对调：带引号 ↔ 裸词。**只动引号，不动限定词**——
# `"X" filetype:pdf` 对调后是 `X filetype:pdf`，`filetype:` 这类限定必须留着。
_PHRASE_PATTERN = re.compile(r'"([^"]*)"')


def toggle_quote_form(query: str) -> str:
    """把检索词的引号形态对调一次：有引号就去掉，没引号就整串加引号。

    为什么要有这个动作：兜底页是"这一条查询这一次"的结论（第 40 节），而实测同一条名称的
    两种形态可以一条坏一条好（见 `build_queries` 的说明）。所以在拿到兜底页之后，
    值得换一种形态**再问一次**——注意这不是"重读同一页"，重读同一 URL 已被证明无效。
    """
    value = (query or "").strip()
    if not value:
        return value
    if '"' in value:
        # 去掉所有引号，保留 filetype:pdf / 最新 现行 这类限定词。
        return re.sub(r"\s+", " ", _PHRASE_PATTERN.sub(lambda match: match.group(1), value)).strip()
    return f'"{value}"'


def build_retry_queries(name: str, max_queries: int = 3) -> list[str]:
    """候选池为 0 时用的“更简单”检索词：去掉精确短语引号，改用核心名称。

    实测长中文标题做精确短语检索时，搜索引擎找不到精确匹配会退化成按头名词匹配
    （查“混凝土结构施工质量验收规范”返回百度百科“混凝土”词条），或者直接返回 0 条。
    不带引号的普通查询更接近人工检索，也更容易命中官网页面。
    """
    normalized, tokens = normalize_filename(name)
    queries: list[str] = []
    core = core_name(name)
    if core:
        queries.append(core)
    queries.append(normalized)
    if len(tokens) > 1:
        queries.append(" ".join(tokens[:6]))
    return list(dict.fromkeys(item for item in queries if item))[:max_queries]


class SearchProvider:
    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        raise NotImplementedError

    def close(self) -> None:
        """Release search resources. Safe to call when nothing was started."""


class SerpApiSearch(SearchProvider):
    """Optional provider. Keeps search behind a small interface for other APIs."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY")
        if not self.api_key:
            raise ValueError("SERPAPI_API_KEY is not configured")

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        response = httpx.get(
            "https://serpapi.com/search.json",
            params={"engine": "google", "q": query, "api_key": self.api_key, "num": limit},
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json().get("organic_results", [])
        return [
            SearchResult(
                title=row.get("title", ""),
                url=row.get("link", ""),
                snippet=row.get("snippet", ""),
                domain=urlparse(row.get("link", "")).netloc,
            )
            for row in rows[:limit]
            if row.get("link")
        ]


def page_responsive(page, timeout_ms: int = 3_000) -> bool:
    """用一次有超时的协议往返，试探页面还能不能响应。

    page.content() / page.title() / page.close() 这类调用都没有超时参数：
    页面渲染进程一旦卡住，它们会把整个任务挂到进程被杀（实测批量里出现过
    600 秒无输出、0 token 的挂起）。调用前先用这个探针确认页面还活着。
    """
    try:
        page.wait_for_function("() => true", timeout=timeout_ms)
        return True
    except Exception:
        return False


# 两次检索之间的间隔。批处理里检索是**串行连发**的（实测 120 次 / 131 秒），
# 人工检索不会这样。加间隔的动机是"降低被引擎当成机器人而降级的概率"，
# 但必须说清楚把握到什么程度：**"短时间连发导致兜底页"这个机制没有被证实**
# （同一秒内两条查询可以一条正常一条兜底，串行 jobs=1 也照样出现兜底页），
# 所以这三个数字是**礼貌间隔**，不是已证实的修复。真正的兜底页是"某个检索词
# 在某个时间窗里没匹配"，只能靠换入口和换时间（见 temp/run_two_pass.py 的两遍跑法）。
DEFAULT_MIN_QUERY_INTERVAL = 3.0
DEFAULT_QUERY_JITTER = 2.0
DEFAULT_FALLBACK_BACKOFF = 8.0


def _env_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def query_gap(
    now: float,
    last_at: float | None,
    base: float,
    jitter: float,
    rand: Callable[[], float],
    extra: float = 0.0,
) -> float:
    """上一次检索至今还没等够的话，还需要等多少秒。

    随机抖动（`base + jitter*rand()`）让间隔不是一个固定周期——固定周期本身就是
    机器人特征，而且实测里"整点整秒连发"与兜底页同时出现过，虽然不能据此断定因果，
    也没有理由用一个更容易被识别成自动化的节奏。
    `extra` 用在刚被判"兜底/无结果"之后：短时间内连发对同一条查询没有收益（结果确定），
    与其马上发下一条，不如多等一会儿。
    """
    if last_at is None:
        return 0.0
    wanted = base + jitter * rand() + extra
    return max(0.0, wanted - (now - last_at))


class BrowserSearch:
    """通过 Playwright 直接读取搜索引擎页面，不需要 SerpAPI Key。

    浏览器实例在多次查询之间复用：搜索结果页结构简单，冷启动浏览器
    比加载页面本身还慢，逐查询重启是明显的浪费。
    """

    ENGINES: dict[str, dict[str, str]] = {
        "bing": {
            # 直接打 cn.bing.com：www.bing.com 会在 domcontentloaded 之后跳到
            # cn.bing.com/...&rdr=1，跳转未完成时读到的是过渡页（实测解析到的是
            # 百度百科/知乎之类的推荐卡片，与被查文件毫无关系）。
            "url": "https://cn.bing.com/search?q={query}",
            # 兜底时改走"搜索框提交"这条路（见 BrowserSearch._submit_via_box）：
            # 实测两条路会给出不同的结果集（一条兜底页、另一条真结果都出现过）。
            "home": "https://cn.bing.com/",
            "input": "#sb_form_q",
            "container": "li.b_algo",
            "title": "h2 a",
            "snippet": ".b_caption p, .b_algoSlug, p",
        },
        "baidu": {
            "url": "https://www.baidu.com/s?wd={query}",
            "home": "https://www.baidu.com/",
            "input": "#kw",
            "container": ".result, .c-container",
            "title": "h3 a",
            "snippet": ".c-abstract, [class*=content-right], span",
        },
        "google": {
            "url": "https://www.google.com/search?q={query}",
            "home": "https://www.google.com/",
            "input": "textarea[name=q]",
            "container": "div.MjjYud",
            "title": "a:has(h3)",
            "snippet": "div.VwiC3b, div.IsZvec",
        },
    }

    def __init__(
        self,
        engine: str | None = None,
        headless: bool | None = None,
        timeout_ms: int = 30_000,
        render_timeout_ms: int = 3_000,
        read_attempts: int = 3,
        read_backoff: float = 0.7,
        min_query_interval: float | None = None,
        query_jitter: float | None = None,
        fallback_backoff: float | None = None,
        rand: Callable[[], float] | None = None,
    ) -> None:
        self.engine = (engine or os.getenv("SEARCH_ENGINE", "bing")).lower()
        if self.engine not in self.ENGINES:
            raise ValueError(f"unsupported browser search engine: {self.engine}")
        # None = 跟随 BROWSER_HEADLESS（见 runtime.headless_from_env），显式传参优先。
        self.headless = headless_from_env() if headless is None else headless
        self.timeout_ms = timeout_ms
        # 等结果容器出现/等地址稳定的有界上限（都不能无限等，本项目有挂起史）。
        self.render_timeout_ms = render_timeout_ms
        # 读不到可用结果时最多读几次；每次之间换干净页面并退避。
        self.read_attempts = read_attempts
        self.read_backoff = read_backoff
        # 检索节奏（见 query_gap / _pause_before_query）：全批任务里检索是串行连续发出去的，
        # 人工检索不会这样。三个值都可以用环境变量调，便于对照实验。
        env = os.getenv
        self.min_query_interval = _env_float(env("SEARCH_MIN_QUERY_INTERVAL"), DEFAULT_MIN_QUERY_INTERVAL) if min_query_interval is None else min_query_interval
        self.query_jitter = _env_float(env("SEARCH_QUERY_JITTER"), DEFAULT_QUERY_JITTER) if query_jitter is None else query_jitter
        self.fallback_backoff = _env_float(env("SEARCH_FALLBACK_BACKOFF"), DEFAULT_FALLBACK_BACKOFF) if fallback_backoff is None else fallback_backoff
        self._rand = rand or random.random
        self._last_query_at: float | None = None
        self._last_verdict: str = ""
        # 最近一次读取的诊断信息（尝试次数、每次条数、判定），供状态图区分
        # "引擎给了降级页（可重跑）" 与 "确实没有结果"。
        self.last_read: dict[str, Any] = {}
        self._playwright = None
        self._browser = None
        self._page = None

    def _ensure_page(self):
        if self._browser is not None and not self._browser.is_connected():
            self.close()
        if self._page is None:
            self._playwright = acquire_playwright()
            self._browser = self._playwright.chromium.launch(
                headless=self.headless, args=chromium_launch_args(self.headless)
            )
            self._page = self._browser.new_page()
        return self._page

    def close(self) -> None:
        page, self._page = self._page, None
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        for closer in (
            lambda: page and page.close(),
            lambda: browser and browser.close(),
        ):
            try:
                closer()
            except Exception:
                pass
        if playwright is not None:
            release_playwright()

    def __enter__(self) -> BrowserSearch:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _parse(self, html: str, limit: int) -> list[SearchResult]:
        config = self.ENGINES[self.engine]
        soup = BeautifulSoup(html, "lxml")
        rows: list[SearchResult] = []
        seen: set[str] = set()

        def push(title: str, href: str, snippet: str = "") -> None:
            title = " ".join(title.split())
            href = (href or "").strip()
            if not href or not title or href.startswith("#") or href in seen:
                return
            seen.add(href)
            rows.append(
                SearchResult(
                    title=title,
                    url=href,
                    snippet=" ".join(snippet.split())[:400],
                    domain=urlparse(href).netloc,
                )
            )

        for block in soup.select(config["container"]):
            anchor = block.select_one(config["title"])
            if anchor is None:
                continue
            snippet_node = block.select_one(config["snippet"])
            push(
                anchor.get_text(" ", strip=True),
                anchor.get("href", ""),
                snippet_node.get_text(" ", strip=True) if snippet_node else "",
            )
            if len(rows) >= limit:
                return rows
        if rows:
            return rows
        # 结果容器一个都没匹配上，说明这一页没有可用结果（精确短语无命中，或引擎改了结构）。
        # 此时绝不能退回“抓取页面任意 h2 链接”的做法——那会把百度首页、词典站之类的
        # 无关链接当成搜索结果，进而让状态图去观察垃圾页面。
        return []

    def _load(self, page, url: str, container: str) -> None:
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        # 等结果容器出现，而不是猜一个固定 sleep：跳转/渲染没完成时读到的页面
        # 不是本次查询的结果页。等待有上限，拿不到就照常解析（解析不到会触发重读）。
        try:
            page.wait_for_selector(container, timeout=self.render_timeout_ms)
        except Exception:
            pass
        self._wait_url_settled(page)
        page.wait_for_timeout(200)

    def _wait_url_settled(self, page, samples: int = 4, interval_ms: int = 250) -> None:
        """等地址连续两次采样一致——避免在 www→cn 这类跳转中途读页面。"""
        previous = None
        for _ in range(samples):
            current = page.url
            if current == previous:
                return
            previous = current
            page.wait_for_timeout(interval_ms)

    def _reset_page(self):
        """换一个干净页面。

        卡死的页面只丢掉引用、不去 close()：关一个卡死的页面同样可能挂住，
        留给浏览器整体退出时回收更安全。
        """
        self._page = None
        return self._ensure_page()

    def _judge(self, rows: list[SearchResult], query: str) -> tuple[list[SearchResult], str, dict[str, Any]]:
        """对这一批结果做判定，返回 (结果, 判定, 判据证据)。

        判定有四种：
          ok       —— 结果里至少有一条与查询相关，且不是头名词兜底页；
          empty    —— 一条结果都没解析到（过渡页/空壳页/无结果页）→ **值得换干净页面重读**；
          no_match —— 解析到了结果，但没有一条与查询相关 → **重读无效**；
          fallback —— 解析到了结果、字符重合度也过了门槛，但**没有任何连续片段命中**
                      （头名词兜底页：查"山东省…专家论证办法"返回省人民政府首页）→ 重读无效。

        no_match / fallback 都是"搜索引擎对这条查询没有匹配"，不等于"这个文件不存在"：
        实测同一条查询在 9 分钟内 12/12 稳定复现同一批头名词结果，
        所以调用方应当**换入口**（编号+平台、机构官网站内检索），而不是反复重读。

        **判定只有一个 owner**：goto 拼 URL 与搜索框提交两条路共用这一个函数，
        否则两条路的判定会慢慢分叉。
        """
        if not rows:
            return [], "empty", {}
        report = relevance_report(query, rows)
        if not report["relevant"]:
            return rows, "no_match", {"relevance": report}
        signature = fallback_signature(query, rows)
        if signature["fallback"]:
            return rows, "fallback", {"fallback": signature, "relevance": report}
        return rows, "ok", {}

    def _read_once(self, page, url: str, query: str, limit: int) -> tuple[list[SearchResult], str, dict[str, Any]]:
        """走"自己拼 URL 直达"这条路读一次。"""
        container = self.ENGINES[self.engine]["container"]
        self._load(page, url, container)
        if not page_responsive(page):
            # 页面卡死就换一个干净页面重来一次，别让 content() 无限等下去。
            page = self._reset_page()
            self._load(page, url, container)
        return self._judge(self._parse_page(page, limit), query)

    def _submit_via_box(self, page, text: str) -> None:
        """回到引擎首页，在搜索框里输入并回车（真实表单提交）。

        **这不是"重读"，是换一种"问法"**：实测（temp/probe_bing_submit.py，56 次读取）
        同一条查询走"拼 URL 直达"与"搜索框提交"两条路会拿到**不同的结果集**，
        且出现过一条是头名词兜底页、另一条是真结果的情形；两种引号形态也一样。
        所以兜底之后值得换路+换形态再问一次——重读同一个 URL 才是已被证明无效的那种。
        """
        engine = self.ENGINES[self.engine]
        page.goto(engine["home"], wait_until="domcontentloaded", timeout=self.timeout_ms)
        page.wait_for_selector(engine["input"], timeout=self.render_timeout_ms)
        page.fill(engine["input"], "")
        # 逐字输入而不是 fill：fill 不触发键盘事件，部分前端逻辑（含"上一次查询"记忆）不会走。
        page.type(engine["input"], text, delay=15)
        page.press(engine["input"], "Enter")
        try:
            page.wait_for_selector(engine["container"], timeout=self.render_timeout_ms + 5_000)
        except Exception:
            pass
        self._wait_url_settled(page)
        page.wait_for_timeout(400)

    def _parse_page(self, page, limit: int) -> list[SearchResult]:
        for attempt in range(3):
            try:
                return self._parse(page.content(), limit)
            except Exception:
                if attempt == 2:
                    return []
                page.wait_for_timeout(600)
        return []

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """检索一次。返回可用结果；读不到就返回空列表，并把原因写进 last_read。

        三种失败要分开对待：
          empty    —— 过渡页/空壳页 → 换干净页面重读（退避），最多 read_attempts 次；
          no_match / fallback —— 搜索引擎对这条查询没有匹配（头名词兜底页）→ 立刻停，
                     交给上层换入口。重读是白费的：实测同一条查询在 9 分钟内稳定返回同一批无关结果。
        """
        url = self.ENGINES[self.engine]["url"].format(query=quote_plus(query))
        attempts: list[dict[str, Any]] = []
        discarded: list[SearchResult] = []
        evidence: dict[str, Any] = {}
        # 两次检索之间留间隔：见 query_gap 的说明（兜底页与"短时间连发"相关，但机制不可观测，
        # 所以这里是**降低触发概率**的礼貌间隔，不是已证实的修复）。
        pause = self._pause_before_query()
        page = self._ensure_page()
        for attempt in range(1, max(1, self.read_attempts) + 1):
            if attempt > 1:
                time.sleep(self.read_backoff * (attempt - 1))
                page = self._reset_page()
            try:
                rows, verdict, evidence = self._read_once(page, url, query, limit)
            except Exception as exc:  # 导航失败等：记下来继续重读
                rows, verdict, evidence = [], f"error:{type(exc).__name__}", {}
            attempts.append(
                {
                    "attempt": attempt,
                    "path": "gotourl",
                    "sent": url,
                    "rows": len(rows),
                    "verdict": verdict,
                    "url": getattr(page, "url", ""),
                }
            )
            if verdict == "ok":
                self.last_read = {"query": query, "engine": self.engine, "verdict": "ok", "attempts": attempts, "waited_s": pause}
                self._last_verdict = "ok"
                return rows
            if verdict in ("no_match", "fallback"):
                # 头名词兜底页是确定性的：实测 9 分钟内 12/12 稳定复现同一批结果，
                # 同秒内重读 3 次也全是同一页（76 行 × 3 次 = 0 收益）。所以**不重读同一个 URL**，
                # 改成换"提交方式 + 引号形态"再问一次（见下面的 rescue，以及 _submit_via_box）。
                discarded = rows
                break
        final = attempts[-1]["verdict"] if attempts else "empty"
        # 兜底页/无匹配 → 换路再问一次：搜索框提交 + 引号形态对调。
        # **这不是重读**：同样的 URL 重读已被证明 0 收益，而"换路+换形态"是换了一条查询，
        # 实测两条路会给出不同结果集（甚至一条兜底页、一条真结果）。
        # 代价：每次兜底多一次首页导航 + 一次检索（一条任务最多 3 条检索词 → 最多 3 次）。
        if final in ("no_match", "fallback"):
            alternate = toggle_quote_form(query)
            box_page = None
            try:
                box_page = self._reset_page()
                self._submit_via_box(box_page, alternate)
                rows, verdict, evidence = self._judge(self._parse_page(box_page, limit), query)
            except Exception as exc:
                rows, verdict, evidence = [], f"error:{type(exc).__name__}", {}
            attempts.append(
                {
                    "attempt": len(attempts) + 1,
                    "path": "searchbox",
                    "sent": alternate,
                    "rows": len(rows),
                    "verdict": verdict,
                    "url": getattr(box_page, "url", ""),
                }
            )
            if verdict == "ok":
                self.last_read = {
                    "query": query,
                    "engine": self.engine,
                    "verdict": "ok",
                    "attempts": attempts,
                    "waited_s": pause,
                    "rescued_by": "searchbox",
                }
                self._last_verdict = "ok"
                return rows
            final = verdict
            if verdict in ("no_match", "fallback"):
                discarded = rows
            else:
                discarded = []
        self.last_read = {"query": query, "engine": self.engine, "verdict": final, "attempts": attempts, "waited_s": pause}
        self._last_verdict = final
        # 判无关/判兜底时这批结果**不会**进候选池；以前它们在这里被直接丢弃，事后无法核对
        # "丢掉的到底是不是垃圾"。现在原样留存（标题/URL/重合度/来源性质）并写进产物。
        # 只留 relevance 里那份带诊断字段的行，避免同一批结果出现两种结构。
        if final in ("no_match", "fallback") and discarded:
            self.last_read["relevance"] = evidence.get("relevance") or relevance_report(query, discarded)
            self.last_read["discarded"] = self.last_read["relevance"]["rows"]
        if final == "fallback" and evidence.get("fallback"):
            self.last_read["fallback"] = evidence["fallback"]
        return []

    def _pause_before_query(self) -> float:
        """按上一个判定的性质，决定这次查询前要等多久（秒），并真的等。"""
        now = time.monotonic()
        extra = self.fallback_backoff if self._last_verdict in ("fallback", "no_match", "empty") else 0.0
        pause = query_gap(now, self._last_query_at, self.min_query_interval, self.query_jitter, self._rand, extra)
        if pause > 0:
            time.sleep(pause)
        self._last_query_at = time.monotonic()
        return round(pause, 2)


SEARCH_NOISE_PUNCTUATION = "《》（）〈〉()、，,。；;：:·-—_/\"'“”‘’"

# 查询里的"通用词"：这些字在头名词兜底页里几乎必然出现（建筑/城市/工程/技术/标准…），
# 算重合度时必须先剔掉，否则短标题会被兜底页蒙混过关。
# 实测反例：「建筑铝型材」的垃圾结果里有「建筑」二字 → 2/5=40% ≥ 30% 门槛，
# 于是 ArchDaily、百度百科「建筑物」被判成"相关"；剔掉通用词后只剩「铝型材」，重合 0%。
GENERIC_QUERY_WORDS = (
    "建筑", "城市", "工程", "技术", "标准", "规范", "规程", "通知", "规定", "办法", "细则", "指南",
    "管理", "安全", "施工", "设计", "质量", "建设", "实施", "部分", "有关", "进一步", "加强",
    "最新", "现行", "文件", "要求", "评价", "检验", "验收",
)
# 权威站点特征：只要这批结果里有落在这里、且与查询有实质重合的，就认为读到了真结果。
AUTHORITY_DOMAIN_HINTS = (
    ".gov.cn",
    "samr.gov.cn",
    "sacinfo.org.cn",
    "sac.gov.cn",
    "csres.com",
    "cecs.org.cn",
    "mot.gov.cn",
    "mee.gov.cn",
    "mem.gov.cn",
    "npc.gov.cn",
)


def _char_signature(text: str) -> set[str]:
    return {char for char in text if not char.isspace() and char not in SEARCH_NOISE_PUNCTUATION}


def distinctive_signature(query: str) -> set[str]:
    """查询里"有辨识度"的字符：剔掉通用词与检索指令后再取字符集合。

    兜底页是按头名词召回的，剔掉通用词后重合度会骤降；若剔完剩不下字（整条查询
    都是通用词），退回完整字符集，避免把正常查询判死。
    """
    cleaned = query.replace('"', " ")
    for token in ("filetype:pdf", "site:", "最新", "现行"):
        cleaned = cleaned.replace(token, " ")
    for word in GENERIC_QUERY_WORDS:
        cleaned = cleaned.replace(word, " ")
    signature = _char_signature(cleaned)
    if len(signature) < 2:
        return _char_signature(query.replace('"', " "))
    return signature


def is_authority_domain(domain: str) -> bool:
    value = (domain or "").lower()
    return any(hint in value for hint in AUTHORITY_DOMAIN_HINTS)


def relevance_report(query: str, results: list[SearchResult], threshold: float = 0.3) -> dict[str, Any]:
    """判定"这批结果与查询是否相关"，**并把依据一并返回**，供落盘核对。

    为什么要返回依据：判无关时这批结果会被整批丢弃、不进候选池，而"丢掉的到底是什么"
    以前没有任何记录（`search()` 拿到 rows 之后直接扔了）。于是"闸门是不是误杀了一个
    标题不匹配、内容却是对的页面"这个问题只能靠猜。实测（2026-09-16）用户手工检索能命中
    `zjt.shandong.gov.cn/art/2022/11/25/art_293162_43735.html`，而那一页在 SERP 里的标题
    是被截断的（「山东省住房和城乡建设厅 行政规范性文件 山东省住房和城乡 ...」）——
    正是"字面判据可能误杀"的形态，所以先把证据留下来，再决定要不要动判据。

    判据顺序（缺一不可，且都不靠"调高阈值"这种钝器——项目有提高阈值误杀正确文件的先例）：
      1. 整批结果全部落在第三方内容站/文库/百科 → 直接判无关（兜底页的典型形态）；
      2. 有结果落在权威站、且与"去通用词后的查询"有实质重合 → 判相关（先放行，避免误杀）；
      3. 其余情况按"去通用词后的重合度"判定。

    实测依据（2026-09-15/16）：
      - 「建筑铝型材」→ ArchDaily / 百度百科「建筑物」/ 谷德设计网（全为内容站）；
      - 「海绵城市设计规程」→ 百度百科「海绵（多孔动物）」/ 海绵音乐；
      - 「DB37/T5285-2024 塔吊安全性能评估」→ D-Sub 37 针连接器、淘宝端子板；
      这些都被旧判据（原始 30% 字符重合）误判为"相关"。
      同时必须不误杀：「一般用途钢丝绳」命中 openstd/std.samr（权威站）要判相关。
    """
    from .ranking import is_low_trust_domain  # 延迟导入，避免模块级循环依赖

    if not results:
        return {"relevant": False, "rule": "没有结果", "signature_chars": 0, "rows": []}

    signature = distinctive_signature(query)

    def overlap(result: SearchResult) -> float:
        haystack = _char_signature(f"{result.title}{result.snippet}{result.url}")
        return len(signature & haystack) / len(signature) if signature else 1.0

    # 逐条记下判据看到的依据：事后核对"被丢弃的批次里有没有真页面"全靠这些数字。
    rows = [
        {
            "title": (result.title or "")[:80],
            "url": result.url,
            "domain": result.domain,
            "overlap": round(overlap(result), 3),
            "authority": is_authority_domain(result.domain),
            "low_trust": is_low_trust_domain(result.domain),
        }
        for result in results[:10]
    ]
    base: dict[str, Any] = {"signature_chars": len(signature), "rows": rows}

    if not signature:
        return {**base, "relevant": True, "rule": "查询去掉通用词后没有可用特征，直接放行"}
    if all(is_low_trust_domain(result.domain) for result in results):
        return {**base, "relevant": False, "rule": "整批结果都落在第三方内容站/文库/百科"}
    for index, result in enumerate(results, 1):
        if is_authority_domain(result.domain) and overlap(result) >= threshold:
            return {**base, "relevant": True, "rule": f"第 {index} 条是权威站且重合度达标"}
    for index, result in enumerate(results, 1):
        if overlap(result) >= threshold:
            return {**base, "relevant": True, "rule": f"第 {index} 条重合度达标"}
    best = max((row["overlap"] for row in rows), default=0.0)
    return {**base, "relevant": False, "rule": f"没有一条达到 {threshold:.0%} 门槛（最高 {best:.0%}）"}


def looks_relevant(query: str, results: list[SearchResult], threshold: float = 0.3) -> bool:
    """粗判搜索结果是否和查询有关；用于识别"无匹配兜底页"，避免垃圾进候选池或白烧 token。"""
    return bool(relevance_report(query, results, threshold)["relevant"])


# 兜底页判据：引擎对"精确短语"找不到匹配时，会退化成**按头名词召回**——返回一批
# 只看开头几个字就召回的结果。实测（2026-09-16，山东省那条）头名词可以是：
#     「工程」「关于」「省」「山东省」
# 于是这批结果里出现的是**省人民政府首页、百度百科「山东省」、山东旅游攻略**，
# 而查询里真正有辨识度的长串（「…安全专项施工方案编制审查与专家论证办法」）
# **一个字都不连续出现**。
#
# 判据：查询（去掉引号/检索指令后）**连续**出现在某条结果里的最长长度，是否 ≥ 门槛。
# 为什么现有的字符集合重合度（relevance_report）挡不住：那是个**无序集合**判据，
# 兜底页只要零散覆盖了查询里若干常用字就能过 30% 门槛。实测那条正是如此（重合约
# 0.4、判"相关"），于是省政务门户首页进了候选池，状态图去观察这些门户、再在站内
# 检索上反复试，整条任务烧掉几十次浏览器往返却注定失败（并最终"导出"了门户首页）。
#
# **不能用"去掉通用词后的片段"当判据**——第一版就是这么写的，回放 2541 条历史批次后
# 发现它把真结果判死了：「公路工程技术标准」去掉 工程/技术/标准 后只剩「公路」2 字，
# 而门槛 3 字，连命中的正主（「交通运输部关于发布《公路工程技术标准》的公告」）都过不了；
# 「住房和城乡建设部关于修改部分部门规章的决定」被剐成「住房和城乡」等碎片后同样过不了。
# 用**原始查询连续比对**就没有这个问题：真结果里查询本来就是连续出现的。
#
# **比较前两侧都掐掉空白**（与 `page_score` 相反，那里刻意保留空白）：引擎会给命中词
# 插空格（实测「住房 和城乡建设部」「山东 省」）。掐空白对判据是**保守**方向——只会让
# 更多批次通过、更少误杀；兜底页靠头名词也只能拼出 3~4 个连续字，仍然过不了门槛。
SEARCH_FILTER_TOKENS = ("filetype:pdf", "filetype:doc", "site:", "最新", "现行")
# 门槛：只要 6 个连续字命中就不判兜底。实测真命中的标题会被引擎截断
# （「山东省住房和城乡建设厅 行政规范性文件 山东省住房和城乡 ...」），也会被插空格，
# 卡得再严就会误杀真页面（本项目有提高阈值误杀正确文件的先例）。
FALLBACK_MIN_RUN = 6
# 查询本身短于这个长度时没有判据可言（3 个字的查询，兜底页头名词也能命中 2 个字）。
FALLBACK_MIN_QUERY_CHARS = 4


def distill_query(query: str) -> str:
    """查询里"字面可比"的部分：去掉引号与检索指令，再掐掉空白与标点。

    与 `distinctive_signature`（字符集合版，用于 relevance_report）的区别是这里**保留字序**：
    判据问的是"查询有没有连续出现"，集合版本问的是"覆盖了哪些字"。
    """
    cleaned = query or ""
    for char in ('"', "“", "”", "‘", "’", "'"):
        cleaned = cleaned.replace(char, "")
    for token in SEARCH_FILTER_TOKENS:
        cleaned = cleaned.replace(token, "")
    return "".join(char for char in cleaned if not char.isspace() and char not in SEARCH_NOISE_PUNCTUATION)


def _squash(text: str) -> str:
    """掐掉空白（保留标点与大小写）：用来抵消引擎给命中词插的空格。"""
    return "".join(char for char in (text or "") if not char.isspace())


def url_depth(url: str) -> int:
    """URL 路径层级（`http://a.gov.cn/` → 0，`/gwyzzjg/` → 1，`/zhengce/202508/x.htm` → 3）。

    用来区分"门户首页/栏目落地页"与"内容页"。实测（2026-09-16 回放 2541 条批次）：
    兜底页里**能用的东西只剩门户首页**（山东省人民政府首页、济南市政府首页、交通运输部首页、
    国务院机构栏目页），而会被判据误伤的那几条真原件都落在**深层内容页**上
    （openstd 标准详情页、市场监管总局政策页、中国政府网政策页）。
    """
    path = urlparse(url or "").path
    return len([part for part in path.split("/") if part])


def has_deep_authoritative_page(results: list[SearchResult]) -> bool:
    """这批结果里有没有"权威站上的深层内容页"。

    有就不判兜底——**判据只在一个方向上会出错**（把真批次判成兜底 = 丢掉一批真线索），
    所以留这个出口：兜底页给不出深层内容页，而真批次经常给得出。
    """
    from .ranking import is_authoritative_domain  # 延迟导入，避免模块级循环依赖

    for result in results:
        domain = result.domain or urlparse(result.url).netloc
        if is_authoritative_domain(domain) and url_depth(result.url) >= 2:
            return True
    return False


def fallback_signature(query: str, results: list[SearchResult], min_run: int = FALLBACK_MIN_RUN) -> dict[str, Any]:
    """判断这批结果是不是"按头名词凑的兜底页"，并返回判据看到的数字。

    `fallback=True` 时整批不该进候选池（重读也没有用，见 `_read_once`）。
    判据只在一个方向上可能出错——**把真批次判成兜底**（丢掉一批线索），所以取的都是
    保守取值：保留整条查询、掐空白、门槛只 6 个连续字，且批次里有权威站深层内容页时不判。
    """
    from .ranking import longest_common_run  # 延迟导入，避免模块级循环依赖

    needle = distill_query(query)
    base: dict[str, Any] = {"needle": needle, "needle_chars": len(needle)}
    if len(needle) < FALLBACK_MIN_QUERY_CHARS:
        # 查询本身太短（"XX办法"），头名词和真命中的字面本来就没区别，不判兜底：
        # 判错的代价是丢掉一整批真结果，而这种查询无从判断。
        return {**base, "fallback": False, "rule": "查询太短，没有可用判据", "required": 0,
                "longest_run": 0, "closest_title": ""}
    required = min(len(needle), min_run)

    best_run = 0
    best_title = ""
    for result in results:
        # 与 page_score 用同一段文本（标题+摘要+URL）：URL 里带标准号之类的 ASCII 线索
        # 也算命中，宁可漏判兜底页，也不要误杀真结果。
        run = longest_common_run(needle, _squash(f"{result.title}{result.snippet}{result.url}"))
        if run > best_run:
            best_run, best_title = run, (result.title or "")[:80]
    signature = {
        **base,
        "fallback": best_run < required,
        "required": required,
        "longest_run": best_run,
        "closest_title": best_title,
        "deep_authoritative": False,
    }
    if signature["fallback"] and has_deep_authoritative_page(results):
        # 有权威站深层内容页 → 这批不判兜底（宁可继续观察，也不要丢掉真线索）
        signature["fallback"] = False
        signature["deep_authoritative"] = True
        signature["rule"] = "字面命中不足，但批次里有权威站深层内容页，按可用批次处理"
    return signature


def deduplicate(results: Iterable[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    output: list[SearchResult] = []
    for item in results:
        if item.url and item.url not in seen:
            seen.add(item.url)
            output.append(item)
    return output
