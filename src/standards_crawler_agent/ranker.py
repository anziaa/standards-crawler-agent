"""候选选择：让一个小模型在**已经过确定性筛选的候选池**里挑一个先观察。

为什么要它：确定性打分只能测字面，测不出"这是不是目标文件"。实测它表达不了的两类判断：

- r19 #4：深圳市转载页只因**摘要里逐字出现完整名称**就拿到满页相关度排到第 1（而且该页
  证书失效），而中国政府网上那件官方 `.docx` 附件直链原本连候选池都没进；
- r20 #4：黑龙江省住建厅页的附件是「…（2024版）宣传画册.pdf」，模型照字段点了它，
  拿到 79 页宣传画册还被记为 `success_scanned`——字面分再准也读不出"附件不是正文"。

**分工（一个决策只有一个 owner）**：`ranking.rank_pages` 负责把几十条搜索结果压成候选池、
并给出默认顺序（它同时是预过滤与兜底顺序）；本模块只负责"池子里先看哪一个"。模型调用失败、
超时、返回越界下标时，**保留确定性顺序**，不做第二次猜测。
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from .models import CandidateChoice, CandidatePage
from .ranking import OFFICIAL_STANDARD_DOMAINS, combined_score, is_central_gov_domain

# 池子里只有 1 个候选时没有可选项，直接跳过调用（省一次 token 与 2~5 秒）。
CHOICE_SKIP_THRESHOLD = 2

CHOOSER_PROMPT = """你是政府文件检索的候选页挑选器。用户要找的文件名称是：
{requested_name}

下面是搜索引擎返回、并已按"相关度 × 来源权威度"过滤后的候选页面（最多 5 条）。
每条包含：下标 idx、域名 domain、页面标题 title、搜索引擎摘要 snippet、URL、
is_document_url（URL 是否直接指向 .pdf/.docx 等文件）、official_score（来源权威分，0~1）、
page_score（标题/摘要与请求名的字面相关度，0~1）、combined_score（上面两个分的平均，即默认排序依据）。
四个分数**都是程序算好的**，不要自己估；它们的口径见下面的说明。

{candidates}

请选出**最可能让流程拿到这份文件官方原件**的那一条，返回它的 idx：

- `source_tier` 是程序已经算好的来源级别，**不要自己去判断域名**：
  `中央/部委`、`官方标准平台`、`地方政府`、`其他`。级别越高越可信。
- **`其他`（非政府、非官方标准平台）域名上的 .pdf 不得优先于政府页面**：那类站点只是转载，
  而我们真正要的是官方原件；政府页面即使本身是网页，也往往带着官方附件。
  只有整个候选池都是 `其他` 时才可以考虑它们。
- **"URL 以 .pdf 结尾"本身不是权威证据**（实测最常见的错误改选就是这个）：内容站/文库站的
  直链可能是扫描版、删减版、预览页，或者干脆是**另一份文件**。要选 `其他` 域名，理由里必须写出
  **页面证据**（正文或附件与请求名逐字一致、含目标文号/标准号、能看出是全文），
  只在理由里写"URL 是 .pdf 直链""标题完全匹配"是不够的——那是排序公式已经算过的字面信息。
- `official_score` 的典型值：政府站/官方标准平台 0.75~0.95，非政府内容站约 0.20。
  两个候选 `page_score` 接近时，这是区分"能不能拿到原件"的主要依据。
- 你负责判断的是**页面性质**（这一栏程序判断不了）：
  网站首页或栏目页（"通知公告""政策法规"这类目录）、只是资讯/问答/百科/文库的页面、
  页面上只有宣传画册·解读·课件之类**非正文附件**的页面、以及明显是**另一份文件**的页面——都要避开。
- 优先：官方发布页（正文带附件下载链接，能拿到 docx/pdf 原件）、
  `is_document_url` 为 true 的政府站/官方标准平台直链、正文完整呈现全文的政府页面。
- 如果所有候选都不像目标文件本身，返回 -1。

只返回 idx 与简短理由；不要输出 URL。理由里写清你依据页面上的哪些信息做的判断。"""


class CandidateChooser(Protocol):
    """候选选择器接口。graph 只依赖它，离线测试可以直接注入假实现。"""

    def choose(self, requested_name: str, candidates: list[CandidatePage]) -> int | None: ...


def source_tier(domain: str) -> str:
    """来源级别：**由程序判定，不让模型判断域名**。

    实测教训：把域名权威交给模型时，它会把 `jnera.cn`、`cnass.cn`、`tunnelling.cn`
    说成"官方标准平台"，并据此把部委页面换掉——5 次错误改选里有 4 次是这么来的。
    """
    value = (domain or "").lower()
    if is_central_gov_domain(value):
        return "中央/部委"
    if any(value.endswith(item) or item in value for item in OFFICIAL_STANDARD_DOMAINS):
        return "官方标准平台"
    if ".gov.cn" in value:
        return "地方政府"
    return "其他"


def _candidate_payload(index: int, page: CandidatePage) -> dict[str, Any]:
    return {
        "idx": index,
        "domain": page.domain,
        "source_tier": source_tier(page.domain),
        "title": page.title,
        "snippet": page.text[:300],
        "url": page.url,
        # 直接指向文件的 URL 是最强信号，单独标出来，别让模型自己去猜扩展名。
        "is_document_url": page.url.lower().split("?", 1)[0].endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")),
        # 这两个分**必须真的发出去**：提示词从第一版起就写着"每条包含 official_score（来源权威分）"，
        # 而这里一直没有这个字段——模型被承诺了一个拿不到的数字，于是只能自己猜来源可信度，
        # 实测它就把"URL 以 .pdf 结尾"当成了权威证据（见下面 CHOOSER_PROMPT 的说明）。
        "official_score": round(page.official_score, 2),
        "page_score": round(page.page_score, 2),
        "combined_score": round(combined_score(page), 2),
    }


class LLMCandidateChooser:
    """用一个小模型（默认 qwen-turbo）从候选池里挑一个下标。

    输出**只有下标**，由调用方对着候选池校验；越界、解析失败、调用异常都返回 None
    （= 采用确定性顺序）。
    """

    def __init__(self, model: Any, max_candidates: int = 5) -> None:
        # include_raw 用来取 token 用量，否则用量信息会被丢掉（与 planner 一致）。
        self.model = model.with_structured_output(CandidateChoice, include_raw=True)
        self.max_candidates = max_candidates
        self.last_usage: dict[str, int] | None = None
        # 供 trace 记录：模型为什么这么选（不参与任何判断）。
        self.last_reason: str = ""

    def choose(self, requested_name: str, candidates: list[CandidatePage]) -> int | None:
        self.last_usage = None
        self.last_reason = ""
        if len(candidates) < CHOICE_SKIP_THRESHOLD:
            return None
        shortlist = candidates[: self.max_candidates]
        payload = [_candidate_payload(index, page) for index, page in enumerate(shortlist)]
        prompt = CHOOSER_PROMPT.format(
            requested_name=requested_name,
            candidates=json.dumps(payload, ensure_ascii=False, indent=1),
        )
        raw = self.model.invoke(prompt)
        message = raw.get("raw") if isinstance(raw, dict) else raw
        choice = raw.get("parsed") if isinstance(raw, dict) else raw
        self.last_usage = _extract_usage(message)
        if choice is None or not isinstance(choice, CandidateChoice):
            return None
        self.last_reason = choice.reason or ""
        index = int(choice.index)
        # -1 = 明确表示"都不像"，按确定性顺序继续（照旧走完候选池），不在这里收工。
        if index < 0 or index >= len(shortlist):
            return None
        return index


def _extract_usage(message: Any) -> dict[str, int] | None:
    usage = getattr(message, "usage_metadata", None) or {}
    if usage:
        return {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
    token_usage = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    if token_usage:
        return {
            "input_tokens": int(token_usage.get("prompt_tokens") or 0),
            "output_tokens": int(token_usage.get("completion_tokens") or 0),
            "total_tokens": int(token_usage.get("total_tokens") or 0),
        }
    return None
