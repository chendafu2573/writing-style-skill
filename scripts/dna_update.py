#!/usr/bin/env python3
"""偏好账本唯一的修改入口。

每次改动先备份 dna.md / dna-archive.md / benchmark.md / diff-log.md，
可用 rollback 撤回。会改变长期风格的命令都必须带 --confirmed，表示作者已确认。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dna_lib  # noqa: E402

TRANSITIONS = {
    ("候选", "生效"): "生效",
    ("生效", "休眠"): "休眠",
    ("休眠", "生效"): "唤醒",
    ("休眠", "淘汰"): "淘汰",
    ("候选", "淘汰"): "淘汰",
    ("生效", "淘汰"): "淘汰",
    ("淘汰", "生效"): "恢复",  # 例如盲选考试建议撤回某次淘汰
}


def _require_confirmed(args) -> None:
    if not args.confirmed:
        raise SystemExit("这项改动会影响长期风格：作者确认后加 --confirmed 再运行")


def _tracked(root: Path) -> list[Path]:
    p = dna_lib.paths(root)
    return [p["dna"], p["archive"], p["benchmark"], p["log"], p["state"]]


def _next_snf(benchmark: str) -> str:
    numbers = [int(n) for n in re.findall(r"(?m)^#### SNF-(\d+)", benchmark)]
    return f"SNF-{max(numbers, default=0) + 1:03d}"


def _commit(root: Path, head: str, prefs, archive_head: str, archived, log_event: str, detail: str, benchmark: str | None = None) -> Path:
    p = dna_lib.paths(root)
    reviews = dna_lib.read_reviews(p["review"])
    dna_lib.recompute(prefs, reviews)
    dna_lib.recompute(archived, reviews)
    for pref in prefs:
        errors = dna_lib.validate_pref(pref, archived=False)
        if errors:
            raise SystemExit("；".join(errors))
    for pref in archived:
        errors = dna_lib.validate_pref(pref, archived=True)
        if errors:
            raise SystemExit("；".join(errors))
    saved = dna_lib.backup(root, _tracked(root))
    changes = {
        p["dna"]: dna_lib.render_ledger(head, prefs),
        p["archive"]: dna_lib.render_ledger(archive_head, archived),
        p["log"]: dna_lib.append_log(p["log"].read_text(encoding="utf-8"), log_event, "—", detail, 1),
        p["state"]: dna_lib.set_state_counts(p["state"].read_text(encoding="utf-8"), len(prefs), len(archived)),
    }
    if benchmark is not None:
        changes[p["benchmark"]] = benchmark
    try:
        dna_lib.write_all(changes)
    except Exception:
        saved.rename(saved.with_name(saved.name + ".failed"))
        raise
    # 记下改完后的样子：rollback 时如果文件又被别的操作改过，就拒绝撤回
    dna_lib.atomic_write(saved / "after.json", json.dumps(dna_lib.file_hashes(_tracked(root))).encode("utf-8"))
    return saved


def cmd_add(root: Path, args) -> dict:
    _require_confirmed(args)
    if args.status not in {"候选", "生效"}:
        raise SystemExit("新增偏好只能是候选或生效")
    p = dna_lib.paths(root)
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        head, prefs, archive_head, archived = dna_lib.load_ledgers(root)
        pid = dna_lib.next_id(prefs, archived)
        date = dna_lib.today()
        evidence = ", ".join(args.evidence) if args.evidence else "—"
        index = dna_lib.read_index(p["index"])
        for sample in args.evidence or []:
            if sample not in index:
                raise SystemExit(f"证据样本未登记在 style-index: {sample}")
        pref = dna_lib.Pref(pid, {
            "写法": args.text, "类型": args.type, "适用场景": args.scene, "状态": args.status,
            "来源": args.source, "证据": evidence, "统计": "用上 0 / 守住 0 / 被改回 0 / 印证 0",
            "创建于": date, "最近相关": date, "状态日期": date,
        })
        benchmark = None
        if args.status == "生效":
            if not (args.snf_title and args.snf_context and args.snf_text and args.snf_reason):
                raise SystemExit("生效偏好必须同时写一条 SNF：--snf-title --snf-context --snf-text --snf-reason")
            text = p["benchmark"].read_text(encoding="utf-8")
            snf = _next_snf(text)
            benchmark = text.rstrip() + (
                f"\n\n#### {snf} {args.snf_title}\n- 触发上下文: {args.snf_context}\n- 原文片段: \"{args.snf_text}\"\n"
                f"- 期望行为: 不改写\n- 理由: {args.snf_reason}对应偏好 {pid}。\n"
            )
        prefs.append(pref)
        saved = _commit(root, head, prefs, archive_head, archived, "新增", f"{pid} [新增·{args.status}] {args.text}", benchmark)
    return {"id": pid, "status": args.status, "backup": str(saved)}


def cmd_edit(root: Path, args) -> dict:
    _require_confirmed(args)
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        head, prefs, archive_head, archived = dna_lib.load_ledgers(root)
        pref = dna_lib.find(prefs, args.id)
        if not pref:
            raise SystemExit(f"dna.md 没有偏好 {args.id}")
        changed = []
        for key, value in (("写法", args.text), ("类型", args.type), ("适用场景", args.scene)):
            if value:
                changed.append(f"{key}: {pref.fields[key]} → {value}")
                pref.fields[key] = value
        if not changed:
            raise SystemExit("没有要修改的字段")
        saved = _commit(root, head, prefs, archive_head, archived, "修改", f"{args.id} [修改] " + "；".join(changed))
    return {"id": args.id, "changed": changed, "backup": str(saved)}


def cmd_status(root: Path, args) -> dict:
    _require_confirmed(args)
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        head, prefs, archive_head, archived = dna_lib.load_ledgers(root)
        pref = dna_lib.find(prefs, args.id) or dna_lib.find(archived, args.id)
        if not pref:
            raise SystemExit(f"没有偏好 {args.id}")
        event = TRANSITIONS.get((pref.fields["状态"], args.to))
        if not event:
            raise SystemExit(f"不能从 {pref.fields['状态']} 改成 {args.to}")
        was_archived = pref.fields["状态"] == "淘汰"
        pref.fields["状态"] = args.to
        pref.fields["状态日期"] = dna_lib.today()
        if args.to == "淘汰":
            prefs.remove(pref)
            archived.append(pref)
        elif was_archived:
            archived.remove(pref)
            prefs.append(pref)
            prefs.sort(key=lambda p: int(p.pid[1:]))
        detail = f"{args.id} [{event}]" + (f" {args.reason}" if args.reason else "")
        saved = _commit(root, head, prefs, archive_head, archived, event, detail)
    return {"id": args.id, "status": args.to, "backup": str(saved)}


def cmd_recompute(root: Path, _args) -> dict:
    p = dna_lib.paths(root)
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        head, prefs, archive_head, archived = dna_lib.load_ledgers(root)
        reviews = dna_lib.read_reviews(p["review"])
        dna_lib.recompute(prefs, reviews)
        dna_lib.recompute(archived, reviews)
        changes = {p["dna"]: dna_lib.render_ledger(head, prefs)}
        archive_after = dna_lib.render_ledger(archive_head, archived)
        if archive_after != p["archive"].read_text(encoding="utf-8"):
            changes[p["archive"]] = archive_after
        dna_lib.write_all(changes)
    return {"recomputed": len(prefs) + len(archived)}


def cmd_report(root: Path, _args) -> dict:
    _, prefs, _, archived = dna_lib.load_ledgers(root)
    by_status = {}
    for pref in prefs:
        by_status.setdefault(pref.fields["状态"], []).append(f"{pref.pid} {pref.fields['写法']}（{pref.fields['统计']}）")
    by_status["淘汰"] = [f"{pref.pid} {pref.fields['写法']}" for pref in archived]
    return {"偏好": by_status, "提示": dna_lib.suggest(root)}


def cmd_rollback(root: Path, args) -> dict:
    _require_confirmed(args)
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        saved = dna_lib.latest_backup(root)
        if not saved:
            raise SystemExit("没有可撤回的备份")
        after = saved / "after.json"
        if not after.exists():
            raise SystemExit("这份备份没有写入完成标记，不能自动撤回")
        if json.loads(after.read_text(encoding="utf-8")) != dna_lib.file_hashes(_tracked(root)):
            raise SystemExit("备份之后账本或日志又被改过（例如又定稿了一篇），不能自动撤回；请用 git 查看后手动恢复")
        changes = {}
        for path in _tracked(root):
            copy = saved / path.relative_to(root)
            if copy.exists():
                changes[path] = copy.read_text(encoding="utf-8")
        dna_lib.write_all(changes)
        shutil.move(str(saved), str(saved) + ".restored")
    return {"restored_from": saved.name}


def main() -> int:
    parser = argparse.ArgumentParser(description="偏好账本：新增、修改、状态切换、统计、提示、撤回")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("add", "edit", "status", "recompute", "report", "suggest", "rollback"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--root", type=Path, default=Path(__file__).parents[1])
        cmd.add_argument("--confirmed", action="store_true", help="作者已确认")
        if name in {"edit", "status"}:
            cmd.add_argument("--id", required=True)
        if name in {"add", "edit"}:
            cmd.add_argument("--text", required=(name == "add"))
            cmd.add_argument("--type", choices=sorted(dna_lib.TYPES), required=(name == "add"))
            cmd.add_argument("--scene", default=("全局" if name == "add" else None))
        if name == "add":
            cmd.add_argument("--status", choices=["候选", "生效"], required=True)
            cmd.add_argument("--source", required=True, help="如：用户口头指令 2026-10-17 或 样本文件名")
            cmd.add_argument("--evidence", nargs="*", help="证据样本文件名（assets 下）")
            for flag in ("--snf-title", "--snf-context", "--snf-text", "--snf-reason"):
                cmd.add_argument(flag)
        if name == "status":
            cmd.add_argument("--to", required=True, choices=["生效", "休眠", "淘汰"])
            cmd.add_argument("--reason")
    args = parser.parse_args()
    root = args.root.resolve()
    handlers = {
        "add": cmd_add, "edit": cmd_edit, "status": cmd_status, "recompute": cmd_recompute,
        "report": cmd_report, "rollback": cmd_rollback,
        "suggest": lambda r, _a: {"提示": dna_lib.suggest(r)},
    }
    print(json.dumps(handlers[args.action](root, args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
