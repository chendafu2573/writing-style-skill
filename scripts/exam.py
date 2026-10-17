#!/usr/bin/env python3
"""盲选考试：基准 DNA 和当前 DNA 各写一遍考卷，作者盲选哪版更像自己。

prepare  建考场：复制两份 DNA，为每张考卷生成写作说明
shuffle  两边都写完后，随机标成「版本甲 / 版本乙」，生成盲选页
record   记录作者的选择，揭晓并给结论；通过时把当前 DNA 设为新基准
rebase   作者确认后，不考试直接把当前 DNA 设为基准
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dna_lib  # noqa: E402

SIDES = ("old", "new")
PICKS = {"甲", "乙", "都不像"}
def exam_dir(root: Path) -> Path:
    return root / "evals" / "exam"


def runs_dir(root: Path) -> Path:
    return root / "evals" / "exam-runs"


def manifest(root: Path, run: Path | None = None) -> list[dict]:
    # 新考场固定题目顺序，旧考场仍兼容全局清单。
    path = run / "manifest.json" if run is not None else exam_dir(root) / "manifest.json"
    if not path.exists():
        path = exam_dir(root) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def baseline(root: Path) -> dict:
    return json.loads((exam_dir(root) / "baseline.json").read_text(encoding="utf-8"))


def task_text(root: Path, run: Path, side: str, item: dict) -> str:
    papers = run / "papers" if (run / "manifest.json").exists() else exam_dir(root)
    paper = (papers / item["paper"]).read_text(encoding="utf-8")
    output = run / f"{side}-{item['paper']}"
    return f"""# 盲选考试写作任务

你是写推文的 Agent，正在参加一次盲选考试。按下面要求只写一条推文正文。

1. 读取 `{root / 'SKILL.md'}` 和它 §0 full 模式清单里的文件，**唯一例外**：偏好库不读 `reference/dna.md`，改读 `{run / f'{side}-dna.md'}`。
   这份偏好库里，状态为「生效」或信心分 ≥3 的偏好视为生效。
2. 不要读取 `assets/`、`evals/exam/manifest.json`、`evals/exam-runs/` 下的其他文件，也不要搜索作者以前写过的同题文章。
3. 素材就是下面这张考卷，输入类型按 B（资料）处理。考试中不向用户提问，跳过写前追问，素材没有的信息不要编。
4. 只把推文正文写进 `{output}`，不要写说明、检查块或标题。不要运行任何脚本，不要修改其他文件。

---

{paper}
"""


def prepare(root: Path) -> dict:
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        run = runs_dir(root) / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        run.mkdir(parents=True)
        shutil.copy2(exam_dir(root) / "manifest.json", run / "manifest.json")
        items = manifest(root, run)
        (run / "papers").mkdir()
        for item in items:
            shutil.copy2(exam_dir(root) / item["paper"], run / "papers" / item["paper"])
        shutil.copy2(exam_dir(root) / "baseline-dna.md", run / "old-dna.md")
        shutil.copy2(dna_lib.paths(root)["dna"], run / "new-dna.md")
        tasks = {}
        for side in SIDES:
            texts = [task_text(root, run, side, item) for item in items]
            path = run / f"task-{side}.md"
            path.write_text("\n\n".join(texts), encoding="utf-8")
            tasks[side] = str(path)
        base = baseline(root)
        meta = {"baseline_since": base["since"], "changes": dna_lib.changes_since(root, base),
                "log_rows": len(dna_lib.log_data_rows(root)),
                "baseline_hash": hashlib.sha256((run / "old-dna.md").read_bytes()).hexdigest()}
        (run / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"run": run.name, "tasks": tasks, "note": "开两个全新的 Agent，一个只执行 task-old.md，一个只执行 task-new.md"}


def shuffle(root: Path, run_name: str, seed: int | None = None) -> dict:
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        run = runs_dir(root) / run_name
        if (run / "result.json").exists():
            raise SystemExit("这个考场已交过卷，不能再洗牌")
        if (run / "key.json").exists():
            raise SystemExit("已经洗过牌；作者可能已看过盲选页，重洗会让答案对不上")
        rng = random.Random(seed)
        items = manifest(root, run)
        key, hashes = {}, {}
        blocks = [f"# 盲选：哪一版更像你？\n\n> 考场 {run_name}。每张卷挑「甲」「乙」或「都不像」。\n"]
        for item in items:
            texts = {}
            for side in SIDES:
                path = run / f"{side}-{item['paper']}"
                if not path.is_file():
                    raise SystemExit(f"还没写完: {path.name}")
                # 展示和校验取同一份正文，避免两次读取之间被改动。
                body = path.read_bytes()
                texts[side] = body.decode("utf-8").strip()
                if not texts[side]:
                    raise SystemExit(f"还没写完: {path.name}")
                hashes[path.name] = hashlib.sha256(body).hexdigest()
            order = list(SIDES)
            rng.shuffle(order)
            key[item["paper"]] = {"甲": order[0], "乙": order[1]}
            blocks.append(f"## {item['title']}（{item['scene']}）\n\n### 版本甲\n\n{texts[order[0]]}\n\n### 版本乙\n\n{texts[order[1]]}\n")
        blind = "\n---\n\n".join(blocks)
        hashes["blind.md"] = hashlib.sha256(blind.encode("utf-8")).hexdigest()
        key["_hashes"] = hashes
        dna_lib.write_all({
            run / "key.json": json.dumps(key, ensure_ascii=False, indent=2),
            run / "blind.md": blind,
        })
    return {"blind": str(run / "blind.md"), "papers": len(items)}


def record(root: Path, run_name: str, picks: list[str]) -> dict:
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        run = runs_dir(root) / run_name
        if (run / "result.json").exists():
            raise SystemExit("这个考场已交过卷")
        key = json.loads((run / "key.json").read_text(encoding="utf-8"))
        items = manifest(root, run)
        if "_hashes" in key:
            names = [f"{side}-{item['paper']}" for item in items for side in SIDES] + ["blind.md"]
            for name in names:
                path = run / name
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != key["_hashes"].get(name):
                    raise SystemExit("考卷正文在洗牌后被改过，这次成绩不能用")
        meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
        current_baseline = exam_dir(root) / "baseline-dna.md"
        if "baseline_hash" in meta and (not current_baseline.is_file() or
                hashlib.sha256(current_baseline.read_bytes()).hexdigest() != meta["baseline_hash"]):
            raise SystemExit("建考场之后基准已被其他考场或 rebase 改过，这个考场过期了，请重新建考场")
        if len(picks) != len(items) or any(pick not in PICKS for pick in picks):
            raise SystemExit(f"需要 {len(items)} 个选择，每个是 甲 / 乙 / 都不像")
        chosen = ["neither" if pick == "都不像" else key[item["paper"]][pick] for item, pick in zip(items, picks)]
        new_wins, old_wins = chosen.count("new"), chosen.count("old")
        passed = new_wins + old_wins > 0 and new_wins >= old_wins
        if new_wins + old_wins == 0:
            verdict = "无结论，保持旧基准"
        else:
            verdict = "保留，当前 DNA 设为新基准" if passed else "旧版更像作者，建议撤回基准之后的改动"
        log = root / "evals" / "exam-log.md"
        log_text = log.read_text(encoding="utf-8") if log.exists() else (
            "# 盲选考试记录\n\n| 时间 | 考场 | 新版赢 | 旧版赢 | 都不像 | 结论 | 基准之后的改动 |\n|---|---|---|---|---|---|---|\n")
        changes = "；".join(meta["changes"]) or "—"
        now = dna_lib.now_text()
        row = f"| {now} | {run_name} | {new_wins} | {old_wins} | {chosen.count('neither')} | {verdict} | {dna_lib.cell(changes)} |"
        writes = {
            run / "result.json": json.dumps({"picks": picks, "chosen": chosen, "passed": passed}, ensure_ascii=False, indent=2),
            log: log_text.rstrip() + "\n" + row + "\n",
        }
        if passed:
            # 只推进到考场快照覆盖的位置，中途的新改动仍需考试。
            writes[exam_dir(root) / "baseline-dna.md"] = (run / "new-dna.md").read_text(encoding="utf-8")
            writes[exam_dir(root) / "baseline.json"] = json.dumps(
                {"since": now, "note": f"考场 {run_name} 通过后设为基准", "log_rows": meta["log_rows"]}, ensure_ascii=False, indent=2)
        dna_lib.write_all(writes)
    return {"新版赢": new_wins, "旧版赢": old_wins, "都不像": chosen.count("neither"), "结论": verdict,
            "每张卷": [f"{item['title']}：{pick} = {'当前 DNA' if c == 'new' else '基准 DNA' if c == 'old' else '都不像'}"
                     for item, pick, c in zip(items, picks, chosen)],
            "基准之后的改动": meta["changes"]}


def rebase(root: Path, reason: str) -> dict:
    """作者确认后，不考试直接把当前 DNA 设为基准（例如刚按考试结论撤回改动）。"""
    if not reason.strip():
        raise SystemExit("rebase 必须写明原因")
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        log = root / "evals" / "exam-log.md"
        now = dna_lib.now_text()
        row = f"| {now} | — | — | — | — | 作者确认直接设为基准：{dna_lib.cell(reason)} | — |"
        writes = {
            exam_dir(root) / "baseline-dna.md": dna_lib.paths(root)["dna"].read_text(encoding="utf-8"),
            exam_dir(root) / "baseline.json": json.dumps(
                {"since": now, "note": f"作者确认直接设为基准：{reason}", "log_rows": len(dna_lib.log_data_rows(root))},
                ensure_ascii=False, indent=2),
        }
        if log.exists():
            writes[log] = log.read_text(encoding="utf-8").rstrip() + "\n" + row + "\n"
        dna_lib.write_all(writes)
    return {"baseline": "已设为当前 DNA", "reason": reason}


def main() -> int:
    parser = argparse.ArgumentParser(description="盲选考试")
    parser.add_argument("action", choices=["prepare", "shuffle", "record", "rebase"])
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--run", help="shuffle/record：考场编号")
    parser.add_argument("--picks", help="record：按考卷顺序，逗号分隔，如 甲,乙,都不像")
    parser.add_argument("--reason", default="", help="rebase：作者确认的原因")
    parser.add_argument("--confirmed", action="store_true", help="rebase：作者已确认")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        result = prepare(root)
    elif args.action == "rebase":
        if not args.confirmed:
            raise SystemExit("rebase 会改变考试基准：作者确认后加 --confirmed 再运行")
        result = rebase(root, args.reason)
    elif args.action == "shuffle":
        result = shuffle(root, args.run)
    else:
        result = record(root, args.run, [p.strip() for p in (args.picks or "").split(",")])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
