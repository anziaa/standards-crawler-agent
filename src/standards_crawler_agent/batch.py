"""任务级并行批量运行：每个文件名一个独立进程。

为什么是进程而不是线程：
  Playwright 的同步 API 把 asyncio 事件循环与 greenlet 调度器绑在创建它的线程上，
  同一线程不能有两个实例（会报 using Playwright Sync API inside the asyncio loop），
  实例也不能跨线程使用。`runtime.py` 里是进程级单例，因此独立进程最干净：
  每个进程各自持有一个 Playwright 实例，互不干扰。

为什么可以共用一个 downloads 目录：
  最终文件名由请求名生成（`{请求名}_{标准编号}.pdf`），不同任务的目标文件不同，
  产物名天然不同。唯一会撞的是 save_page_pdf / click_print 的两个常量文件名，
  已在 browser.py 中加上进程号后缀（`page_export_<pid>.pdf`），并发进程不会互相覆盖。

用法：
    python -m standards_crawler_agent.batch "电梯用钢丝绳" "建筑防火通用规范" --jobs 3

产物结构：
    results/batch_<时间戳>/
        01_电梯用钢丝绳/
            result.json      # 完整节点轨迹（含 timings 与 usage_total）
            run.log          # 该进程的 stdout/stderr
        02_建筑防火通用规范/
        ...
        batch_summary.json   # 汇总：状态、耗时、token、失败原因
    downloads/               # 共用下载目录（与单任务运行时一致）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", value).strip(" .")
    return cleaned[:60] or "task"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="official-file-batch",
        description="并行批量下载多个官方文件（每个任务一个独立进程，共用 downloads 目录）",
    )
    parser.add_argument("filenames", nargs="+", help="目标文件名称，可传多个")
    parser.add_argument("--jobs", type=int, default=3, help="并行进程数，默认 3")
    parser.add_argument("--output-root", help="批量产物根目录，默认 results/batch_<时间戳>")
    parser.add_argument("--timeout", type=int, default=120, help="单个任务超时秒数，默认 120")
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="进程级偶发故障（超时/崩溃/没生成结果）时整任务重试次数，默认 1",
    )
    parser.add_argument(
        "--stagger",
        type=float,
        default=2.0,
        help="任务启动间隔秒数，避免并发请求触发搜索引擎风控，默认 2.0",
    )
    return parser.parse_args(argv)


def deduplicate(names: list[str]) -> tuple[list[str], list[str]]:
    """去掉重复的请求名：指向同一目标的条目会写出同一个最终文件，互相覆盖。"""
    seen: set[str] = set()
    unique: list[str] = []
    duplicates: list[str] = []
    for name in names:
        key = re.sub(r"[\s　]+", "", name).lower()
        if key in seen:
            duplicates.append(name)
            continue
        seen.add(key)
        unique.append(name)
    return unique, duplicates


def run_one(index: int, name: str, output_root: Path, jobs: int, timeout: int, stagger: float, cwd: Path, attempt: int = 1) -> dict[str, Any]:
    # 重试写到单独目录：否则第二次尝试会覆盖第一次的 result.json 与 run.log，
    # 第一次到底为什么失败就查不到了。
    suffix = "" if attempt == 1 else f"_retry{attempt - 1}"
    task_dir = output_root / f"{index:02d}_{safe_name(name)}{suffix}"
    result_path = task_dir / "result.json"
    log_path = task_dir / "run.log"
    task_dir.mkdir(parents=True, exist_ok=True)

    # 错峰启动：并发数越大越容易同时打到搜索引擎上，这里让启动时间错开。
    delay = min(index, max(0, jobs - 1)) * stagger
    if delay:
        time.sleep(delay)

    command = [
        sys.executable,
        # -u：无缓冲。任务被超时杀掉时，缓冲里的进度日志还能留下来。
        "-u",
        "-m",
        "standards_crawler_agent.cli",
        name,
        "--output",
        str(result_path.resolve()),
    ]

    summary: dict[str, Any] = {"index": index, "filename": name, "task_dir": str(task_dir), "attempt": attempt}
    started = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=str(cwd), timeout=timeout)
            summary["returncode"] = completed.returncode
        except subprocess.TimeoutExpired:
            summary["returncode"] = None
            summary["status"] = "timeout"
            summary["errors"] = [f"超过 {timeout} 秒未结束，已终止"]
    summary["wall_seconds"] = round(time.perf_counter() - started, 1)

    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            summary.setdefault("errors", []).append(f"结果 JSON 解析失败：{exc}")
        else:
            verification = payload.get("verification") or {}
            summary.update(
                {
                    "status": payload.get("status"),
                    "downloaded_path": payload.get("downloaded_path"),
                    "source_url": payload.get("source_url"),
                    "lifecycle_status": payload.get("lifecycle_status"),
                    "confidence": payload.get("confidence"),
                    "usage_total": payload.get("usage_total"),
                    "total_elapsed_ms": payload.get("total_elapsed_ms"),
                    "verification_size": verification.get("size"),
                    "verification_pages": (verification.get("metadata") or {}).get("pages"),
                    "search_degraded": payload.get("search_degraded"),
                    "search_unmatched": payload.get("search_unmatched"),
                    "entry_reason": payload.get("entry_reason"),
                    "errors": payload.get("errors", []),
                }
            )
    elif "errors" not in summary:
        summary["errors"] = ["未生成结果 JSON，进程可能异常退出"]

    return summary


def should_retry(summary: dict[str, Any], attempt: int, max_retries: int) -> bool:
    """只有"进程级偶发故障"才**原地**重试。

    超时、崩溃、没生成结果 JSON 属于这一类——任务本身通常没问题，重跑一次就能过
    （实测那条 600 秒挂起的任务单独重跑只用了 20.4 秒）。

    **不在这里重试 `search_degraded` / `search_unmatched`**：曾经加过"搜索降级就原地重跑一次"，
    实测收益极低。原因是"无匹配兜底页"在分钟级窗口内是确定的——同一查询 9 分钟内 12/12
    稳定返回同一批无关结果，同秒内重读 3 次也全是同一页（76 行 × 3 次 = 0 收益）。
    有效做法是**隔一段时间换一批再跑**：实测块 1 与块 2 相隔约 25 分钟，229 行里恢复 39 行。
    因此冷却重跑放在驱动层（跑完一遍、等待、再跑失败行），而不是在这里原地白等。
    """
    if attempt > max_retries:
        return False
    status = summary.get("status")
    if status in {None, "timeout", "driver_error"}:
        return True
    return summary.get("returncode") not in (0, None)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cwd = Path.cwd()
    names, duplicates = deduplicate(args.filenames)
    output_root = Path(args.output_root) if args.output_root else Path("results") / f"batch_{datetime.now():%Y%m%d_%H%M%S}"
    output_root.mkdir(parents=True, exist_ok=True)

    print(f"批量任务 {len(names)} 个 | 并行度 {min(args.jobs, len(names))} | 工作目录 {cwd}")
    print(f"产物目录 {output_root} | 下载目录 {cwd / 'downloads'}（共用）")
    if duplicates:
        print(f"已跳过重复请求名 {len(duplicates)} 个：{'、'.join(duplicates)}")
    print(f"错峰间隔 {args.stagger}s | 单任务超时 {args.timeout}s | 偶发故障重试 {args.retries} 次\n")

    jobs = max(1, min(args.jobs, len(names)))
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    attempts: dict[int, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        # 线程只负责等待子进程，真正的工作在独立进程里。
        futures = {
            pool.submit(run_one, index, name, output_root, jobs, args.timeout, args.stagger, cwd, 1): (index, name, 1)
            for index, name in enumerate(names, 1)
        }
        while futures:
            for future in as_completed(list(futures)):
                index, name, attempt = futures.pop(future)
                try:
                    summary = future.result()
                except Exception as exc:  # 驱动自身出错也不能丢掉其他任务
                    summary = {"index": index, "filename": name, "status": "driver_error", "errors": [f"{type(exc).__name__}: {exc}"]}
                if should_retry(summary, attempt, args.retries):
                    attempts.setdefault(index, []).append(summary)
                    print(
                        f"    ↻ {name[:22]} 第 {attempt} 次未成功"
                        f"（{summary.get('status') or 'unknown'}，{summary.get('wall_seconds') or 0}s），重试一次"
                    )
                    next_attempt = attempt + 1
                    futures[
                        pool.submit(run_one, index, name, output_root, jobs, args.timeout, args.stagger, cwd, next_attempt)
                    ] = (index, name, next_attempt)
                    continue
                prior = attempts.get(index) or []
                if prior:
                    summary["attempts"] = attempt
                    summary["first_attempt"] = {
                        key: prior[0].get(key) for key in ("status", "wall_seconds", "errors")
                    }
                results.append(summary)
                mark = f"（第 {attempt} 次尝试）" if attempt > 1 else ""
                print(f"[{len(results)}/{len(names)}] {name[:22]:<24}{(summary.get('status') or 'unknown'):<18}{summary.get('wall_seconds') or 0:>6.1f}s{mark}")

    total_seconds = round(time.perf_counter() - started, 1)
    results.sort(key=lambda item: item["index"])
    tokens = sum((item.get("usage_total") or {}).get("total_tokens") or 0 for item in results)

    print("\n" + "=" * 76)
    print(f"{'#':<3}{'文件名':<24}{'状态':<20}{'耗时':>8}{'页数':>6}{'token':>8}")
    print("-" * 76)
    for item in results:
        usage = item.get("usage_total") or {}
        print(
            f"{item['index']:<3}{item['filename'][:22]:<24}{(item.get('status') or 'unknown'):<20}"
            f"{item.get('wall_seconds') or 0:>7.1f}s{item.get('verification_pages') or '-':>6}{usage.get('total_tokens') or 0:>8}"
        )
    print("-" * 76)
    def is_success(status: object) -> bool:
        return isinstance(status, str) and status.startswith("success")

    succeeded = sum(1 for item in results if is_success(item.get("status")))
    originals = sum(1 for item in results if item.get("status") == "success")
    exports = sum(1 for item in results if item.get("status") == "success_page_export")
    scanned = sum(1 for item in results if item.get("status") == "success_scanned")
    print(
        f"成功 {succeeded}/{len(results)}（官方原件 {originals}，网页导出 {exports}，扫描件 {scanned}）"
        f" | 墙钟总计 {total_seconds}s | 大模型 token 合计 {tokens}"
    )

    failed = [item for item in results if not is_success(item.get("status"))]
    if failed:
        print("\n未成功的任务：")
        for item in failed:
            reasons = "；".join(str(reason).splitlines()[0] for reason in (item.get("errors") or [])[:2])
            print(f"  - {item['filename']}：{item.get('status') or 'unknown'}{'　' + reasons if reasons else ''}")

    summary_path = output_root / "batch_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "jobs": jobs,
                "retries": args.retries,
                "total_seconds": total_seconds,
                "total_tokens": tokens,
                "skipped_duplicates": duplicates,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n汇总已写入 {summary_path}")


if __name__ == "__main__":
    main()
