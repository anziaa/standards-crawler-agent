from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .content import (
    clickable_reading_entries,
    online_reading_only,
    page_without_fulltext,
    reading_entries,
)
from .models import DownloadPlan
from .runtime import acquire_playwright, chromium_launch_args, headless_from_env, release_playwright

FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".ofd")
FILE_SIGNAL_WORDS = ("下载", "附件", "download", "全文", "原文", "查看文件", "标准文本", "文件")
NAVIGATION_WORDS = (
    "登录",
    "注册",
    "首页",
    "返回",
    "上一页",
    "下一页",
    "网站地图",
    "联系我们",
    "版权",
    "关于我们",
    "无障碍",
    "english",
    "分享",
    "微信",
    "微博",
    "收藏",
    "设为首页",
    "手机版",
    "友情链接",
)
BOILERPLATE_TAGS = ("script", "style", "noscript", "nav", "header", "footer")
CONTENT_SELECTORS = (
    "main",
    "article",
    "#content",
    ".content",
    ".article",
    ".detail",
    ".main-content",
    "#main",
    ".TRS_Editor",
)


def _normalize(text: str, limit: int = 160) -> str:
    return " ".join(text.split())[:limit]


def _is_file_url(url: str) -> bool:
    return url.lower().split("?", 1)[0].rstrip("/").endswith(FILE_EXTENSIONS)


def _collect_links(soup: BeautifulSoup, page_url: str, page_domain: str, max_links: int) -> list[dict[str, Any]]:
    """按“是否可能是目标文件或附件”给链接打分，丢弃导航与页脚噪音。

    观察结果会整体进入大模型提示词，所以这里的目标是保留高信号链接，
    而不是把页面上所有 <a> 都交出去。
    """
    seen: set[str] = set()
    scored: list[tuple[int, dict[str, Any]]] = []
    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(page_url, href).split("#", 1)[0]
        if not absolute or absolute in seen:
            continue
        seen.add(absolute)
        text = _normalize(anchor.get_text(" ", strip=True))
        has_extension = _is_file_url(absolute)
        haystack = f"{text} {absolute}".lower()
        priority = 0
        if has_extension:
            priority += 5
        if any(word in haystack for word in FILE_SIGNAL_WORDS):
            priority += 2
        if urlparse(absolute).netloc.lower() == page_domain:
            priority += 1
        if not has_extension and any(word in text.lower() for word in NAVIGATION_WORDS):
            priority -= 4
        scored.append((priority, {"text": text, "url": absolute, "has_file_extension": has_extension}))
    # Python 的排序是稳定的：同分链接保持页面原有顺序。
    scored.sort(key=lambda item: item[0], reverse=True)
    useful = [item for item in scored if item[0] >= 0]
    if len(useful) < 5:
        useful = scored
    return [payload for _, payload in useful[:max_links]]


def _ensure_ok(response) -> None:
    """校验响应状态。

    Playwright 的 APIResponse 只有 ok / status / status_text，没有 httpx 的
    raise_for_status——这条分支此前一直是死代码，启用后才暴露出来。
    """
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status} {response.status_text}".strip())


CSS_SELECTOR_HINTS = re.compile(r"^[a-zA-Z*#.]|^\[")
ENGINE_PREFIX = re.compile(r"^[a-zA-Z-]+=")


def _locator(page, selector: str):
    """把模型给出的 selector 变成可用的 Locator。

    模型有时直接给出链接文本（例如 “2.《混凝土结构工程施工质量验收规范》
    （GB 50204-2015）.pdf”），直接交给 page.locator 会被当成 CSS 解析并报
    “Unexpected token .”。这里按是否带引擎前缀、是否像 CSS 来选路。
    """
    raw = (selector or "").strip()
    if not raw:
        return None
    if ENGINE_PREFIX.match(raw) or raw.startswith("//"):
        locator = page.locator(raw)
    elif CSS_SELECTOR_HINTS.match(raw) or any(token in raw for token in ("[", ">", ":has(")):
        locator = page.locator(raw)
    else:
        locator = page.locator(f"text={raw}")
    # 同一个 URL/文字在页面上常常出现多次，其中一部分可能位于折叠区或隐藏容器里。
    # 直接取 .first 会点到不可见那个（实测连点 4 次超时），所以逐个找第一个可见的。
    try:
        count = min(locator.count(), 10)
    except Exception:
        return locator.first
    for index in range(count):
        item = locator.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return locator.first


VISIBILITY_JS = """
() => {
  const items = [];
  const nodes = document.querySelectorAll(
    "a[href], button, input[type=button], input[type=submit], [role=button], [class*=btn], [onclick]"
  );
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const visible =
      rect.width > 0 && rect.height > 0 &&
      style.visibility !== "hidden" && style.display !== "none" &&
      style.opacity !== "0";
    const label = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim();
    items.push({
      text: label.slice(0, 80),
      visible: visible,
      tag: el.tagName.toLowerCase(),
      cls: String(el.className || "").slice(0, 80),
      onclick: el.hasAttribute("onclick"),
    });
  }
  return items;
}
"""


def drop_hidden_controls(
    summary: dict[str, Any], controls: list[dict[str, Any]], max_buttons: int = 30
) -> int:
    """按浏览器给出的可见性，剔掉观察结果里点不到的控件。

    HTML 解析看不出元素是否可见，隐藏的按钮/链接照样会被列出来，
    模型于是反复去点一个点不动的东西（实测连续两轮超时）。
    返回被剔除的数量。
    """
    def norm(value: str) -> str:
        return " ".join((value or "").split())[:80]

    hidden = {norm(item.get("text", "")) for item in controls if not item.get("visible")}
    shown = {norm(item.get("text", "")) for item in controls if item.get("visible")}
    hidden -= shown  # 同名控件里有可见的，就不能当成隐藏
    removed = 0
    dropped_names: list[str] = []
    # 只剔除隐藏的**按钮**：隐藏按钮点不动，留着只会让模型反复尝试。
    # 隐藏的**链接**必须保留——它们往往仍是可直链下载的 URL，不少站点把下载入口
    # 放在 display:none 的 <a> 里，靠 JS 触发。
    kept = []
    for item in summary.get("buttons", []):
        label = norm(item.get("text", ""))
        if label and label in hidden:
            removed += 1
            dropped_names.append(label)
            continue
        kept.append(item)
    summary["buttons"] = kept
    hidden_links = sum(
        1 for item in summary.get("links", []) if norm(item.get("text", "") or item.get("url", "")) in hidden
    )
    # 把 HTML 解析漏掉的“长得像按钮”的可点元素补进来（例如 <div class="sidebar-btn">
    # 形式的“查看文本”）。模型看不到可点项时，会从正文里找按钮文字去点隐藏的元素。
    existing = {norm(item.get("text", "")) for item in summary.get("buttons", [])}
    added = 0
    def button_like(entry: dict[str, Any]) -> bool:
        tag = str(entry.get("tag", "")).lower()
        cls = str(entry.get("cls", "")).lower()
        return tag in {"button", "input"} or "btn" in cls or "button" in cls or bool(entry.get("onclick"))

    for item in controls:
        if not item.get("visible") or not button_like(item):
            continue
        label = norm(item.get("text", ""))
        if not label or len(label) > 24 or label in existing or label in hidden:
            continue
        existing.add(label)
        summary.setdefault("buttons", []).append({"text": label, "tag": item.get("tag", "?")})
        added += 1
        if len(summary["buttons"]) >= max_buttons:
            break
    stats = summary.setdefault("stats", {})
    stats["buttons_hidden_filtered"] = removed
    if added:
        stats["buttons_added_from_dom"] = added
    if dropped_names:
        # 关键：正文文本里可能仍然留着这些按钮的文字（例如隐藏的“查看全文”），
        # 模型会据此去点击一个不可见的控件。这里把话说清楚。
        summary["controls_note"] = (
            f"本页有 {removed} 个按钮因为不可见已被剔除（{'、'.join(dropped_names[:5])}）；"
            "正文中即使出现这些文字，也不是可点击的控件，不要把它们作为点击目标。"
        )
    if hidden_links:
        stats["links_hidden"] = hidden_links
    return removed


SEARCH_INPUT_SELECTORS = (
    "input[type=search]",
    "input[name=q]",
    "input[name*=search i]",
    "input[id*=search i]",
    "input[class*=search i]",
    "input[placeholder*=搜索]",
    "input[placeholder*=关键字]",
    "input[placeholder*=关键词]",
    "input[placeholder*=检索]",
)
SEARCH_SUBMIT_SELECTORS = (
    "input[type=submit][class*=search i]",
    "button[class*=search i]",
    "form:has(input[name=q]) input[type=submit]",
    "form:has(input[name=q]) button",
    "button:has-text('搜索')",
    "a:has-text('搜索')",
)


def _first_visible(page, selectors: tuple[str, ...]):
    """在候选选择器里找出第一个可见的元素。站内搜索框形态太多，只能逐个试。"""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 3)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
    return None


# 站内检索结果页的“零结果”特征：检索词出现在页面上并不等于检索成功，
# “没有找到与「xx」相关的结果”这类页面里同样带着检索词。
EMPTY_RESULT_PATTERN = re.compile(
    r"(没有找到|未找到(?:相关|匹配|符合)?|找不到(?:相关|匹配)?|无(?:相关|匹配)(?:结果|内容)|"
    r"(?:共|总计|检索到|找到)\s*0\s*(?:条|个|项))"
)
# 提交检索后判定“结果已渲染”：链接数或正文长度发生明显变化。
SEARCH_STABLE_JS = """
() => {
  const links = document.querySelectorAll('a[href]').length;
  const text = document.body ? document.body.innerText.trim().length : 0;
  if (text < 200) return false;
  const sig = links + ':' + text;
  if (window.__dshSettleSig === sig) return true;
  window.__dshSettleSig = sig;
  return false;
}
"""
SEARCH_BASELINE_JS = """
() => ({
  links: document.querySelectorAll('a[href]').length,
  text: document.body ? document.body.innerText.length : 0,
})
"""
# 最后一种提交兜底：搜索框没有 <form> 时，把标记到的输入框所在表单直接提交。
SUBMIT_SEARCH_FORM_JS = """
() => {
  const el = document.querySelector('[data-dsh-search]');
  const form = el && el.closest('form');
  if (form) form.submit();
}
"""
SEARCH_SETTLE_JS = """
(baseline) => {
  const links = document.querySelectorAll('a[href]').length;
  const text = document.body ? document.body.innerText.length : 0;
  return links !== baseline.links || Math.abs(text - baseline.text) > 80;
}
"""


def _site_root(url: str) -> str | None:
    """站点根地址：站内检索的入口通常在本站首页，而不是内容详情页。"""
    parts = urlparse(url or "")
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/"


def _same_url(left: str | None, right: str | None) -> bool:
    def norm(value: str | None) -> str:
        return (value or "").split("#", 1)[0].rstrip("/")

    return bool(norm(left)) and norm(left) == norm(right)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _search_probes(query: str) -> list[str]:
    """检索词及其“去标点”形态：结果页回显检索词时标点常被改写。"""
    probes = [_squash(query)]
    core = _squash(re.sub(r"[（）()〔〕【】《》\[\]{}<>\"'“”‘’\-—_;；,，。.、:：/\\|]", "", query or ""))
    if core and core not in probes:
        probes.append(core)
    return [item for item in probes if len(item) >= 4]


def _close_quietly(page: Any) -> None:
    try:
        page.close()
    except Exception:
        pass


def newest_popup(context: Any, current: Any) -> Any | None:
    """挑出点击之后真正新打开的那个窗口；多余的弹窗顺手关掉。

    Playwright 的 `context.pages` 按打开顺序追加，所以最后一个是新开的。
    与当前页地址相同的窗口不算"新页面"——观察它只会原地打转。
    单独抽成函数是为了能用一个假 context 做单元测试（真浏览器跑不了离线测试）。
    """
    try:
        current_url = current.url
    except Exception:
        current_url = None
    candidates = []
    for item in list(context.pages):
        if item is current:
            continue
        try:
            if item.is_closed():
                continue
            if current_url and _same_url(item.url, current_url):
                continue
        except Exception:
            continue
        candidates.append(item)
    if not candidates:
        return None
    keeper = candidates[-1]
    for item in candidates[:-1]:
        _close_quietly(item)
    return keeper


def _is_download_start(exc: Exception) -> bool:
    """直链附件会让 Playwright 的 goto 抛“Download is starting”。"""
    return "download is starting" in str(exc).lower()


# 内联渲染的文档页里，阅读器自己那点 UI 文字的上限。超过它就不像是"空白阅读器"了。
DOCUMENT_VIEWER_TEXT_CHARS = 200


def _direct_file_observation(url: str) -> dict[str, Any]:
    """把"这个地址就是文件本身"这件事交给模型（是否下载由模型与校验节点决定）。"""
    return {
        "url": url,
        "title": "",
        "text": "",
        "links": [],
        "buttons": [],
        "embedded": [],
        "has_print_hint": False,
        "direct_file": {
            "url": url,
            "extension": Path(urlparse(url).path).suffix.lower(),
            "note": (
                "该地址是文件直链（浏览器把导航转成了下载，没有网页内容可观察）。"
                "如果它的文件名或来源与目标文件一致，用 download_direct 取它；"
                "内容与目标文件不符时会被校验拦下。"
            ),
        },
        "stats": {"html_chars": 0, "links_total": 0, "links_kept": 0, "text_total_chars": 0, "text_kept_chars": 0},
    }


def _looks_like_blank_document_view(url: str, summary: dict[str, Any]) -> bool:
    """文档直链被浏览器**内联渲染**成了空页面（有头 Chromium 的内置 PDF 阅读器）。

    为什么必须认出来：无头 Chromium 没有 PDF 阅读器，导航到 .pdf 会变成"开始下载"，
    于是走 `_is_download_start` 分支返回 `direct_file`，模型可以 `download_direct`；
    有头 Chromium 却把同一份 PDF 渲染成"没有正文、没有链接"的页面，
    在 `graph.observe` 里会被当成空白页而**跳过这个候选**。

    实测（2026-09-16，同一批 27 个任务）：无头跑 0 次这个形态、有头跑 13 次，
    而有头那 13 次里全是官方附件直链（jinan.gov.cn/attach/*.pdf、zfxxgk.ndrc.gov.cn/*.pdf …）。
    而 `direct_file` 这条路本身很成熟（历史 252 个任务用过、169 个成功），
    所以这里把结论统一成 `direct_file`：**浏览器模式不该改变结果**。

    只认"没有链接、也没有正文"的文档页——真正的文档发布页一定带链接，
    所以这条判据不会把可操作的信息丢掉。
    """
    if Path(urlparse(url or "").path).suffix.lower() not in FILE_EXTENSIONS:
        return False
    if summary.get("links"):
        return False
    stats = summary.get("stats") or {}
    text_chars = stats.get("text_total_chars")
    if text_chars is None:
        text_chars = len((summary.get("text") or "").strip())
    return int(text_chars or 0) < DOCUMENT_VIEWER_TEXT_CHARS


TAG_SEARCH_INPUT_JS = """
() => {
  const isVisible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const searchish = (el) => {
    let node = el;
    for (let depth = 0; depth < 4 && node; depth++, node = node.parentElement) {
      const blob = ((node.className || "") + " " + (node.id || "")).toLowerCase();
      if (blob.includes("search") || blob.includes("sousuo") || blob.includes("query")) return true;
    }
    return false;
  };
  document.querySelectorAll("[data-dsh-search]").forEach((el) => el.removeAttribute("data-dsh-search"));
  const skip = ["hidden", "submit", "button", "checkbox", "radio", "file", "image", "reset"];
  const candidates = Array.from(document.querySelectorAll("input"))
    .filter((el) => isVisible(el) && !skip.includes((el.type || "text").toLowerCase()));
  const marked = candidates.filter(searchish);
  const pool = marked.length ? marked : candidates;
  if (pool.length !== 1) return null;
  pool[0].setAttribute("data-dsh-search", "1");
  return "input[data-dsh-search]";
}
"""

TAG_SEARCH_SUBMIT_JS = """
() => {
  const anchor = document.querySelector("[data-dsh-search]") || document.querySelector("input[name=q]");
  if (!anchor) return null;
  const isVisible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  document.querySelectorAll("[data-dsh-submit]").forEach((el) => el.removeAttribute("data-dsh-submit"));
  let scope = anchor.closest("form") || anchor.parentElement;
  for (let depth = 0; depth < 3 && scope && scope !== document.body; depth++, scope = scope.parentElement) {
    const controls = Array.from(
      scope.querySelectorAll("button, input[type=submit], input[type=button], a, [role=button], [onclick]")
    ).filter((el) => isVisible(el) && el !== anchor);
    const labelled = controls.find((el) =>
      /搜索|检索|查询|search/i.test((el.innerText || "") + " " + (el.value || "") + " " + (el.className || ""))
    );
    if (labelled) {
      labelled.setAttribute("data-dsh-submit", "1");
      return "[data-dsh-submit]";
    }
    // 纯图标按钮：取输入框所在容器里的第一个可见可点元素
    const iconish = controls.find((el) => /btn|icon|submit|search/i.test(String(el.className || "")));
    if (iconish) {
      iconish.setAttribute("data-dsh-submit", "1");
      return "[data-dsh-submit]";
    }
  }
  return null;
}
"""


def _collect_buttons(soup: BeautifulSoup, max_buttons: int) -> list[dict[str, str]]:
    buttons: list[dict[str, str]] = []
    seen: set[str] = set()
    for element in soup.select("button, input[type=button], input[type=submit], [role=button]"):
        raw = element.get_text(" ", strip=True) or element.get("value", "") or element.get("aria-label", "") or ""
        text = _normalize(raw, 80)
        if not text or text in seen:
            continue
        seen.add(text)
        buttons.append({"text": text, "tag": element.name})
    return buttons[:max_buttons]


def refine_reading_entry(summary: dict[str, Any]) -> None:
    """按**最终可见的控件**复核 reading_entry，不可点的入口一律撤掉。

    单独抽成函数，是为了让"可见性过滤之后"这一步也能被离线测试直接调用
    （真浏览器跑不了离线测试，而这一步是这次超时的关键）。
    """
    entry = summary.get("reading_entry")
    if not entry:
        return
    labels = [item.get("text", "") for item in (summary.get("buttons") or [])]
    labels += [item.get("text", "") for item in (summary.get("links") or [])]
    original = list(entry.get("entries") or [])
    kept = clickable_reading_entries(original, labels)
    dropped = [item for item in original if item not in kept]
    entry["entries"] = kept
    if dropped:
        entry["note"] = (
            f"「{'、'.join(dropped)}」在页面上不是**可见可点**的控件，已从入口里撤掉——"
            "不要用它们做 selector。"
        )
    if not kept and not entry.get("statement"):
        # 既没有可点入口、页面也没声明"只能在线阅读"：不提入口，免得模型去点不存在的东西。
        summary.pop("reading_entry", None)


def summarize_html(
    html: str,
    page_url: str,
    page_title: str,
    max_links: int = 60,
    max_text_chars: int = 8_000,
    max_buttons: int = 30,
) -> dict[str, Any]:
    """把页面压缩成“决策够用”的观察结果，避免把整页噪音交给大模型。"""
    soup = BeautifulSoup(html, "lxml")
    links_total = len(soup.select("a[href]"))
    buttons_total = len(soup.select("button, input[type=button], input[type=submit], [role=button]"))
    for tag in soup(BOILERPLATE_TAGS):
        tag.decompose()

    page_domain = urlparse(page_url).netloc.lower()
    content_root = soup
    for selector in CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node is not None and len(node.get_text(" ", strip=True)) > 200:
            content_root = node
            break

    full_text = content_root.get_text(" ", strip=True)
    headings = [_normalize(heading.get_text(" ", strip=True), 80) for heading in content_root.select("h1, h2, h3")]
    links = _collect_links(soup, page_url, page_domain, max_links)
    buttons = _collect_buttons(soup, max_buttons)
    embedded = [
        {"tag": element.name, "url": urljoin(page_url, element.get("src") or element.get("data") or "")}
        for element in soup.select("iframe[src], embed[src], object[data]")
        if (element.get("src") or element.get("data"))
    ]
    # 页面自己声明"本站没有可读全文"（标准平台暂不提供在线阅读、需到馆阅读、
    # 文库付费外壳、扫码登录墙）。这类页面导出成 PDF 也只会得到网站页面，
    # 因此在观察阶段就告诉模型，而不是等导出之后再靠校验拦下。
    no_fulltext = page_without_fulltext(full_text)
    # 「仅提供在线阅读服务」是另一回事：正文能在线读到，只是没有下载入口，
    # 所以要引导模型点开阅读入口，而**不是**判 no_download。
    reading_only = online_reading_only(full_text)
    mentioned = reading_entries(full_text)
    # **只把真实存在的控件当入口。** 页面文本里出现"在线预览"不等于页面上有这个按钮
    # （它还会出现在 i18n 脚本、"仅提供在线阅读服务"那句话、隐藏模板里）。
    # 实测模型照着一个只存在于文本里的入口去点，白等 8 秒定位超时。
    control_labels = [item.get("text", "") for item in buttons] + [item.get("text", "") for item in links]
    entries = clickable_reading_entries(mentioned, control_labels)
    only_mentioned = [item for item in mentioned if item not in entries]
    guidance: dict[str, Any] = {}
    if no_fulltext:
        guidance["no_fulltext"] = {
            "evidence": no_fulltext,
            "instruction": (
                f"本页自己说明拿不到全文（{no_fulltext}）。导出本页只会得到网站页面而不是目标文件，"
                "因此不要选择 save_page_pdf / click_print；请选择 no_download，"
                "并在 reason 中写明该平台未提供全文。"
            ),
        }
    elif entries or reading_only:
        entry_text = "、".join(entries) if entries else "（无）"
        instructions = [
            f"本页不是正文页。页面上**真正可以点击**的正文入口：{entry_text}。",
            "请用 click_download 点击其中一个（selector 用它的文字），它会打开阅读页或直接下载文件；"
            "打开新窗口后系统会自动重新观察那一页，不要在旧页面上重复点同一个控件。",
            "本页本身没有正文，不要用 save_page_pdf。",
        ]
        if not entries:
            instructions.append(
                "注意：本页**找不到可点击的阅读入口**——页面只是提到过这些字样而已，"
                "不要拿它们当 selector 去点。"
            )
        if only_mentioned:
            instructions.append(
                f"页面文本里还出现过「{'、'.join(only_mentioned)}」，但它们在页面上不是可点击控件"
                "（只是提示文案或脚本文本），**不要**用它们做 selector。"
            )
        guidance["reading_entry"] = {
            "statement": reading_only,
            "entries": entries,
            "instruction": "".join(instructions),
        }
    return {
        "url": page_url,
        "title": page_title,
        "text": full_text[:max_text_chars],
        "headings": [heading for heading in headings if heading][:15],
        "links": links,
        "buttons": buttons,
        "embedded": embedded[:20],
        "has_print_hint": any(word in full_text for word in ("打印", "print", "Print")),
        "content_type": "pdf" if _is_file_url(page_url) else "html",
        "no_fulltext": guidance.get("no_fulltext"),
        "reading_entry": guidance.get("reading_entry"),
        "stats": {
            "html_chars": len(html),
            "links_total": links_total,
            "links_kept": len(links),
            "buttons_total": buttons_total,
            "text_total_chars": len(full_text),
            "text_kept_chars": min(len(full_text), max_text_chars),
        },
    }


class PageBrowser:
    """Browser abstraction. A fake implementation can be injected in tests."""

    def observe(self, url: str) -> dict[str, Any]:
        raise NotImplementedError

    def execute(self, url: str, plan: DownloadPlan, output_dir: str) -> str | None:
        raise NotImplementedError

    def close(self) -> None:
        """Release browser resources. Safe to call when nothing was started."""


class PlaywrightBrowser(PageBrowser):
    """复用一个 Chromium 实例处理所有观察与动作，避免每步冷启动浏览器。"""

    def __init__(
        self,
        headless: bool | None = None,
        timeout_ms: int = 30_000,
        max_links: int = 60,
        max_text_chars: int = 8_000,
        download_wait_ms: int = 8_000,
        click_timeout_ms: int = 8_000,
        search_popup_ms: int = 2_000,
    ) -> None:
        # None = 跟随 BROWSER_HEADLESS（见 runtime.headless_from_env），显式传参优先。
        self.headless = headless_from_env() if headless is None else headless
        self.timeout_ms = timeout_ms
        self.max_links = max_links
        self.max_text_chars = max_text_chars
        self.download_wait_ms = download_wait_ms
        # 定位并点击的等待时间要远短于页面加载：模型给出的选择器经常不存在，
        # 用默认 30 秒会让一次失败白等半分钟（实测单任务因此多花 300 秒）。
        self.click_timeout_ms = click_timeout_ms
        # 站内检索常是 target=_blank：等新窗口的时间要短，
        # 否则同页就出结果的站点每次提交都要白等。
        self.search_popup_ms = search_popup_ms
        self.last_url: str | None = None
        # 原地渲染出结果的检索页只存在于那个活着的页面里，重新打开 URL 是看不到结果的：
        # 这里把它连同渲染结果一起留着，交给状态图复用（见 _submit_site_search）。
        self.pending_observation: dict[str, Any] | None = None
        self._live_page: Any = None
        self._playwright = None
        self._browser = None
        self._context = None

    def _ensure_context(self):
        if self._browser is not None and not self._browser.is_connected():
            self.close()
        if self._browser is None:
            self._playwright = acquire_playwright()
            self._browser = self._playwright.chromium.launch(
                headless=self.headless, args=chromium_launch_args(self.headless)
            )
            self._context = self._browser.new_context(accept_downloads=True)
        return self._context

    def close(self) -> None:
        context, self._context = self._context, None
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        self.pending_observation = None
        self._live_page = None
        for closer in (
            lambda: context and context.close(),
            lambda: browser and browser.close(),
        ):
            try:
                closer()
            except Exception:
                pass
        if playwright is not None:
            release_playwright()

    def __enter__(self) -> PlaywrightBrowser:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _open(self, url: str):
        context = self._ensure_context()
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        # SPA 详情页在 domcontentloaded 之后才渲染正文。这里只等到“正文出现”就返回，
        # 不用 networkidle：持有长连接的页面会让它一直等到超时，反而比冷启动更慢。
        try:
            page.wait_for_function(
                "document.body && document.body.innerText.trim().length > 200",
                timeout=2_000,
            )
        except Exception:
            pass
        # 有些页面是异步渲染的（政府网站的站内检索结果页、SPA 列表页）：静态外壳很快
        # 就超过正文阈值，真正的结果稍后才注入。这里看链接数量是否还在增长，
        # 还在增长就继续等（最多再等 3 秒），稳定后立即返回。
        # 等页面稳定：连续两次采样链接数一致即视为渲染完成。
        # 用 wait_for_function（有超时参数）而不是 page.evaluate——后者没有超时，
        # 页面处于导航/忙碌状态时可能一直阻塞（实测批量里出现过 600 秒无任何输出）。
        try:
            page.wait_for_function(
                """() => {
                     const count = document.querySelectorAll('a[href]').length;
                     if (window.__dshLastLinkCount === count) return true;
                     window.__dshLastLinkCount = count;
                     return false;
                   }""",
                polling=200,
                timeout=3_000,
            )
        except Exception:
            pass
        return page

    def observe(self, url: str) -> dict[str, Any]:
        # 站内检索若是原地渲染结果（URL 不变），结果只存在于那个活着的页面里，
        # 靠重新打开 URL 复现不出来——执行器当时就把渲染结果存下来了，这里直接交出去。
        pending, self.pending_observation = self.pending_observation, None
        if pending and _same_url(pending.get("url"), url):
            return pending
        try:
            page = self._open(url)
        except Exception as exc:
            if not _is_download_start(exc):
                raise
            # 直链附件：Playwright 不允许导航，会把“开始下载”当异常抛出。
            # 这里只说明“该地址是文件直链”，由模型决定是否 download_direct；
            # 文件是否就是目标文件仍由 verify 节点按内容判定，不在这一步下结论。
            return _direct_file_observation(url)
        try:
            self.last_url = page.url
            summary = self._summarize(page)
        finally:
            page.close()
        # 有头 Chromium 会把 PDF **内联渲染**：导航成功、却没有正文也没有链接。
        # 同一份官方附件在无头下走的是上面的"下载"分支，有头下却会走到这里被判空白页——
        # 见 `_looks_like_blank_document_view` 的实测数据（0 次 vs 13 次）。统一成 direct_file。
        if _looks_like_blank_document_view(url, summary):
            return _direct_file_observation(url)
        return summary

    def _summarize(self, page) -> dict[str, Any]:
        """把当前页面压成给模型看的观察结果（observe 与站内检索共用）。"""
        html = page.content()
        try:
            controls = page.evaluate(VISIBILITY_JS)
        except Exception:
            controls = []
        summary = summarize_html(
            html,
            page.url,
            page.title(),
            max_links=self.max_links,
            max_text_chars=self.max_text_chars,
        )
        if controls:
            drop_hidden_controls(summary, controls)
            # 可见性过滤会剔掉一些控件、也会从 DOM 补进一些（实测标准平台的『查看文本』
            # 就是靠 DOM 补进来的），所以必须在这之后再收一次 reading_entry：
            # `summarize_html` 只能看到 HTML 里的控件，而"能不能点"最终由可见性结果决定。
            # controls 为空说明浏览器没给出可见性数据，此时**不做**这一步——
            # 否则会拿一份更弱的控件清单去删入口，把真实可点的入口误删掉。
            refine_reading_entry(summary)
        return summary

    def _handoff(self, page) -> None:
        """把一个刚打开/跳转过去的新页面**交给下一轮观察**，而不是关掉它。

        为什么不象以前那样关掉、只留地址：阅读器页/预览页常常靠 JS + token 渲染，
        重新打开同一个 URL 可能看到空白；页面里也可能已经带着上一跳的状态
        （表单、postMessage、会话）。做法与站内检索"原地渲染结果"的复用机制一致
        （`pending_observation` + `_live_page`），下一轮 `observe` 直接拿到摘要，
        后续动作也复用同一个活页面。

        实测：国家标准平台点『查看文本』弹出的那一页才是带下载按钮的正文页，
        以前把它关掉、又只把地址交给 reassess，等于这一页从没被看过。
        """
        try:
            page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            pass
        try:
            self.last_url = page.url
            self.pending_observation = self._summarize(page)
            self._live_page = page
        except Exception:
            # 摘要不出来也不关掉：地址已经记下，下一轮 observe 会重新打开。
            try:
                self.last_url = page.url
            except Exception:
                pass

    def _close_page(self, page) -> None:
        """原地渲染的检索结果页要留给后续动作复用，不能在这里关掉。"""
        if page is self._live_page:
            return
        try:
            page.close()
        except Exception:
            pass

    def _reuse_live_page(self, url: str):
        """原地渲染过结果的检索页只能复用：重新打开同一个 URL 只会看到空白。"""
        live = self._live_page
        if live is not None:
            try:
                current = None if live.is_closed() else live.url
            except Exception:
                current = None
            if current and _same_url(current, url):
                return live
            # 已经不在那个地址上的活页面留着只会泄漏，关掉。
            self._live_page = None
            try:
                live.close()
            except Exception:
                pass
        return self._open(url)

    def _search_field(self, page):
        field = _first_visible(page, SEARCH_INPUT_SELECTORS)
        if field is not None:
            return field
        # 兜底：用 JS 找出页面里唯一的（或位于 search 容器内的）可见文本框。
        # 各站点输入框命名千奇百怪（实测有 placeholder="请输入关键词" 的）。
        try:
            selector = page.evaluate(TAG_SEARCH_INPUT_JS)
        except Exception:
            selector = None
        return page.locator(selector).first if selector else None

    def _try_submit(self, page, kind: str, control):
        """执行一次检索提交，返回弹出的新窗口（已加载完成）或 None。"""
        if kind == "button":
            if control is None:
                control = _first_visible(page, SEARCH_SUBMIT_SELECTORS)
            if control is None:
                # 兜底：有些站点的搜索框没有 <form>，提交靠 JS 绑定在某个按钮/图标上
                # （实测上海住建委：target 属性挂在 div 上，回车无效）。
                try:
                    selector = page.evaluate(TAG_SEARCH_SUBMIT_JS)
                except Exception:
                    selector = None
                control = page.locator(selector).first if selector else None
            if control is None:
                return None
        try:
            with page.expect_popup(timeout=self.search_popup_ms) as popup_info:
                if kind == "enter":
                    control.press("Enter")
                elif kind == "button":
                    control.click(timeout=self.click_timeout_ms)
                else:
                    page.evaluate(SUBMIT_SEARCH_FORM_JS)
            child = popup_info.value
            child.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
            return child
        except Exception:
            return None

    def _settle(self, page, baseline: dict[str, int]) -> None:
        """提交后等结果渲染：链接数或正文长度一变就返回，最多等一小会儿。"""
        try:
            page.wait_for_function(SEARCH_SETTLE_JS, arg=baseline, polling=250, timeout=2_500)
        except Exception:
            pass

    def _wait_loaded(self, page, minimum_ms: int = 1_500) -> None:
        """等新窗口把结果渲染出来。

        实测 jncc 结果页：刚打开 233 字 / 13 个链接，4 秒后才是 2079 字 / 55 个链接。
        因此先给一段最短等待，再等“链接数与正文长度都不再变化”才认为渲染完成。
        """
        try:
            page.wait_for_timeout(minimum_ms)
        except Exception:
            pass
        try:
            page.wait_for_function(SEARCH_STABLE_JS, polling=400, timeout=6_000)
        except Exception:
            pass

    def _retry_judge(self, page, query: str, extra_ms: int = 2_500) -> bool:
        """再等一会儿重新判定。

        有些结果页先渲染外壳、稍后才按检索词筛选（实测 jncc 要 4 秒），
        首判看到的可能就是未筛选的列表——多等一次比误判失败便宜。
        """
        try:
            page.wait_for_timeout(extra_ms)
        except Exception:
            return False
        return self._judge_results(page, query, {"links": 0, "text": 0}, fresh=True, require_probe=True)

    def _query_in_url(self, url: str, query: str) -> bool:
        """结果页 URL 里是否带着检索词——带着才能靠重新打开复现出结果。"""
        flat = _squash(unquote(url or ""))
        probes = _search_probes(query) or [_squash(query)]
        return any(probe and probe in flat for probe in probes)

    def _judge_results(
        self,
        page,
        query: str,
        baseline: dict[str, int],
        fresh: bool = False,
        require_probe: bool = False,
    ) -> bool:
        """按“结果页真的出结果”判定检索成功，而不是看 URL 有没有变。

        fresh=True 表示这是一个刚打开的新窗口：整页都是新内容，只能用绝对量判断；
        否则和提交前的基线比增量（原页面的导航文字不算检索结果）。
        require_probe=True 用于既不可靠复现（URL 不带检索词）、又无法据增量判断的页面：
        这时必须看到检索词出现在正文里才算数。
        实测济南 www.jinan.gov.cn 的站内检索返回 xxgksearch.html?searchVal=<令牌>，
        页面是**未筛选的公文列表**（道路命名、行政审批……），宽松条件会让它蒙混过关。
        反过来，把“有结果但某分类下没有”的提示误判成失败代价更大（整条路径作废），
        所以有检索词出现时一律放行。
        """
        try:
            text = page.evaluate("() => (document.body ? document.body.innerText : '')") or ""
            links = page.evaluate("() => document.querySelectorAll('a[href]').length")
        except Exception:
            return False
        flat = _squash(text)
        if len(flat) < 40:
            return False
        probe_hit = any(probe in flat for probe in _search_probes(query))
        if require_probe and not probe_hit:
            return False
        substantial = links >= 5 and len(flat) >= 200
        if EMPTY_RESULT_PATTERN.search(flat[:1200]) and not substantial:
            # 明确的零结果页（“没有找到与「xx」相关的内容”）。
            return False
        if probe_hit:
            return True
        if fresh:
            return substantial
        grew_links = links - int(baseline.get("links") or 0)
        grew_text = len(text) - int(baseline.get("text") or 0)
        return grew_links >= 5 and grew_text > 200

    def _submit_site_search(self, page, query: str) -> str | None:
        """在给定页面上提交站内检索，返回结果页 URL（没拿到结果返回 None）。

        成功判据是“结果页真的出结果”，不是“URL 变了”：
        原地渲染结果的站点 URL 不变（TRS/jpaas 一类），而内容详情页上的装饰性搜索框
        提交后 URL 可能变了却依旧没有结果（实测济南市住建局详情页）。
        """
        field = self._search_field(page)
        if field is None:
            return None
        try:
            baseline = page.evaluate(SEARCH_BASELINE_JS)
        except Exception:
            baseline = {"links": 0, "text": 0}
        try:
            field.fill(query, timeout=self.click_timeout_ms)
        except Exception:
            return None
        old_url = page.url
        for kind in ("enter", "button", "form"):
            child = self._try_submit(page, kind, field if kind == "enter" else None)
            if child is not None:
                try:
                    self._wait_loaded(child)
                    url = child.url
                    verdict = self._judge_results(
                        child,
                        query,
                        {"links": 0, "text": 0},
                        fresh=True,
                        # URL 不带检索词就没法靠重新打开复现，必须看到检索词才算数。
                        require_probe=not self._query_in_url(url, query),
                    )
                except Exception:
                    verdict, url = False, None
                if not verdict and url and self._retry_judge(child, query):
                    verdict = True
                if verdict and url and self._query_in_url(url, query):
                    # 结果页 URL 自带检索词（实测 jncc 的
                    # /jpaas-jsearch-web-server/search?q=...），重新打开就能复现。
                    try:
                        child.close()
                    except Exception:
                        pass
                    return url
                if verdict and url:
                    # URL 只是个不带检索词的令牌（实测济南 xxgksearch.html?searchVal=xxx），
                    # 重新打开只会看到未筛选的列表：把渲染好的活页面连同结果一起交出去。
                    self.pending_observation = self._summarize(child)
                    self._live_page = child
                    return url
                try:
                    child.close()
                except Exception:
                    pass
                continue
            self._settle(page, baseline)
            if self._judge_results(page, query, baseline, require_probe=_same_url(page.url, old_url)):
                if _same_url(page.url, old_url):
                    # 原地渲染：URL 没变，结果只存在于这个活着的页面里，
                    # 留着给状态图复用（重新打开这个 URL 是看不到结果的）。
                    self.pending_observation = self._summarize(page)
                    self._live_page = page
                return page.url
        return None

    def _export_pdf(self, page, output_dir: str, filename: str) -> Path:
        """导出网页 PDF 前先等页面就绪。

        实测同一页面：立刻导出只有 1 页，页面开着十几秒后再导出是 18 页——
        内容还没渲染完就调用 page.pdf() 会被截断。
        """
        try:
            page.wait_for_load_state("load", timeout=5_000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        path = Path(output_dir) / filename
        page.pdf(path=str(path), format="A4", print_background=True)
        return path

    def _save_response(self, response, output_dir: str, fallback_name: str) -> str:
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" in content_type:
            # 直链指向网页时存成 .html 交给用户没有意义。
            # 实测模型会把页面 URL 当文件直链返回（reason 里却写着自己应选 save_page_pdf）。
            raise RuntimeError(
                f"目标地址返回的是网页而不是文件（content-type: {content_type}）——直链不能指向网页，请改用页面导出或附件链接"
            )
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > 100 * 1024 * 1024:
            raise ValueError("file exceeds configured size limit")
        body = response.body()
        if not body:
            raise RuntimeError("下载响应为空")
        filename = response.url.rsplit("/", 1)[-1].split("?", 1)[0] or fallback_name
        path = Path(output_dir) / (Path(filename).name or fallback_name)
        path.write_bytes(body)
        return str(path)

    def execute(self, url: str, plan: DownloadPlan, output_dir: str) -> str | None:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        context = self._ensure_context()
        if plan.action in {"download_direct", "inspect_embedded"} and plan.url:
            # 无需先打开页面：直接复用浏览器上下文的会话（Cookie/Referer）取文件。
            response = context.request.get(plan.url, timeout=self.timeout_ms)
            _ensure_ok(response)
            return self._save_response(response, output_dir, "downloaded_file")

        if plan.action == "follow_attachment_page" and plan.url and not _is_file_url(plan.url):
            # 附件列表或下一层详情页是 HTML，应当进入后重新观察，而不是当成文件存盘。
            page = self._open(plan.url)
            try:
                self.last_url = page.url
            finally:
                page.close()
            return None

        # 原地渲染过结果的检索页只能复用，重新打开会看到空白页面。
        page = self._reuse_live_page(url)
        path: Path | None = None
        try:
            if plan.action == "click_view_original":
                old_url = page.url
                handed_off = False
                try:
                    with page.expect_popup(timeout=3_000) as popup_info:
                        _locator(page, plan.selector or "text=查看原文").click(timeout=self.click_timeout_ms)
                    child = popup_info.value
                    if not _same_url(child.url, old_url):
                        self._handoff(child)
                        handed_off = True
                except Exception:
                    handed_off = False
                if not handed_off:
                    _locator(page, plan.selector or "text=查看原文").click(timeout=self.click_timeout_ms)
                    page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
                    if _same_url(page.url, old_url):
                        raise RuntimeError("点击查看原文后页面地址未发生变化")
                    self._handoff(page)
                return None
            elif plan.action == "click_download":
                selector = plan.selector or "text=下载"
                captured: list[Any] = []

                # 注意：这里必须是普通 Python 函数，不能直接用 captured.append。
                # Playwright 的同步封装会对处理器对象做 setattr(handler, "_pw_impl_instance_", ...)，
                # builtin 方法没有 __dict__，会直接抛 AttributeError。
                def _capture_download(download: Any) -> None:
                    captured.append(download)

                # 不少站点的下载按钮是 window.open(...) 弹新页再触发下载的，
                # 只监听当前页会漏掉，所以这里在 context 级别收集下载事件。
                context.on("download", _capture_download)
                try:
                    try:
                        _locator(page, selector).click(timeout=self.click_timeout_ms)
                    except Exception as exc:
                        raise RuntimeError(f"无法点击下载控件（{selector}）：{exc}") from exc
                    waited = 0
                    while not captured and waited < self.download_wait_ms:
                        page.wait_for_timeout(250)
                        waited += 250
                    if captured:
                        download = captured[0]
                        path = Path(output_dir) / (download.suggested_filename or "downloaded_file")
                        download.save_as(str(path))
                    elif page.url != url:
                        # 有些“下载”按钮只是把当前页面导航过去。
                        response = page.request.get(page.url, timeout=self.timeout_ms)
                        _ensure_ok(response)
                        content_type = (response.headers.get("content-type") or "").lower()
                        if "text/html" in content_type:
                            # 导航到的其实是另一个网页（阅读器/预览页），不是文件。
                            # 存成文件只会得到一份无法识别的产物、白占一个候选
                            # （校验会以"无法识别文件类型"把它拒掉），
                            # 更糟的是它可能盖住真正该走的路。交给下一轮观察。
                            self._handoff(page)
                            return None
                        filename = page.url.rsplit("/", 1)[-1].split("?", 1)[0] or "downloaded_file"
                        path = Path(output_dir) / Path(filename).name
                        path.write_bytes(response.body())
                    else:
                        # 按钮弹出了新窗口但没有触发下载（例如在线阅读器、
                        # 国家标准平台的『查看文本』）。**这一页往往才是带下载入口的正文页**，
                        # 所以不能关掉——把它连同渲染结果一起交给下一轮观察。
                        # 实测以前在这里把弹窗关掉、只留地址，导致 7 个任务
                        # （6 个交通部页面 + 国家标准平台）退化成导出网页。
                        popup = newest_popup(context, page)
                        if popup is not None:
                            self._handoff(popup)
                            return None
                        raise RuntimeError("点击下载按钮后没有产生下载或文件导航")
                finally:
                    context.remove_listener("download", _capture_download)
            elif plan.action == "click_print":
                selector = plan.selector or "text=打印"
                # 某些页面点击“打印”只改变页面状态，随后由浏览器导出当前正文。
                try:
                    _locator(page, selector).click(timeout=3_000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                # 文件名加进程号：批量并行时多个任务可能同时退化成网页导出，
                # 常量文件名会互相覆盖，甚至让先完成的那个校验到别人的文件。
                path = self._export_pdf(page, output_dir, f"printed_page_{os.getpid()}.pdf")
            elif plan.action == "save_page_pdf":
                path = self._export_pdf(page, output_dir, f"page_export_{os.getpid()}.pdf")
            elif plan.action == "site_search":
                query = (plan.search_query or plan.selector or "").strip()
                if not query:
                    raise RuntimeError("site_search 需要提供检索词（search_query）")
                # 先选对站点：站内检索的入口在本站首页，而内容详情页上的搜索框常是装饰性的
                # （实测济南市住建局详情页：提交后页面毫无反应，四次重试全废）。
                attempts: list[tuple[str, str | None]] = [("当前页面", None)]
                root = _site_root(page.url)
                if root and not _same_url(root, page.url):
                    attempts.append(("站点首页", root))
                reasons: list[str] = []
                for label, target in attempts:
                    if target is not None:
                        try:
                            page.goto(target, wait_until="domcontentloaded", timeout=self.timeout_ms)
                        except Exception as exc:
                            reasons.append(f"{label}打开失败（{exc}）")
                            continue
                    results_url = self._submit_site_search(page, query)
                    if results_url:
                        self.last_url = results_url
                        return None
                    reasons.append(f"{label}没有出现检索结果")
                raise RuntimeError("站内检索没有拿到结果：" + "；".join(reasons))
            elif plan.action == "no_download":
                return None
            elif plan.action in {"inspect_embedded", "follow_attachment_page"} and plan.url:
                response = page.request.get(plan.url, timeout=self.timeout_ms)
                _ensure_ok(response)
                filename = plan.url.rsplit("/", 1)[-1].split("?", 1)[0] or "embedded_file"
                path = Path(output_dir) / Path(filename).name
                path.write_bytes(response.body())
            else:
                raise RuntimeError(f"Action {plan.action} requires a specialized handler")
        finally:
            self._close_page(page)
        return str(path) if path and path.exists() else None
