#!/usr/bin/env python3
"""Audit persisted writing assets without changing them."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dna_lib  # noqa: E402

VALID_STATUSES = {"active", "rejected", "superseded", "legacy_unclassified"}


def state_value_pair(text: str, key: str) -> tuple[str, str] | None:
    yaml = re.search(rf"(?m)^{re.escape(key)}:\s*([^\n]+)", text)
    table = re.search(rf"(?m)^\|\s*`{re.escape(key)}`\s*\|[^\n]*?\|\s*([^|]+?)\s*\|$", text)
    if not yaml or not table:
        return None
    return yaml.group(1).strip(), table.group(1).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    p = dna_lib.paths(root)
    assets = root / "assets"
    state = p["state"].read_text(encoding="utf-8")
    findings = []
    if (assets / ".finalize-journal.json").exists():
        findings.append("有未完成的定稿存档记录，先运行 finalize.py --recover")

    # 1. 状态文件两处数值一致
    for key in ("polish_count_total", "dna_active_count", "dna_archive_count"):
        pair = state_value_pair(state, key)
        if pair is None:
            findings.append(f"state.md缺少{key}的YAML或表格值")
        elif pair[0] != pair[1]:
            findings.append(f"state.md中{key}的YAML值与表格值不一致")

    # 2. 偏好账本格式、编号唯一、统计与复盘记录一致
    _, prefs, _, archived = dna_lib.load_ledgers(root)
    for pref in prefs:
        findings += dna_lib.validate_pref(pref, archived=False)
    for pref in archived:
        findings += dna_lib.validate_pref(pref, archived=True)
    ids = [pref.pid for pref in prefs + archived]
    if len(ids) != len(set(ids)):
        findings.append("偏好编号重复")
    review_text = p["review"].read_text(encoding="utf-8") if p["review"].exists() else ""
    reviews = dna_lib.read_reviews_text(review_text)
    expected = {pref.pid: pref.fields.get("统计") for pref in prefs + archived}
    for pref in dna_lib.recompute([dna_lib.Pref(x.pid, dict(x.fields)) for x in prefs + archived], reviews):
        if pref.fields["统计"] != expected[pref.pid]:
            findings.append(f"{pref.pid}统计与复盘记录不一致")
    if str(len(prefs)) != (state_value_pair(state, "dna_active_count") or ("",))[0]:
        findings.append("state.dna_active_count与dna.md不一致")
    if str(len(archived)) != (state_value_pair(state, "dna_archive_count") or ("",))[0]:
        findings.append("state.dna_archive_count与dna-archive.md不一致")

    # 3. 复盘记录指向存在的样本和偏好
    for line in review_text.splitlines():
        parts = [item.strip() for item in line.strip().strip("|").split("|")]
        if re.match(r"^\d{4}-\d{2}-\d{2}", parts[0]) and len(parts) != 6:
            findings.append(f"复盘记录有格式错误的行：{line[:40]}")
    for row in reviews:
        if not (assets / row["sample"]).is_file():
            findings.append(f"复盘记录指向不存在的样本: {row['sample']}")
        if row["pid"] not in ids:
            findings.append(f"复盘记录指向不存在的偏好: {row['pid']}")
        if row["tag"] not in dna_lib.TAGS or row["state"] not in dna_lib.REVIEW_STATES:
            findings.append(f"复盘记录标签或状态无效: {row['sample']} {row['pid']}")

    # 4. 样本索引与样本文件一致
    index = dna_lib.read_index(p["index"])
    active_by_identity = {}
    # 按原始行计数，避免同一个文件重复登记后被字典合并。
    for line in p["index"].read_text(encoding="utf-8").splitlines():
        parts = [item.strip() for item in line.strip().strip("|").split("|")]
        if line.startswith("|") and ".md" in line and parts[0] not in {"身份键", "---"} and len(parts) != 5:
            findings.append(f"样本索引有格式错误的行：{line[:40]}")
        if len(parts) == 5 and parts[1].endswith(".md") and parts[4] == "active":
            active_by_identity.setdefault(parts[0], []).append(parts[1])
    for filename, row in index.items():
        if not (assets / filename).is_file():
            findings.append(f"样本索引指向不存在文件: {filename}")
        if row["source_type"] not in dna_lib.VALID_SOURCE_TYPES:
            findings.append(f"样本索引包含无效来源类型: {filename}")
        if row["status"] not in VALID_STATUSES:
            findings.append(f"样本索引包含无效资产状态: {filename}")
        if row["status"] != "active" and row["eligible"] == "是":
            findings.append(f"非active资产不能标记为风格资格: {filename}")
        if row["source_type"] == "user_original" and row["status"] == "active" and row["eligible"] != "是":
            findings.append(f"用户原稿风格资格错误: {filename}")
    for identity, filenames in active_by_identity.items():
        if len(filenames) > 1:
            findings.append(f"同一身份键有多个active样本: {identity} {', '.join(filenames)}")
    legacy = []
    for path in assets.glob("*-v*.md"):
        text = path.read_text(encoding="utf-8")
        meta = {key: (m.group(1).strip() if (m := re.search(rf"(?m)^\| {key} \| ([^|]+?) \|$", text)) else None)
                for key in ("来源类型", "资产状态", "身份键")}
        if meta["来源类型"] not in dna_lib.VALID_SOURCE_TYPES or not meta["资产状态"] or not meta["身份键"]:
            legacy.append(path.name)
            continue
        if meta["资产状态"] not in VALID_STATUSES:
            findings.append(f"资产状态无效: {path.name}")
        row = index.get(path.name)
        if meta["资产状态"] == "active" and not row:
            findings.append(f"active资产未登记style-index: {path.name}")
        elif row and (row["identity"] != meta["身份键"] or row["source_type"] != meta["来源类型"] or row["status"] != meta["资产状态"]):
            findings.append(f"资产元数据与style-index不一致: {path.name}")

    # 5. 待定区文件能读、字段齐全
    for draft_path in sorted(p["drafts"].glob("*.json")) if p["drafts"].exists() else []:
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            for key in ("draft_id", "created_at", "input_source", "first_generated", "anchors"):
                if key not in draft:
                    findings.append(f"待定区初稿缺字段{key}: {draft_path.name}")
            if draft.get("finalized_as") and not (assets / draft["finalized_as"]).is_file():
                findings.append(f"待定区初稿指向不存在的定稿: {draft_path.name}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            findings.append(f"待定区初稿无法读取: {draft_path.name}")

    result = {"status": "FAIL" if findings else "PASS", "indexed_samples": len(index), "legacy_assets": sorted(legacy), "findings": findings}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
