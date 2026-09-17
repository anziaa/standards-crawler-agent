from __future__ import annotations

import sys
import time
from typing import Any, Protocol
from urllib.parse import urlparse

from langgraph.graph import END, START, StateGraph

from .browser import PageBrowser
from .content import (
    compare_declared_identity,
    declared_identity,
    normalize_standard_number,
    page_is_not_a_document,
    prefer_reading_entry,
)
from .download import (
    build_canonical_filename,
    same_content,
    detect_lifecycle_status,
    download_direct,
    quarantine_artifact,
    verify_file,
)
from pathlib import Path
from .models import AgentConfig, AgentState, CandidatePage, DownloadPlan, SearchResult
from .ranking import combined_score, derive_official_entry, is_low_trust_domain, rank_pages
from .search import (
    SearchProvider,
    build_queries,
    build_retry_queries,
    deduplicate,
    looks_relevant,
    normalize_filename,
    relevance_report,
)


def _timed(name: str, node):
    """记录每个节点的耗时，写回该节点生成的最后一条 trace 记录。"""

    def wrapper(state: AgentState):
        started = time.perf_counter()
        update = node(state) or {}
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        trace = update.get("trace")
        if trace:
            trace[-1]["elapsed_ms"] = elapsed_ms
        else:
            update["trace"] = list(state.get("trace", [])) + [{"node": name, "elapsed_ms": elapsed_ms, "details": {}}]
        # 进度写 stderr：批量驱动把 stderr 落进 run.log，任务被超时杀掉时也能看到
        # 最后进到哪个节点；stdout 只留最终 JSON，不污染管道输出。
        print(f"[node] {name} {elapsed_ms}ms", file=sys.stderr, flush=True)
        return update

    return wrapper


def _with_usage(plan: DownloadPlan, planner: Any) -> dict[str, Any]:
    """把本次决策的 token 用量并入 trace 明细。"""
    details = plan.model_dump()
    usage = getattr(planner, "last_usage", None)
    if usage:
        details["usage"] = usage
    return details


def _sum_usage(trace: list[dict[str, Any]]) -> dict[str, int]:
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for item in trace:
        usage = (item.get("details") or {}).get("usage") or {}
        for key in total:
            total[key] += int(usage.get(key) or 0)
    return total


def _plan_or_failure(planner: Any, requested: str, observation: dict[str, Any]) -> tuple[DownloadPlan | None, str | None]:
    """让模型做一次页面决策；调用本身失败时返回 (None, 原因) 而不是抛出去。

    模型客户端已经设了调用超时（见 cli.build_chat_model），上游停顿会抛异常。
    没有这层保护时，异常会一路冒到 `graph.invoke`，整条任务死掉、连 result.json 都不生成
    （r20 的 #6 两次尝试都卡在这一步，最后只能被批量的 120 秒上限杀掉）。
    接住之后就能像"这一页拿不到计划"一样处理：换下一个候选，或如实收工。
    """
    try:
        return planner.plan(requested, observation), None
    except Exception as exc:
        return None, f"模型决策失败：{type(exc).__name__}: {exc}"


class DownloadPlanner(Protocol):
    def plan(self, requested_name: str, observation: dict[str, Any]) -> DownloadPlan: ...


class HeuristicDownloadPlanner:
    """Safe fallback; replace with an LLM planner for richer site-specific decisions."""

    def plan(self, requested_name: str, observation: dict[str, Any]) -> DownloadPlan:
        links = observation.get("links", [])
        buttons = observation.get("buttons", [])
        lowered = requested_name.lower()
        for link in links:
            text = (link.get("text", "") + " " + link.get("url", "")).lower()
            if any(ext in text for ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")) or any(word in text for word in ("附件", "下载", "download")):
                return DownloadPlan(action="download_direct", url=link.get("url"), confidence=0.82, reason="发现疑似文件或附件链接")
        for button in buttons:
            text = button.get("text", "")
            if any(word in text for word in ("查看原文", "查看文件", "原文链接", "在线阅读")):
                return DownloadPlan(action="click_view_original", selector=f"text={text}", confidence=0.72, reason="需要先进入原文或文件详情页面", fallback_actions=["click_download", "inspect_embedded"])
            if "下载" in text or "download" in text.lower():
                return DownloadPlan(action="click_download", selector=f"text={text}", confidence=0.7, reason="发现下载按钮", fallback_actions=["inspect_embedded"])
            if "打印" in text or "print" in text.lower() or observation.get("has_print_hint"):
                return DownloadPlan(action="click_print", selector=f"text={text}" if text else "text=打印", confidence=0.62, reason="页面提供打印导出线索")
        embedded = observation.get("embedded", [])
        for item in embedded:
            if item.get("url"):
                return DownloadPlan(action="inspect_embedded", url=item["url"], confidence=0.68, reason="页面包含嵌入式文档")
        for link in links:
            text = link.get("text", "")
            if any(word in text for word in ("附件", "查看文件", "下载文件", "相关材料")) and link.get("url"):
                return DownloadPlan(action="follow_attachment_page", url=link["url"], confidence=0.55, reason="需要进入附件或下一层页面")
        if len(observation.get("text", "")) >= 800:
            return DownloadPlan(action="save_page_pdf", confidence=0.45, reason="未发现附件，但页面包含较完整正文，可尝试导出网页 PDF", fallback_actions=["no_download"])
        return DownloadPlan(action="no_download", confidence=0.8, downloadable=False, reason="页面未发现附件、文件链接、下载控件或足够完整的目标正文")


def redirect_page_export(
    observation: dict[str, Any], plan: DownloadPlan, tried: list[str]
) -> DownloadPlan:
    """页面上还有通往正文的入口时，不许把当前页拍成 PDF。

    实测 GB/T 8923.1-2011 那条：所有真实下载路径失败后，模型被推到兜底链末端的
    `save_page_pdf`，导出的是标准著录页——产物里没有正文，等于把"下不到"伪装成"成功"。
    现在改成两步：还没点过入口就先去点（那才是正文所在），点过仍失败就如实收工。

    只在**能断定本页不是正文页**时才干预（`page_is_not_a_document`）：
    官网 HTML 正文的法律法规页本来就该用 save_page_pdf，不能拦。
    """
    if plan.action not in {"save_page_pdf", "click_print"}:
        return plan
    entries = list((observation.get("reading_entry") or {}).get("entries") or [])
    if not entries:
        return plan
    if page_is_not_a_document(observation.get("text", "")) is None:
        # 这一页本身可能就是正文（含全文的政务页），不要干预。
        return plan
    best = prefer_reading_entry(entries)
    if best and "click_download" not in tried:
        return plan.model_copy(
            update={
                "action": "click_download",
                "selector": f"text={best}",
                "downloadable": True,
                "reason": (
                    f"本页不是正文页（页面上的正文入口：{'、'.join(entries)}），"
                    f"改为点击入口『{best}』取正文"
                ),
            }
        )
    return plan.model_copy(
        update={
            "action": "no_download",
            "downloadable": False,
            "reason": (
                f"本页没有正文，正文入口（{'、'.join(entries)}）已尝试过仍未拿到，如实收工"
            ),
        }
    )


def _choose_candidate_safely(chooser: Any, name: str, pages: list[CandidatePage]) -> tuple[int | None, dict[str, Any]]:
    """让候选选择器挑一个下标；任何异常都降级成"保持确定性顺序"。

    与小模型打交道不能假设它一定回答：超时、解析失败、下标越界都会发生，
    而"排序没被优化"只是没赚到，不该升级成任务失败。
    """
    try:
        index = chooser.choose(name, pages)
    except Exception as exc:
        return None, {"error": f"候选选择失败：{type(exc).__name__}: {exc}"}
    note: dict[str, Any] = {"index": index, "reason": getattr(chooser, "last_reason", "") or ""}
    usage = getattr(chooser, "last_usage", None)
    if usage:
        note["usage"] = usage
    return index, note


def build_graph(searcher: SearchProvider, browser: PageBrowser, planner: DownloadPlanner | None = None, config: AgentConfig | None = None, chooser: Any | None = None):
    planner = planner or HeuristicDownloadPlanner()
    config = config or AgentConfig()

    def parse_task(state: AgentState):
        normalized, tokens = normalize_filename(state["requested_filename"])
        queries = build_queries(state["requested_filename"])[: config.max_search_queries]
        return {"normalized_name": normalized, "filename_tokens": tokens, "search_queries": queries, "search_rounds": 0, "attempts": 0, "max_attempts": config.max_page_attempts, "candidate_index": 0, "fallback_index": 0, "tried_actions": [], "last_download_error": None, "errors": [], "trace": [{"node": "parse_task", "details": {"normalized_name": normalized, "tokens": tokens, "queries": queries}}]}

    def search(state: AgentState):
        found: list[SearchResult] = []
        errors = list(state.get("errors", []))
        # 第一轮用信息量最高的精确短语检索；候选池为 0 时（见 rank）再跑一轮更简单的
        # 检索词。长中文标题做精确短语检索时容易退化成按头名词匹配或直接返回 0 条。
        rounds = state.get("search_rounds", 0)
        queries = state["search_queries"] if rounds == 0 else build_retry_queries(state["normalized_name"])
        if rounds:
            errors.append(f"候选池为 0，改用更简单的检索词重试：{'、'.join(queries)}")
        degraded: list[str] = []
        unmatched: list[str] = []
        # 被丢弃的批次要留证：判"无关"时整批不进候选池，而"丢掉的到底是什么"以前没有记录，
        # 于是"闸门有没有误杀一个标题不匹配、内容却对的页面"只能靠猜。
        discarded_batches: list[dict[str, Any]] = []
        for query in queries:
            try:
                rows = searcher.search(query, limit=10)
            except Exception as exc:
                errors.append(f"搜索失败（{query}）：{exc}")
                continue
            if rows:
                # 读取层（BrowserSearch）已经判过一次；这里兜底过滤，保证任何 provider
                # 返回的垃圾都不进候选池，且不再重复发起检索。
                relevance = relevance_report(query, rows)
                if not relevance["relevant"]:
                    errors.append(f"搜索结果与查询无关，已丢弃（{query}）")
                    discarded_batches.append({"query": query, "verdict": "graph_drop", "relevance": relevance})
                    continue
                found.extend(rows)
                continue
            # 读不到结果要分清两种原因，对策完全不同：
            #   no_match / fallback —— 引擎给了头名词兜底页（对这条查询没有匹配）→ 换入口才有效；
            #                         fallback 是"字符重合度过了、但没有连续片段命中"的那种
            #                         （查文件返回省人民政府首页），重读同样无效。
            #   empty    —— 过渡页/空壳页 → 换干净页面重读才有效。
            report = getattr(searcher, "last_read", None) or {}
            verdict = str(report.get("verdict") or "")
            tries = len(report.get("attempts") or []) or 1
            if verdict in ("no_match", "fallback"):
                unmatched.append(query)
                if verdict == "fallback":
                    signature = report.get("fallback") or {}
                    errors.append(
                        f"搜索引擎返回头名词兜底页（最长连续命中 {signature.get('longest_run')} 字 < "
                        f"要求的 {signature.get('required')} 字，最接近的是「{signature.get('closest_title')}」），"
                        f"重读无效（{query}）"
                    )
                else:
                    errors.append(f"搜索引擎未匹配到该文档（疑似头名词兜底页，重读无效）（{query}）")
                entry: dict[str, Any] = {"query": query, "verdict": verdict, "attempts": report.get("attempts")}
                if report.get("fallback"):
                    entry["fallback"] = report["fallback"]
                if report.get("discarded"):
                    entry["discarded"] = report["discarded"]
                if report.get("relevance"):
                    entry["relevance"] = report["relevance"]
                discarded_batches.append(entry)
            else:
                degraded.append(query)
                errors.append(f"搜索无结果（已重读 {tries} 次）（{query}）")
                discarded_batches.append({"query": query, "verdict": verdict or "empty", "attempts": report.get("attempts")})
        results = deduplicate(found)[:config.max_search_results]
        details: dict[str, Any] = {"round": rounds + 1, "queries": queries, "count": len(results), "failed_queries": len(errors), "degraded_queries": degraded, "unmatched_queries": unmatched, "results": [item.model_dump() for item in results[:20]]}
        if discarded_batches:
            details["discarded_batches"] = discarded_batches
        return {"search_results": results, "search_rounds": rounds + 1, "search_degraded": bool(degraded), "search_unmatched": bool(unmatched), "errors": errors, "trace": state.get("trace", []) + [{"node": "search", "details": details}]}

    def rank(state: AgentState):
        # 第三方文库/内容/电商站直接不进候选池：它们不会托管官方原件，
        # 之前只降权时模型会被"标题逐字相同"诱惑过去，最后只能导出付费外壳。
        trustworthy: list[SearchResult] = []
        rejected: list[dict[str, str]] = []
        for item in state["search_results"]:
            domain = item.domain or urlparse(item.url).netloc
            if is_low_trust_domain(domain):
                rejected.append({"url": item.url, "domain": domain})
                continue
            trustworthy.append(item)
        if rejected:
            errors = list(state.get("errors", [])) + [
                f"已排除 {len(rejected)} 条第三方文库/内容站结果（{'、'.join(sorted({r['domain'] for r in rejected}))}）："
                "这类站点不托管官方原件"
            ]
        else:
            errors = list(state.get("errors", []))

        ranked = rank_pages(state["normalized_name"], trustworthy)
        # 丢掉连最低可信度都不到的候选：搜索引擎无命中时会解析出无关链接，
        # 直接观察它们只会白花一次大模型调用。
        pages = [page for page in ranked if combined_score(page) >= config.min_candidate_score]
        dropped = len(ranked) - len(pages)
        # 兜底：候选全部低于门槛时，如果结果里有官方网站，就把它当作站内检索的入口。
        # 官网首页本身没有目标文件，但可以在站内检索到（实测济南市住建局官网可行）；
        # 这个入口只在正常候选全部不可用时才启用。
        official_fallback = False
        entry_reason = ""
        if not pages:
            gov_pages = [page for page in ranked if ".gov.cn" in page.domain]
            if gov_pages:
                pages = gov_pages[:1]
                official_fallback = True
                entry_reason = "结果里有政府站点，作为站内检索入口"
        # 一个候选都没有：这是最典型的“当次搜索运气差”（实测约占 4.8% 的任务轮次），
        # 换更简单的检索词再搜一轮；只重试一次（search_rounds < 2），避免反复无效检索。
        research = not pages and state.get("search_rounds", 0) < 2
        # 换检索词也没救（搜索引擎对这条查询没有匹配）→ **换入口**：按名称推断发文机关
        # 官网或标准平台，用它的站内检索去拿文件。实测站内检索可用：
        # dbba.sacinfo.org.cn → /stdList?key=…；机构官网 → 其 jpaas 检索接口。
        if not pages and not research and not state.get("entry_attempted"):
            entry = derive_official_entry(state["normalized_name"])
            if entry:
                url, reason = entry
                pages = [
                    CandidatePage(
                        url=url,
                        title="官方入口（站内检索）",
                        text=reason,
                        domain=urlparse(url).netloc,
                        official_score=0.9,
                        page_score=0.5,
                        evidence=["候选池为 0，改用官方入口做站内检索"],
                    )
                ]
                official_fallback = True
                entry_reason = reason
                errors.append(f"搜索引擎无匹配，改用官方入口：{url}（{reason}）")
        # 候选池里不止一条时，让候选选择器挑一个先看。确定性打分只能测字面（"标题里有没有
        # 出现这个名称"），读不懂"这一页的附件是宣传画册""这只是网站首页"这类语义；
        # 而**先看哪一页**几乎决定了整条任务的走向：状态图拿到第一个成功就收工。
        # 选择器失败/超时/返回越界下标时保留上面的确定性顺序，不改变行为。
        choice_note: dict[str, Any] | None = None
        choices = pages[: config.max_candidate_domains]
        deterministic_first = choices[0].url if choices else None
        if chooser is not None and len(choices) > 1:
            chosen, choice_note = _choose_candidate_safely(chooser, state["normalized_name"], choices)
            # 记下"确定性排序本来会先看哪一页"：候选池随后会被重排，只留重排后的名单就分不清
            # "模型认同排序"和"模型把顺序改了"（实测复核时正是这一点差点看错）。
            choice_note["deterministic_first"] = deterministic_first
            if choice_note.get("error"):
                # 失败了要留痕：否则"排序没被优化"和"没配选择器"在产物里分不出来。
                errors.append(str(choice_note["error"]))
            # 下标在**使用它的这一行**校验：选择器是可注入的组件，越界访问发生在这里，
            # 保护也就该放在这里（真实实现另有一层校验，因为它还要防模型自己乱填）。
            if chosen is not None and 0 < chosen < len(choices):
                pages = [choices[chosen]] + [page for index, page in enumerate(choices) if index != chosen] + pages[len(choices):]
        selected = pages[0] if pages else None
        pages = pages[:config.max_candidate_domains]
        details = {"candidates": [item.model_dump() for item in pages], "selected": selected.url if selected else None, "dropped_low_score": dropped, "official_fallback": official_fallback, "entry_reason": entry_reason, "research_needed": research, "rejected_low_trust": rejected}
        if choice_note:
            details["chooser"] = choice_note
            if choice_note.get("usage"):
                details["usage"] = choice_note["usage"]
        return {"candidate_pages": pages, "selected_page": selected, "candidate_index": 0, "official_fallback": official_fallback, "entry_reason": entry_reason, "entry_attempted": bool(entry_reason) or state.get("entry_attempted", False), "research_needed": research, "errors": errors, "trace": state.get("trace", []) + [{"node": "rank", "details": details}]}

    def observe(state: AgentState):
        selected = state.get("selected_page")
        if not selected:
            return {"page_observation": {}, "errors": state.get("errors", []) + ["未发现候选页面"], "needs_confirmation": True, "trace": state.get("trace", []) + [{"node": "observe", "details": {"error": "未发现候选页面"}}]}
        try:
            page_url = state.get("current_url") or selected.url
            observation = browser.observe(page_url)
            stats = observation.get("stats") or {}
            if (
                (stats.get("text_total_chars") or 0) < config.min_observation_text_chars
                and not observation.get("links")
                # 直链附件的观察结果本来就没有正文和链接，不算“空白页”。
                and not observation.get("direct_file")
            ):
                # 空白页（例如 SPA 的跳转链接只返回空壳）没有可决策的内容。
                # 直接当成观察失败去换下一个候选，而不是让模型对着空页面说 no_download 后收工。
                raise RuntimeError(
                    f"页面几乎没有内容（正文 {stats.get('text_total_chars', 0)} 字符、链接 {len(observation.get('links', []))} 条）"
                )
            if state.get("official_fallback"):
                observation = dict(observation)
                observation["official_site_entry"] = (
                    "这是官方网站的入口页（正常候选都不可用时的兜底）。"
                    "如果本页没有目标文件或附件，请用 site_search 在站内检索："
                    "search_query 优先填文号/标准号，其次填文件名称的关键部分。"
                )
            previous_url = (state.get("page_observation") or {}).get("url")
            page_changed = bool(previous_url) and previous_url != page_url
            # 页面上明写了「标准号 / 中文标准名称」时，先核对它是不是目标文件，
            # **在下载之前**就把它挡掉。
            # 实测：请求《坠落防护 安全绳》，排在第一的候选却是《坠落防护 安全带》
            # GB 6095-2021 的著录页；页面自己写得明明白白，模型却照着点了下载，
            # 把错文件当成成功交了出去。而产物 PDF 的文本层是乱码，产物层校验永远查不出来。
            # 页面元数据（HTML）是可读的，所以这一步用页面而不是产物。
            # 不匹配就清空观察结果 → after_observe 自动切下一个候选（还有候选的话）。
            declared_number, declared_title = declared_identity(observation.get("text", ""))
            if declared_title or declared_number:
                identity_ok, identity_note = compare_declared_identity(
                    declared_number, declared_title, state["requested_filename"]
                )
                if not identity_ok:
                    message = (
                        f"候选页不是目标文件（{page_url}）：{identity_note}；"
                        "已跳过该候选"
                    )
                    return {
                        "page_observation": {},
                        "errors": state.get("errors", []) + [message],
                        "trace": state.get("trace", []) + [{
                            "node": "observe",
                            "details": {
                                "url": page_url,
                                "declared_standard_number": declared_number,
                                "declared_title": declared_title,
                                "identity_rejected": identity_note,
                            },
                        }],
                    }
            # 换了页面就清空“本页已失败动作”，否则会错误地拦住模型重选动作。
            return {"page_observation": observation, "current_url": page_url, "tried_actions": [] if page_changed else state.get("tried_actions", []), "last_download_error": None if page_changed else state.get("last_download_error"), "trace": state.get("trace", []) + [{"node": "observe", "details": {"url": page_url, "title": observation.get("title"), "stats": observation.get("stats", {}), "link_count": len(observation.get("links", [])), "button_count": len(observation.get("buttons", [])), "embedded": observation.get("embedded", []), "has_print_hint": observation.get("has_print_hint", False), "declared_standard_number": declared_number, "declared_title": declared_title}}]}
        except Exception as exc:
            return {"page_observation": {}, "errors": state.get("errors", []) + [f"页面观察失败（{state.get('current_url') or selected.url}）: {exc}"], "trace": state.get("trace", []) + [{"node": "observe", "details": {"url": state.get("current_url") or selected.url, "error": str(exc)}}]}

    def retry_candidate(state: AgentState):
        pages = state.get("candidate_pages", [])
        next_index = state.get("candidate_index", 0) + 1
        selected = pages[next_index] if next_index < len(pages) else None
        return {"candidate_index": next_index, "fallback_index": 0, "tried_actions": [], "last_download_error": None, "selected_page": selected, "current_url": selected.url if selected else None, "page_observation": {}, "download_plan": None, "downloaded_path": None, "verification": None, "trace": state.get("trace", []) + [{"node": "retry_candidate", "details": {"candidate_index": next_index, "selected": selected.url if selected else None}}]}

    def choose_download(state: AgentState):
        if not state.get("selected_page") or not state.get("page_observation"):
            return {"download_plan": None, "needs_confirmation": True, "trace": state.get("trace", []) + [{"node": "choose_download", "details": {"error": "缺少页面观察结果"}}]}
        # 同一页上站内检索已经连续两次没拿到结果时，不要再问模型：它会一轮一轮重复同一个动作
        # （实测 r19~r21 的 #5 在平台首页连选 5 次 site_search，5 轮全空、烧掉 45.4k token，
        #  而 previous_attempts 里明明写着"这个动作失败过"）。
        # 页面一旦换了就不会触发——`observe` 会清空 tried_actions 与 last_download_error，
        # 而 #8 的 DB37 标准正是靠站内检索跳到结果页才拿到的。
        exhausted = _site_search_exhausted(state)
        if exhausted:
            return {"download_plan": None, "errors": state.get("errors", []) + [exhausted], "trace": state.get("trace", []) + [{"node": "choose_download", "details": {"skipped": True, "reason": exhausted}}]}
        observation = state["page_observation"]
        tried = state.get("tried_actions", [])
        if state.get("entry_reason") and not tried:
            # 这一页是"候选池为 0 时的官方入口"（官网/平台首页）。它本身没有目标文件，
            # 正确动作是用站内检索找；不给提示的话模型容易直接判 no_download 或导出首页。
            observation = dict(observation)
            observation["official_entry"] = {
                "reason": state.get("entry_reason"),
                "instruction": (
                    "搜索引擎对该文件没有匹配，这一页是官方入口。"
                    "请优先使用 site_search 动作，检索词用文件名称中最有辨识度的部分；"
                    "只有在页面上确实没有任何检索入口时才另作选择"
                ),
            }
        if tried:
            # 把本页已经失败过的动作告诉模型：否则它会一轮一轮重复选同一个失败动作
            # （实测同一个动作连选 5 次，每轮还各烧掉 60 秒超时）。
            observation = dict(observation)
            observation["previous_attempts"] = {
                "tried_actions": tried,
                "last_error": state.get("last_download_error"),
                "instruction": (
                    "以下动作在本页已经失败，不要重复选择；"
                    "换一个有页面证据支持的其他动作，没有依据时选择 no_download"
                ),
            }
        plan, failure = _plan_or_failure(planner, state["requested_filename"], observation)
        if failure:
            return {
                "download_plan": None,
                "errors": state.get("errors", []) + [f"{failure}（本页 {state.get('current_url') or state['selected_page'].url}）"],
                "trace": state.get("trace", []) + [{"node": "choose_download", "details": {"error": failure}}],
            }
        plan = redirect_page_export(observation, plan, list(state.get("tried_actions", [])))
        return {"download_plan": plan, "fallback_index": 0, "tried_actions": state.get("tried_actions", []), "trace": state.get("trace", []) + [{"node": "choose_download", "details": _with_usage(plan, planner)}]}

    def check_lifecycle(state: AgentState):
        selected = state.get("selected_page")
        observation = state.get("page_observation", {})
        status, reason = detect_lifecycle_status(
            observation.get("text", ""),
            subject=state["requested_filename"],
            snippet=" ".join(
                part
                for part in (
                    selected.title if selected else "",
                    selected.text if selected else "",
                    observation.get("title", ""),
                )
                if part
            ),
        )
        return {"lifecycle_status": status, "skip_reason": reason, "trace": state.get("trace", []) + [{"node": "check_lifecycle", "details": {"status": status, "reason": reason}}]}

    def after_plan(state: AgentState) -> str:
        plan = state.get("download_plan")
        if plan is None:
            # 这一步没拿到计划（模型调用失败/超时，或页面没有观察结果）：
            # 换下一个候选，而不是当成"下载失败"再走一遍下载节点。
            return _next_candidate_or_report(state)
        return "report" if plan.action == "no_download" else "download"

    def download(state: AgentState):
        selected, plan = state.get("selected_page"), state.get("download_plan")
        if not selected or not plan:
            return {"errors": state.get("errors", []) + ["缺少页面或下载计划"], "trace": state.get("trace", []) + [{"node": "download", "details": {"error": "缺少页面或下载计划"}}]}
        try:
            tried_actions = state.get("tried_actions", []) + [plan.action]
            page_url = state.get("current_url") or selected.url
            if plan.action == "download_direct" and plan.url:
                # 优先走浏览器上下文（带 Cookie / UA / Referer）：不少政府站点会对裸 HTTP
                # 客户端返回 403（实测上海住建委的 PDF）。失败再退回 httpx 流式下载。
                try:
                    path = browser.execute(page_url, plan, config.download_dir)
                except Exception as browser_exc:
                    try:
                        path = download_direct(plan.url, config.download_dir, config.max_download_bytes)
                    except Exception as httpx_exc:
                        raise RuntimeError(
                            f"浏览器会话下载失败（{browser_exc}）；httpx 回退也失败（{httpx_exc}）"
                        ) from httpx_exc
            else:
                path = browser.execute(page_url, plan, config.download_dir)
            update = {"downloaded_path": path, "attempts": state.get("attempts", 0) + 1, "tried_actions": tried_actions, "last_download_error": None, "trace": state.get("trace", []) + [{"node": "download", "details": {"path": path, "action": plan.action}}]}
            last_url = getattr(browser, "last_url", None)
            if last_url:
                update["current_url"] = last_url
                update["trace"][-1]["details"]["navigated_to"] = last_url
            return update
        except Exception as exc:
            tried_actions = state.get("tried_actions", []) + [plan.action]
            return {"downloaded_path": None, "last_download_error": str(exc), "tried_actions": tried_actions, "errors": state.get("errors", []) + [f"下载失败: {exc}"], "attempts": state.get("attempts", 0) + 1, "trace": state.get("trace", []) + [{"node": "download", "details": {"error": str(exc), "action": plan.action, "reassess_instead_of_blind_fallback": True}}]}

    def verify(state: AgentState):
        path = state.get("downloaded_path")
        if not path:
            return {"verification": None, "needs_confirmation": True, "trace": state.get("trace", []) + [{"node": "verify", "details": {"error": "没有下载文件"}}]}
        renamed_path = path
        plan = state.get("download_plan")
        # 模型声称的目标文件名称一并交给校验：它与请求名称无关时视为下错文件。
        result = verify_file(path, state["requested_filename"], plan.matched_title if plan else None)
        # 网页导出（打印/保存页面）得到的是网页快照，不是原始附件，
        # 因此不按目标文件名重命名，避免看起来像官方原件。
        page_export = bool(plan and plan.action in {"save_page_pdf", "click_print"})
        if page_export:
            result.warnings.append("保存的是网页导出的 PDF，不是原始附件")
        # 产物性质写进元数据，便于事后按来源筛选（原来只靠状态区分，
        # 而被污染的网页外壳会被重命名成官方原件的样子，磁盘上看不出区别）。
        result.metadata["source_kind"] = (
            "scanned_document"
            if result.metadata.get("scan_only")
            else "page_export"
            if page_export
            else "original_file"
        )
        # 标准号优先用模型在页面上识别到的，其次才是 PDF 正文提取的结果。
        # 网页导出同样按规范名落盘，靠 status=page_export_only 区分性质。
        standard_number = (plan.standard_number if plan else None) or result.metadata.get("standard_number")
        if standard_number:
            result.metadata["standard_number"] = normalize_standard_number(standard_number)
        if result.ok:
            source = Path(path)
            # 扩展名以文件头判定的结果为准：端点名（downfile.jsp）不能留在产物名里。
            extension = (
                result.metadata.get("canonical_extension")
                or source.suffix
                or result.metadata.get("detected_extension", "")
            )
            canonical_name = build_canonical_filename(
                state["requested_filename"],
                extension,
                standard_number,
                plan.matched_title if plan else None,
            )
            target = source.with_name(canonical_name)
            if target != source:
                try:
                    if target.exists() and same_content(source, target):
                        # 同名文件里已经有一份**字节完全相同**的：直接丢掉这次下的临时件，
                        # 不重写目标（省一次几十 MB 写盘，也保留旧文件的时间戳）。
                        # 内容不同时按既定规则走——**新版直接盖旧版**（不留旧版副本）。
                        source.unlink(missing_ok=True)
                        renamed_path = str(target)
                    else:
                        source.replace(target)
                        renamed_path = str(target)
                except OSError as exc:
                    result.warnings.append(f"文件已验证，但重命名失败: {exc}")
        return {"downloaded_path": renamed_path, "verification": result, "needs_confirmation": not result.ok, "trace": state.get("trace", []) + [{"node": "verify", "details": {**result.model_dump(), "canonical_path": renamed_path}}]}

    def report(state: AgentState):
        verification = state.get("verification")
        selected = state.get("selected_page")
        lifecycle = state.get("lifecycle_status", "unknown")
        plan = state.get("download_plan")
        if lifecycle == "withdrawn":
            final_status = "skipped_withdrawn"
        elif lifecycle == "ambiguous":
            final_status = "needs_confirmation"
        elif plan and plan.action == "no_download":
            final_status = "not_downloadable"
        elif verification and verification.ok and verification.metadata.get("scan_only"):
            # 扫描件：正文以图像承载，原始内容确实在，但没有文本层可比对，
            # 因此既不算"官方原件"也不该混进网页导出，单列一个状态。
            final_status = "success_scanned"
        elif plan and plan.action in {"save_page_pdf", "click_print"} and verification and verification.ok:
            # 网页导出（法律/法规在官网就是 HTML 正文形态）内容正确即算成功，
            # 但状态上仍与“下载到官方原件”区分开。
            final_status = "success_page_export"
        elif verification is not None and not verification.ok and verification.metadata.get("body_absent"):
            # 产物里压根没有文件正文（标准平台著录页、文库付费外壳、登录墙）。
            # 这是"这里拿不到该文件"，而不是"需要人去确认一下"。
            final_status = "not_downloadable"
        else:
            final_status = "success" if verification and verification.ok else "needs_confirmation"
        return {"final": {"status": final_status, "requested_filename": state["requested_filename"], "downloaded_path": state.get("downloaded_path"), "source_url": selected.url if selected else None, "confidence": state.get("download_plan").confidence if state.get("download_plan") else 0, "lifecycle_status": lifecycle, "skip_reason": state.get("skip_reason"), "search_degraded": bool(state.get("search_degraded")), "search_unmatched": bool(state.get("search_unmatched")), "entry_reason": state.get("entry_reason") or "", "verification": verification.model_dump() if verification else None, "errors": state.get("errors", []), "usage_total": _sum_usage(state.get("trace", [])), "timings": [{"node": item["node"], "elapsed_ms": item.get("elapsed_ms")} for item in state.get("trace", [])], "total_elapsed_ms": sum(item.get("elapsed_ms") or 0 for item in state.get("trace", [])), "trace": state.get("trace", [])}}

    def after_verify(state: AgentState) -> str:
        if state.get("verification") and state["verification"].ok:
            return "report"
        # 被拒的产物先移出下载目录，再决定换候选还是收工。
        return "quarantine"

    def quarantine(state: AgentState):
        """把被校验拒绝的产物移出下载目录（downloads/_rejected/）。

        被拒的文件以前会一直留在下载目录里，多轮回归后混进结果
        （r15/r16 的 89d65074….pdf、r17 的 downfile.jsp）。
        """
        path = state.get("downloaded_path")
        verification = state.get("verification")
        if not path or verification is None or verification.ok:
            return {}
        selected = state.get("selected_page")
        detail = "\n".join(
            [
                f"请求名称: {state['requested_filename']}",
                f"来源页面: {(selected.url if selected else state.get('current_url')) or '-'}",
                f"原路径: {path}",
                f"决策动作: {state.get('download_plan').action if state.get('download_plan') else '-'}",
                "拒绝原因:",
                *[f"  - {item}" for item in (verification.warnings or ["校验未通过但未给出原因"])],
            ]
        )
        moved = quarantine_artifact(path, detail, config.download_dir)
        if not moved:
            return {}
        verification.metadata["quarantined_to"] = moved
        return {
            "downloaded_path": None,
            "errors": state.get("errors", []) + [f"被拒产物已移出下载目录：{moved}"],
            "trace": state.get("trace", []) + [{"node": "quarantine", "details": {"from": path, "to": moved, "warnings": verification.warnings}}],
        }

    def after_quarantine(state: AgentState) -> str:
        return "retry_candidate" if state.get("candidate_index", 0) + 1 < len(state.get("candidate_pages", [])) else "report"

    def after_observe(state: AgentState) -> str:
        if state.get("page_observation"):
            return "check_lifecycle"
        return _next_candidate_or_report(state)

    def _next_candidate_or_report(state: AgentState) -> str:
        """还有候选就换下一个，没有就收工。"""
        return "retry_candidate" if state.get("candidate_index", 0) + 1 < len(state.get("candidate_pages", [])) else "report"

    def _site_search_exhausted(state: AgentState) -> str | None:
        """同一页上站内检索连续两次没结果 → 返回停止原因；否则 None。"""
        failures = state.get("tried_actions", []).count("site_search")
        if failures >= 2 and "站内检索没有拿到结果" in (state.get("last_download_error") or ""):
            return f"站内检索已在同一页连续 {failures} 次没有结果，停止在本页重试"
        return None

    def after_download(state: AgentState) -> str:
        if state.get("downloaded_path"):
            return "verify"
        plan = state.get("download_plan")
        attempts = state.get("attempts", 0)
        limit = state.get("max_attempts", config.max_page_attempts)
        # 动作把浏览器带到了**另一个地址**时必须重新观察新页面，再让模型重新决策。
        # 这不只适用于"查看原文"，同样适用于「下载」类控件弹出的阅读器页：
        # 实测 GB/T 8923.1-2011 点『查看文本』后弹出了
        # openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=…（正文与下载入口都在那一页），
        # 但状态图当时直接进了 reassess_download，模型对着**旧页面**的观察结果重新决策，
        # 于是又选了一次 click_download（被判为重复动作拦下），最后退化成导出网页。
        # 判据用"当前地址 != 刚观察过的地址"，而不是列举动作名——否则每多一种
        # 会跳转/弹窗的控件就要再补一次白名单。
        observed_url = (state.get("page_observation") or {}).get("url")
        navigated = bool(state.get("current_url")) and state["current_url"] != observed_url
        if attempts < limit and (
            navigated or (plan and plan.action in {"click_view_original", "follow_attachment_page", "site_search"})
        ):
            return "observe"
        if plan and attempts < limit:
            return "reassess_download"
        return _next_candidate_or_report(state)

    def reassess_download(state: AgentState):
        observation = dict(state.get("page_observation", {}))
        observation["download_recovery"] = {"last_action": state.get("download_plan").action if state.get("download_plan") else None, "last_error": state.get("last_download_error"), "tried_actions": state.get("tried_actions", []), "instruction": "重新判断，只选择一个尚未尝试且有页面证据支持的动作；没有充分依据时选择 no_download"}
        plan, failure = _plan_or_failure(planner, state["requested_filename"], observation)
        if failure:
            return {
                "download_plan": None,
                "errors": state.get("errors", []) + [failure],
                "trace": state.get("trace", []) + [{"node": "reassess_download", "details": {"error": failure}}],
            }
        # 重新决策时继承上一轮拿到的命名信息：编号与正式名称是页面事实，
        # 不该因为换了动作就丢掉（实测重试后文号没有进入文件名）。
        previous = state.get("download_plan")
        if previous:
            carried: dict[str, str] = {}
            if not plan.matched_title and previous.matched_title:
                carried["matched_title"] = previous.matched_title
            if not plan.standard_number and previous.standard_number:
                carried["standard_number"] = previous.standard_number
            if carried:
                plan = plan.model_copy(update=carried)
        tried = set(state.get("tried_actions", []))
        if plan.action in tried:
            alternatives = [item for item in plan.fallback_actions if item not in tried]
            plan = plan.model_copy(update={"action": alternatives[0] if alternatives else "no_download", "downloadable": bool(alternatives), "reason": "模型重复选择已失败动作，已阻止重复尝试；无未尝试策略时停止"})
        plan = redirect_page_export(observation, plan, sorted(tried))
        return {"download_plan": plan, "fallback_index": 0, "trace": state.get("trace", []) + [{"node": "reassess_download", "details": _with_usage(plan, planner)}]}

    def after_reassess(state: AgentState) -> str:
        plan = state.get("download_plan")
        if plan is None:
            return _next_candidate_or_report(state)
        return "report" if plan.action == "no_download" else "download"

    graph = StateGraph(AgentState)
    nodes = {
        "parse_task": parse_task,
        "search": search,
        "rank": rank,
        "observe": observe,
        "retry_candidate": retry_candidate,
        "choose_download": choose_download,
        "check_lifecycle": check_lifecycle,
        "download": download,
        "reassess_download": reassess_download,
        "verify": verify,
        "quarantine": quarantine,
        "report": report,
    }
    for node_name, node_func in nodes.items():
        graph.add_node(node_name, _timed(node_name, node_func))
    graph.add_edge(START, "parse_task")
    graph.add_edge("parse_task", "search")
    graph.add_edge("search", "rank")
    # 候选池为 0 时回到 search 换一组检索词重试一次，否则照常观察候选。
    graph.add_conditional_edges(
        "rank",
        lambda state: "search" if state.get("research_needed") else "observe",
        {"search": "search", "observe": "observe"},
    )
    graph.add_conditional_edges("observe", after_observe, {"check_lifecycle": "check_lifecycle", "retry_candidate": "retry_candidate", "report": "report"})
    graph.add_edge("retry_candidate", "observe")
    graph.add_conditional_edges("choose_download", after_plan, {"download": "download", "report": "report", "retry_candidate": "retry_candidate"})
    graph.add_conditional_edges("check_lifecycle", lambda state: "report" if state.get("lifecycle_status") in {"withdrawn", "ambiguous"} else "choose_download", {"report": "report", "choose_download": "choose_download"})
    graph.add_conditional_edges("download", after_download, {"verify": "verify", "observe": "observe", "reassess_download": "reassess_download", "retry_candidate": "retry_candidate", "report": "report"})
    graph.add_conditional_edges("reassess_download", after_reassess, {"download": "download", "report": "report", "retry_candidate": "retry_candidate"})
    graph.add_conditional_edges("verify", after_verify, {"report": "report", "quarantine": "quarantine"})
    graph.add_conditional_edges("quarantine", after_quarantine, {"retry_candidate": "retry_candidate", "report": "report"})
    graph.add_edge("report", END)
    return graph.compile()


def run(requested_filename: str, searcher: SearchProvider, browser: PageBrowser, planner: DownloadPlanner | None = None, config: AgentConfig | None = None, chooser: Any | None = None):
    try:
        return build_graph(searcher, browser, planner, config, chooser).invoke({"requested_filename": requested_filename})
    finally:
        # 浏览器实例在整条流程中复用，结束时必须显式回收，否则会留下 Chromium 进程。
        for resource in (browser, searcher):
            closer = getattr(resource, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
