#!/usr/bin/env python3
"""给已存档的稿子补标「仿写口吻」：原文保留，复盘退出统计，不再当风格证据。"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dna_lib  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="补标仿写稿：作废复盘、重算统计、风格资格改为否")
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--voice", required=True, help="口吻名，如：鲁迅·梦境体")
    args = parser.parse_args()
    root = args.root.resolve()
    artifact = args.artifact.resolve()
    p = dna_lib.paths(root)
    try:
        voice = dna_lib.check_voice(args.voice)
    except ValueError as exc:
        raise SystemExit(str(exc))
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        if not artifact.is_file() or artifact.parent != root / "assets":
            raise SystemExit("artifact必须是assets下的文件")
        text = artifact.read_text(encoding="utf-8")
        if dna_lib.artifact_voice(text):
            raise SystemExit(f"已经标过仿写口吻：{dna_lib.artifact_voice(text)}")
        if "| 资产状态 | active |" not in text:
            raise SystemExit("只有active样本可以补标")
        name = artifact.name
        review, voided = dna_lib.set_review_state(p["review"].read_text(encoding="utf-8"), name, "有效", "仿写")
        head, prefs, archive_head, archived = dna_lib.load_ledgers(root)
        reviews = dna_lib.read_reviews_text(review)
        dna_lib.recompute(prefs, reviews)
        dna_lib.recompute(archived, reviews)
        index, count = re.subn(
            rf"(?m)^(\| [^|]+ \| {re.escape(name)} \| [^|]+ \| )[^|]+( \| active \|)$", r"\g<1>否\g<2>",
            p["index"].read_text(encoding="utf-8"), count=1,
        )
        if count != 1:
            raise SystemExit("style-index找不到active样本")
        if re.search(r"(?m)^\| 仿写口吻 \|", text):
            artifact_after = re.sub(r"(?m)^\| 仿写口吻 \| .*? \|$", f"| 仿写口吻 | {voice} |", text, count=1)
        else:
            artifact_after, count = re.subn(r"(?m)^(\| 来源类型 \| .*? \|)$", rf"\g<1>\n| 仿写口吻 | {voice} |", text, count=1)
            if count != 1:
                raise SystemExit("样本文件找不到来源类型行")
        changes = {
            p["review"]: review,
            p["dna"]: dna_lib.render_ledger(head, prefs),
            p["index"]: index,
            artifact: artifact_after,
            p["log"]: dna_lib.append_log(p["log"].read_text(encoding="utf-8"), "仿写", name,
                                         f"补标仿写口吻 {voice}，复盘 {voided} 条退出统计", voided),
        }
        archive_after = dna_lib.render_ledger(archive_head, archived)
        if archive_after != p["archive"].read_text(encoding="utf-8"):
            changes[p["archive"]] = archive_after
        dna_lib.write_all(changes)
    print(f"marked: {name}; voice: {voice}; reviews_out: {voided}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
