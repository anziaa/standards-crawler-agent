from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import CandidatePage, SearchResult

# 国家标准/行业标准的官方发布与信息服务平台。
OFFICIAL_STANDARD_DOMAINS = (
    "openstd.samr.gov.cn",
    "std.samr.gov.cn",
    "std.sac.gov.cn",
    "samr.gov.cn",
    "sac.gov.cn",
    "csres.com",
)
# 详情页特征：这些路径提供的是权威标准记录，即使标题里没写中文标准名称。
STANDARD_DETAIL_HINTS = ("newgbinfo", "gbdetailed", "hcno=", "/gb/", "/bzgk/", "stddetail")
INSTITUTION_WORDS = ("政府", "局", "委员会", "厅", "部", "公告", "通知", "标准", "发布", "现行", "规范")
# 只保留真正指向"文件端点"的路径词。原先还含 "info"/"detail"，而这两个词描述的是
# 普通的文章详情页，会把地方站的转载页抬高：实测 r19 的 #4，阿里地区住建局的
# /info/1963/12182.htm 就是靠这一分拿到 0.95，压过了中国政府网与住建部（0.90）。
FILE_URL_WORDS = ("download", "attach", "upload", "file")
LOW_TRUST_DOMAINS = (
    "wenku.baidu.com",
    "doc88.com",
    "docin.com",
    "book118.com",
    "renrendoc.com",
    "antpedia.com",
    "baike.baidu.com",
    "zhihu.com",
    "toutiao.com",
    "sohu.com",
    "jd.com",
    "taobao.com",
    "1688.com",
    # 以下为"无匹配兜底页"实测里出现的头名词内容站：这类站点永远不会托管官方原件，
    # 且它们在兜底页里往往占据前几条，会成为"结果看起来有了"的假象来源。
    "archdaily.com",
    "gooood.cn",
    "archcollege.com",
    "haimian.com",
    "haimianmba.com",
    "hanyuguoxue.com",
    "ludashi.com",
    "biaozhuns.com",
    "biaozhunwang.com",
    "ndls.org.cn",
    "szlcsc.com",
    "rsonline.cn",
    "hqbuy.com",
    "book118",
    # 2026-09-16 按"实测 1117 份产物"的审计结果补充（判据与脚本：temp/audit_selected_domains.py）：
    # 这些域名都满足三条——性质上是文库/内容站/建站托管/商城/问卷（不会托管官方原件）、
    # 历史里**被 rank 选中过**、那些任务 **0 成功**、且**从未作为交付来源出现过**。
    # 最贵的一个是 www.soujianzhu.cn：被选中 17 次、17 次都没拿到文件。
    # 刻意**没有**收录性质不明的（whrsm.com 像科研院所、zjtapi.jnzhijiantong.com 像市政接口）——
    # "0 成功"可能是流程没走通，而不是站点没有文件；黑名单宁少勿滥。
    # 反例警示：`www.ndls.org.cn` 在黑名单里，但它**交付过 8 次**，说明加错是要付代价的。
    # `faiusr.com`（凡科建站托管）也被撤掉了：按域名审计时它"0 交付"，
    # 但按任务状态拆开的验证（temp/verify_blacklist.py）发现另一个子域
    # `26431875.s21i.faiusr.com` **成功交付过**《岩棉薄抹灰外墙外保温系统材料》——
    # 同域不同子域会漏判，所以只按"性质明确 + 全子域 0 交付"收录。
    "soujianzhu.cn",
    "cbi360.net",
    "waizi.org.cn",
    "doc.quark.cn",
    "xuehai.net",
    "ks.wjx.com",
    "cp.baidu.com",
    "report.baidu.com",
    "home.baidu.com",
    "detail.youzan.com",
    "biaozhun.org",
)
MILD_TRUST_DOMAINS = ("chinabuilding.com.cn", "ebook.", "mall.", "shop.")

# 政府站点的行政层级：同一份文件常被中央、省、市县网站同时转载，但只有发文机关
# 那一级才是权威来源。此前对所有 .gov.cn 一律 +0.6，等于把"是政府域名"当成
# "来源够权威"，于是一个地区级住建局的详情页（再叠加下面的路径词加分）能拿到
# 0.95，反而高过中国政府网与住建部（0.90）。实测 r19 的 #4 就是这样落到
# 阿里地区住建局，而真正带官方 .docx 附件的中国政府网页面排在第 3、从未被访问。
# 这里只分"中央/部委"与"地方"两档：省级与市县级再细分需要一张容易过时又容易
# 误判的域名表，收益不足以抵消风险。
CENTRAL_GOV_PORTALS = ("gov.cn", "www.gov.cn")
CENTRAL_GOV_DOMAINS = (
    "mohurd.gov.cn", "mem.gov.cn", "mot.gov.cn", "mee.gov.cn", "mof.gov.cn",
    "mwr.gov.cn", "miit.gov.cn", "ndrc.gov.cn", "mohrss.gov.cn", "moa.gov.cn",
    "moj.gov.cn", "mct.gov.cn", "mofcom.gov.cn", "nhc.gov.cn", "mnr.gov.cn",
    "stats.gov.cn", "samr.gov.cn", "sac.gov.cn", "npc.gov.cn",
)
CENTRAL_GOV_SCORE = 0.65
LOCAL_GOV_SCORE = 0.45


def is_central_gov_domain(domain: str) -> bool:
    """中国政府网与各部委。

    这里**不能**用 "gov.cn" 做子串匹配：那样 zjt.shandong.gov.cn 之类的地方站
    会全部被算成中央站点，档位就白分了。
    """
    value = (domain or "").lower()
    if value in CENTRAL_GOV_PORTALS:
        return True
    return any(value == item or value.endswith("." + item) for item in CENTRAL_GOV_DOMAINS)


def is_low_trust_domain(domain: str) -> bool:
    """第三方文库 / 内容农场 / 电商站：这些站点永远不会托管官方原件。

    只做降权是不够的：`doc88.com` 在 official_score 上被扣到 0.0，
    但它靠逐字相同的标题把 page_score 拿满，最终 `0.5×0 + 0.5×1.0 = 0.50`
    仍然越过了 0.35 的候选门槛（实测 22G101-1、木结构设计规范、低压配电设计规范
    三个任务都被它接走，只能导出付费阅读器外壳，却记为成功）。
    因此这里改为硬拒绝：不进候选池，省下观察与决策的 token。
    """
    value = (domain or "").lower()
    return any(item in value for item in LOW_TRUST_DOMAINS)

STANDARD_NUMBER_PATTERN = re.compile(
    r"\b([A-Z]{2,}(?:\s*/\s*[A-Z]{1,3})?\s*\d{1,6}\s*[—–-]\s*(\d{4}))\b",
    re.I,
)


# --------------------------- 候选池为 0 时的"官方入口" ---------------------------
# 背景：搜索引擎对某条查询没有匹配时会返回一页头名词兜底结果（实测「建筑铝型材」→
# ArchDaily/百度百科「建筑物」；「DB37/T5285」→ D-Sub 37 针连接器）。这时继续在
# 搜索引擎里换词往往无效，正确做法是**换入口**：直接去发文机关官网或标准平台做站内检索。
#
# 站内检索的可用性已实测（2026-09-16）：
#   dbba.sacinfo.org.cn  → 可用，提交后落到 /stdList?key=<检索词>
#   机构官网（如 jncc.jinan.gov.cn）→ 可用，落到其 jpaas 检索接口
#   std.samr.gov.cn      → 首页可观察，但站内检索当前拿不到结果（未适配），故不选它
AGENCY_ENTRIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("住房和城乡建设部", "住房城乡建设部", "住建部"), "https://www.mohurd.gov.cn/"),
    (("山东省住房和城乡建设厅", "山东省住建厅"), "http://zjt.shandong.gov.cn/"),
    (("济南市住房和城乡建设局", "济南市住建局"), "https://jncc.jinan.gov.cn/"),
    (("交通运输部", "公路局"), "https://xxgk.mot.gov.cn/"),
    (("生态环境部", "环境保护部"), "https://www.mee.gov.cn/"),
    (("应急管理部", "国家安全生产监督管理总局"), "https://www.mem.gov.cn/"),
    (("山东省人民政府", "山东省人民政府办公厅"), "http://www.shandong.gov.cn/"),
    (("济南市人民政府", "济南市人民政府办公厅"), "http://www.jinan.gov.cn/"),
    (("国务院", "全国人民代表大会常务委员会", "主席令"), "https://www.gov.cn/"),
)
# **只写行政区划**（没写发文机关全称）时的入口。放在标准类判断**之后**：
# 带 DB37 / 标准这类词的名字仍旧走地方标准平台（那条路实测可用），
# 而“关于印发《…实施细 则》的通知”这种**规范性文件**不该进标准平台——它只会空转。
# 实测（r19~r21 的 #5）：一个省级规范性文件被送进 dbba.sacinfo.org.cn，站内检索 5 轮全空、
# 烧掉 45.4k token 才收工；而山东省政府公报页其实就在搜索引擎结果里。
# 这张表只放**验证过门户可用**的条目，宁少勿滥。
JURISDICTION_ENTRIES: tuple[tuple[str, str], ...] = (
    ("山东省", "http://www.shandong.gov.cn/"),
    ("济南市", "http://www.jinan.gov.cn/"),
)
# 注意这里**不再包含“实施细则”**：它不是标准类词，是规范性文件的常见文种。
STANDARD_ENTRY_WORDS = ("规范", "规程", "标准", "技术要求", "技术条件", "导则", "指南")
# 地方标准/省级文件优先走地方标准平台（站内检索实测可用）。
LOCAL_ENTRY_HINTS = ("山东省", "济南市", "DB37", "鲁建", "济建", "地方标准")


def derive_official_entry(name: str) -> tuple[str, str] | None:
    """按名称推断一个"官方入口"页面，返回 (url, 说明)；推不出返回 None。

    只在正常候选全部不可用时使用：官网首页/平台首页本身没有目标文件，
    但可以在站内检索到。
    """
    value = re.sub(r"[\s　]+", "", name or "")
    if not value:
        return None
    for keywords, url in AGENCY_ENTRIES:
        for keyword in keywords:
            if keyword in value:
                return url, f"名称含发文机关「{keyword}」→ 用其官网站内检索"
    if any(word in value for word in STANDARD_ENTRY_WORDS):
        if any(hint in value for hint in LOCAL_ENTRY_HINTS):
            return "https://dbba.sacinfo.org.cn/", "标准类且带地方/省级线索 → 地方标准平台站内检索"
        return "https://dbba.sacinfo.org.cn/", "标准类名称 → 标准平台站内检索（当前可用入口为地方标准平台）"
    for keyword, url in JURISDICTION_ENTRIES:
        if keyword in value:
            return url, f"名称含行政区划「{keyword}」→ 用该级政府门户站内检索"
    return None


# 总分 = 0.5×官方分 + 0.5×相关度。候选池门槛与排序都用它——**总分只有一个 owner**，
# 不在这里各算一遍（原先 graph.rank 里又抄了一遍同样的表达式）。
#
# 试过、**被 575 条真实 SERP 的数据否决**的改法：把官方分从"平均项"改成**门控系数**
# （`总分 = 相关度 × (0.4 + 0.6×官方分)`）。动机是"官方首页不该赢过第三方站上的文件页"，
# 但实测走向反面：法规类任务里，中国政府网页面对一长串文种名的**字面重合本来就低**
# （page 0.15~0.33），官方分那 0.5 的权重正是把它们留在池子里的东西。改门控后
# 这些页面掉到 0.22~0.33、低于 0.35 门槛，"候选池为 0"的 SERP 从 169 涨到 **221**，
# "当时真正用到的页面在池内"从 396 掉到 **322**，掉出前 5 从 1 涨到 8。
# 折中方案（0.35/0.65 偏相关度）池为 0 最少（142），但"第 1 名是权威来源"从 272 掉到 260。
# 结论：**问题出在相关度算不出部分命中，而不是官方分权重太大**——修相关度即可，
# 权重保持对半。top-1 选错没有回退路径（状态图拿到第一个成功就收工），池子空还能靠
# 换检索词与官方入口兜底，所以优先保"第 1 名是权威来源"这个指标。
def combined_score(page: CandidatePage) -> float:
    return 0.5 * page.official_score + 0.5 * page.page_score


def extract_standard_numbers(text: str) -> list[str]:
    return [match.group(1) for match in STANDARD_NUMBER_PATTERN.finditer(text or "")]


def _standard_year(text: str) -> int:
    years = [int(match.group(2)) for match in STANDARD_NUMBER_PATTERN.finditer(text or "")]
    return max(years) if years else 0


def official_score(result: SearchResult) -> tuple[float, list[str]]:
    domain = (result.domain or urlparse(result.url).netloc).lower()
    title = result.title or ""
    url_lower = result.url.lower()
    score = 0.2
    evidence: list[str] = []
    if domain.endswith(".gov.cn") or ".gov.cn" in domain:
        if is_central_gov_domain(domain):
            score += CENTRAL_GOV_SCORE
            evidence.append("域名属于中央/部委政府站点")
        else:
            score += LOCAL_GOV_SCORE
            evidence.append("域名属于地方政府站点")
    if any(domain.endswith(item) or item in domain for item in OFFICIAL_STANDARD_DOMAINS):
        score += 0.15
        evidence.append("域名属于官方标准发布/信息平台")
    if any(word in title for word in INSTITUTION_WORDS):
        score += 0.1
        evidence.append("标题具有机构或政务内容特征")
    if any(word in url_lower for word in FILE_URL_WORDS):
        score += 0.05
        evidence.append("URL 指向文件端点（download/attach/upload/file）")
    if any(item in domain for item in LOW_TRUST_DOMAINS):
        score -= 0.25
        evidence.append("域名属于第三方资料/内容站，降权")
    elif any(item in domain for item in MILD_TRUST_DOMAINS):
        score -= 0.1
        evidence.append("域名属于商业文档平台，轻微降权")
    return max(0.0, min(score, 1.0)), evidence


DOCUMENT_URL_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".wps", ".et", ".ofd",
)
# 指向文档文件的 URL，比"页面里出现过文件名称"更能说明这条结果就是目标文件本身。
# 分数定在 0.95：高于所有普通页面，但仍低于"标题与请求名逐字相同"的发布页（1.0 档），
# 那种页面通常正是附件所在的官方发布页。
#
# **只给权威站点**：这条加分曾一度对所有域名生效，回放 571 条历史 SERP 后发现
# 恰好相反的效果——`zhzzy.com.cn/uploads/allimg/*.pdf`、`www.zbl.cn/uploadfile/*.pdf`
# 这类无关 PDF 被抬进前 5，把正确的政府页面挤了出去（"任务实际用到的页面掉出前 5"
# 从 6 条涨到 14 条）。文档直链只有在政府站/官方标准平台上才等于"官方原件"。
DOCUMENT_URL_PAGE_SCORE = 0.95
# 相关度计算里参与比较的文本上限：标题+摘要通常 < 600 字符，再长也只是浪费时间。
SCORING_HAYSTACK_CHARS = 600


def is_authoritative_domain(domain: str) -> bool:
    """政府站或官方标准平台（与 official_score 里的判定保持一致）。"""
    value = (domain or "").lower()
    if ".gov.cn" in value:
        return True
    return any(value.endswith(item) or item in value for item in OFFICIAL_STANDARD_DOMAINS)


def is_document_url(url: str) -> bool:
    path = urlparse(url or "").path.lower().rstrip("/")
    return path.endswith(DOCUMENT_URL_EXTENSIONS)


def longest_common_run(needle: str, haystack: str) -> int:
    """needle 在 haystack 里**连续出现**的最长长度。

    两个地方共用它（**一个判据只有一个实现**）：
      · `page_score`：给"被搜索引擎截断/插空格"的命中记部分分；
      · `search.fallback_signature`：判断一批结果是不是"按头名词凑的兜底页"。
    滚动数组 DP，needle 是一个文件名（10~60 字），haystack 已按上限截断，代价可控。
    """
    if not needle or not haystack:
        return 0
    if needle in haystack:  # 快路径：整串命中（绝大多数正常结果走这里）
        return len(needle)
    if not (set(needle) & set(haystack)):
        return 0
    previous = [0] * (len(haystack) + 1)
    best = 0
    for char_needle in needle:
        current = [0] * (len(haystack) + 1)
        for index, char_hay in enumerate(haystack, 1):
            if char_needle == char_hay:
                run = previous[index - 1] + 1
                current[index] = run
                if run > best:
                    best = run
        previous = current
    return best


def name_relevance(normalized: str, haystack: str) -> float:
    """请求名与一段文本的相关度，0~1。

    早先只做"每个词是否**整串**出现"的二值判断，于是中文长名称只有 0.15 / 1.00 两档：
    搜索引擎把标题截断（「…燃气供热工程」）或给命中词插空格（「标准 （ 2024版 ）」）时，
    **真命中的页面也和无关页面同分**——实测济南那条通知，所有候选都拿 0.15，
    排序完全由"官方域名加分"决定，于是官方首页进了候选池、真正带文件的第三方页被筛掉
    （整条任务 0 token 收工）。这里改成按**最长连续命中占比**给分，截断也能拿到部分分。
    """
    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return 0.0
    matched = sum(1 for token in tokens if token in haystack)
    if matched == len(tokens):
        return 1.0
    window = haystack[:SCORING_HAYSTACK_CHARS]
    coverage = sum(longest_common_run(token, window) / len(token) for token in tokens) / len(tokens)
    return max(matched / len(tokens), coverage)


def page_score(result: SearchResult, normalized: str) -> float:
    # 注意：这里比较的是搜索引擎给的 title/snippet，而它们**会被引擎截断或插空格**
    # （实测「住房城乡建设部关于印发《 房屋市政工程生产安全重大事故 ...」）。曾试过
    # "比较前先掐掉空白"来抵消插空格，回放 571 条历史 SERP 后放弃了：被插空格的是
    # **转载页**、被截断的是**官方发布页**，掐空白只会让标题完整的转载页（page 1.0）
    # 反超官方发布页（page 0.15），"名次退后"从 54 条涨到 75 条而自身零收益。
    # 现在用最长连续命中占比（见 name_relevance）来吸收截断，效果不再依赖谁的字面更完整。
    haystack = " ".join((result.title, result.snippet, result.url))
    score = 0.15 + name_relevance(normalized, haystack) * 0.85

    # URL 本身就是文档文件：这是"这条结果就是目标文件"的最强信号。
    # 实测 r19 #4 的官方附件直链 www.gov.cn/…/P020250101711302201621.docx 就在检索
    # 结果里，但旧公式对它一分不加（标题被搜索引擎截断 → page 只拿下限 0.15），
    # 结果它排在第 6 名之外、连候选池都没进。
    if is_document_url(result.url) and is_authoritative_domain(result.domain or urlparse(result.url).netloc):
        score = max(score, DOCUMENT_URL_PAGE_SCORE)

    # 标准详情页的标题常常只写“国家标准|GB 8903-2024”，中文名称只出现在正文里，
    # 这类权威记录不应因为标题字面不匹配而被内容农场挤掉。
    url_lower = result.url.lower()
    if any(hint in url_lower for hint in STANDARD_DETAIL_HINTS):
        score = max(score, 0.75)
    # 请求里没写标准编号时，多个版本并存意味着应优先较新的版本。
    requested_years = [int(match.group(2)) for match in STANDARD_NUMBER_PATTERN.finditer(normalized)]
    result_years = [int(match.group(2)) for match in STANDARD_NUMBER_PATTERN.finditer(haystack)]
    if requested_years and result_years:
        if set(requested_years) & set(result_years):
            score += 0.25
        else:
            score -= 0.25
    return max(0.0, min(score, 1.0))


def rank_pages(name: str, results: list[SearchResult]) -> list[CandidatePage]:
    normalized = name.replace(".pdf", "").replace(".docx", "").strip()
    newest_year = max((_standard_year(f"{item.title} {item.snippet}") for item in results), default=0)
    ranked: list[CandidatePage] = []
    for result in results:
        source_score, evidence = official_score(result)
        page = page_score(result, normalized)
        if newest_year and _standard_year(f"{result.title} {result.snippet}") == newest_year:
            page = min(1.0, page + 0.1)
            evidence.append(f"命中最新版本年份 {newest_year}")
        ranked.append(
            CandidatePage(
                url=result.url,
                title=result.title,
                text=result.snippet,
                domain=result.domain,
                official_score=source_score,
                page_score=page,
                evidence=evidence,
            )
        )
    return sorted(ranked, key=combined_score, reverse=True)
