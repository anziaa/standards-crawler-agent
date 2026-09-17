# Standards Crawler Agent

一个基于 [LangGraph](https://github.com/langgraph) 的「文件名 → 官方来源 → 文件下载」Agent 原型。

给它一个文件名（标准、规范、条例、通知、规程……），它会自己去全网发现候选来源、判断哪个是官方网站、
打开页面观察真实结构，然后**动态选择**一种下载方式把文件取回来，并在落盘前校验「下到的确实是目标文件」。

> **它和普通爬虫的分界线在最后一步。** 产物里必须真有文件正文、页面身份要对得上、名称片段要命中、
> 不能被打印稿片段糊弄 —— 不满足就换候选，或明确报失败。**不把网页外壳、著录页、另一份相近文件
> 当成成功交付**（`downloads/_rejected/` 里那 30 个实测空壳件就是这条线的战果）。

下载方式不是写死的。同一个任务在不同站点上会走出不同路径：

| 动作 | 用在什么页面 |
| --- | --- |
| `download_direct` | 已有文件直链 |
| `click_download` | 有点得动的下载按钮（含 `window.open` 弹窗里触发的下载）|
| `click_view_original` | 「查看原文 / 查看文件」要进下一层页面 |
| `click_print` | 页面提供打印提示，导出为 PDF |
| `save_page_pdf` | 正文就在网页上、没有下载按钮（法规条例的常见发布形态）|
| `inspect_embedded` | 页面内嵌 PDF / 文档 |
| `follow_attachment_page` | 附件列表或下一层详情页 |
| `site_search` | 官网栏目页上找不到目标，改走站内检索 |
| `no_download` | 确认没有可获取的文件，明确结束 |

每个动作都只从**页面观察结果里的真实证据**出发，由大模型从上面这个有限集合里挑一个；动作失败后，
状态图把失败原因、已尝试动作和当前页面重新交给模型再选，而不是机械地把备用策略逐个跑一遍。

## 它专门解决的坑

这类「从政府网站找红头文件」的活，难点几乎都不在下载本身，而在**判断**。项目把踩过的坑固化成了机制：

- **官方来源识别** —— 国家标准委、住建部等权威站与标准详情页路径获得加成；文库、内容站降权；
  请求里带标准编号时优先较新版本。
- **生命周期把关** —— 判定「废止 / 作废 / 失效」时先把判定依据收敛到**目标名称附近**的文本，
  避免把法规正文里「已批准的施工许可证失效」这类句子误读成整份文件废止。判定放在花钱的大模型决策**之前**，
  废止页面 0 token 收工。
- **内容实质校验** —— 产物里到底有没有文件正文。这一条拦掉了 30 个「标准著录页 / 文库付费外壳」
  被当成成功交付的情况（清单见 `tests/known_shells.json`）。
- **下载内容相关性校验** —— 按名称片段命中率 + 字符覆盖率双判据，拦下「有效但不对」的文件
  （下错过同属一个局、内容相近的另一份文件）。
- **状态语义分层** —— `success`（官方原件）/ `success_page_export`（网页导出，本身就是官方发布形态）/
  `success_scanned`（扫描件）分开统计，不再把网页快照冒充成官方原件。
- **可见性过滤** —— 只把**真实可见**的控件交给模型。`display:none` 的按钮点不动，留着只会让模型反复重试；
  可点但不是 `<button>` 标签的入口（`div.btn`、`span[onclick]`）也会被补进来。
- **搜索兜底页** —— 搜索引擎被限流时会返回与查询完全无关的结果（实测整页路虎官网、B 站下载器）。
  相关性闸门会丢弃这类结果，并用「换入口 + 换引号形态」再问一次，而不是把垃圾当候选。

完整的实测过程、数字和结论见 [`docs/DEVELOPMENT-LOG.md`](docs/DEVELOPMENT-LOG.md)（42 节）。

## 目录结构

```text
src/standards_crawler_agent/
  models.py        状态、候选项、下载计划、AgentConfig
  graph.py         LangGraph 状态图（12 个节点）
  search.py        搜索提供商：SerpApiSearch / BrowserSearch
  browser.py       Playwright 页面观察与动作执行
  download.py      下载、类型嗅探、内容校验、命名
  ranking.py       官方来源与候选页打分
  ranker.py        LLM 候选选择器（候选池里先看哪一个）
  llm_planner.py   LLM 下载规划器（从动作集合里选一个）
  content.py       正文实质校验（产物里究竟有没有文件正文）
  runtime.py       Playwright 进程级单例与引用计数
  batch.py         并行批量（每个任务一个独立进程）
  sheet_crawl.py   按 Excel 编制依据表逐行爬取
  report.py        结果汇总回表，输出带新增列的 Excel
  cli.py           单任务命令行入口

tests/
  test_content_gate.py      断言套件（310 项，离线不触网）
  fixtures_serp_ranking.json / fixtures_name_mismatch.json / fixtures_fallback_pages.json
  known_shells.json         30 个实测空壳件清单（是数据，不是硬编码）
  fixtures_artifacts/       正确 / 错误的真实产物对照

docs/
  DEVELOPMENT-LOG.md        开发日志与踩坑记录
```

## 环境要求

- Python **>= 3.11**
- Windows / macOS / Linux（开发和实测都在 Windows 上完成，因此文件名全角处理、
  GBK 终端兼容这些细节都是按 Windows 做的）
- 可访问外网（首次安装需要下载 Chromium）

### 已验证的版本组合

`pyproject.toml` 里只写了下界（例如 `langgraph>=0.2.0`），所以下面前这套**实际上跑通全部
310 项断言**的版本组合值得记下来当参照（Python 3.11.16）：

| 包 | 版本 | | 包 | 版本 |
| --- | --- | --- | --- | --- |
| `langgraph` | 1.2.11 | | `pypdf` | 6.18.1 |
| `langchain-core` | 1.6.3 | | `cryptography` | 50.0.1 |
| `langchain-openai`（可选）| 1.6.2 | | `beautifulsoup4` | 4.15.0 |
| `playwright` | 1.62.0 | | `lxml` | 6.1.3 |
| `pydantic` | 2.13.5 | | `httpx` | 0.28.1 |
| `python-docx` | 1.2.0 | | `openpyxl` | 3.1.5 |
| `python-dotenv` | 1.2.3 | | `pip check` | 无冲突 |

注意 `langgraph` / `langchain-core` 装的是 **1.x**（远高于下界），状态图在 1.x 上验证通过。
浏览器用 `playwright install chromium` 安装即可，不需要单独装系统级依赖。

## 安装

### 方式一：venv（推荐）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium
```

### 方式二：Conda

本仓库开发与实测时用的就是这个方式（环境目录 `.conda-env/`）：

```powershell
conda create -p .conda-env python=3.11 -y
conda activate <本仓库绝对路径>\.conda-env
pip install -e .
playwright install chromium
```

`.venv/` 与 `.conda-env/` 都已在 `.gitignore` 里，不会进版本库。

要以编程方式使用大模型规划器，另外装可选的 OpenAI 适配器：

```powershell
pip install -e ".[openai]"
```

复制环境变量模板并按需填写：

```powershell
Copy-Item .env.example .env
```

`.env` 已被 `.gitignore` 忽略，密钥只留在本地。**填之前请先看模板里的注释**——
有一处容易踩：本项目**不读取** `DASHSCOPE_API_KEY`，密钥要填在 `OPENAI_API_KEY`。
只填前者不会报错，会静默退化成规则型策略。

**不填任何 Key 也能跑**：

- 没有 `OPENAI_API_KEY` → 使用规则型下载策略（不调用大模型）；
- 没有 `SERPAPI_API_KEY` → 用 Playwright 直接访问搜索引擎结果页。

## 快速开始

```powershell
standards-crawler-agent "公路桥涵施工技术规范"
standards-crawler-agent "电梯用钢丝绳" --output results\elevator.json
```

默认**无头**运行（不弹窗、不抢焦点）。需要看着过程时：

```powershell
$env:BROWSER_HEADLESS = "0"          # 本会话内有效
standards-crawler-agent "公路桥涵施工技术规范"

standards-crawler-agent "公路桥涵施工技术规范" --headed    # 只这一次
```

`BROWSER_HEADLESS` 取值写错（例如 `Flase`）会**当场报错**，不会静默退回默认模式。

> 有头模式下每个任务会开两个浏览器（检索一个、页面一个），`--jobs 3` 最多 6 个窗口。
> 内存量级随 `jobs` 线性增长（每个无头 Chromium 约 150~300 MB）。

## 命令行

### 单任务

```powershell
standards-crawler-agent <名称> [--output <json 路径>] [--headed | --headless]
```

`--headed` 与 `--headless` 不能同时使用；`--headless` 可以盖过 `BROWSER_HEADLESS=0`。

### 批量并行

```powershell
python -m standards_crawler_agent.batch "电梯用钢丝绳" "工程结构通用规范" --jobs 3
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--jobs` | `3` | 并行进程数 |
| `--timeout` | `120` | 单任务超时（秒）|
| `--retries` | `1` | 进程级偶发故障时整任务重试次数 |
| `--stagger` | `2.0` | 任务启动间隔（秒），避免并发触发搜索引擎风控 |
| `--output-root` | `results/batch_<时间戳>` | 产物根目录 |

**每个任务一个独立进程**，原因是 Playwright 同步 API 把 asyncio 事件循环绑在创建它的线程上：
同一线程不能有两个实例，实例也不能跨线程使用。因此进程隔离是最干净的做法。

产物结构：`results/batch_<时间戳>/<序号_文件名>/{result.json, run.log}`，
外加汇总文件 `batch_summary.json`（含每个任务的状态、耗时、token、失败原因）。
下载文件统一落在 `downloads/`。

### 按编制依据表爬取

```powershell
python -m standards_crawler_agent.sheet_crawl --list-types                    # 只看分类与抽样预览
python -m standards_crawler_agent.sheet_crawl --rows 3,4,5 --jobs 3           # 指定行号试点
python -m standards_crawler_agent.sheet_crawl --remark 下载 --limit 10
python -m standards_crawler_agent.sheet_crawl --start 1 --end 100 --jobs 3
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--source` | 见下方「已知限制」 | 原始 Excel 路径 |
| `--sheet` | 见下方「已知限制」 | 工作表名 |
| `--rows` / `--start` / `--end` / `--limit` / `--spread` | — | 选行方式 |
| `--remark` | — | 只跑备注等于该值的行 |
| `--list-types` | — | 只打印分类统计与抽样预览，不爬取 |
| `--jobs` / `--timeout` / `--retries` / `--stagger` | `3` / `300` / `1` / `2.0` | 同批量 |

### 汇总回表

```powershell
python -m standards_crawler_agent.report --source <原表路径> --sheet <工作表名> `
    --results results\batch_sheet_* --output <输出路径>
```

把爬取结果汇总回编制依据表，输出带新增列的 Excel。`--results` 可传多个批次目录，
默认汇总 `results/batch_sheet_*`。

## 环境变量

全部可选，都通过 `.env` 或进程环境变量提供。

| 变量 | 作用 |
| --- | --- |
| `OPENAI_API_KEY` | 启用大模型规划器与候选选择器；不设则用规则型策略 |
| `OPENAI_BASE_URL` / `OPENAI_API_BASE` | OpenAI 兼容端点（两者取其一）|
| `OPENAI_MODEL` | 规划器模型，默认 `qwen-plus` |
| `OPENAI_CHOOSER_MODEL` | 候选选择器模型，默认 `qwen-turbo`；设为 `off` / `none` 可关闭 |
| `OPENAI_TIMEOUT` | 单次调用超时秒数，默认 `40` |
| `OPENAI_MAX_RETRIES` | 单次调用重试次数，默认 `1` |
| `SERPAPI_API_KEY` | 有则用结构化搜索 API，没有则用 Playwright 抓搜索页 |
| `SEARCH_ENGINE` | 浏览器搜索的引擎，`bing`（默认）/ `baidu` / `google` |
| `BROWSER_HEADLESS` | `1/true/yes/on/headless` → 无头（**默认**）；`0/false/no/off/headed` → 有头 |
| `SHEET_SOURCE` | 编制依据表（`.xlsx`）路径，供 `sheet_crawl` / `report` 使用；默认 `编制依据汇总表.xlsx` |
| `SHEET_NAME` | 编制依据表里的工作表名，默认 `编制依据` |

> `OPENAI_TIMEOUT × (OPENAI_MAX_RETRIES + 1)` 要明显小于批量的 `--timeout`。
> 上游偶发停顿能把一次决策拖过 120 秒，若两者相等，整次尝试会被白杀且没机会换候选。
> 默认 40 × 2 = 80 秒，留 40 秒给「换候选再试一次」；实测正常调用在 5~12 秒之间。

## 结果状态

| 状态 | 含义 |
| --- | --- |
| `success` | 拿到官方原件 |
| `success_page_export` | 网页导出（法规条例在官网常以 HTML 正文形态发布，即其官方发布形态）|
| `success_scanned` | 扫描件（无文本层，单独统计）|
| `skipped_withdrawn` | 判定为已废止 / 作废，不下载，`skip_reason` 说明原因 |
| `needs_confirmation` | 证据矛盾，需要人工确认（例如页面同时提到旧版废止和新版替代）|
| `not_downloadable` | 没有可获取的文件或附件 |

产物还会带 `verification` 信息：`pages`、`mime_type`、`sha256`、`warnings`，
以及 `body_absent`（产物里没有正文）、`scan_only`、`name_match` 等判据字段。

落盘命名规则是 `名称_编号.扩展名`（页面没有编号时就是 `名称.扩展名`）：

- 名称**优先采用模型在页面上识别到的正式名称**，因此请求名里的笔误会被自动纠正；
- 标准号统一写法（破折号归一为半角 `-`），Windows 落盘时把 `/` 转成全角 `／`；
- 无扩展名的附件按**文件头嗅探**补扩展名（`%PDF-`、`PK` 开头的 OOXML、OLE2 复合文档）；
- 网页导出的产物用 `success_page_export` 区分，不冒充官方原件。

## 配置项

`AgentConfig`（`models.py`）可在编程调用时覆盖：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `max_search_results` | `50` | 候选池上限 |
| `max_search_queries` | `3` | 每次运行加载的搜索结果页数量 |
| `min_candidate_score` | `0.35` | 候选可信度门槛（`0.5×官方分 + 0.5×页面分`），低于此值不进入观察环节 |
| `min_observation_text_chars` | `50` | 正文短于此值且无任何链接，视为空白页 → 观察失败换候选 |
| `max_candidate_domains` | `5` | 最多观察几个域名 |
| `max_page_attempts` | `5` | 页面动作与跳转的总次数上限 |
| `max_download_bytes` | `100 MB` | 单文件下载上限 |
| `allowed_domains` | 空（不限）| 域名白名单 |
| `download_dir` | `downloads` | 产物目录 |

编程调用：

```python
from standards_crawler_agent.graph import run
from standards_crawler_agent.search import SerpApiSearch
from standards_crawler_agent.browser import PlaywrightBrowser

result = run("公路桥涵施工技术规范", SerpApiSearch(), PlaywrightBrowser())
```

## 状态图

`graph.py` 注册了 12 个节点：

```text
parse_task → search → rank → observe → check_lifecycle → choose_download
           → download → verify → report
```

条件分支：

- `rank` 候选池为 0 → 回 `search` 换一组检索词重试一次；
- `observe` → 观察失败则 `retry_candidate` 换下一个候选；正文空白同样换候选；
- `check_lifecycle` 判定 `withdrawn` / `ambiguous` → 直接 `report`，**不消耗 token**；
- `download` → 需要重新观察则回 `observe`；动作失败则 `reassess_download` 让模型重选；换候选则 `retry_candidate`；
- `verify` → 校验失败进 `quarantine`（移入 `downloads/_rejected/`）再决定换候选还是收工。

`trace[*]` 记录每个节点的耗时，`final.timings` 与 `final.total_elapsed_ms` 做汇总，
`final.usage_total` 给出整条流程的 token 合计。

## 测试

```powershell
.\.conda-env\python.exe tests\test_content_gate.py     # 或先激活 venv 再 python tests\test_content_gate.py
```

**310 项断言，全部通过**（脚本以 `exit 0` 结束）。断言套件离线运行、不触网，
输入是本地夹具与 `downloads/` 里的真实产物。它覆盖：

- 内容判据的正反例；
- 30 个实测空壳件**必须被拒**（清单在 `tests/known_shells.json`）；
- 合法网页导出仍须通过、扫描件只标记不判失败；
- 域名放行 / 拒绝；
- **端到端状态映射** —— 用假检索、假浏览器、假决策跑完整张图，断言各状态映射正确。

> ⚠️ **这套测试依赖 `downloads/` 目录里的真实产物。** `downloads/` 已被 `.gitignore` 排除，
> 因此**新克隆的仓库直接跑会大面积失败**（`FileNotFoundError`）。这些文件是需要实际跑一遍
> 爬取任务才会产生的产物，同时也是 `downloads/_rejected/` 里那 30 个空壳夹具的来源。

## 已知限制

- **`sheet_crawl` / `report` 需要你自己提供编制依据表。** 代码里**没有任何本机绝对路径**：
  默认值是相对当前工作目录的 `编制依据汇总表.xlsx` 与工作表名 `编制依据`。
  请用 `--source` / `--sheet` 指定，或把 `SHEET_SOURCE` / `SHEET_NAME` 写进 `.env`
  （推荐，这样每行命令都不用再带参数）。
- **测试依赖 `downloads/`**，见上一节。
- **搜索引擎会限流。** 短时间大量检索会返回与查询完全无关的兜底结果，项目用相关性闸门丢弃并换入口重问，
  但更稳的做法是配置 `SERPAPI_API_KEY`。
- **有头 / 无头会改变一类结果。** 有头 Chromium 用内置阅读器把 PDF 直链内联渲染，导致
  「候选是 PDF 直链却判空白页」；代码里对这种情况做了识别，但默认无头也是出于这个原因。
- **版权。** 下载到的标准 / 法规原文受版权保护，`downloads/` 目录不入库，请自行确认使用范围。
- **合规边界。** 本工具不绕过登录、验证码或访问控制，所有浏览器动作都受域名、页数和下载大小限制。
  实际部署时建议再加域名白名单、请求限速与 MIME / 大小校验。

## 开发日志

`README.md` 只保留使用说明。开发过程中每一轮实测的问题、修法与验证结果（42 节）记在
[`docs/DEVELOPMENT-LOG.md`](docs/DEVELOPMENT-LOG.md)——包括「官方全文页被内容农场挤出候选」
「把失败信号改造成成功信号引来的假成功」「站内检索通用性 7 站点实测」这类结论，以及它们当时的
真实数字。要理解某个判据为什么长这样，那里是唯一的地方。

## 许可

尚未指定许可证。在补充之前，默认保留所有权利。
