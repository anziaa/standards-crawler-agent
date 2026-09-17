from __future__ import annotations

import json
from typing import Any, Protocol

from .models import DownloadPlan


class StructuredChatModel(Protocol):
    def with_structured_output(self, schema: Any): ...


def _extract_usage(message: Any) -> dict[str, int] | None:
    """从模型响应里取出 token 用量，字段名统一为 input/output/total。"""
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


class LLMDownloadPlanner:
    """让模型只做页面动作决策，不直接执行任意代码或访问任意域名。"""

    def __init__(self, model: StructuredChatModel, max_payload_chars: int = 16_000) -> None:
        # include_raw 用于取回原始响应中的 token 用量；不加它用量信息会被丢掉。
        self.model = model.with_structured_output(DownloadPlan, include_raw=True)
        self.max_payload_chars = max_payload_chars
        self.last_usage: dict[str, int] | None = None

    def _serialize(self, observation: dict[str, Any]) -> str:
        """用紧凑 JSON 而不是 Python repr，并在必要时压缩正文，控制单次决策的提示词长度。"""
        payload = json.dumps(observation, ensure_ascii=False)
        if len(payload) <= self.max_payload_chars:
            return payload
        trimmed = dict(observation)
        text = trimmed.get("text", "")
        overflow = len(payload) - self.max_payload_chars
        trimmed["text"] = text[: max(500, len(text) - overflow)]
        return json.dumps(trimmed, ensure_ascii=False)

    def plan(self, requested_name: str, observation: dict[str, Any]) -> DownloadPlan:
        prompt = f"""
你是网页文件下载策略判断器。用户要找的文件名是：{requested_name}

下面是浏览器观察到的页面信息（links 已按“疑似目标文件/附件”排序，URL 只能作为候选，不得扩展到其他域名）：
{self._serialize(observation)}

请从以下动作中选择最安全、最可能成功的一个：
- download_direct：页面已有目标文件直链
- click_download：需要点击下载按钮或链接
- click_view_original：需要点击“查看原文/查看文件/查看详情”进入下一页面
- click_print：页面正文就是文档，需要通过打印/导出为 PDF
- save_page_pdf：页面正文就是目标内容，直接保存当前页面为 PDF
- inspect_embedded：文件嵌入 iframe/embed/object 中
- follow_attachment_page：需要进入附件列表或下一层详情页
- site_search：**在官方网站（域名含 .gov.cn）的首页或栏目页上找不到目标文件时，优先选它**。
  检索词写在 search_query：优先文号或标准号（例如“济建质安字〔2025〕1号”“GB 50204-2015”），
  其次填文件名称的关键部分（去掉“关于”“的通知”“进一步加强”等虚词）。
  非官方网站不要使用此动作。

**重要**：当页面上的链接只是栏目导航（“通知公告”“下载中心”“政策法规”“工作动态”这类），
不要靠逐个点击栏目、逐页翻列表去找文件——那条路要翻十几页，而且容易点错文件。
官方网站有站内搜索框时，一律先选 site_search。
- no_download：页面没有文件、附件或可合理导出的目标内容

输出 DownloadPlan。selector 必须是页面中可定位的文本或 CSS 选择器；如果动作不需要则留空。
**selector 优先级**：列表页/检索结果页请优先用 URL 片段定位，例如 `a[href*='art_40602_4786332']`——
这类页面的标题文字常被网站插入空格或换行（如“工 程 危 险 性 较 大”），用标题文本做选择器往往点不中；
只有在按钮/链接文字干净时才用 `text=` 定位。

另外两个字段用于给下载文件命名，请一并填写：
- matched_title：页面上识别到的**正式文件名称**（例如“混凝土结构工程施工质量验收规范”）。
  只写文件本身的名称，不要带站点名、栏目标题或“国家标准|”这类前缀；无法确认时留空。
  如果用户给的名称有笔误（漏字、简称、文种词写错），请填页面上的正式名称。
- standard_number：页面上明确写出的标准号或文号（例如“GB 50204-2015”“建质〔2018〕31号”）；
  页面上没有就留空，不要自行推断或补全。
links 中 has_file_extension 为 true 的条目优先考虑 download_direct。
**只能点击 buttons 列表中列出的控件**；正文文本里出现的按钮文字（例如正文里的“查看全文”）
不代表页面上存在可用控件——被剔除的隐藏控件会在 controls_note 里说明，请不要再选择它们。
页面上的“下载 / 下载标准 / 附件”类控件统一使用 click_download：这类按钮常通过 window.open
弹出新窗口再触发下载，执行器已经能够捕获这种情况。
如果页面正文完整呈现了目标文件内容，即使没有“下载”按钮，也可以选择 save_page_pdf；但这只会
生成网页导出 PDF（不是原始附件，结果状态会标记为 success_page_export），因此只要存在任何可行的
下载控件或附件链接，就应优先选择它们而不是 save_page_pdf。
reason 请控制在 100 字以内，并且**必须与你填写的 action 一致**——不要把与 action 不同的结论写进 reason。
download_direct 只能用于真正的**文件直链**（URL 以 .pdf/.doc/.docx/.xls/.xlsx/.zip 等结尾）；
页面本身的 URL 不是文件直链，正文就在网页上时应选 save_page_pdf 或 click_print。
页面观察里出现 direct_file 时，说明该地址就是文件直链（浏览器把导航转成了下载）：
它的文件名或来源与目标文件相符时选 download_direct，url 填 direct_file.url。
页面标题与请求名称只有文种词差异时（例如“技术规范 / 技术规程 / 标准 / 规程”“通知 / 公告 / 规定”），
视为同一份文件，可以正常选择下载动作，并在 reason 中写出实际匹配到的正式名称。
如果页面只有摘要、目录、搜索结果、登录页、验证码或明确没有目标内容，请选择 no_download，并在 reason 中说明原因。
**观察结果里出现 no_fulltext 字段时，必须选择 no_download**：该字段说明页面自己声明拿不到全文
（标准平台标注"暂不提供在线阅读服务"、需到馆阅读、文库付费阅读器、扫码登录墙等）。
这类页面导出成 PDF 只会得到网站页面而不是目标文件，因此不要选 save_page_pdf 或 click_print；
请把 no_fulltext.evidence 里的说明写进 reason。
**观察结果里出现 reading_entry 字段时，要选 click_download**，selector **只能用
reading_entry.entries 里列出的文字**：它表示本页只是著录/详情页，正文在入口后面。
这类入口包括「在线阅读」「在线预览」「下载标准」「查看文本」——**它们都算下载入口**，
不要因为字面上没有"下载"二字就忽略它们。
但 **entries 为空时一律不要点**：那说明本页找不到可点击的阅读入口，页面只是提到过这些字样
（提示文案或脚本文本）。`reading_entry.note` 会列明哪些字样**不是**可点控件，一律不要用。
**selector 必须来自 buttons 或 links 里真实列出的控件**：正文文本里出现的按钮文字
不代表页面上存在可用控件——实测照着正文里的"在线预览"去点，白等 8 秒定位超时，
整条任务就此断掉（被剔除的隐藏控件会在 controls_note 里说明）。
特别注意：「仅提供在线阅读服务」表示正文可以在线读到、只是不给下载，
**不是**"没有全文"，不要据此选 no_download。
点击这类入口常常会**弹出新窗口**（实测国家标准平台的『查看文本』会打开新的全文页，
那一页才有『下载标准』按钮）。弹出新窗口后系统会**自动重新观察新页面**，
你不需要、也不要在旧页面上重复点击同一个控件——重复动作会被系统拦下。
判断"页面正文是否就是目标内容"时，看的是**正文结构**（条款、章节、目次、前言、附录），
不是页面标题：标准著录页/详情页也会完整印着标准号和中文标准名称，但那不是文件正文。
不要编写 JavaScript，不要建议绕过验证码、登录或访问控制。
"""
        result = self.model.invoke(prompt)
        self.last_usage = _extract_usage(result.get("raw"))
        if result.get("parsing_error") is not None:
            raise RuntimeError(f"模型输出无法解析为 DownloadPlan：{result['parsing_error']}")
        parsed = result.get("parsed")
        if parsed is None:
            raise RuntimeError("模型未返回可用的 DownloadPlan")
        return parsed
