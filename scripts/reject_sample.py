#!/usr/bin/env python3
"""Reject a finalized agent sample: void its review rows and recompute statistics."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dna_lib  # noqa: E402


def _meta(text: str, field: str) -> str | None:
    match = re.search(rf"(?m)^\| {re.escape(field)} \| (.*?) \|$", text)
    return match.group(1).strip() if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description="拒绝失败样本：作废它的复盘记录并重算统计")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    artifact = args.artifact.resolve()
    p = dna_lib.paths(root)
    rejection_log = root / "assets" / "rejection-log.md"
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        if not artifact.is_file() or artifact.parent != root / "assets":
            raise SystemExit("artifact必须是assets下的文件")
        text = artifact.read_text(encoding="utf-8")
        source_type = _meta(text, "来源类型")
        if source_type is None:
            raise SystemExit("旧资产没有来源类型，禁止自动回滚；请先人工标注")
        if _meta(text, "资产状态") != "active":
            raise SystemExit(f"只有active资产可以拒绝，当前状态: {_meta(text, '资产状态') or '缺失'}")
        if source_type not in {"agent_draft", "user_revision", "mixed"}:
            raise SystemExit(f"来源类型不允许自动拒绝回滚: {source_type}")
        name = artifact.name
        review, voided = dna_lib.set_review_state(p["review"].read_text(encoding="utf-8"), name, "有效", "作废")
        head, prefs, archive_head, archived = dna_lib.load_ledgers(root)
        reviews = dna_lib.read_reviews_text(review)
        dna_lib.recompute(prefs, reviews)
        dna_lib.recompute(archived, reviews)
        index, count = re.subn(
            rf"(?m)^(\| [^|]+ \| {re.escape(name)} \| [^|]+ \| )[^|]+ \| active( \|)$", r"\g<1>否 | rejected\g<2>",
            p["index"].read_text(encoding="utf-8"), count=1,
        )
        if count != 1:
            raise SystemExit("style-index找不到active样本")
        artifact_after, count = re.subn(r"(?m)^\| 资产状态 \| active \|$", "| 资产状态 | rejected |", text, count=1)
        if count != 1:
            raise SystemExit("样本文件找不到active资产状态")
        rejected = rejection_log.read_text(encoding="utf-8") if rejection_log.exists() else "# 拒绝记录\n\n"
        changes = {
            p["review"]: review,
            p["dna"]: dna_lib.render_ledger(head, prefs),
            p["index"]: index,
            artifact: artifact_after,
            p["log"]: dna_lib.append_log(p["log"].read_text(encoding="utf-8"), "撤销", name, f"作废复盘 {voided} 条：{args.reason}", voided),
            rejection_log: rejected.rstrip() + f"\n| {name} | {dna_lib.cell(args.reason)} |\n",
        }
        archive_after = dna_lib.render_ledger(archive_head, archived)
        if archive_after != p["archive"].read_text(encoding="utf-8"):
            changes[p["archive"]] = archive_after
        dna_lib.write_all(changes)
    print(f"rejected: {name}; voided_reviews: {voided}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
