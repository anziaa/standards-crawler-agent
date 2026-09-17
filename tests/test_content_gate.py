# -*- coding: utf-8 -*-
"""离线断言：产物里到底有没有目标文件正文。

不触网。用法：
    python tests/test_content_gate.py

覆盖：
  A. content.py 的判据本身（正例/反例）
  B. 30 个实测空壳件必须被拒（含 body_absent 标记）
  C. 合法产物必须照常通过：官方原件、官网 HTML 正文的网页导出、扫描件
  D. 扫描件只标记不判失败
  E. 第三方文库域名硬拒绝
  F. 观察结果里暴露 no_fulltext，模型据此判 no_download
  G. 新状态在汇总层有定义、会被标为需人工复核
  M. 候选排序：政府站的行政层级档位、路径词、权威站文档直链（用两条真实 SERP 当夹具）
  T. 浏览器有头/无头：BROWSER_HEADLESS 的取值表与"显式传参优先"
  U. 选择器字段与提示词一致、黑名单不误杀、兜底页换路重试（假页面，不触网）
  V. 有头浏览器的 PDF 内联渲染，必须与无头下的"下载"得到同一个结论
  W. 有头时显式无痕启动（--incognito）、无头时行为不变
  X. 同名产物：内容相同则跳过重写、内容不同则新版盖旧版
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from standards_crawler_agent import ranking  # noqa: E402
from standards_crawler_agent.content import (  # noqa: E402
    detect_absent_body,
    detect_no_fulltext_statement,
    detect_scanned_only,
    document_body_evidence,
    online_reading_only,
    page_without_fulltext,
    prefer_reading_entry,
    reading_entries,
)
from standards_crawler_agent.download import verify_file  # noqa: E402
from standards_crawler_agent.models import AgentConfig, SearchResult  # noqa: E402
from standards_crawler_agent.ranking import is_low_trust_domain  # noqa: E402
from standards_crawler_agent.report import STATUS_FILL, STATUS_LABEL, review_flag  # noqa: E402

DL = ROOT / "downloads"
REJECTED = DL / "_rejected"
HERE = Path(__file__).resolve().parent

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
    else:
        FAILED.append(f"{name}{'  → ' + detail if detail else ''}")


def locate(filename: str) -> Path | None:
    """空壳件清理后会被移进 _rejected/，两个位置都找。"""
    for candidate in (DL / filename, REJECTED / filename):
        if candidate.exists():
            return candidate
    return None


def locate_shell(filename: str) -> Path | None:
    """**空壳件固定夹具**必须优先在 _rejected/ 里找。

    known_shells.json 记的是"被隔离的空壳产物"。这些名字后来被批量重跑出来的
    **正确文件**占用了（同名落在 downloads/），如果先找 downloads/，夹具就会
    指向新下的正确文件，于是"空壳必须被拒"的断言会莫名其妙地失败
    （实测 9 项这样误报过——也就是说那 9 个任务已经有正确文件了）。
    """
    for candidate in (REJECTED / filename, DL / filename):
        if candidate.exists():
            return candidate
    return None


def verified(filename: str, finder=None) -> dict:
    """按文件名推出请求名后跑一次校验（与 tests/verify_snapshot.py 同一套推导）。"""
    import re

    path = (finder or locate)(filename)
    if path is None:
        raise FileNotFoundError(filename)
    stem = Path(filename).stem
    stem = re.sub(r"_[A-Za-z]{1,8}(?:／|/)?[A-Za-z]{0,4}\s*\d[\d.]*\s*[-—]\s*\d{4}$", "", stem)
    stem = re.sub(r"_[^_]*令\s*第?\s*[\d〇一二三四五六七八九十]+号$", "", stem)
    result = verify_file(str(path), stem or filename)
    return {
        "ok": bool(result.ok),
        "body_absent": result.metadata.get("body_absent"),
        "scan_only": result.metadata.get("scan_only"),
        "warnings": list(result.warnings),
        "notes": list(result.notes),
    }


# ---------------------------------------------------------------- A. 判据本身
print("=== A. content.py 判据 ===")

STD_SAMR_TEXT = (
    "标准号： GB/T 8923.1-2011  中文标准名称：涂覆涂料前钢材表面处理  表面清洁度的目视评定  "
    "英文标准名称： Preparation of steel substrates  标准状态： 现行  实施信息反馈  "
    "该标准采用了 ISO 、 IEC 等国际国外组织的标准，由于涉及版权保护问题，本系统暂不提供在线阅读服务。 "
    "发布日期 2011-12-30  实施日期 2012-10-01  主管部门 中国石油和化学工业联合会"
)
check("标准平台著录页被识别为无正文", bool(detect_absent_body(STD_SAMR_TEXT, 2)))
check(
    "识别到『暂不提供在线阅读服务』",
    detect_no_fulltext_statement(STD_SAMR_TEXT) is not None,
    str(detect_no_fulltext_statement(STD_SAMR_TEXT)),
)

DOC88_SHELL = "还剩 230 页未读，是否继续阅读？请拖动滑块继续阅读 不看了，直接下载 上传于：2023-03-24 粉丝量：13 下载积分：1800"
check("文库付费外壳被识别为无正文", bool(detect_absent_body(DOC88_SHELL, 2)))

NDLS_TEXT = "现行 GB 50909-2014 到馆阅读 收藏跟踪 城市轨道交通结构抗震设计规范 发布⽇期：2014-03-31 登录 / 注册 购买正版 相似标准推荐"
check("『到馆阅读』被识别为无正文", bool(detect_absent_body(NDLS_TEXT, 2)))

LAW_TEXT = (
    "中华人民共和国突发事件应对法\n目　次\n第一章 总　则\n"
    + "\n".join(f"第{n}条　为了……" for n in "一二三四五六七八九十")
    + "\n第二章 预防与应急准备\n前言\n"
)
check("真实法条正文有正文结构", "条款" in document_body_evidence(LAW_TEXT))
check("真实法条正文不会被判无正文", detect_absent_body(LAW_TEXT, 18) is None)

NOTICE_TEXT = (
    "住房城乡建设部办公厅关于实施《危险性较大的分部分项工程安全管理规定》有关问题的通知\n"
    "一、关于危大工程范围\n二、关于专项施工方案内容\n三、关于专家论证会参会人员\n"
    "四、关于专家论证内容\n五、关于专项施工方案修改\n"
)
check("政务『一、二、三』条目算正文结构", "编号条目" in document_body_evidence(NOTICE_TEXT))
check("政务通知不会被判无正文", detect_absent_body(NOTICE_TEXT, 8) is None)

GOV_FOOTER = (
    "山东省高层建筑消防安全管理规定\n" + "\n".join(f"第{n}条　……" for n in "一二三四五六七八九十")
    + "\n相关推荐\n技术支持：某某有限公司\n版权所有 山东省人民政府"
)
check("页脚有『有限公司』不会误伤有正文的政府页", detect_absent_body(GOV_FOOTER, 8) is None)

check("登录墙页被识别", bool(detect_absent_body("使用微信扫一扫登录「电子标准网」", 1)))
check(
    "扫码登录也是登录墙证据",
    page_without_fulltext("使用微信扫一扫登录「电子标准网」") is not None,
)

# ---------------------------------------------------------------- B. 空壳件
print("=== B. 实测空壳件必须被拒 ===")
shells = json.loads((HERE / "known_shells.json").read_text(encoding="utf-8"))
shell_total = 0
for group, names in shells.items():
    if group.startswith("_"):
        continue
    for name in names:
        shell_total += 1
        if locate_shell(name) is None:
            check(f"[{group}] {name} 存在", False, "文件既不在 downloads/ 也不在 downloads/_rejected/")
            continue
        info = verified(name, locate_shell)
        check(
            f"[{group}] 拒收 {name[:46]}",
            info["body_absent"] == "true" and info["ok"] is False,
            f"body_absent={info['body_absent']} ok={info['ok']}",
        )
check("空壳件清单共 30 个", shell_total == 30, f"实际 {shell_total}")

# ---------------------------------------------------------------- C. 合法产物
print("=== C. 合法产物必须照常通过 ===")
# 官网 HTML 正文导出的法律法规：这是被认可的合法形态，不能被新判据误杀。
LEGIT_PAGE_EXPORTS = [
    "中华人民共和国突发事件应对法.pdf",
    "生产安全事故应急条例_国务院令 第708号.pdf",
    "山东省房屋建筑和市政工程质量监督管理办法_山东省人民政府令第308号.pdf",
    "山东省高层建筑消防安全管理规定_山东省人民政府令第285号.pdf",
    "建设工程质量管理条例_国务院令第279号.pdf",
    "建设工程施工现场管理规定_建设部令第15号.pdf",
    "房屋建筑工程和市政基础设施工程实行见证取样和送检的规定_建建〔2000〕211号.pdf",
    "关于实施《危险性较大的分部分项工程安全管理规定》有关问题的通知.pdf",
    "实施工程建设强制性标准监督规定.pdf",
    "中华人民共和国大气污染防治法.pdf",
    "山东省扬尘污染防治管理办法_山东省人民政府令第248号.pdf",
    "电力设施保护条例.pdf",
]
for name in LEGIT_PAGE_EXPORTS:
    if locate(name) is None:
        check(f"合法网页导出存在 {name}", False, "文件不在 downloads/")
        continue
    info = verified(name)
    check(
        f"合法网页导出仍通过 {name[:40]}",
        info["body_absent"] is None and info["ok"] is True,
        f"body_absent={info['body_absent']} ok={info['ok']} {info['warnings']}",
    )

LEGIT_ORIGINALS = [
    "房屋市政工程生产安全重大事故隐患判定标准（2024版）_建质规〔2024〕5号.docx",
    "关于印发《危险性较大的分部分项工程专项施工方案编制指南》的通知.docx",
]
for name in LEGIT_ORIGINALS:
    if locate(name) is None:
        check(f"官方原件存在 {name}", False, "文件不在 downloads/")
        continue
    info = verified(name)
    check(f"官方原件仍通过 {name[:40]}", info["ok"] is True, f"{info['warnings']}")

# ---------------------------------------------------------------- D. 扫描件
print("=== D. 扫描件只标记不判失败 ===")
SCANNED = [
    "1kV及以下配线工程施工与验收规范.pdf",
    "火灾自动报警系统施工及验收标准_GB 50166-2019.pdf",
    "坠落防护 安全网.pdf",
    "泡沫灭火系统技术标准_GB50151-2021.pdf",
    "重型结构和设备整体提升技术规范_GB51162-2016.pdf",
    "公路工程质量检验评定标准 第一册 土建工程_JTG F80／1-2017.pdf",
]
# 注意：《混凝土结构工程施工质量验收规范_GB50204-2015.pdf》虽然也是扫描件，
# 但带部分 OCR 文本层（75 页里能抽出约 6000 字），文本比对仍能跑，
# 因此**故意**不标 scan_only——把阈值放宽会让正常带图的标准 pdf 误报。
for name in SCANNED:
    if locate(name) is None:
        # 不静默跳过：文件名写错会让整段断言凭空消失（本仓库真踩过一次）。
        check(f"扫描件存在 {name}", False, "文件不在 downloads/ 也不在 _rejected/")
        continue
    info = verified(name)
    check(
        f"扫描件标记 scan_only {name[:38]}",
        info["scan_only"] == "true",
        f"scan_only={info['scan_only']} pages-warning={info['warnings']}",
    )
    check(
        f"扫描件不判失败 {name[:38]}",
        info["ok"] is True,
        f"ok={info['ok']} {info['warnings']}",
    )
    check(
        f"扫描件说明进 notes 而非 warnings {name[:38]}",
        any("扫描件" in item for item in info["notes"]),
        f"notes={info['notes']}",
    )

# 扫描件判据本身
check(
    "页数够多+无文本层+有图像 = 扫描件",
    detect_scanned_only(40, "", 40) is not None,
)
check("2 页空壳不算扫描件", detect_scanned_only(2, "", 2) is None)
check("有文本层不算扫描件", detect_scanned_only(40, "第1条 " * 200, 40) is None)
check(
    "带部分 OCR 文本层的扫描件不标 scan_only（避免误报）",
    detect_scanned_only(75, "x" * 6000, 75) is None,
)

# ---------------------------------------------------------------- E. 域名
print("=== E. 第三方文库域名硬拒绝 ===")
for domain in ("www.doc88.com", "wenku.baidu.com", "www.docin.com", "max.book118.com", "www.renrendoc.com"):
    check(f"拒绝 {domain}", is_low_trust_domain(domain))
for domain in ("www.gov.cn", "std.samr.gov.cn", "zjt.shandong.gov.cn", "openstd.samr.gov.cn"):
    check(f"放行 {domain}", not is_low_trust_domain(domain))

# ---------------------------------------------------------------- F. 观察层
print("=== F. 观察结果暴露 no_fulltext ===")
from standards_crawler_agent.browser import summarize_html  # noqa: E402

samr_html = (
    "<html><head><title>国家标准|GB/T 8923.1-2011</title></head><body><div id='content'>"
    "<p>标准号： GB/T 8923.1-2011</p><p>中文标准名称：涂覆涂料前钢材表面处理</p>"
    "<p>标准状态： 现行</p>"
    "<p>该标准采用了 ISO、IEC 等国际国外组织的标准，由于涉及版权保护问题，本系统暂不提供在线阅读服务。</p>"
    "</div></body></html>"
)
summary = summarize_html(samr_html, "https://std.samr.gov.cn/gb/search/gbDetailed?id=X", "国家标准|GB/T 8923.1-2011")
check("标准平台页面观察带 no_fulltext", bool(summary.get("no_fulltext")), str(summary.get("no_fulltext")))
check(
    "no_fulltext 里给出可写进 reason 的证据",
    bool((summary.get("no_fulltext") or {}).get("evidence")),
)

law_html = (
    "<html><head><title>中华人民共和国突发事件应对法</title></head><body><div id='content'>"
    + "".join(f"<p>第{n}条　正文内容……</p>" for n in "一二三四五六七八九十")
    + "</div></body></html>"
)
law_summary = summarize_html(law_html, "https://www.gov.cn/yaowen/liebiao/x.htm", "中华人民共和国突发事件应对法")
check("有正文的法律页面不带 no_fulltext", law_summary.get("no_fulltext") is None, str(law_summary.get("no_fulltext")))

planner_src = (ROOT / "src" / "standards_crawler_agent" / "llm_planner.py").read_text(encoding="utf-8")
check("提示词要求 no_fulltext 时选 no_download", "no_fulltext" in planner_src and "必须选择 no_download" in planner_src)
check("提示词说明看正文结构而非标题", "正文结构" in planner_src)

# ---------------------------------------------------------------- G. 汇总层
print("=== G. 新状态在汇总层有定义 ===")
check("STATUS_LABEL 定义 success_scanned", "success_scanned" in STATUS_LABEL)
check("STATUS_LABEL 说明扫描件无文本层", "扫描件" in STATUS_LABEL.get("success_scanned", ""))
check("STATUS_FILL 定义 success_scanned", "success_scanned" in STATUS_FILL)
flagged, reason = review_flag("success_scanned", [], ["扫描件：37 页均无可提取文本层"], [], "", False, True)
check("扫描件被标为需人工复核", flagged and "扫描件" in reason, reason)
flagged2, reason2 = review_flag("not_downloadable", ["产物中没有文件正文：页面只有标准著录信息"], [], [], "", True, True)
check(
    "无正文的产物被标为需人工复核且写明原因",
    flagged2 and "没有文件正文" in reason2,
    reason2,
)

graph_src = (ROOT / "src" / "standards_crawler_agent" / "graph.py").read_text(encoding="utf-8")
check("graph 对 body_absent 判 not_downloadable", "body_absent" in graph_src and "not_downloadable" in graph_src)
check("graph 对扫描件给 success_scanned", "success_scanned" in graph_src)
check("graph 记录 source_kind", "source_kind" in graph_src)
check("graph 在候选阶段排除低信任域名", "is_low_trust_domain" in graph_src)

batch_src = (ROOT / "src" / "standards_crawler_agent" / "batch.py").read_text(encoding="utf-8")
check("批量汇总单列扫描件", "success_scanned" in batch_src and "扫描件" in batch_src)

# ---------------------------------------------------------------- H. 端到端状态映射
print("=== H. 端到端：状态映射（假浏览器/假检索/假决策，不触网）===")
import shutil  # noqa: E402
import tempfile  # noqa: E402

from standards_crawler_agent.graph import build_graph  # noqa: E402
from standards_crawler_agent.models import AgentConfig, DownloadPlan, SearchResult  # noqa: E402


class FakeSearcher:
    def __init__(self, url: str, title: str) -> None:
        self.url = url
        self.title = title
        self.last_read = {"verdict": "ok", "attempts": [1]}

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return [SearchResult(title=self.title, url=self.url, snippet=self.title, domain="www.gov.cn")]

    def close(self) -> None:
        pass


class FakeBrowser:
    """直接返回预先放好的产物，模拟一次"页面导出"或"直链下载"的结果。"""

    def __init__(self, artifact: str) -> None:
        self.artifact = artifact
        self.last_url = None

    def observe(self, url: str) -> dict:
        return {
            "url": url,
            "title": "测试页面",
            "text": "测试页面正文 " * 40,
            "headings": [],
            "links": [{"text": "下载", "url": "https://www.gov.cn/a.pdf", "has_file_extension": True}],
            "buttons": [],
            "embedded": [],
            "has_print_hint": False,
            "content_type": "html",
            "no_fulltext": None,
            "stats": {"html_chars": 4000, "links_total": 3, "links_kept": 3, "buttons_total": 0, "text_total_chars": 400, "text_kept_chars": 400},
        }

    def execute(self, url: str, plan: DownloadPlan, output_dir: str) -> str:
        return self.artifact

    def close(self) -> None:
        pass


class FakePlanner:
    def __init__(self, action: str) -> None:
        self.action = action
        self.last_usage = None

    def plan(self, requested_name: str, observation: dict) -> DownloadPlan:
        return DownloadPlan(action=self.action, confidence=0.9, reason="测试", matched_title=requested_name)


def run_scenario(source_file: str, requested: str, action: str) -> str:
    """把 source_file 复制到临时下载目录后跑完整图，返回最终状态。

    复制是必须的：校验失败时图会走 quarantine 把产物移走，不能让它动真实文件。
    """
    workdir = Path(tempfile.mkdtemp(prefix="gate_"))
    try:
        artifact = workdir / Path(source_file).name
        shutil.copy2(locate(source_file) or (DL / source_file), artifact)
        config = AgentConfig(download_dir=str(workdir))
        graph = build_graph(
            FakeSearcher("https://www.gov.cn/zhengce/x.htm", requested),
            FakeBrowser(str(artifact)),
            FakePlanner(action),
            config,
        )
        return str(graph.invoke({"requested_filename": requested})["final"].get("status"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


status = run_scenario("中华人民共和国突发事件应对法.pdf", "中华人民共和国突发事件应对法", "save_page_pdf")
check("官网 HTML 正文的网页导出 → success_page_export", status == "success_page_export", status)

status = run_scenario(
    "涂覆涂料前钢材表面处理 表面清洁度的目视评定 第1部分：未涂覆过的钢材表面和全面清除原有涂层后的钢材表面的锈蚀等级和处理等级_GB／T 8923.1-2011.pdf",
    "涂覆涂料前钢材表面处理表面清洁度的目视评定",
    "save_page_pdf",
)
check("空壳网页导出 → not_downloadable（不再冒充成功）", status == "not_downloadable", status)

status = run_scenario("1kV及以下配线工程施工与验收规范.pdf", "1kV及以下配线工程施工与验收规范", "download_direct")
check("扫描件 → success_scanned（单列，不算原件）", status == "success_scanned", status)

status = run_scenario(
    "房屋市政工程生产安全重大事故隐患判定标准（2024版）_建质规〔2024〕5号.docx",
    "房屋市政工程生产安全重大事故隐患判定标准（2024版）",
    "download_direct",
)
check("真实原件 → success", status == "success", status)


# ---------------------------------------------------------------- I. 阅读入口与弹窗
print("=== I. 『查看文本』弹窗必须重新观察（GB/T 8923.1-2011 的实际死因）===")

RECORD_PAGE = "https://std.samr.gov.cn/gb/search/gbDetailed?mode=p&id=ABC"
READER_PAGE = "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=6BC8D52A103AFEE95AC318BEB5DC4EC8"

# 判据层：『仅提供在线阅读服务』不是"没有全文"，而是"正文在阅读页里"。
ONLINE_ONLY_TEXT = (
    "标准号： GB 24543-2009  中文标准名称：坠落防护  安全绳  标准状态： 现行  "
    "该标准采用了 ISO 、 IEC 等国际国外组织的标准，由于涉及版权保护问题，本系统仅提供在线阅读服务。 "
    "在线阅读  在线预览"
)
check(
    "『仅提供在线阅读服务』不算『没有全文』",
    detect_no_fulltext_statement(ONLINE_ONLY_TEXT) is None,
    str(detect_no_fulltext_statement(ONLINE_ONLY_TEXT)),
)
check("识别为『只能在线读』", online_reading_only(ONLINE_ONLY_TEXT) is not None)
check("暂不提供在线阅读服务仍算没有全文", detect_no_fulltext_statement(STD_SAMR_TEXT) is not None)
check(
    "『在线阅读/在线预览』被看作通往正文的入口",
    set(reading_entries(ONLINE_ONLY_TEXT)) >= {"在线阅读", "在线预览"},
    str(reading_entries(ONLINE_ONLY_TEXT)),
)
check(
    "『查看文本』也算入口",
    "查看文本" in reading_entries("左侧有 查看文本 按钮，右侧有 下载标准"),
)
safety_rope = verified("坠落防护 安全绳_GB 24543-2009.pdf")
check(
    "安全绳那件：产物仍无正文，但理由改成『有入口，应点进阅读页』",
    safety_rope["body_absent"] == "true"
    and any("通往正文的入口" in w for w in safety_rope["warnings"]),
    str(safety_rope["warnings"]),
)


class PopupBrowser:
    """模拟"点『查看文本』弹出新窗口、新窗口里才有下载入口"的真实站点。

    这正是 std.samr.gov.cn 的行为：记录页 → 点『查看文本』→ 弹出
    openstd.samr.gov.cn 的全文页 → 那一页点『下载标准』才拿到 PDF。
    """

    def __init__(self, artifact: str) -> None:
        self.artifact = artifact
        self.last_url = None
        self.observations: list[str] = []

    def observe(self, url: str) -> dict:
        self.observations.append(url)
        on_reader = url == READER_PAGE
        return {
            "url": url,
            "title": "全文页" if on_reader else "标准详情页",
            "text": "阅读页正文 " * 40 if on_reader else "标准号： GB 8923.1-2011 " * 20,
            "headings": [],
            "links": ([{"text": "下载标准", "url": f"{READER_PAGE}/dl.pdf", "has_file_extension": True}]
                      if on_reader else []),
            "buttons": [{"text": "查看文本", "tag": "div"}],
            "embedded": [],
            "has_print_hint": False,
            "content_type": "html",
            "no_fulltext": None,
            "reading_entry": {"entries": ["查看文本"], "instruction": "点它"},
            "stats": {"html_chars": 5000, "links_total": 5, "links_kept": 1, "buttons_total": 1,
                      "text_total_chars": 500, "text_kept_chars": 500},
        }

    def execute(self, url: str, plan: DownloadPlan, output_dir: str):
        if url == RECORD_PAGE:
            # 点『查看文本』弹出新窗口，没有产生下载 → 执行器记录新地址并返回 None
            self.last_url = READER_PAGE
            return None
        return self.artifact

    def close(self) -> None:
        pass


class ReaderPlanner:
    """第一次在记录页上选 click_download，到了阅读页再选一次（这次能拿到文件）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.last_usage = None

    def plan(self, requested_name: str, observation: dict) -> DownloadPlan:
        self.calls += 1
        return DownloadPlan(
            action="click_download",
            selector="text=查看文本" if observation.get("url") == RECORD_PAGE else "text=下载标准",
            confidence=0.9,
            reason="点开阅读入口",
            matched_title=requested_name,
        )


workdir = Path(tempfile.mkdtemp(prefix="gate_popup_"))
try:
    # 用一份**有正文的真文件**当"阅读页里那个下载按钮拿到的文件"，
    # 这样断言的是"路走通了"，而不是被内容校验拦下。
    requested = "生产安全事故应急条例"
    artifact = workdir / "生产安全事故应急条例_国务院令 第708号.pdf"
    shutil.copy2(locate("生产安全事故应急条例_国务院令 第708号.pdf"), artifact)
    browser = PopupBrowser(str(artifact))
    graph = build_graph(
        FakeSearcher(RECORD_PAGE, requested),
        browser,
        ReaderPlanner(),
        AgentConfig(download_dir=str(workdir)),
    )
    final = graph.invoke({"requested_filename": requested})["final"]
    trace = final["trace"]
    observed_urls = [t["details"].get("url") for t in trace if t["node"] == "observe"]
    check(
        "弹窗打开的新页面被重新观察",
        READER_PAGE in observed_urls,
        f"观察过的地址：{observed_urls}",
    )
    check(
        "记录页 → 阅读页 → 拿到文件（下载入口这条路走通）",
        final.get("downloaded_path") is not None and str(final.get("status")).startswith("success"),
        f"status={final.get('status')} path={final.get('downloaded_path')}",
    )
    actions = [t["details"].get("action") for t in trace if t["node"] == "download"]
    check(
        "在阅读页上又点了一次下载入口（而不是被当成重复动作拦下）",
        len(actions) >= 2 and actions[0] == "click_download" and actions[1] == "click_download",
        f"动作序列：{actions}",
    )
finally:
    shutil.rmtree(workdir, ignore_errors=True)

graph_src2 = (ROOT / "src" / "standards_crawler_agent" / "graph.py").read_text(encoding="utf-8")
check(
    "after_download 用『地址变了』判断是否重新观察，而不是只白名单动作名",
    "navigated" in graph_src2 and "observed_url" in graph_src2,
)
planner_src2 = (ROOT / "src" / "standards_crawler_agent" / "llm_planner.py").read_text(encoding="utf-8")
check("提示词把在线预览/查看文本列为下载入口", "在线阅读" in planner_src2 and "reading_entry" in planner_src2)
check(
    "提示词明确『仅提供在线阅读服务』不是没有全文",
    '不是**"没有全文"' in planner_src2,
    "提示词里找不到该表述",
)


# ---------------------------------------------------------------- J. 三跳重放
print("=== J. 三跳重放：记录页 → 阅读页 → 文件端点（离线，假浏览器）===")

from standards_crawler_agent.browser import newest_popup  # noqa: E402
from standards_crawler_agent.graph import redirect_page_export  # noqa: E402
from standards_crawler_agent.content import page_is_not_a_document  # noqa: E402

# J1. 入口优先级：离文件最近的先试
check(
    "入口按『下载标准 > 查看文本 > 在线阅读 > 在线预览』排序",
    reading_entries("在线阅读 在线预览 下载标准 查看文本")
    == ["下载标准", "查看文本", "在线阅读", "在线预览"],
    str(reading_entries("在线阅读 在线预览 下载标准 查看文本")),
)
check("优先挑能直接下文件的入口", prefer_reading_entry(["在线阅读", "下载标准"]) == "下载标准")
check("没有下载标准时挑阅读器", prefer_reading_entry(["在线阅读", "在线预览"]) == "在线阅读")

# J2. 「本页不是正文页」只在有证据时才成立
check(
    "著录页判定为『不是正文页』",
    page_is_not_a_document("标准号： GB/T 1-2020 中文标准名称：x 标准状态： 现行") is not None,
)
check(
    "有条款的页面不判定为『不是正文页』（官网 HTML 正文靠它放行）",
    page_is_not_a_document("第一条 a " + "第二条 b " + "第三条 c " + "第四条 d") is None,
)

# J3. 有入口时不许把当前页拍成 PDF
entry_obs = {
    "text": "标准号： GB/T 6946-2008 中文标准名称：钢丝绳铝合金压制接头 标准状态： 现行 在线预览 下载标准",
    "reading_entry": {"entries": ["下载标准", "在线预览"]},
}
forced = redirect_page_export(entry_obs, DownloadPlan(action="save_page_pdf"), [])
check(
    "有入口 + 模型想拍网页 → 改成点入口",
    forced.action == "click_download" and forced.selector == "text=下载标准",
    f"{forced.action} / {forced.selector}",
)
gave_up = redirect_page_export(entry_obs, DownloadPlan(action="save_page_pdf"), ["click_download"])
check("入口已试过仍失败 → 如实 no_download", gave_up.action == "no_download", gave_up.action)
law_obs = {"text": "第一条 x 第二条 y 第三条 z", "reading_entry": {"entries": ["下载标准"]}}
kept = redirect_page_export(law_obs, DownloadPlan(action="save_page_pdf"), [])
check("本就是正文页时不动模型的选择", kept.action == "save_page_pdf", kept.action)

# J4. newest_popup：挑最后打开的那个，关掉多余的，忽略同址窗口
class FakePage:
    def __init__(self, url, closed=False):
        self.url = url
        self._closed = closed
        self.was_closed = False

    def is_closed(self):
        return self._closed

    def close(self):
        self.was_closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


current = FakePage("https://std.samr.gov.cn/gb/search/gbDetailed?id=1")
stale = FakePage("https://example.com/ad")
same = FakePage(current.url)
newest = FakePage("https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=ABC")
picked = newest_popup(FakeContext([current, stale, same, newest]), current)
check("挑出最后打开的新窗口", picked is newest, str(getattr(picked, "url", None)))
check("多余的弹窗被关掉", stale.was_closed)
check("同地址窗口不算新页面", not same.was_closed)
check("没有新窗口时返回 None", newest_popup(FakeContext([current]), current) is None)


# J5. 端到端三跳：记录页点『下载标准』→ 阅读页 → 文件端点 → 拿到真 PDF
RECORD = "https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D77C65D3A7E05397BE0A0AB82A"
READER = "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=3BEE46C92ED8405E96A75FD8C60A84BE"
ENDPOINT = "https://openstd.samr.gov.cn/bzgk/std/showGb?type=download&hcno=3BEE46C92ED8405E96A75FD8C60A84BE"


class ThreeHopBrowser:
    """复刻真实站点的三跳行为，含"导航被转成下载"这一步。

    第 3 跳的 `direct_file` 是模仿 Playwright 的行为：导航到文件端点时
    `page.goto` 抛 `Download is starting`，observe 据此回报"该地址是文件直链"。
    """

    def __init__(self, artifact: str) -> None:
        self.artifact = artifact
        self.last_url = None

    def observe(self, url: str) -> dict:
        base = {
            "url": url, "title": "标准", "headings": [], "buttons": [], "embedded": [],
            "has_print_hint": False, "content_type": "html", "no_fulltext": None,
            "stats": {"html_chars": 4000, "links_total": 5, "links_kept": 1, "buttons_total": 1,
                      "text_total_chars": 500, "text_kept_chars": 500},
        }
        if url == RECORD:
            base["text"] = "生产安全事故应急条例 标准号： GB 708-2009 中文标准名称：生产安全事故应急条例 标准状态： 现行 在线阅读 在线预览"
            base["links"] = [{"text": "查看文本", "url": f"{RECORD}#t", "has_file_extension": False}]
            base["reading_entry"] = {"entries": ["下载标准", "在线预览"], "instruction": "点它"}
            return base
        if url == READER:
            base["text"] = "生产安全事故应急条例 下载标准 在线预览"
            base["buttons"] = [{"text": "下载标准", "tag": "button"}]
            return base
        # 文件端点：浏览器把导航转成了下载，没有网页内容可观察
        base["text"] = ""
        base["direct_file"] = {"url": url, "extension": "", "note": "该地址是文件直链"}
        return base

    def execute(self, url: str, plan: DownloadPlan, output_dir: str):
        if url in (RECORD, READER):
            self.last_url = READER if url == RECORD else ENDPOINT
            return None
        return self.artifact

    def close(self) -> None:
        pass


class HopPlanner:
    def __init__(self) -> None:
        self.last_usage = None
        self.seen: list[str] = []

    def plan(self, requested_name: str, observation: dict) -> DownloadPlan:
        self.seen.append(observation.get("url") or "")
        if observation.get("direct_file"):
            return DownloadPlan(action="download_direct", url=observation["direct_file"]["url"],
                                confidence=0.9, reason="文件直链", matched_title=requested_name)
        entries = (observation.get("reading_entry") or {}).get("entries") or []
        label = entries[0] if entries else "下载标准"
        return DownloadPlan(action="click_download", selector=f"text={label}", confidence=0.9,
                            reason="点入口", matched_title=requested_name)


workdir = Path(tempfile.mkdtemp(prefix="gate_3hop_"))
try:
    requested = "生产安全事故应急条例"
    artifact = workdir / "生产安全事故应急条例_国务院令 第708号.pdf"
    # 用一份**有正文、且正文与请求名对得上**的真文件代表"最终下到的 PDF"，
    # 这样断言的是"路走通了"，而不是被名称校验拦下。
    shutil.copy2(locate("生产安全事故应急条例_国务院令 第708号.pdf"), artifact)
    planner = HopPlanner()
    graph = build_graph(
        FakeSearcher(RECORD, requested),
        ThreeHopBrowser(str(artifact)),
        planner,
        AgentConfig(download_dir=str(workdir)),
    )
    final = graph.invoke({"requested_filename": requested})["final"]
    observed = [t["details"].get("url") for t in final["trace"] if t["node"] == "observe"]
    actions = [t["details"].get("action") for t in final["trace"] if t["node"] == "download"]
    check("第 2 跳（阅读页）被观察到", READER in observed, str(observed))
    check("第 3 跳（文件端点）被观察到", ENDPOINT in observed, str(observed))
    check(
        "三跳动作序列 = 点入口 → 点入口 → download_direct",
        actions[:3] == ["click_download", "click_download", "download_direct"],
        str(actions),
    )
    check(
        "最终拿到文件并判为成功",
        final.get("downloaded_path") is not None and str(final.get("status")).startswith("success"),
        f"status={final.get('status')} path={final.get('downloaded_path')}",
    )
    check("没有退化成网页导出", "save_page_pdf" not in actions and "click_print" not in actions, str(actions))
finally:
    shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- K. 页面声明的身份
print("=== K. 著录页声明的身份：下载之前就认出『这不是目标文件』 ===")

from standards_crawler_agent.content import (  # noqa: E402
    compare_declared_identity,
    declared_identity,
)

# K1. 用**真实抓下来的页面原文**（这两段是从候选页上原样取的）
SAFETY_BELT_PAGE = (
    "标准号：GB 6095-2021 中文标准名称： 坠落防护 安全带 "
    "英文标准名称：Fall protection—Personal fall protection systems 标准状态： 现行"
)
SAFETY_ROPE_PAGE = (
    "标准号：GB 24543-2009 采 中文标准名称： 坠落防护 安全绳 "
    "英文标准名称：Personal fall protection equipment - Lanyards 标准状态： 现行"
)
check(
    "从著录页抽出标准号与正式名称",
    declared_identity(SAFETY_BELT_PAGE) == ("GB 6095-2021", "坠落防护 安全带"),
    str(declared_identity(SAFETY_BELT_PAGE)),
)
check(
    "正式名称不会把『英文标准名称』一并吞进来",
    "英文" not in (declared_identity(SAFETY_BELT_PAGE)[1] or ""),
    str(declared_identity(SAFETY_BELT_PAGE)[1]),
)

belt_ok, belt_note = compare_declared_identity(*declared_identity(SAFETY_BELT_PAGE), "坠落防护安全绳")
check("《坠落防护 安全带》页对请求『安全绳』→ 拒绝（这次下错的根因）", not belt_ok, belt_note)
rope_ok, rope_note = compare_declared_identity(*declared_identity(SAFETY_ROPE_PAGE), "坠落防护安全绳")
check("《坠落防护 安全绳》页对请求『安全绳』→ 通过", rope_ok, rope_note)

# 容忍度必须与既有 check_name_match 一致，否则会把正确候选误杀
kind_ok, kind_note = compare_declared_identity(
    "JGJ 99-2015", "高层民用建筑钢结构技术规程", "高层民用建筑钢结构技术规范"
)
check("文种词差异（规程/规范）仍放行", kind_ok, kind_note)
typo_ok, typo_note = compare_declared_identity(
    "GB 50204-2015", "混凝土结构工程施工质量验收规范", "混凝土结构施工质量验收规范"
)
check("请求名漏字的笔误仍放行", typo_ok, typo_note)
num_ok, num_note = compare_declared_identity(
    "GB 50204-2015", "混凝土结构工程施工质量验收规范", "混凝土结构工程施工质量验收规范 GB 9999-2020"
)
check("请求名与页面标准号冲突时拒绝", not num_ok, num_note)
none_ok, _ = compare_declared_identity(None, None, "任何一个名字")
check("页面没有著录字段时不干预（列表页/公文页）", none_ok)
long_ok, long_note = compare_declared_identity(None, "坠落防护 安全绳用连接器", "坠落防护安全绳")
check("声明名比请求名长出一截（兄弟标准）时拒绝", not long_ok, long_note)


class IdentityBrowser:
    """复刻这次的真实失败形状：排在第一的候选是**兄弟文件**的著录页。

    两个页面都明写自己的标准号和正式名称（爬下来的原文形态）。
    错的那个页面上有下载按钮——闸门不生效就会把错文件下下来。
    """

    WRONG = "https://std.samr.gov.cn/gb/search/gbDetailed?id=WRONG"
    RIGHT = "https://www.gov.cn/zhengce/content/RIGHT"

    def __init__(self, artifact: str) -> None:
        self.artifact = artifact
        self.executed: list[str] = []

    def observe(self, url: str) -> dict:
        wrong = url == self.WRONG
        text = (
            "标准号：国务院令第493号 中文标准名称：生产安全事故报告和调查处理条例 标准状态： 现行"
            if wrong
            else "标准号：国务院令第708号 中文标准名称：生产安全事故应急条例 标准状态： 现行"
        )
        return {
            "url": url, "title": text[:20], "text": text,
            "headings": [], "embedded": [], "has_print_hint": False,
            "content_type": "html", "no_fulltext": None, "reading_entry": None,
            "links": [{"text": "下载", "url": f"{url}#dl", "has_file_extension": True}],
            "buttons": [{"text": "下载标准", "tag": "button"}],
            "stats": {"html_chars": 4000, "links_total": 5, "links_kept": 1, "buttons_total": 1,
                      "text_total_chars": 400, "text_kept_chars": 400},
        }

    def execute(self, url: str, plan: DownloadPlan, output_dir: str):
        self.executed.append(url)
        return self.artifact

    def close(self) -> None:
        pass


class TwoCandidateSearcher:
    """第一个候选是兄弟文件，第二个才是目标。"""

    def __init__(self, requested: str) -> None:
        self.requested = requested
        self.last_read = {"verdict": "ok", "attempts": [1]}

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return [
            SearchResult(title=self.requested, url=IdentityBrowser.WRONG, snippet=self.requested,
                         domain="std.samr.gov.cn"),
            SearchResult(title=self.requested, url=IdentityBrowser.RIGHT, snippet=self.requested,
                         domain="www.gov.cn"),
        ]

    def close(self) -> None:
        pass


workdir = Path(tempfile.mkdtemp(prefix="gate_identity_"))
try:
    requested = "生产安全事故应急条例"
    artifact = workdir / "生产安全事故应急条例_国务院令 第708号.pdf"
    shutil.copy2(locate("生产安全事故应急条例_国务院令 第708号.pdf"), artifact)
    browser = IdentityBrowser(str(artifact))
    graph = build_graph(
        TwoCandidateSearcher(requested),
        browser,
        FakePlanner("click_download"),
        AgentConfig(download_dir=str(workdir)),
    )
    final = graph.invoke({"requested_filename": requested})["final"]
    trace = final["trace"]
    rejected = [t["details"] for t in trace if (t["details"] or {}).get("identity_rejected")]
    observed = [t["details"].get("url") for t in trace if t["node"] == "observe"]

    check(
        "兄弟文件的著录页被识别并跳过",
        len(rejected) >= 1 and IdentityBrowser.WRONG in str(rejected[0].get("url") or ""),
        str(rejected),
    )
    check(
        "跳过时给出了『页面声明 vs 请求名』的具体理由",
        any("页面声明" in str(d.get("identity_rejected") or "") for d in rejected),
        str(rejected),
    )
    check(
        "**没有从错的那个候选页下载任何文件**",
        IdentityBrowser.WRONG not in browser.executed,
        f"实际下载过的页面：{browser.executed}",
    )
    check(
        "自动切到第二个候选并拿到文件",
        IdentityBrowser.RIGHT in browser.executed
        and final.get("downloaded_path") is not None
        and str(final.get("status")).startswith("success"),
        f"executed={browser.executed} status={final.get('status')}",
    )
    check(
        "两个候选页都被观察过",
        IdentityBrowser.WRONG in observed and IdentityBrowser.RIGHT in observed,
        str(observed),
    )
finally:
    shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- L. 只把真实控件当入口
print("=== L. reading_entry 只认真实可点的控件（那次 8 秒超时的根因）===")

from standards_crawler_agent.browser import refine_reading_entry  # noqa: E402
from standards_crawler_agent.content import clickable_reading_entries  # noqa: E402

# L1. 判据本体
check(
    "只保留真实存在于控件标签里的入口",
    clickable_reading_entries(["在线阅读", "在线预览", "下载标准"], ["在线预览", "打印"]) == ["在线预览"],
    str(clickable_reading_entries(["在线阅读", "在线预览", "下载标准"], ["在线预览", "打印"])),
)
check(
    "控件标签里没有的入口一律去掉",
    clickable_reading_entries(["在线预览"], []) == [],
)
check(
    "控件标签更长时也算命中（按钮文字带修饰）",
    clickable_reading_entries(["下载标准"], ["下载标准PDF 全文"]) == ["下载标准"],
)

# L2. 用真实页面结构做样本
#   A) 著录页：有真的『在线预览』按钮（class="btn ck_btn"），另有一句"仅提供在线阅读服务"
PAGE_WITH_BUTTON = """
<html><head><title>国家标准|GB 24543-2009</title></head><body>
<div id="content">
  <table class="tdlist"><tr>
    <td><button class="btn ck_btn btn-sm btn-primary" data-value="B7C913">在线预览</button></td>
    <td><button class="btn fk_btn btn-sm btn-success app-hide">实施信息反馈</button></td>
  </tr></table>
  <span class="text-danger">该标准采用了ISO、IEC等国际国外组织的标准，由于涉及版权保护问题，本系统仅提供在线阅读服务。</span>
  <p>标准号：GB 24543-2009 采 中文标准名称：坠落防护 安全绳 标准状态： 现行</p>
</div>
<script type="text/javascript">var i18n = {'zh': {'preview': '在线预览', 'download': '下载标准'}};</script>
</body></html>
"""
summary_a = summarize_html(PAGE_WITH_BUTTON, "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=X", "国家标准|GB 24543-2009")
entry_a = summary_a.get("reading_entry") or {}
check("有真按钮时『在线预览』保留为入口", "在线预览" in (entry_a.get("entries") or []), str(entry_a))
check(
    "只出现在提示文案里的『在线阅读』不算入口",
    "在线阅读" not in (entry_a.get("entries") or []),
    str(entry_a),
)
check(
    "被撤掉的字样在 instruction 里点明『不是可点击控件』",
    "不是可点击控件" in str(entry_a.get("instruction") or ""),
    str(entry_a.get("instruction"))[:160],
)

#   B) 弹回来的那一页：文本里到处是"在线预览"，但**没有任何对应控件**
PAGE_WITHOUT_BUTTON = """
<html><head><title>国家标准|GB 24543-2009</title></head><body>
<div id="content">
  <span class="text-danger">该标准采用了ISO、IEC等国际国外组织的标准，由于涉及版权保护问题，本系统仅提供在线阅读服务。</span>
  <p>标准号：GB 24543-2009 采 中文标准名称：坠落防护 安全绳 标准状态： 现行</p>
</div>
<script type="text/javascript">
var i18n = {'zh': {'preview': '在线预览', 'download': '下载标准',
  'previewMsg': '当前浏览器暂不支持标准全文在线预览服务，请使用现代浏览器重新打开当前页面。'}};
</script>
</body></html>
"""
summary_b = summarize_html(PAGE_WITHOUT_BUTTON, "https://openstd.samr.gov.cn/bzgk/std/showGb?type=online&hcno=X", "国家标准|GB 24543-2009")
entry_b = summary_b.get("reading_entry") or {}
check(
    "**没有控件时 entries 为空**（不再递一个点不到的选择器给模型）",
    (entry_b.get("entries") or []) == [],
    str(entry_b),
)
check(
    "明确告知『本页找不到可点击的阅读入口』",
    "找不到可点击的阅读入口" in str(entry_b.get("instruction") or ""),
    str(entry_b.get("instruction"))[:200],
)
check(
    "并点明这些字样不是可点击控件",
    "不是可点击控件" in str(entry_b.get("instruction") or ""),
    str(entry_b.get("instruction"))[:200],
)

# L3. 可见性过滤之后要再收一次：被 drop_hidden_controls 剔掉的按钮不能还算入口
summary_c = {
    "buttons": [{"text": "在线预览", "tag": "button"}],
    "links": [],
    "reading_entry": {"statement": None, "entries": ["在线预览"], "instruction": "x"},
}
refine_reading_entry(summary_c)
check("可见控件仍在时入口保留", summary_c.get("reading_entry", {}).get("entries") == ["在线预览"])

summary_d = {
    "buttons": [],           # 隐藏按钮已被 drop_hidden_controls 剔除
    "links": [],
    "reading_entry": {"statement": None, "entries": ["在线预览"], "instruction": "x"},
}
refine_reading_entry(summary_d)
check(
    "按钮被可见性过滤剔除后 reading_entry 整段消失",
    summary_d.get("reading_entry") is None,
    str(summary_d.get("reading_entry")),
)

summary_e = {
    "buttons": [],
    "links": [],
    "reading_entry": {"statement": "平台声明仅提供在线阅读（没有下载入口，正文需在阅读页里看）",
                      "entries": ["在线预览"], "instruction": "x"},
}
refine_reading_entry(summary_e)
check(
    "页面声明只能在在线阅读但无控件时：保留 statement、清空 entries",
    summary_e["reading_entry"]["entries"] == [] and summary_e["reading_entry"]["statement"],
    str(summary_e.get("reading_entry")),
)

planner_src3 = (ROOT / "src" / "standards_crawler_agent" / "llm_planner.py").read_text(encoding="utf-8")
check("提示词要求 entries 为空时不要点", "entries 为空时一律不要点" in planner_src3)
check("提示词要求 selector 来自 buttons/links", "必须来自 buttons 或 links" in planner_src3)


# ---------------------------------------------------------------- M. 候选排序：权威层级与文档直链
# 起因：r19 的 #4《房屋市政工程生产安全重大事故隐患判定标准（2024版）》落到阿里地区住建局
# 的网页导出，而中国政府网上那件**官方 .docx 附件**就在同一个检索结果里、却没进候选池。
# 夹具是两条**真实 SERP**（tests/fixtures_serp_ranking.json，由 temp/make_serp_fixture.py
# 从 results/ 的 result.json 里抽出，未做任何加工）。
SERP = json.loads((HERE / "fixtures_serp_ranking.json").read_text(encoding="utf-8"))


def combined(page) -> float:
    return 0.5 * page.official_score + 0.5 * page.page_score


def pick_url(results, fragment: str):
    return next(item for item in results if fragment in item.url)


def rank_case(key: str):
    case = SERP[key]
    results = [SearchResult(**item) for item in case["results"]]
    # 与 graph.rank 一致：第三方文库/内容站先硬拒绝，再按可信度门槛留候选池。
    usable = [item for item in results if not is_low_trust_domain(item.domain)]
    ranked = ranking.rank_pages(case["requested"], usable)
    threshold = AgentConfig().min_candidate_score
    pool = [page for page in ranked if combined(page) >= threshold]
    return case["requested"], results, ranked, pool


name_a, results_a, ordered_a, pool_a = rank_case("case_with_attachment")
name_b, results_b, ordered_b, pool_b = rank_case("case_without_attachment")

docx = pick_url(results_a, "P020250101711302201621.docx")
gov_page = pick_url(results_a, "content_6995806.htm")
ali_page = pick_url(results_a, "zjj.al.gov.cn")
szwb_page = pick_url(results_a, "szwb.sz.gov.cn")
mohurd_page = pick_url(results_a, "art_1446154772")

check(
    "中国政府网/部委算中央档位，地方厅局不算",
    ranking.is_central_gov_domain("www.gov.cn")
    and ranking.is_central_gov_domain("www.mohurd.gov.cn")
    and not ranking.is_central_gov_domain("zjt.shandong.gov.cn")
    and not ranking.is_central_gov_domain("zjj.al.gov.cn"),
)
check(
    "地方政府站仍按政府站计分（只是不再与中央同档）",
    ranking.official_score(ali_page)[0] >= 0.6,
    str(ranking.official_score(ali_page)),
)
check(
    "中央发布页的官方分高于地区级住建局详情页（r19 里是反过来的）",
    ranking.official_score(gov_page)[0] > ranking.official_score(ali_page)[0],
    f"gov={ranking.official_score(gov_page)[0]} al={ranking.official_score(ali_page)[0]}",
)
check(
    "住建部的官方分高于深圳转载页",
    ranking.official_score(mohurd_page)[0] > ranking.official_score(szwb_page)[0],
)

check(
    "路径词不再把 info/detail 当文件信号",
    "info" not in ranking.FILE_URL_WORDS and "detail" not in ranking.FILE_URL_WORDS,
)
plain_path = SearchResult(
    title=ali_page.title, url="https://zjj.al.gov.cn/art/1963/12182.htm", snippet="", domain="zjj.al.gov.cn"
)
check(
    "/info/ 详情页不再比同站普通路径多拿 0.05",
    ranking.official_score(plain_path)[0] == ranking.official_score(ali_page)[0],
    f"{ranking.official_score(plain_path)[0]} vs {ranking.official_score(ali_page)[0]}",
)
attach_path = SearchResult(
    title=ali_page.title, url="https://zjj.al.gov.cn/attach/0/20250102.pdf", snippet="", domain="zjj.al.gov.cn"
)
check(
    "真正的文件端点路径词（attach/upload/download/file）仍加 0.05",
    ranking.official_score(attach_path)[0] > ranking.official_score(ali_page)[0],
)

check(
    "文档直链判定：.pdf/.docx 是，.htm 与下载接口不是",
    ranking.is_document_url("https://www.gov.cn/a/b.pdf")
    and ranking.is_document_url("https://www.gov.cn/x/P020250101711302201621.docx")
    and not ranking.is_document_url("https://www.gov.cn/zhengce/content_6995806.htm")
    and not ranking.is_document_url("https://www.mohurd.gov.cn/api/document/download?fileUrl=abc"),
)
check(
    "权威站上的文档直链拿到 0.95 页面分",
    ranking.page_score(docx, name_a) == ranking.DOCUMENT_URL_PAGE_SCORE,
    str(ranking.page_score(docx, name_a)),
)
junk_pdf = pick_url(results_b, "s21i.faiusr.com")
check(
    "非权威站的 PDF 不给这份加分（否则无关 PDF 会挤掉政府页面）",
    ranking.page_score(junk_pdf, name_b) < ranking.DOCUMENT_URL_PAGE_SCORE,
    str(ranking.page_score(junk_pdf, name_b)),
)

check(
    "【r19 #4 的复现用例】官方 .docx 附件直链排到第 1",
    ordered_a[0].url == docx.url,
    ordered_a[0].url,
)
check(
    "深圳页靠摘要逐字命中拿满页面分（旧公式正是因此把它顶到第 1）",
    ranking.page_score(szwb_page, name_a) == 1.0,
    str(ranking.page_score(szwb_page, name_a)),
)
check(
    "官方 .docx 的综合分高于深圳页与阿里地区页",
    combined(ranking.rank_pages(name_a, [docx])[0]) > combined(ranking.rank_pages(name_a, [szwb_page])[0])
    and combined(ranking.rank_pages(name_a, [docx])[0]) > combined(ranking.rank_pages(name_a, [ali_page])[0]),
)
check(
    "候选池里没有第三方文库站",
    all("wenku.baidu.com" not in page.domain and "baike.baidu.com" not in page.domain for page in ordered_a),
)
check(
    "前 5 名全是政府站点，且其中至少 2 个是中央/部委页面",
    all(".gov.cn" in page.domain for page in ordered_a[:5])
    and sum(1 for page in ordered_a[:5] if ranking.is_central_gov_domain(page.domain)) >= 2,
    " | ".join(f"{page.domain}:{combined(page):.3f}" for page in ordered_a[:5]),
)

check(
    "【没有附件直链时】第 1 名仍是中央发布页，而不是标题完整的转载页",
    ranking.is_central_gov_domain(ordered_b[0].domain),
    ordered_b[0].url,
)
repost_b = pick_url(results_b, "yjt.zj.gov.cn")
check(
    "转载页的官方分低于中央发布页（标题被引擎插空格不再额外加分）",
    ranking.official_score(pick_url(results_b, "content_6995806.htm"))[0] > ranking.official_score(repost_b)[0],
)
check(
    "房产网噪声低于候选门槛、进不了候选池",
    not any(page.domain.endswith(("anjuke.com", "fang.com", "58.com", "ke.com", "house365.com")) for page in pool_b)
    and pool_b,
    " | ".join(f"{page.domain}:{combined(page):.3f}" for page in pool_b[:5]),
)


# ---------------------------------------------------------------- N. 候选选择器与模型调用失败降级
print("=== N. 候选选择器（只挑下标）与模型调用失败降级 ===")

from standards_crawler_agent.ranker import CHOICE_SKIP_THRESHOLD, LLMCandidateChooser  # noqa: E402

REQUESTED = "中华人民共和国突发事件应对法"
# 两条候选刻意做成"字面分最高的不是来源最权威的那一条"——这正是 r19 #4 的形状：
#   X = 地方转载页，摘要里逐字出现完整名称 → 相关度 1.00，确定性排序第 1；
#   Y = 部委发布页，但标题被搜索引擎**截断**（"…《中华人民共和国…"）→ 相关度只有 0.58，
#       虽然官方分更高，总分仍低于 X。
# 只有把"来源权威 + 页面是发布页而非转载页"这类语义交给模型，才可能选对 Y。
CANDIDATE_X = SearchResult(title=REQUESTED, url="https://www.sz.gov.cn/post_11988484.html", snippet=REQUESTED + " 已印发，详见附件。", domain="www.sz.gov.cn")
CANDIDATE_Y = SearchResult(title="住房城乡建设部关于印发《中华人民共和国…", url="https://www.mohurd.gov.cn/gongkai/art_1446154772.html", snippet="各省、自治区住房城乡建设厅…", domain="www.mohurd.gov.cn")


class MultiSearcher:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.last_read = {"verdict": "ok", "attempts": [1]}

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return list(self.results)

    def close(self) -> None:
        pass


class RecordingChooser:
    """假的选择器：记录被调用几次、看到哪些候选，返回预设下标（或抛异常）。"""

    def __init__(self, index: int | None, boom: bool = False) -> None:
        self.index = index
        self.boom = boom
        self.calls: list[list[str]] = []
        self.last_usage = {"input_tokens": 120, "output_tokens": 15, "total_tokens": 135}
        self.last_reason = "部委发布页才有官方附件；转载页只是网页"

    def choose(self, requested_name: str, candidates: list) -> int | None:
        self.calls.append([page.url for page in candidates])
        if self.boom:
            raise RuntimeError("APITimeoutError: 请求超时")
        return self.index


class ExplodingPlanner:
    def __init__(self) -> None:
        self.last_usage = None

    def plan(self, requested_name: str, observation: dict) -> DownloadPlan:
        raise RuntimeError("APITimeoutError: 上游请求超时")


def run_choice(results, chooser=None, planner=None):
    """跑完整图，返回 (最终 state, 选择器)。产物用真的网页导出夹具，保证能走到收尾。"""
    workdir = Path(tempfile.mkdtemp(prefix="choice_"))
    try:
        artifact = workdir / "中华人民共和国突发事件应对法.pdf"
        shutil.copy2(locate("中华人民共和国突发事件应对法.pdf"), artifact)
        graph = build_graph(
            MultiSearcher(results),
            FakeBrowser(str(artifact)),
            planner or FakePlanner("save_page_pdf"),
            AgentConfig(download_dir=str(workdir)),
            chooser,
        )
        return graph.invoke({"requested_filename": REQUESTED})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


state = run_choice([CANDIDATE_X, CANDIDATE_Y])
check("没有选择器时按确定性排序选第 1 名（字面分最高的转载页）",
      state["selected_page"].url == CANDIDATE_X.url, state["selected_page"].url)

chooser = RecordingChooser(1)
state = run_choice([CANDIDATE_X, CANDIDATE_Y], chooser)
check("选择器返回下标 1 → 选中第 2 条（模型可以不同意字面分）",
      state["selected_page"].url == CANDIDATE_Y.url, state["selected_page"].url)
rank_step = [step for step in state["trace"] if step["node"] == "rank"][-1]
check("选择结果与理由写进 trace", rank_step["details"]["chooser"]["index"] == 1 and rank_step["details"]["chooser"]["reason"],
      str(rank_step["details"].get("chooser"))[:120])
check("候选池按选择结果重排（选中的排到最前，其余不丢）",
      [page.url for page in state["candidate_pages"]][:2] == [CANDIDATE_Y.url, CANDIDATE_X.url],
      str([page.url for page in state["candidate_pages"]]))
check("选择器的 token 用量计入 usage_total",
      (state["final"]["usage_total"] or {}).get("total_tokens", 0) >= 135,
      str(state["final"]["usage_total"]))

state = run_choice([CANDIDATE_X, CANDIDATE_Y], RecordingChooser(1, boom=True))
check("选择器调用失败不改变行为（仍选确定性第 1 名）",
      state["selected_page"].url == CANDIDATE_X.url, state["selected_page"].url)
check("选择器失败写进 errors 与 trace",
      any("候选选择失败" in item for item in state["final"]["errors"])
      and "error" in [step for step in state["trace"] if step["node"] == "rank"][-1]["details"]["chooser"],
      str(state["final"]["errors"])[-160:])

state = run_choice([CANDIDATE_X, CANDIDATE_Y], RecordingChooser(9))
check("选择器给出越界下标 → 当作没选（不越界访问）",
      state["selected_page"].url == CANDIDATE_X.url, state["selected_page"].url)

single = RecordingChooser(0)
state = run_choice([CANDIDATE_X], single)
check(f"候选池不足 {CHOICE_SKIP_THRESHOLD} 条时不调用选择器（省一次 token）",
      single.calls == [], str(single.calls))

check("候选池只有一条时选择器也不会被调用（阈值常量未变）", CHOICE_SKIP_THRESHOLD == 2)


class RecordingModel:
    """给 LLMCandidateChooser 用的假模型：验证它自己把越界/异常挡在外面。"""

    def __init__(self, index: int, parsed: bool = True) -> None:
        self.index = index
        self.parsed = parsed
        self.prompts: list[str] = []

    def with_structured_output(self, schema, include_raw: bool = False):
        return self

    def invoke(self, prompt: str):
        self.prompts.append(prompt)
        parsed = CandidateChoice(index=self.index, reason="测试") if self.parsed else None
        return {"raw": None, "parsed": parsed}


from standards_crawler_agent.models import CandidateChoice, CandidatePage  # noqa: E402

pool = [
    CandidatePage(url=CANDIDATE_X.url, title=CANDIDATE_X.title, text=CANDIDATE_X.snippet, domain=CANDIDATE_X.domain, official_score=0.75, page_score=1.0),
    CandidatePage(url=CANDIDATE_Y.url, title=CANDIDATE_Y.title, text=CANDIDATE_Y.snippet, domain=CANDIDATE_Y.domain, official_score=0.95, page_score=0.4),
]
model = RecordingModel(1)
llm_chooser = LLMCandidateChooser(model)
check("LLMCandidateChooser 正常返回下标", llm_chooser.choose(REQUESTED, pool) == 1)
check("提示词里带上了请求名与候选下标", REQUESTED in model.prompts[0] and '"idx": 1' in model.prompts[0])
check("提示词不要求模型输出 URL（只认下标）", "不要输出 URL" in model.prompts[0])
check("越界下标被 LLMCandidateChooser 自己挡下", LLMCandidateChooser(RecordingModel(9)).choose(REQUESTED, pool) is None)
check("模型没给出结构化结果时返回 None", LLMCandidateChooser(RecordingModel(0, parsed=False)).choose(REQUESTED, pool) is None)

state = run_choice([CANDIDATE_X, CANDIDATE_Y], None, ExplodingPlanner())
check("规划模型调用失败不再打死整条任务（能正常收尾）",
      state["final"]["status"] in {"needs_confirmation", "not_downloadable"}, state["final"]["status"])
check("模型失败原因写进 errors 与 trace",
      any("模型决策失败" in item for item in state["final"]["errors"])
      and any(step["node"] == "choose_download" and step["details"].get("error") for step in state["trace"]),
      str(state["final"]["errors"])[-160:])
check("模型失败后仍走完所有候选（每个候选各试一次）",
      sum(1 for step in state["trace"] if step["node"] == "observe") == 2,
      str(sum(1 for step in state["trace"] if step["node"] == "observe")))


# ---------------------------------------------------------------- O. 同名不同文件（地方版本）必须被拒
print("=== O. 同名的另一份地方文件必须被拒（r21 #3 的真实错件）===")

from standards_crawler_agent.download import (  # noqa: E402
    TYPE_WORD_FAMILY,
    _core_name,
    _type_word,
    check_name_match,
    detect_near_miss_document,
)

NAME_FIXTURE = json.loads((HERE / "fixtures_name_mismatch.json").read_text(encoding="utf-8"))
NAME_CASES = NAME_FIXTURE["cases"]
REQUESTED_RULE = NAME_FIXTURE["_requested"]


def name_verdict(key: str) -> tuple[bool, str]:
    return check_name_match(NAME_CASES[key]["text"], REQUESTED_RULE)


passed, note = name_verdict("wrong_local_variant")
check("【r21 #3 的复现用例】《北京市…办法》被拒（旧判据按覆盖率 91% 放行）", not passed, note)
check("拒绝理由点明是『另一份文件（地方版本）』", "地方版本" in note and "北京市建设工程施工现场管理办法" in note, note)

for key, label in (("correct_national", "部令原件"), ("correct_page_export", "湖南住建厅网页导出")):
    passed, note = name_verdict(key)
    check(f"同一请求的正确产物（{label}）照常通过", passed, note)

check("文种词取最长匹配", _type_word("山东省…实施细则的通知") == "实施细则" or _type_word("危险源安全管理实施细则") == "实施细则")
check("核心名去掉文种词", _core_name("建设工程施工现场管理规定") == "建设工程施工现场管理", _core_name("建设工程施工现场管理规定"))
check(
    "标准类文种词算同族（规范/规程/标准可互换）",
    len({TYPE_WORD_FAMILY[w] for w in ("技术规范", "技术规程", "标准")}) == 1,
    str({w: TYPE_WORD_FAMILY[w] for w in ("技术规范", "技术规程", "标准")}),
)
check("规章类文种词不算同族（规定 ≠ 办法）", TYPE_WORD_FAMILY["规定"] != TYPE_WORD_FAMILY["办法"])

# 合成文本必须够长：looks_readable_text 要求 ≥120 个非空白字符，否则整段校验会被跳过
# （那样测的就不是这条判据了）。
SYNTH_PREFIX = "第一章 总则 第一条 为加强建设工程施工现场管理，保障安全生产和绿色施工，制定本办法。市住房城乡建设行政主管部门负责施工现场的监督管理工作。" * 3
check(
    "合成：北京市…管理办法 → 拒",
    not check_name_match("北京市建设工程施工现场管理办法 " + SYNTH_PREFIX, REQUESTED_RULE)[0],
)
check(
    "合成：济南市…管理办法 → 拒（市级行政区划同样识别）",
    not check_name_match("济南市建设工程施工现场管理办法 " + SYNTH_PREFIX, REQUESTED_RULE)[0],
)
check(
    "合成：同名同文种（规定）→ 放行",
    check_name_match("建设部令第15号《建设工程施工现场管理规定》 " + SYNTH_PREFIX, REQUESTED_RULE)[0],
)
check(
    "合成：无行政区划的同名不同文种 → **不**触发否决（判据刻意做窄）",
    check_name_match("建设工程施工现场管理办法 " + SYNTH_PREFIX, REQUESTED_RULE)[0],
)
check(
    "合成：漏字笔误（混凝土结构施工质量验收规范）仍放行——覆盖率兜底没被破坏",
    check_name_match("混凝土结构工程施工质量验收规范 " + "第一章 总则 第一条 为了加强混凝土结构工程施工质量的验收，制定本规范。" * 2, "混凝土结构施工质量验收规范")[0],
)
check(
    "合成：文种词差异（技术规范 vs 技术规程）仍放行",
    check_name_match("高层民用建筑钢结构技术规程 " + "第一章 总则 第一条 为了在高层民用建筑钢结构设计中贯彻执行国家的技术经济政策，制定本规程。" * 2, "高层民用建筑钢结构技术规范")[0],
)
check(
    "detect_near_miss_document 对不相关文本返回 None",
    detect_near_miss_document("这是一篇与目标无关的科普文章，讲的是城市建设与住房政策。" * 4, REQUESTED_RULE) is None,
)


class PerUrlArtifactBrowser(FakeBrowser):
    """按 URL 返回不同产物：模拟"候选 1 下到错件、候选 2 才是对的"。"""

    def __init__(self, artifacts: dict[str, str]) -> None:
        super().__init__(artifact="")
        self.artifacts = artifacts
        self.seen: list[str] = []

    def execute(self, url: str, plan: DownloadPlan, output_dir: str) -> str:
        self.seen.append(url)
        for fragment, path in self.artifacts.items():
            if fragment in url:
                target = Path(output_dir) / Path(path).name
                shutil.copy2(path, target)
                return str(target)
        raise RuntimeError(f"没有为 {url} 准备产物")


ARTIFACTS = HERE / "fixtures_artifacts"
WRONG_URL = "https://www.gov.cn/zhengce/2018-02/12/content_5721992.htm"
RIGHT_URL = "https://zjt.hunan.gov.cn/zjt/hngczl/zlgl/201806/t20180619_5034782.html"

workdir = Path(tempfile.mkdtemp(prefix="namemiss_"))
try:
    browser = PerUrlArtifactBrowser(
        {
            "content_5721992": str(ARTIFACTS / "wrong_local_variant.pdf"),
            "zjt.hunan.gov.cn": str(ARTIFACTS / "correct_page_export.pdf"),
        }
    )
    graph = build_graph(
        MultiSearcher(
            [
                # 错件页排第 1：它的摘要在正文里确实提到过部令《建设工程施工现场管理规定》
                # （地方实施办法常以部令为依据），于是相关度被拉满——r21 里正是这个形状。
                SearchResult(
                    title="北京市建设工程施工现场管理办法_北京市人民政府第247号令",
                    url=WRONG_URL,
                    snippet="依据《建设工程施工现场管理规定》（建设部令第15号），结合本市实际，制定本办法。",
                    domain="www.gov.cn",
                ),
                SearchResult(title="建设工程施工现场管理规定（建设部令第15号）", url=RIGHT_URL, snippet="建设工程施工现场管理规定", domain="zjt.hunan.gov.cn"),
            ]
        ),
        browser,
        FakePlanner("save_page_pdf"),
        AgentConfig(download_dir=str(workdir)),
    )
    end = graph.invoke({"requested_filename": REQUESTED_RULE})
finally:
    shutil.rmtree(workdir, ignore_errors=True)

quarantined = [step for step in end["trace"] if step["node"] == "quarantine"]
check("端到端：错件（北京市…办法）被校验拦下并隔离",
      bool(quarantined) and any("地方版本" in " ".join(step["details"].get("warnings") or []) for step in quarantined),
      json.dumps(quarantined, ensure_ascii=False)[:220])
check("端到端：自动换到第 2 个候选并拿到正确文件",
      end["final"]["status"] == "success_page_export" and end["selected_page"].url == RIGHT_URL,
      f"{end['final']['status']} / {end['selected_page'].url if end.get('selected_page') else '-'}")
check("端到端：错件所在页面确实被访问过（不是碰巧跳过）", WRONG_URL in browser.seen, str(browser.seen))


# ---------------------------------------------------------------- P. 检索词与"候选池为 0"的入口
print("=== P. 检索词组合 + 官方入口推断 + 站内检索空转硬停（山东那份文件的失败链）===")

from standards_crawler_agent.ranking import derive_official_entry  # noqa: E402
from standards_crawler_agent.search import build_queries, core_name  # noqa: E402

SHANDONG = "关于印发《山东省房屋建筑和市政基础设施工程危险性较大分部分项工程安全管理实施细则》的通知"

check(
    "core_name 拆掉『印发通知』外壳，取书名号里的正式名称",
    core_name(SHANDONG) == "山东省房屋建筑和市政基础设施工程危险性较大分部分项工程安全管理实施细则",
    core_name(SHANDONG),
)
check(
    "不再产出「关于印发《…》的」这种检索不到任何东西的词（实测 0 条）",
    all(not query.endswith('》的"') for query in build_queries(SHANDONG)),
    str(build_queries(SHANDONG)),
)
queries = build_queries(SHANDONG, max_queries=3)
check("实际发出的 3 个检索词 = 精确短语 + **裸词** + filetype:pdf（形态互不相同）",
      len(queries) == 3 and queries[0].startswith('"关于印发') and not queries[1].startswith('"')
      and "山东省房屋建筑" in queries[1] and "filetype:pdf" in queries[2],
      str(queries))
check("核心名（截掉文种词的纠错形态）没丢，只是退到第 4 位（2026-09-16 起）",
      any(query.strip('"') == core_name(SHANDONG) for query in build_queries(SHANDONG, max_queries=5)),
      str(build_queries(SHANDONG, max_queries=5)))
check(
    "「最新 现行」仍在最后（实测该形态返回的是无匹配兜底页）",
    "最新 现行" in build_queries(SHANDONG, max_queries=5)[4],
    str(build_queries(SHANDONG, max_queries=5)),
)
check(
    "标准类名称的核心名仍然去掉文种词（编制依据表的笔误纠错没被破坏）",
    core_name("高层民用建筑钢结构技术规范") == "高层民用建筑钢结构",
    core_name("高层民用建筑钢结构技术规范"),
)

entry = derive_official_entry(SHANDONG)
check("【#5 的失败链】省级规范性文件的入口指向政府门户，不再送进标准平台",
      entry is not None and "shandong.gov.cn" in entry[0] and "dbba" not in entry[0],
      str(entry))
check("DB37 地方标准仍然走地方标准平台（r21 #8 就是靠它拿到 144 页原件）",
      derive_official_entry("建筑与市政基坑工程监测技术标准")[0].startswith("https://dbba.sacinfo.org.cn/"),
      str(derive_official_entry("建筑与市政基坑工程监测技术标准")))
check("发文机关全称仍然优先（住房和城乡建设部 → 部官网）",
      derive_official_entry("住房和城乡建设部关于修改部分部门规章的决定")[0].startswith("https://www.mohurd.gov.cn/"))


class SiteSearchFailingBrowser(FakeBrowser):
    """站内检索永远拿不到结果：模拟 #5 在平台首页连选 5 次的那个循环。"""

    def __init__(self) -> None:
        super().__init__(artifact="")
        self.site_searches = 0

    def execute(self, url: str, plan: DownloadPlan, output_dir: str) -> str:
        if plan.action == "site_search":
            self.site_searches += 1
            raise RuntimeError("站内检索没有拿到结果：当前页面没有出现检索结果")
        return super().execute(url, plan, output_dir)


class SiteSearchPlanner:
    """永远选 site_search（真实模型在 previous_attempts 存在时也会这么干）。"""

    def __init__(self) -> None:
        self.last_usage = None

    def plan(self, requested_name: str, observation: dict) -> DownloadPlan:
        return DownloadPlan(action="site_search", search_query=requested_name[:12], confidence=0.8, reason="站内检索")


# 候选池为 0 且结果里有政府站 → 走"官方入口"兜底，正是 #5 那条路
workdir = Path(tempfile.mkdtemp(prefix="sitesearch_"))
try:
    loop_browser = SiteSearchFailingBrowser()
    graph = build_graph(
        MultiSearcher([SearchResult(title="山东省人民政府", url="http://www.shandong.gov.cn/", snippet="", domain="www.shandong.gov.cn")]),
        loop_browser,
        SiteSearchPlanner(),
        AgentConfig(download_dir=str(workdir)),
    )
    loop_state = graph.invoke({"requested_filename": SHANDONG})
finally:
    shutil.rmtree(workdir, ignore_errors=True)

check("站内检索空转被硬停在 2 次（旧行为是撞满 5 轮上限）",
      loop_browser.site_searches == 2,
      f"site_search 次数 = {loop_browser.site_searches}")
check("停止原因写进 errors 与 trace",
      any("站内检索已在同一页连续" in item for item in loop_state["final"]["errors"])
      and any(step["node"] == "choose_download" and step["details"].get("skipped") for step in loop_state["trace"]),
      str(loop_state["final"]["errors"])[-160:])
check("硬停之后如实收尾（needs_confirmation），不是崩溃也不是假成功",
      loop_state["final"]["status"] == "needs_confirmation", loop_state["final"]["status"])


# ---------------------------------------------------------------- Q. 被丢弃的搜索结果必须留证
print("=== Q. 判「无关」而丢弃的搜索结果要落盘（回答「阀门有没有误杀」的唯一依据）===")

from standards_crawler_agent.search import relevance_report  # noqa: E402

JUNK_BATCH = [
    SearchResult(title="山东省_百度百科", url="https://baike.baidu.com/item/山东省", snippet="山东省，简称鲁", domain="baike.baidu.com"),
    SearchResult(title="山东省人民政府", url="http://www.shandong.gov.cn/", snippet="山东省人民政府门户网站", domain="www.shandong.gov.cn"),
    SearchResult(title="山东旅游景点大全", url="https://zhuanlan.zhihu.com/p/1", snippet="最值得打卡的50个山东旅游景点", domain="zhuanlan.zhihu.com"),
]
report = relevance_report('"山东省住房和城乡建设厅关于印发山东省建筑施工安全文明标准化工地管理办法的通知"', JUNK_BATCH)
check("relevance_report 给出判定与依据（逐条重合度、来源性质）",
      set(report) >= {"relevant", "rule", "signature_chars", "rows"} and len(report["rows"]) == 3,
      json.dumps(report, ensure_ascii=False)[:200])
check("依据里带上了每条的域名与重合度，可事后核对是否误杀",
      all({"url", "domain", "overlap", "authority", "low_trust"} <= set(row) for row in report["rows"]),
      json.dumps(report["rows"][:1], ensure_ascii=False))
check("整批都是内容站 → 判无关，且理由写明是内容站",
      relevance_report("建筑铝型材", [SearchResult(title="建筑物", url="https://baike.baidu.com/item/建筑物", snippet="", domain="baike.baidu.com")])["relevant"] is False,
      )
check("权威站 + 实质重合 → 判相关（不能误杀 openstd/std.samr 那类真命中）",
      relevance_report("一般用途钢丝绳", [SearchResult(title="国家标准|GB/T 20118-2017 一般用途钢丝绳", url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=X", snippet="一般用途钢丝绳", domain="openstd.samr.gov.cn")])["relevant"] is True)


class DiscardingSearcher:
    """模拟 BrowserSearch 判"头名词兜底页"的那条路：返回空 + last_read 里带原始结果。"""

    def __init__(self, rows: list[SearchResult]) -> None:
        self.rows = rows
        self.last_read: dict = {}

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        self.last_read = {
            "query": query,
            "engine": "bing",
            "verdict": "no_match",
            "attempts": [{"attempt": 1, "rows": len(self.rows), "verdict": "no_match", "url": "https://www.bing.com/search?q=x"}],
            "relevance": relevance_report(query, self.rows),
        }
        # 真实实现里 discarded 就是 relevance["rows"]（同一份带诊断字段的记录）
        self.last_read["discarded"] = self.last_read["relevance"]["rows"]
        return []

    def close(self) -> None:
        pass


class IrrelevantSearcher:
    """模拟"provider 返回了结果、但图里兜底判定为无关"的那条路。"""

    def __init__(self, rows: list[SearchResult]) -> None:
        self.rows = rows
        self.last_read: dict = {}

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        self.last_read = {"verdict": "ok", "attempts": [1]}
        return list(self.rows)

    def close(self) -> None:
        pass


def trace_search_details(searcher, requested: str) -> list[dict]:
    workdir = Path(tempfile.mkdtemp(prefix="discard_"))
    try:
        artifact = workdir / "中华人民共和国突发事件应对法.pdf"
        shutil.copy2(locate("中华人民共和国突发事件应对法.pdf"), artifact)
        graph = build_graph(searcher, FakeBrowser(str(artifact)), FakePlanner("save_page_pdf"), AgentConfig(download_dir=str(workdir)))
        state = graph.invoke({"requested_filename": requested})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return [step["details"] for step in state["trace"] if step["node"] == "search"]


details_list = trace_search_details(DiscardingSearcher(JUNK_BATCH), REQUESTED)
recorded = [batch for details in details_list for batch in details.get("discarded_batches") or []]
check("兜底页被丢弃时，原始结果写进 trace（含标题/URL/重合度）",
      bool(recorded) and len(recorded[0]["discarded"]) == 3 and recorded[0]["relevance"]["rule"],
      json.dumps(recorded[:1], ensure_ascii=False)[:220])
check("被丢弃批次的判定理由也一并落盘（本轮实测就是这条要拿来核对）",
      all("relevance" in batch for batch in recorded) and recorded[0]["verdict"] == "no_match",
      json.dumps([batch.get("verdict") for batch in recorded], ensure_ascii=False))

details_list = trace_search_details(IrrelevantSearcher(JUNK_BATCH), REQUESTED)
recorded = [batch for details in details_list for batch in details.get("discarded_batches") or []]
check("图内兜底判定丢弃的批次同样落盘（两条丢弃路径都留证）",
      any(batch.get("verdict") == "graph_drop" and batch["relevance"]["rows"] for batch in recorded),
      json.dumps(recorded[:1], ensure_ascii=False)[:200])


# ---------------------------------------------------------------- R. Word 产物也要核对内容
print("=== R. .doc/.docx 的内容核对（附件冒充正文的那类假成功）===")

from standards_crawler_agent.download import (  # noqa: E402
    extract_doc_text,
    extract_docx_text,
    verify_file as verify_word_file,
)

WRONG_ANNEX = ARTIFACTS / "wrong_annex.doc"
CORRECT_SPLIT_TITLE = ARTIFACTS / "correct_split_title.docx"

# 真实错件：请求《山东省建筑施工安全文明标准化工地管理办法》，从官方页面下到的却是
# "培育公示牌"附件（第一条/本办法/施行 各 0 次）。旧实现里 Word 文档**完全不做内容核对**，
# 于是它被判成 `success（官方原件）`。
annex_result = verify_word_file(str(WRONG_ANNEX), "山东省建筑施工安全文明标准化工地管理办法")
check("【真实现场复现】附件冒充正文的 .doc 被内容校验拒掉", annex_result.ok is False, str(annex_result.warnings))
check("拒绝理由来自名称核对（不是文件类型问题）",
      any("不匹配" in warning for warning in annex_result.warnings)
      and annex_result.metadata.get("name_checked") == "true",
      f"{annex_result.warnings} | {annex_result.metadata.get('name_match')}")
check("被拒件里确实没有正文特征（第一条/本办法/施行 均不出现）",
      all(word not in extract_doc_text(WRONG_ANNEX) for word in ("第一条", "本办法", "施行")),
      "抽出的文本里出现了正文特征词，说明判据可能误伤")

# 正确件：标题在 docx 里被换行切开（"专项施工方案\n编制指南"），必须仍然通过
split_result = verify_word_file(
    str(CORRECT_SPLIT_TITLE), "关于印发《危险性较大的分部分项工程专项施工方案编制指南》的通知"
)
check("标题被换行切开的正确 docx 仍通过（比片段时忽略空白）", split_result.ok is True, str(split_result.warnings))
check("该件的名称片段命中 2/2", "2/2" in str(split_result.metadata.get("name_match")), str(split_result.metadata.get("name_match")))
check(".docx 会跑正文实质校验（content_checked 标记）",
      split_result.metadata.get("content_checked") == "true", str(split_result.metadata))

# 抽不出文本时：不判失败，但要如实标注"内容未核对"
check("非 OLE 文件按 .doc 抽取时返回空串（不抛异常）",
      extract_doc_text(CORRECT_SPLIT_TITLE) == "", extract_doc_text(CORRECT_SPLIT_TITLE)[:40])
check(".docx 抽取器拿到的是正文而不是二进制",
      "危险性较大" in extract_docx_text(CORRECT_SPLIT_TITLE), extract_docx_text(CORRECT_SPLIT_TITLE)[:40])

_check, _reason = review_flag("success", [], ["内容未核对：.xls 暂不支持文本核对，只核对了文件类型与来源"], [], "", False, True)
check("交付表会把『内容未核对』的产物标进问题清单（而不是当成干净成功）",
      _check is True and "内容未核对" in _reason, _reason)


# ---------------------------------------------------------------- S. 兜底页判据
print("=== S. 兜底页判据：按头名词凑的推荐页不该进候选池 ===")

from standards_crawler_agent.search import (  # noqa: E402
    BrowserSearch,
    distill_query,
    fallback_signature,
    query_gap,
    url_depth,
)

FALLBACK_FIXTURE = json.loads((HERE / "fixtures_fallback_pages.json").read_text(encoding="utf-8"))


def fixture_rows(entry: dict) -> list[SearchResult]:
    return [SearchResult(**row) for row in entry["rows"]]


# 现场复现：查《山东省建筑工程安全专项施工方案编制审查与专家论证办法》，
# 引擎按头名词「山东省」召回——百度百科「山东 省」、省人民政府首页、山东旅游攻略。
# 旧判据（字符集合重合度）判它"相关"，于是省门户首页进了候选池、被观察、被导出，
# 几十次浏览器往返注定失败；判据之后修正为"含字序的连续命中"。
incident = FALLBACK_FIXTURE["现场复现"][0]
incident_signature = fallback_signature(incident["query"], fixture_rows(incident))
check("【真实现场复现】查山东省的文件却返回省人民政府首页/百度百科 → 判兜底",
      incident_signature["fallback"] is True, json.dumps(incident_signature, ensure_ascii=False))
check("现场复现的最长连续命中只有 3 字（就是头名词「山东省」）",
      incident_signature["longest_run"] == 3, str(incident_signature["longest_run"]))
check("判据把依据一并返回（最长连续命中/门槛/最接近的标题/比对用的查询）",
      {"longest_run", "required", "closest_title", "needle"} <= set(incident_signature),
      json.dumps(sorted(incident_signature), ensure_ascii=False))

for entry in FALLBACK_FIXTURE["正例"]:
    signature = fallback_signature(entry["query"], fixture_rows(entry))
    check(f"历史兜底批次被判出（{entry['query'][:20]}…）", signature["fallback"] is True,
          json.dumps(signature, ensure_ascii=False))
for entry in FALLBACK_FIXTURE["反例"]:
    signature = fallback_signature(entry["query"], fixture_rows(entry))
    check(f"真命中的批次没有被误判（{entry['query'][:20]}…）", signature["fallback"] is False,
          f"run={signature['longest_run']}/{signature['required']} {signature['closest_title']}")

# 插空格：引擎会给命中词插空格（「住房 和城乡建设部」），比对前两侧掐空白才不会误杀
SPACED_HIT = [SearchResult(
    title="中华人民共和国 住房 和城乡建设部",
    url="https://www.mohurd.gov.cn/about.html",
    snippet="住房和城乡建设部关于修改部分部门规章的决定",
    domain="www.mohurd.gov.cn",
)]
spaced = fallback_signature("住房和城乡建设部关于修改部分部门规章的决定", SPACED_HIT)
check("命中词被插空格的批次不算兜底（比对前掐空白）", spaced["fallback"] is False,
      json.dumps(spaced, ensure_ascii=False))

# 只给门户首页 + 百科：这正是兜底页的形态（省政务门户首页也是 .gov.cn，不能靠域名放行）
PORTAL_ONLY = [
    SearchResult(title="山东 省人民政府", url="http://www.shandong.gov.cn/", snippet="山东省人民政府", domain="www.shandong.gov.cn"),
    SearchResult(title="山东 省_百度百科", url="https://baike.baidu.com/item/山东省/209822", snippet="山东省，简称鲁", domain="baike.baidu.com"),
]
check("只剩政府门户首页 + 百科 → 判兜底（域名是 .gov.cn 也不放行）",
      fallback_signature("山东省房屋建筑和市政工程质量监督管理办法", PORTAL_ONLY)["fallback"] is True,
      json.dumps(fallback_signature("山东省房屋建筑和市政工程质量监督管理办法", PORTAL_ONLY), ensure_ascii=False))

# 反向出口：批次里有权威站**深层内容页**时不判兜底。实测（回放 2541 条批次）被这条救回来的
# 是「建筑防雷设计」→ openstd 标准详情页、市场监管总局政策页、中国政府网政策页——都是真原件。
DEEP_AUTHORITY = [SearchResult(
    title="国家标准|GB 50057-2010 建筑物防雷设计规范",
    url="https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=8F7AA566756EBD0BB55E7B27D8D54668",
    snippet="建筑物防雷设计规范",
    domain="openstd.samr.gov.cn",
)]
deep = fallback_signature("建筑防雷设计", DEEP_AUTHORITY)
check("字面命中不足但批次里有权威站深层内容页 → 不判兜底（保住真线索）",
      deep["fallback"] is False and deep["deep_authoritative"] is True, json.dumps(deep, ensure_ascii=False))
check("URL 层级：门户首页 0 / 栏目页 1 / 内容页 ≥2",
      (url_depth("http://www.shandong.gov.cn/"), url_depth("https://www.gov.cn/gwyzzjg/"),
       url_depth("https://www.gov.cn/zhengce/202508/content_7038144.htm")) == (0, 1, 3))

# 查询太短（2 个字）时头名词和真命中的字面本来就没区别 → 不判，宁可漏判不误杀
short = fallback_signature("防雷", PORTAL_ONLY)
check("查询太短时不作判断（判错的代价是丢掉一整批真结果）",
      short["fallback"] is False and short["required"] == 0, json.dumps(short, ensure_ascii=False))
check("检索指令与引号在比对前被剥掉",
      distill_query('"建筑与市政降水工程技术规范" filetype:pdf 最新 现行') == "建筑与市政降水工程技术规范",
      distill_query('"建筑与市政降水工程技术规范" filetype:pdf 最新 现行'))

# 检索节奏：串行连发是机器人特征，且实测"整秒连发"与兜底页同时出现过（不能据此断定因果，
# 但没有理由用一个更容易被识别成自动化的节奏）。
check("第一次检索不等（没有上一次）", query_gap(100.0, None, 3.0, 2.0, lambda: 0.5) == 0.0)
check("两次检索挨着发时补足间隔（含随机抖动）", query_gap(100.0, 100.0, 3.0, 2.0, lambda: 0.5) == 4.0,
      str(query_gap(100.0, 100.0, 3.0, 2.0, lambda: 0.5)))
check("已经等够了就不再等", query_gap(200.0, 100.0, 3.0, 2.0, lambda: 0.5) == 0.0)
check("刚判过兜底要多等一会儿（backoff 计入下一次间隔）",
      query_gap(100.0, 100.0, 3.0, 0.0, lambda: 0.0, extra=8.0) == 11.0,
      str(query_gap(100.0, 100.0, 3.0, 0.0, lambda: 0.0, extra=8.0)))
paced = BrowserSearch(min_query_interval=1.0, query_jitter=0.0, fallback_backoff=0.0, rand=lambda: 0.0)
check("BrowserSearch 的节奏参数可注入（默认值来自环境变量）",
      (paced.min_query_interval, paced.query_jitter, paced.fallback_backoff) == (1.0, 0.0, 0.0))
check("首次检索不额外等待", paced._pause_before_query() == 0.0)
paced._last_query_at = time.monotonic()
check("紧接着的第二次检索会补足间隔", paced._pause_before_query() >= 0.9, "间隔没生效")
paced.close()


class FallbackSearcher:
    """模拟 BrowserSearch 判"头名词兜底页"（fallback 判定）的那条路。"""

    def __init__(self, rows: list[SearchResult]) -> None:
        self.rows = rows
        self.last_read: dict = {}

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        signature = fallback_signature(query, self.rows)
        self.last_read = {
            "query": query,
            "engine": "bing",
            "verdict": "fallback",
            "attempts": [{"attempt": 1, "rows": len(self.rows), "verdict": "fallback", "url": "https://cn.bing.com/search?q=x"}],
            "fallback": signature,
            "relevance": relevance_report(query, self.rows),
        }
        self.last_read["discarded"] = self.last_read["relevance"]["rows"]
        return []

    def close(self) -> None:
        pass


details_list = trace_search_details(FallbackSearcher(fixture_rows(incident)), incident["query"])
recorded = [batch for details in details_list for batch in details.get("discarded_batches") or []]
check("判兜底的批次走「未匹配」这条路（不再浪费重读），并把判据数字写进 trace",
      bool(recorded) and recorded[0]["verdict"] == "fallback" and recorded[0]["fallback"]["longest_run"] == 3,
      json.dumps(recorded[:1], ensure_ascii=False)[:260])
check("判兜底时也留下原始批次（事后能核对丢掉的到底是不是垃圾）",
      bool(recorded) and len(recorded[0]["discarded"]) >= 5 and recorded[0]["relevance"]["rule"],
      json.dumps(recorded[:1], ensure_ascii=False)[:200])


# ---------------------------------------------------------------- T. 有头/无头开关
print("=== T. 有头/无头：BROWSER_HEADLESS 与显式传参的优先级 ===")
from standards_crawler_agent.browser import PlaywrightBrowser  # noqa: E402
from standards_crawler_agent.runtime import headless_from_env  # noqa: E402
from standards_crawler_agent.search import BrowserSearch  # noqa: E402

_SAVED_HEADLESS_ENV = os.environ.pop("BROWSER_HEADLESS", None)


def env_headless(value: str | None) -> bool:
    """设定 BROWSER_HEADLESS 后取一次判定结果（用于断言取值表）。"""
    if value is None:
        os.environ.pop("BROWSER_HEADLESS", None)
    else:
        os.environ["BROWSER_HEADLESS"] = value
    return headless_from_env()


def raise_on(value: str) -> bool:
    try:
        env_headless(value)
    except ValueError:
        return True
    return False


check("未设 BROWSER_HEADLESS 时默认**无头**（有头会弹窗抢焦点，只在要看过程时显式开）",
      env_headless(None) is True)
check("BROWSER_HEADLESS 的 0/false/no/off/headed 都判有头",
      all(env_headless(value) is False for value in ("0", "false", "no", "off", "headed", "HEAD")),
      str([env_headless(value) for value in ("0", "false", "no", "off", "headed", "HEAD")]))
check("BROWSER_HEADLESS 的 1/true/yes/on/headless 都判无头",
      all(env_headless(value) is True for value in ("1", "true", "yes", "on", "headless")),
      str([env_headless(value) for value in ("1", "true", "yes", "on", "headless")]))
check("取值写错要当场报错（否则 'Flase' 会静默跑成默认模式）", raise_on("Flase") and raise_on("有头"))
# 只构造对象、不启动浏览器：_ensure_context 才 launch，因此这里仍然是离线断言。
env_headless("1")
check("不传参时 BrowserSearch / PlaywrightBrowser 跟随环境变量（这里压回无头）",
      BrowserSearch().headless is True and PlaywrightBrowser().headless is True)
env_headless(None)
check("未设变量时两个类都走默认（无头）",
      BrowserSearch().headless is True and PlaywrightBrowser().headless is True)
env_headless("0")
explicit_headless = BrowserSearch(headless=True).headless is True and PlaywrightBrowser(headless=True).headless is True
env_headless("1")
explicit_headed = BrowserSearch(headless=False).headless is False and PlaywrightBrowser(headless=False).headless is False
check("显式传参优先于环境变量（两个方向都要能压）", explicit_headless and explicit_headed)
if _SAVED_HEADLESS_ENV is None:
    os.environ.pop("BROWSER_HEADLESS", None)
else:
    os.environ["BROWSER_HEADLESS"] = _SAVED_HEADLESS_ENV


# ---------------------------------------------------------------- U. 选择器字段 / 黑名单 / 兜底换路
print("=== U. 选择器字段与提示词一致、黑名单不误杀、兜底页换路重试 ===")
from standards_crawler_agent.ranking import is_low_trust_domain  # noqa: E402
from standards_crawler_agent.ranker import CHOOSER_PROMPT, _candidate_payload  # noqa: E402
from standards_crawler_agent.search import BrowserSearch, toggle_quote_form  # noqa: E402

# 1) 提示词点名的字段必须**真的发出去**：曾经提示词写着"每条包含 official_score（来源权威分）"，
#    而 payload 里从来没有这个字段——模型被承诺了一个拿不到的数字，于是自己猜来源可信度，
#    把"URL 以 .pdf 结尾"当成了权威证据（实测 12 次改序里 8 次失败，理由多为这一条）。
sample_page = CandidatePage(
    url="https://www.gov.cn/zhengce/2022/x.pdf",
    title="建设工程质量检测管理条例",
    text="住房和城乡建设部令",
    domain="www.gov.cn",
    official_score=0.8,
    page_score=0.95,
    evidence=["域名属于中央/部委政府站点"],
)
payload = _candidate_payload(0, sample_page)
promised_fields = (
    "idx", "domain", "title", "snippet", "url", "is_document_url", "source_tier",
    "official_score", "page_score", "combined_score",
)
check("提示词点名的每个字段都在 payload 里（含曾被漏掉的 official_score）",
      set(promised_fields) <= set(payload)
      and all(name in CHOOSER_PROMPT for name in promised_fields),
      f"缺字段：{sorted(set(promised_fields) - set(payload))}")
check("payload 里的分数就是页面对象上的分数（不重算、不串位）",
      payload["official_score"] == 0.8 and payload["page_score"] == 0.95
      and abs(payload["combined_score"] - 0.875) < 0.01,
      str(payload))
check("提示词写明了『URL 以 .pdf 结尾不是权威证据』（实测最常见的错误改选理由）",
      ".pdf 结尾" in CHOOSER_PROMPT and "页面证据" in CHOOSER_PROMPT)

# 2) 黑名单：按"实测 1117 份产物"审计出、且**从未交付过任何文件**的域名才收；
#    凡科建站托管（faiusr.com）被验证抓出误杀后撤掉，这里把它钉住。
ADDED_BLACKLIST = (
    "www.soujianzhu.cn", "www.cbi360.net", "www.waizi.org.cn", "doc.quark.cn",
    "doc.xuehai.net", "ks.wjx.com", "cp.baidu.com", "report.baidu.com",
    "home.baidu.com", "detail.youzan.com", "www.biaozhun.org",
)
check("审计出的 11 个『被选中过、0 成功、0 交付』域名进入硬拒绝",
      all(is_low_trust_domain(domain) for domain in ADDED_BLACKLIST),
      str([domain for domain in ADDED_BLACKLIST if not is_low_trust_domain(domain)]))
check("凡科建站托管不在黑名单里：它交付过《岩棉薄抹灰外墙外保温系统材料》",
      not is_low_trust_domain("26431875.s21i.faiusr.com")
      and not is_low_trust_domain("www.16334437.s21i.faiusr.com"))

# 3) 引号形态对调：驱动"兜底页后换一种问法"的那一步。
check("带引号的检索词对调成裸词，且**保留** filetype:pdf 限定",
      toggle_quote_form('"建设工程质量检测管理条例" filetype:pdf') == "建设工程质量检测管理条例 filetype:pdf",
      toggle_quote_form('"建设工程质量检测管理条例" filetype:pdf'))
check("裸词对调成精确短语", toggle_quote_form("建设工程质量检测管理条例") == '"建设工程质量检测管理条例"')
check("多个引号段一起处理，空串不炸",
      toggle_quote_form('"甲" "乙"') == "甲 乙" and toggle_quote_form("") == "")

# 4) 兜底页 → 换"提交方式 + 引号形态"再问一次（假页面，离线不触网）。
JUNK_SERP = """<html><body>
<li class="b_algo"><h2><a href="https://www.ccb.com/ebank">中国建设银行-网上银行</a></h2>
<p>中国建设银行个人网上银行</p></li>
</body></html>"""
REAL_SERP = """<html><body>
<li class="b_algo"><h2><a href="https://www.gov.cn/zhengce/2022/x.htm">建设工程质量检测管理条例</a></h2>
<p>住房和城乡建设部令 建设工程质量检测管理办法</p></li>
</body></html>"""


class FakeSerpPage:
    """假结果页：拼 URL 直达给兜底页，搜索框提交后给真结果。"""

    def __init__(self) -> None:
        self.url = ""
        self.mode = "goto"
        self.typed = ""
        self.presses: list[str] = []

    def goto(self, url, **_kwargs):
        self.mode = "box" if url.rstrip("/").endswith("cn.bing.com") else "goto"
        self.url = url

    def wait_for_selector(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, *_args):
        return None

    def wait_for_function(self, *_args, **_kwargs):
        return True

    def fill(self, *_args):
        return None

    def type(self, _selector, text, delay=0):
        self.typed = text

    def press(self, _selector, key):
        if key == "Enter":
            self.presses.append(self.typed)
            self.url = f"https://cn.bing.com/search?q={self.typed}"

    def content(self):
        return REAL_SERP if self.mode == "box" else JUNK_SERP


class FakePageSearch(BrowserSearch):
    def __init__(self, page, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fake_page = page

    def _ensure_page(self):
        return self.fake_page

    def close(self) -> None:
        pass


fake_page = FakeSerpPage()
rescuer = FakePageSearch(
    fake_page, min_query_interval=0.0, query_jitter=0.0, fallback_backoff=0.0, rand=lambda: 0.0, read_attempts=1
)
rescued_rows = rescuer.search('"建设工程质量检测管理条例"', limit=10)
attempt_paths = [item.get("path") for item in (rescuer.last_read.get("attempts") or [])]
check("goto 拿到兜底页后改用搜索框，并把引号**去掉**再问一次",
      attempt_paths == ["gotourl", "searchbox"] and fake_page.presses == ["建设工程质量检测管理条例"],
      f"paths={attempt_paths} presses={fake_page.presses}")
check("搜索框那次拿到真结果就直接采用，并在 last_read 里留下 rescued_by",
      len(rescued_rows) == 1 and rescuer.last_read.get("verdict") == "ok"
      and rescuer.last_read.get("rescued_by") == "searchbox",
      str(rescuer.last_read.get("verdict")))
check("换路拿到的页面确实是政府站那条（不是兜底页那批）",
      rescued_rows and rescued_rows[0].domain == "www.gov.cn", str([row.url for row in rescued_rows]))
rescuer.close()


# ---------------------------------------------------------------- V. 文档直链的内联渲染
print("=== V. 有头浏览器的 PDF 内联渲染，必须与无头下的『下载』得到同一个结论 ===")
from standards_crawler_agent.browser import PlaywrightBrowser, _looks_like_blank_document_view  # noqa: E402

check("空文档页判为『文档直链』（有头 Chromium 内置 PDF 阅读器的形态）",
      _looks_like_blank_document_view(
          "https://www.jinan.gov.cn/attach/0/abc.pdf", {"links": [], "stats": {"text_total_chars": 0}}
      ))
check("带链接的文档页不算（真正的发布页一定带链接，判据不能把可操作信息丢掉）",
      not _looks_like_blank_document_view(
          "https://x.gov.cn/a.pdf", {"links": [{"url": "https://y"}], "stats": {"text_total_chars": 0}}
      ))
check("有正文的文档页不算",
      not _looks_like_blank_document_view(
          "https://x.gov.cn/a.pdf", {"links": [], "stats": {"text_total_chars": 900}}
      ))
check("非文档扩展名不算（普通网页仍按网页处理）",
      not _looks_like_blank_document_view(
          "https://x.gov.cn/a.htm", {"links": [], "stats": {"text_total_chars": 0}}
      ))


class FakeDocumentPage:
    """有头 Chromium 内联渲染文档之后的页面：导航成功，但正文与链接都可能是空的。"""

    def __init__(self, html: str, url: str) -> None:
        self._html = html
        self.url = url
        self.closed = False

    def content(self):
        return self._html

    def title(self):
        return ""

    def evaluate(self, *_args):
        return []

    def close(self):
        self.closed = True


class FakeDocumentBrowser(PlaywrightBrowser):
    def __init__(self, page) -> None:
        super().__init__()
        self.fake = page

    def _ensure_context(self):
        return None

    def _open(self, url):
        return self.fake


PDF_URL = "https://www.jinan.gov.cn/attach/0/78be0c19a3ad4346a50415bc7012b728.pdf"
blank_pdf = FakeDocumentBrowser(FakeDocumentPage("<html><body></body></html>", PDF_URL))
blank_observation = blank_pdf.observe(PDF_URL)
check("内联渲染的空 PDF：observe 直接返回 direct_file（不再让 graph 判『页面几乎没有内容』）",
      bool(blank_observation.get("direct_file")),
      str(blank_observation.get("direct_file"))[:70])
check("direct_file 里的地址与扩展名都对得上，且页面被关掉（不漏页面）",
      (blank_observation.get("direct_file") or {}).get("extension") == ".pdf"
      and blank_observation["direct_file"]["url"] == PDF_URL
      and blank_pdf.fake.closed is True)
with_content = FakeDocumentBrowser(FakeDocumentPage(
    '<html><body><main>' + "正文" * 200 + '</main><a href="https://a.gov.cn/f.pdf">附件</a></body></html>',
    PDF_URL,
))
with_content_observation = with_content.observe(PDF_URL)
check("文档页里有正文和链接时不受影响（仍按页面观察，不给 direct_file）",
      not with_content_observation.get("direct_file") and with_content_observation.get("links"),
      str(with_content_observation.get("stats")))


# ---------------------------------------------------------------- W. 无痕（--incognito）
print("=== W. 有头时显式无痕启动、无头时行为不变 ===")
import standards_crawler_agent.browser as browser_module  # noqa: E402
import standards_crawler_agent.search as search_module  # noqa: E402
from standards_crawler_agent.runtime import chromium_launch_args  # noqa: E402

check("有头启动参数 = ['--incognito']", chromium_launch_args(False) == ["--incognito"], str(chromium_launch_args(False)))
check("无头不传参数（无头 shell 不保证支持 --incognito，那里也没有窗口要标明）",
      chromium_launch_args(True) == [], str(chromium_launch_args(True)))


class FakeLaunchBrowser:
    def is_connected(self) -> bool:
        return True

    def new_context(self, **_kwargs):
        return object()

    def new_page(self):
        return object()

    def close(self) -> None:
        pass


class FakeLaunchChromium:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        return FakeLaunchBrowser()


class FakeLaunchPlaywright:
    def __init__(self) -> None:
        self.chromium = FakeLaunchChromium()


def launch_kwargs() -> list[dict]:
    """用假 Playwright 走一遍两个类的启动路径，收回它们真正传出去的参数。"""
    fake = FakeLaunchPlaywright()
    saved_browser, saved_search = browser_module.acquire_playwright, search_module.acquire_playwright
    browser_module.acquire_playwright = lambda: fake
    search_module.acquire_playwright = lambda: fake
    try:
        PlaywrightBrowser(headless=False)._ensure_context()
        BrowserSearch(headless=True, min_query_interval=0.0, query_jitter=0.0)._ensure_page()
    finally:
        browser_module.acquire_playwright = saved_browser
        search_module.acquire_playwright = saved_search
    return fake.chromium.calls


actual_kwargs = launch_kwargs()
check("页面浏览器（有头）确实把 --incognito 传给了 launch",
      bool(actual_kwargs) and actual_kwargs[0] == {"headless": False, "args": ["--incognito"]},
      str(actual_kwargs[:1]))
check("检索浏览器（无头）传的是空参数，与改动前一致",
      len(actual_kwargs) > 1 and actual_kwargs[1] == {"headless": True, "args": []},
      str(actual_kwargs[1:2]))


# ---------------------------------------------------------------- X. 同名产物：相同就跳过
print("=== X. 同名产物：内容相同不重写、内容不同新版盖旧版 ===")
import tempfile  # noqa: E402
from standards_crawler_agent.download import same_content  # noqa: E402

work = Path(tempfile.mkdtemp(prefix="dsh_same_content_"))
old_file = work / "old.pdf"
same_file = work / "same.pdf"
diff_file = work / "diff.pdf"
old_file.write_bytes(b"%PDF-1.4 identical payload")
same_file.write_bytes(b"%PDF-1.4 identical payload")
diff_file.write_bytes(b"%PDF-1.4 a different payload")

check("字节完全相同 → 判相同（走『跳过重写』，不白写一次盘、也不改旧文件时间戳）",
      same_content(old_file, same_file) is True)
check("大小相同但内容不同 → 判不同（按规则走新版盖旧版）", same_content(old_file, diff_file) is False)
check("对方不存在 → 判不同（不能让 stat 的异常冒到上层）",
      same_content(old_file, work / "nope.pdf") is False)

# 规则是"新版直接盖旧版"，但**相同就别写**——这条要在 graph 的改名步骤里真的接上，
# 不能只写一个没人调用的函数（此前 payload 缺字段就是这么错的）。
graph_source = (ROOT / "src" / "standards_crawler_agent" / "graph.py").read_text(encoding="utf-8")
check("graph 的改名步骤确实接上了 same_content（相同就跳过重写）",
      "same_content(source, target)" in graph_source, "graph.py 里没找到该调用")


# ---------------------------------------------------------------- 汇总
print()
print("=" * 70)
print(f"断言 {PASSED + len(FAILED)} 项，通过 {PASSED} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("\n失败明细：")
    for item in FAILED:
        print(f"  [FAIL] {item}")
sys.exit(0 if not FAILED else 1)
