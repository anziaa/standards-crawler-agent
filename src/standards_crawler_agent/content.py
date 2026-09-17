"""判定「一份产物里到底有没有目标文件的正文」。

这是本项目唯一一处"内容是否真的是文件"的判据来源，`download.py`（校验落盘产物）
与 `browser.py`（在导出网页之前就识别"本页没有全文"）都从这里取用，避免两处各写一套。

为什么需要它
------------
原来的内容校验只问"请求名称是否出现在前几页文本里"。而**标准平台元数据页、
第三方文库付费外壳页恰恰完整印着目标文件的全名**（这是它们的立身之本），
于是名称校验必然通过，一份空壳被改名成官方原件的样子、记为成功。
实测：`downloads/` 里 24 个文件只有网页外壳（其中 GB/T 8923.1-2011 那一页
自己写着"本系统暂不提供在线阅读服务"），却全部记为成功。

这里的判据分成两类，顺序不能颠倒：
  1. **否定证据**（页面自认没有全文 / 登录墙 / 商业营销外壳）——这些页面在结构上
     就不是文档宿主，即使正文里出现几条条款编号也仍是预览或软文；
  2. **肯定证据**（条款、章节、目次、前言、附录、编号条目等文档结构）。
两类都没有、篇幅又极小，按"未见正文"处理。
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# 一、否定证据
# --------------------------------------------------------------------------

# 页面自己声明"本站没有可读全文"。这是最强证据：平台主动告知没有全文，
# 此时无论导出成什么都是空壳。
NO_FULLTEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"暂不提供在线阅读服务", "平台声明暂不提供在线阅读服务"),
    (r"不提供在线阅读", "平台声明不提供在线阅读服务"),
    (r"到馆阅读|到馆提醒", "平台标注需到馆阅读，未提供在线全文"),
    (r"还剩\s*\d+\s*页未读", "文库阅读器只公开部分页（还剩 N 页未读）"),
    (r"请拖动滑块继续阅读", "文库以滑块拦截继续阅读，正文未公开"),
    (r"不看了，直接下载", "文库下载引导页，正文未公开"),
)

# 平台声明"只能在线读、不提供下载"。**这不是"没有全文"**：正文可以在该站的
# 在线阅读器里看到，正确做法是点开阅读入口（在线阅读/在线预览/查看文本）后导出阅读页。
# 曾经把「仅提供在线阅读服务」错当成"平台声明没有全文"，那会把
# 《坠落防护 安全绳》GB 24543-2009 这类本来能拿到的标准直接判成"拿不到"——
# 也就是说，那条判据本身会**挡住正确的路**，必须与"确实没有全文"分开。
ONLINE_READING_ONLY_PATTERNS: tuple[str, ...] = (
    r"仅提供在线阅读服务",
    r"仅提供在线预览",
)

# 登录/注册墙：要扫码或登录之后才有全文，agent 不绕过访问控制，因此本页没有正文。
LOGIN_WALL_PATTERNS: tuple[str, ...] = (
    r"使用微信扫一扫登录",
    r"扫一扫登录",
    r"微信扫码登录",
    r"登录后(?:即可)?(?:查看|阅读|下载)",
    r"注册后(?:即可)?(?:查看|阅读|下载)",
)

# 商业营销 / 文库聚合外壳的自身标识。阈值取 3 且要求**没有正式文档结构**：
# 政府站点页脚常有一句"技术支持：某某有限公司"，只有 1 处命中，不会误伤。
COMMERCIAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"有限公司|有限责任公司|股份公司", "企业主体信息"),
    (r"主营|进店|通过真实性核验|法定代表人|法人\s*[:：]", "企业营销信息"),
    (r"导读\s*[:：]|推荐文章", "内容营销结构"),
    (r"上传于\s*[:：]|粉丝量\s*[:：]|上传文档|阅读了该文档的用户还阅读了|下载积分|加入阅读清单", "文库站点外壳"),
    (r"提供正版标准|批量采购服务|标准数据定制化|时效性核查服务", "标准代购/定制服务"),
    (r"立即购买|加入购物车|开通会员|付费下载|付费阅读", "付费墙"),
)
COMMERCIAL_MIN_HITS = 3

# 标准著录页：只有标准号/名称/状态/日期这些登记信息，没有正文。
METADATA_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"标准号\s*[：:]", "标准号"),
    (r"中文(?:标准)?名称\s*[：:]", "中文标准名称"),
    (r"英文(?:标准)?名称\s*[：:]", "英文标准名称"),
    (r"中国标准分类号|国际标准分类号", "标准分类号"),
    (r"归口(?:部门|单位)|主管(?:部门|单位)", "归口/主管部门"),
    (r"标准状态\s*[：:]|标准类型", "标准状态"),
    (r"发布(?:日期|单位)\s*[：:]|实施日期", "发布/实施信息"),
)
METADATA_MIN_HITS = 2

# 页面上通往正文的入口控件：命中说明"本页没有正文"不等于"这个文件拿不到"。
# 元组顺序即优先级——能直接下到文件的最先试，纯阅读器最后试，见 prefer_reading_entry。
READING_ENTRY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"下载标准", "下载标准"),
    (r"查看文本|查看全文|查看原文", "查看文本"),
    (r"在线阅读", "在线阅读"),
    (r"在线预览", "在线预览"),
)

# --------------------------------------------------------------------------
# 二、肯定证据：文档正文的结构特征
# --------------------------------------------------------------------------

# (标签, 正则, 至少出现次数)
STRUCTURE_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("条款", re.compile(r"第[一二三四五六七八九十百零]{1,4}条"), 3),
    ("章节", re.compile(r"第[一二三四五六七八九十百零]{1,4}章"), 1),
    ("目次", re.compile(r"目\s*次"), 1),
    ("前言", re.compile(r"前\s*言"), 1),
    ("附录", re.compile(r"附\s*录\s*[A-ZＡ-Ｚ]"), 1),
    ("规范性引用文件", re.compile(r"规范性引用文件"), 1),
    ("编号条款", re.compile(r"(?m)^\s*\d+(?:\.\d+){1,3}\s"), 5),
    # 政务文体的"一、二、三……"条目。实测《关于实施〈危险性较大的分部分项工程
    # 安全管理规定〉有关问题的通知》整篇用这种编号，没有"第X条"。
    ("编号条目", re.compile(r"(?m)^\s*[一二三四五六七八九十]{1,3}、"), 3),
)

# 判定"这是一份正式文档"时**算数**的结构；只命中"编号条目"不算——
# 营销软文也用"一、二、三"分节，不能靠它证明文件正文存在。
FORMAL_STRUCTURE = ("条款", "章节", "目次", "前言", "附录", "规范性引用文件")

# 极短且毫无结构：网页外壳而不是文件。
TINY_PAGE_LIMIT = 3
TINY_CHAR_LIMIT = 2500

# 扫描件判定：页数够多、文本层几乎为空、页面以图像承载正文。
SCANNED_MIN_PAGES = 5
SCANNED_MAX_CHARS = 200


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _hits(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [pattern for pattern in patterns if re.search(pattern, text or "")]


def detect_no_fulltext_statement(text: str) -> str | None:
    """页面是否自己声明"没有可读全文"。返回说明文字，没命中返回 None。"""
    for pattern, label in NO_FULLTEXT_PATTERNS:
        if re.search(pattern, text or ""):
            return label
    return None


def page_without_fulltext(text: str) -> str | None:
    """页面上是否存在"这里拿不到全文"的直接证据（平台声明 / 登录墙）。

    观察阶段用它在**导出网页之前**就判定"本页没有全文"，避免白白导出一个空壳；
    校验阶段用同一套判据复查产物，两处不会各说各话。

    注意：「仅提供在线阅读服务」**不**算没有全文，见 ONLINE_READING_ONLY_PATTERNS。
    """
    label = detect_no_fulltext_statement(text)
    if label:
        return label
    if _hits(text, LOGIN_WALL_PATTERNS):
        return "页面是登录/扫码墙，需要有账号才能看到全文（agent 不绕过访问控制）"
    return None


def online_reading_only(text: str) -> str | None:
    """平台声明"只能在线读、不提供下载"。返回说明，没命中返回 None。

    这类页面的正确走法是点开「在线阅读 / 在线预览 / 查看文本」进入阅读页后导出，
    绝不能被当成 no_download。
    """
    for pattern in ONLINE_READING_ONLY_PATTERNS:
        if re.search(pattern, text or ""):
            return "平台声明仅提供在线阅读（没有下载入口，正文需在阅读页里看）"
    return None


def reading_entries(text: str) -> list[str]:
    """页面上通往正文的入口控件（按优先级排序）。

    命中这些说明"本页没有正文"不等于"这个文件拿不到"——正文在入口后面。
    实测 30 个空壳件里有 12 个（6 个交通部页面 + 5 个国家标准平台页面 + 1 个
    坠落防护 安全绳）页面上就有「在线阅读 / 在线预览 / 下载标准」入口，
    属于"有入口但没走通"，不是"平台不提供"。
    """
    return [label for pattern, label in READING_ENTRY_PATTERNS if re.search(pattern, text or "")]


def prefer_reading_entry(entries: list[str]) -> str | None:
    """从若干入口标签里挑一个最可能直接拿到文件的。

    顺序按"离文件有多近"：『下载标准』直接给文件，『查看文本』进阅读页
    （国家标准平台那一页才有下载按钮），『在线阅读/在线预览』是纯阅读器。
    """
    for label in [item[1] for item in READING_ENTRY_PATTERNS]:
        if label in entries:
            return label
    return entries[0] if entries else None


def clickable_reading_entries(entries: list[str], control_labels: list[str]) -> list[str]:
    """只保留**在页面控件里真实存在**的入口标签。

    为什么需要这一步（实测）：`reading_entry` 是从页面**文本**里认出来的，而
    「在线预览」这四个字还会出现在按钮标签之外的很多地方——i18n 脚本、
    「本系统仅提供在线阅读服务」那句话、隐藏模板。提示词又要求
    "出现 reading_entry 就选 click_download（用里面的入口文字）"，于是模型在
    **没有该控件**的一页上点了它，白等 8 秒定位超时后收工：

        [download] err=无法点击下载控件（text=在线预览）：Locator.click: Timeout 8000ms exceeded.
        Call log: - waiting for locator("text=在线预览").first

    文本里有 ≠ 页面上有可点的控件，所以这里和真实控件标签求一次交集。
    """
    if not entries:
        return []
    labels = [_squash_ws(label) for label in (control_labels or [])]
    labels = [label for label in labels if label]
    kept: list[str] = []
    for entry in entries:
        needle = _squash_ws(entry)
        if any(needle in label or label in needle for label in labels):
            kept.append(entry)
    return kept


def _squash_ws(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def page_is_not_a_document(text: str) -> str | None:
    """观察阶段判断"这一页本身不是正文页"。

    用一个大 page_count 调用 detect_absent_body：观察结果只带前 8000 字，
    "篇幅太小"那条判据在这里没有意义，会误伤正文很长的页面。
    返回 None 表示"不能断定它不是正文页"——此时不要干预模型的动作选择。
    """
    return detect_absent_body(text, page_count=10_000)


def document_body_evidence(text: str, formal_only: bool = False) -> list[str]:
    """文本里出现了哪些文档正文结构特征。"""
    labels = list(FORMAL_STRUCTURE) if formal_only else [item[0] for item in STRUCTURE_PATTERNS]
    found: list[str] = []
    for label, pattern, need in STRUCTURE_PATTERNS:
        if label not in labels:
            continue
        if len(pattern.findall(text or "")) >= need:
            found.append(label)
    return found


def detect_absent_body(text: str, page_count: int) -> str | None:
    """判断这份产物里是否根本没有目标文件的正文。返回原因；有正文时返回 None。

    判据顺序见模块文档：否定证据 → 肯定证据 → 无证据且篇幅极小。
    """
    body = document_body_evidence(text)
    formal = [label for label in body if label in FORMAL_STRUCTURE]

    label = page_without_fulltext(text)
    if label:
        return f"产物中没有文件正文：{label}（落盘的是网站页面，不是文件本身）"

    commercial = [name for pattern, name in COMMERCIAL_PATTERNS if re.search(pattern, text or "")]
    if len(commercial) >= COMMERCIAL_MIN_HITS and not formal:
        return (
            "产物中没有文件正文：页面是商业营销/文库聚合外壳"
            f"（命中 {'、'.join(commercial)}），不是目标文件"
        )

    if body:
        return None

    metadata = [name for pattern, name in METADATA_PATTERNS if re.search(pattern, text or "")]
    if len(metadata) >= METADATA_MIN_HITS:
        entries = reading_entries(text)
        hint = (
            f"；但本页有通往正文的入口（{'、'.join(entries)}），应点进阅读页取正文，而不是导出本页"
            if entries
            else ""
        )
        return (
            f"产物中没有文件正文：页面只有标准著录信息（{'、'.join(metadata)}），"
            f"没有任何正文结构（条款/章节/目次/前言）{hint}"
        )

    if page_count <= TINY_PAGE_LIMIT and len(_compact(text)) < TINY_CHAR_LIMIT:
        return (
            f"产物中没有文件正文：只有 {page_count} 页、约 {len(_compact(text))} 字，"
            "未见任何正文结构（条款/章节/目次/前言），是网页外壳而非目标文件"
        )
    return None


def detect_scanned_only(page_count: int, text: str, image_pages: int) -> str | None:
    """整份 PDF 没有可用文本层、正文以图像承载时的说明（原始内容是存在的）。"""
    if page_count < SCANNED_MIN_PAGES or image_pages <= 0:
        return None
    if len(_compact(text)) > SCANNED_MAX_CHARS:
        return None
    return (
        f"扫描件：{page_count} 页均无可提取文本层（其中 {image_pages} 页为图像），"
        "正文以图像承载，无法做文本比对"
    )


# --------------------------------------------------------------------------
# 三、标准号的文本工具
#
# 这两个函数原先在 download.py 里。为了让"页面声明的身份"核对也能量标准号，
# 而 download.py 又 import 本模块（反向依赖会成环），把纯文本工具迁到这里，
# download.py / graph.py 改为从本模块取用——标准号的写法只在处一维护。
# --------------------------------------------------------------------------


def extract_standard_number(text: str) -> str | None:
    """从文档首页/前几页提取常见国家、行业或地方标准编号。"""
    text = re.sub(r"\s+", " ", text).replace("－", "—").replace("−", "—")
    patterns = [
        r"\b(?:GB|GB\s*/\s*T|JGJ|JTG\s*(?:/\s*T)?|DL\s*/\s*T|SL\s*/\s*T|DB\d{2,})\s*[A-Z]?\s*\d{1,6}\s*[—–-]\s*\d{4}\b",
        r"\b[A-Z]{1,8}(?:/[A-Z]{1,4})?\s*\d{2,6}\s*[—–-]\s*\d{4}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            value = value.replace("-", "—").replace("–", "—")
            value = re.sub(r"\s*/\s*", "/", value)
            value = re.sub(r"\s*—\s*", "—", value)
            return value
    return None


def normalize_standard_number(value: str | None) -> str:
    """统一标准号写法：各种破折号统一成半角连字符，去掉"/"与"-"两侧空格。

    PDF 正文里常见 "GB 50204—2015"（全角破折号），与编制依据表里的
    "GB50204-2015"不一致，落盘前统一，便于和表格对照。
    """
    if not value:
        return ""
    text = value.strip()
    text = text.replace("／", "/")
    for dash in ("—", "–", "－", "−", "‐", "‑", "―"):
        text = text.replace(dash, "-")
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_")


# --------------------------------------------------------------------------
# 四、著录页自己声明的身份 —— 在**下载之前**就认出"这不是目标文件"
# --------------------------------------------------------------------------

# 著录页（标准平台的详情页）会明写这两个字段的样子：
#   「标准号：GB 6095-2021 中文标准名称： 坠落防护 安全带 英文标准名称：…」
# 只在**字段存在**时才核对：普通列表页 / 公文页 / 检索结果页没有它们 → 不干预，
# 避免挡住"进入栏目页继续探索"这条正常路径。上线前用真实候选页验证过：
# 19 个候选里只有 2 个带这些字段，其余一律不干预。
IDENTITY_NUMBER_FIELD = re.compile(
    r"标准号\s*[：:]\s*([A-Za-z]{1,8}(?:\s*/\s*[A-Za-z]{1,3})?\s*\d[\d.]*\s*[-—–－]\s*\d{4})"
)
IDENTITY_NAME_FIELD = re.compile(
    r"中文(?:标准)?名称\s*[：:]\s*(.+?)(?=(?:英文标准名称|英文名称|归口|主管|标准状态|标准类型|"
    r"发布日期|实施日期|中国标准分类号|国际标准分类号|备注|起草|代替|$))"
)

# 阈值来自实测：
#   《坠落防护 安全绳》vs 页面《坠落防护 安全带》          6/7 = 86%  → 拒绝（这次下错的根因）
#   《高层民用建筑钢结构技术规范》vs 页面《…技术规程》      12/13 = 92% → 放行（文种词差异）
#   《混凝土结构施工质量验收规范》vs 页面《混凝土结构工程施工质量验收规范》 100% → 放行（漏字笔误）
TITLE_COVERAGE_MIN = 0.9
TITLE_LENGTH_RATIO_MAX = 1.6


def declared_identity(text: str) -> tuple[str | None, str | None]:
    """从著录页抽出 (标准号, 中文标准名称)；两个字段都没有时返回 (None, None)。"""
    number = IDENTITY_NUMBER_FIELD.search(text or "")
    name = IDENTITY_NAME_FIELD.search(text or "")
    return (
        normalize_standard_number(number.group(1)) if number else None,
        re.sub(r"\s+", " ", name.group(1)).strip() if name else None,
    )


def _title_chars(value: str) -> str:
    return re.sub(r"[\s《》〈〉「」『』（）()、，,。；;：:·\-—_/／－]", "", value or "")


# 两个名字长短不一（一个是另一个的前缀）时，多出来的部分**必须**是限定语，
# 不能是新的中心词。反例（实测写下这条断言时踩到的）：
#   请求《坠落防护 安全绳》 vs 页面《坠落防护 安全绳用连接器》——那是另一份标准，
#   但纯子串判断会直接放行。
# 正例（必须放行）：
#   《…安全绳》+（附条文说明）、《固定式钢梯及平台安全要求》+ 第1部分：钢直梯、
#   以及纯粹的编号/标点/拉丁后缀。
# 注意 _title_chars 已经把括号和标点去掉了，所以这里比较的是去括号后的形态。
QUALIFIER_SUFFIX = re.compile(
    r"^(?:"
    r"[（(][^)）]*[)）]"                      # （附条文说明）(2018年版)
    r"|附?条文说明"
    r"|第[一二三四五六七八九十0-9]+部分.*"     # 第1部分：钢直梯
    r"|[A-Za-z0-9０-９.．\-—_/、:：]*"          # 纯编号/标点/拉丁后缀
    r")$"
)


def compare_declared_identity(
    declared_number: str | None, declared_title: str | None, requested_name: str
) -> tuple[bool, str]:
    """核对"页面自己声明的身份"是否就是请求的目标文件。

    **不要直接复用 check_name_match**：它的第一道闸门是 `looks_readable_text`
    （要求 ≥120 字、高频汉字占比 ≥1%），而"中文标准名称"字段只有十来个字，
    永远过不了那道闸门，于是它会一律返回"文本层不可读或过短，跳过名称校验"——
    闸门等于没接上。这个坑在做上线前验证时先踩到了一次（安全带那页被判成"通过"）。

    容忍度与 check_name_match 保持一致：允许文种词差异与漏字笔误，
    但《安全绳》和《安全带》这种"同长度、换一个关键字"必须被拒。
    """
    number_in_request = extract_standard_number(requested_name or "")
    if declared_number and number_in_request:
        left = normalize_standard_number(declared_number)
        right = normalize_standard_number(number_in_request)
        if left != right:
            return False, f"页面声明的标准号「{left}」与请求名里的「{right}」不一致"

    requested = _title_chars(requested_name)
    declared = _title_chars(declared_title or "")
    if not requested or not declared:
        return True, "页面未声明可比对的正式名称，跳过核对"

    if requested == declared:
        return True, f"页面声明的正式名称与请求名一致（{declared_title}）"

    # 前缀关系：多出来的那段必须是限定语，否则是另一份文件（见 QUALIFIER_SUFFIX）。
    if declared.startswith(requested) or requested.startswith(declared):
        longer, shorter = (declared, requested) if declared.startswith(requested) else (requested, declared)
        extra = longer[len(shorter):]
        if QUALIFIER_SUFFIX.match(extra):
            return True, f"页面声明的正式名称与请求名相符（{declared_title}；多出的限定语「{extra}」）"
        return False, (
            f"页面声明的正式名称与请求名不符（页面声明「{declared_title}」，"
            f"比请求名多出「{extra}」，像是另一份文件）"
        )

    wanted, got = set(requested), set(declared)
    coverage = len(wanted & got) / len(wanted)
    ratio = len(declared) / len(requested)
    if coverage >= TITLE_COVERAGE_MIN and ratio <= TITLE_LENGTH_RATIO_MAX:
        return True, f"页面声明的正式名称与请求名相符（{declared_title}，字符覆盖 {coverage:.0%}）"
    return False, (
        f"页面声明的正式名称与请求名不符（页面声明「{declared_title}」，"
        f"字符覆盖 {coverage:.0%}，长度比 {ratio:.2f}）"
    )
