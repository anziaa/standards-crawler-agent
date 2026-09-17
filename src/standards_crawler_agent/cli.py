from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .browser import PlaywrightBrowser
from .graph import run
from .llm_planner import LLMDownloadPlanner
from .ranker import LLMCandidateChooser
from .search import BrowserSearch, SerpApiSearch


def build_chat_model(model_name: str):
    """统一的模型客户端。**必须设调用超时**。

    实测（r19、r20、r22）：上游偶发停顿能把一次 `choose_download` 拖过 120 秒，而批量的
    单任务上限也是 120 秒——结果整次尝试被白杀，重试又可能再卡一次（r20 的 #6、r22 的 #6
    两次都死在同一个位置，token 记 0）。langchain-openai 默认沿用 SDK 的 600 秒 + 重试，
    远大于这里的任何上限。

    **两个数必须一起算**：`timeout × (max_retries + 1)` 要明显小于批量的单任务上限，
    否则停顿照样吃满预算、状态图没机会换候选（r22 就是 60 × 2 = 120，正好等于上限）。
    默认 40 秒 × 2 = 80 秒，留 40 秒给"换候选再试一次"；实测正常调用在 5~12 秒之间。
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model_name,
        temperature=0,
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE"),
        timeout=float(os.getenv("OPENAI_TIMEOUT", "40")),
        max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "1")),
    )


def main() -> None:
    load_dotenv()
    # Windows 终端可能使用 GBK；流程记录统一以 UTF-8 输出，避免中文文件名乱码。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Discover and download an official document")
    parser.add_argument("filename", help="目标文件名称")
    parser.add_argument("--output", help="将完整结果和节点轨迹保存为 UTF-8 JSON")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="有头模式：真的弹出浏览器窗口（需要显式指定；会抢焦点，影响你同时干别的活）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（**默认**）：显式写出来可以盖过 BROWSER_HEADLESS=0",
    )
    args = parser.parse_args()
    if args.headed and args.headless:
        parser.error("--headed 与 --headless 不能同时使用")
    # None = 交给 runtime.headless_from_env（读 BROWSER_HEADLESS）。
    headless = True if args.headless else (False if args.headed else None)
    planner = None
    chooser = None
    if os.getenv("OPENAI_API_KEY"):
        try:
            planner = LLMDownloadPlanner(build_chat_model(os.getenv("OPENAI_MODEL", "qwen-plus")))
            # 候选选择用一个小模型：它只做"池子里先看哪一个"这一件事，输入是候选的标题+摘要，
            # 输出只有下标，因此可以便宜地跑在每条任务上。设成 off/none 可关掉它（用于 A/B 对比）。
            chooser_model = os.getenv("OPENAI_CHOOSER_MODEL", "qwen-turbo").strip()
            if chooser_model.lower() not in {"", "off", "none"}:
                chooser = LLMCandidateChooser(build_chat_model(chooser_model))
        except ImportError:
            print("未安装 langchain-openai，将使用规则型下载策略。")
    # 有 API Key 时优先使用结构化搜索；没有时直接使用浏览器搜索。
    use_search_api = bool(os.getenv("SERPAPI_API_KEY"))
    searcher = SerpApiSearch() if use_search_api else BrowserSearch(headless=headless)
    browser = PlaywrightBrowser(headless=headless)
    # 把实际生效的模式写进日志：跑批几十条时，"这次到底是不是有头"必须能从产物里查到。
    def mode(flag: bool | None) -> str:
        return "有头（真实窗口）" if flag is False else "无头"

    print(
        f"[浏览器] 页面={mode(browser.headless)}｜检索="
        f"{'SerpAPI' if use_search_api else mode(getattr(searcher, 'headless', True))}",
        flush=True,
    )
    result = run(args.filename, searcher, browser, planner=planner, chooser=chooser)
    payload = result.get("final", result)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
