from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    domain: str = ""


class CandidatePage(BaseModel):
    url: str
    title: str = ""
    text: str = ""
    domain: str = ""
    official_score: float = 0.0
    page_score: float = 0.0
    evidence: list[str] = Field(default_factory=list)


ActionName = Literal[
    "download_direct",
    "click_download",
    "click_view_original",
    "click_print",
    "save_page_pdf",
    "inspect_embedded",
    "follow_attachment_page",
    "site_search",
    "no_download",
]


class DownloadPlan(BaseModel):
    # 字段顺序即结构化输出里的属性顺序：先让模型定下 action 与命名信息，
    # 最后才写 reason（实测出现过 reason 结论与 action 字段互相矛盾的情况）。
    action: ActionName
    selector: str | None = None
    url: str | None = None
    # 页面上识别到的正式文件名称，用于给下载文件命名；无法确认时留空
    matched_title: str | None = None
    # 页面上出现的标准号/文号，例如 GB 50204-2015、建质〔2018〕31号
    standard_number: str | None = None
    # site_search 的检索词（优先文号/标准号，其次文件名称的关键部分）
    search_query: str | None = None
    confidence: float = 0.0
    downloadable: bool = True
    requires_user_action: bool = False
    fallback_actions: list[ActionName] = Field(default_factory=list)
    reason: str = ""


class CandidateChoice(BaseModel):
    """候选选择器的输出。

    **只允许给下标**：让模型回传 URL 会引入"编链接"的风险，而下标能直接对着候选池校验。
    """
    index: int = Field(description="候选列表里的下标（从 0 开始）；没有一个像目标文件时填 -1")
    reason: str = Field(default="", description="为什么选它（或为什么都不选），100 字以内")


class Verification(BaseModel):
    ok: bool
    mime_type: str = ""
    size: int = 0
    title_matches: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 致命的校验问题：任一条存在即视为未通过。
    warnings: list[str] = Field(default_factory=list)
    # 非致命的观察：例如"扫描件没有文本层，无法比对"。原始内容是存在的，
    # 因此不影响 ok，但要如实告知，让人知道这份产物的可信度打了折扣。
    notes: list[str] = Field(default_factory=list)


class AgentState(TypedDict, total=False):
    requested_filename: str
    normalized_name: str
    filename_tokens: list[str]
    search_queries: list[str]
    search_results: list[SearchResult]
    # 下面三个键必须在这里声明：AgentState 是 TypedDict，没声明的键会被 LangGraph
    # 静默丢弃，节点里读到的永远是默认值（曾因此让 rank 与 search 无限互跳）。
    search_rounds: int
    research_needed: bool
    # 本轮检索是否出现"引擎给了降级页/读不到结果"（而非确实没有页面）。
    # 这类任务值得重跑：实测同一条查询前后两次的结果可以完全不同。
    search_degraded: bool
    # 搜索引擎对这条查询没有匹配（头名词兜底页）——重读无效，只能换入口。
    search_unmatched: bool
    # 候选池为 0 时启用的"官方入口"（官网/平台首页 + 站内检索）及其原因
    entry_reason: str
    entry_attempted: bool
    official_fallback: bool
    candidate_pages: list[CandidatePage]
    selected_page: CandidatePage | None
    page_observation: dict[str, Any]
    current_url: str | None
    download_plan: DownloadPlan | None
    downloaded_path: str | None
    verification: Verification | None
    errors: list[str]
    attempts: int
    max_attempts: int
    candidate_index: int
    fallback_index: int
    tried_actions: list[str]
    last_download_error: str | None
    needs_confirmation: bool
    final: dict[str, Any]
    trace: list[dict[str, Any]]
    lifecycle_status: Literal["unknown", "active", "withdrawn", "ambiguous"]
    skip_reason: str | None


@dataclass
class AgentConfig:
    max_search_results: int = 50
    max_search_queries: int = 3
    # 候选页最低可信度（0.5*官方分 + 0.5*页面分）；低于此值不进入观察环节
    min_candidate_score: float = 0.35
    # 观察结果正文短于此值且没有任何链接时，视为空白页/观察失败
    min_observation_text_chars: int = 50
    max_candidate_domains: int = 5
    max_page_attempts: int = 5
    max_download_bytes: int = 100 * 1024 * 1024
    allowed_domains: set[str] = field(default_factory=set)
    download_dir: str = "downloads"
