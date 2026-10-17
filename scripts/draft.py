#!/usr/bin/env python3
"""待定区：保存 Agent 初稿、锚点句和交接卡。

初稿不算定稿、不计数。定稿时 finalize.py 用它判断来源和锚点结果。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dna_lib  # noqa: E402

INPUT_SOURCES = {"A", "B", "C", "D"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def validate(root: Path, data: dict) -> None:
    for key in ("topic", "input_source", "scene", "original_material", "first_generated"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key}不能为空")
    if data["input_source"] not in INPUT_SOURCES:
        raise ValueError("input_source只能是A、B、C、D")
    anchors = data.get("anchors", [])
    if not isinstance(anchors, list):
        raise ValueError("anchors必须是列表")
    _, prefs, _, _ = dna_lib.load_ledgers(root)
    seen = set()
    for anchor in anchors:
        pid, text = anchor.get("id"), anchor.get("text")
        pref = dna_lib.find(prefs, pid or "")
        if not pref or pref.fields.get("状态") != "生效":
            raise ValueError(f"锚点偏好不存在或未生效: {pid}")
        if pid in seen:
            raise ValueError(f"锚点偏好重复: {pid}")
        seen.add(pid)
        if not isinstance(text, str) or len(_norm(text)) < 4 or _norm(text) not in _norm(data["first_generated"]):
            raise ValueError(f"{pid}的锚点句必须是初稿里的原句（至少4个字）")
    handoff = data.get("handoff", {})
    if not isinstance(handoff, dict):
        raise ValueError("handoff必须是对象")


def save(root: Path, data: dict) -> dict:
    validate(root, data)
    now = datetime.now().astimezone()
    slug = re.sub(r"[^0-9A-Za-z一-鿿_-]", "", data["topic"])[:30] or "draft"
    draft_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}-{slug}"
    record = {
        "draft_id": draft_id,
        "created_at": now.isoformat(),
        "topic": data["topic"],
        "input_source": data["input_source"],
        "scene": data["scene"],
        "original_material": data["original_material"],
        "first_generated": data["first_generated"],
        "agent_versions": [],
        "anchors": data.get("anchors", []),
        "handoff": data.get("handoff", {}),
        "finalized_as": None,
    }
    path = dna_lib.paths(root)["drafts"] / f"{draft_id}.json"
    if path.exists():
        raise FileExistsError(path)
    dna_lib.atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"draft_id": draft_id, "path": str(path)}


def load(root: Path, draft_id: str) -> dict:
    path = dna_lib.paths(root)["drafts"] / f"{draft_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"待定区没有这份初稿: {draft_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def note(root: Path, draft_id: str, updates: dict) -> dict:
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        # 等到拿锁后再读，避免把刚写好的定稿标记覆盖成旧值。
        record = load(root, draft_id)
        record["handoff"].update({k: v for k, v in updates.items() if v})
        record["handoff"]["updated_at"] = datetime.now().astimezone().isoformat()
        path = dna_lib.paths(root)["drafts"] / f"{draft_id}.json"
        dna_lib.atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
    return record["handoff"]


def revise(root: Path, draft_id: str, text: str, author_lines: list[str] | None = None) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("新版正文不能为空")
    author_lines = [] if author_lines is None else author_lines
    if not isinstance(author_lines, list):
        raise ValueError("author_lines必须是列表")
    for line in author_lines:
        if not isinstance(line, str) or len(_norm(line)) < 4 or _norm(line) not in _norm(text):
            raise ValueError("作者口述句必须在新版正文里（至少4个字）")
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        record = load(root, draft_id)
        if record.get("finalized_as"):
            raise ValueError(f"这份初稿已定稿为 {record['finalized_as']}，不能继续revise")
        version = {"text": text, "at": datetime.now().astimezone().isoformat(), "author_lines": author_lines}
        record.setdefault("agent_versions", []).append(version)
        path = dna_lib.paths(root)["drafts"] / f"{draft_id}.json"
        dna_lib.atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
    return version


def expire(root: Path, days: int) -> list[str]:
    """超期未定稿的初稿挪到 drafts/_expired/，不删除。"""
    limit = datetime.now().astimezone().timestamp() - days * 86400
    moved = []
    folder = dna_lib.paths(root)["drafts"]
    with dna_lib.lock(root):
        dna_lib.require_no_pending_finalize(root)
        for draft in dna_lib.load_drafts(root):
            if draft.get("finalized_as") or datetime.fromisoformat(draft["created_at"]).timestamp() >= limit:
                continue
            source = Path(draft["_path"])
            target = folder / "_expired" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            moved.append(draft["draft_id"])
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="待定区：初稿、锚点句、交接卡")
    parser.add_argument("action", choices=["save", "show", "note", "revise", "expire"])
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--file", type=Path, help="save：初稿 JSON；revise：新版正文")
    parser.add_argument("--draft", help="show/note/revise：初稿编号")
    parser.add_argument("--author-line", action="append", default=[], help="revise：作者口述原句，可重复")
    parser.add_argument("--progress", help="note：写到哪了")
    parser.add_argument("--requirements", help="note：作者提过的要求")
    parser.add_argument("--todo", help="note：还差什么")
    parser.add_argument("--days", type=int, default=dna_lib.DRAFT_EXPIRE_DAYS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "save":
        result = save(root, json.loads(args.file.read_text(encoding="utf-8")))
    elif args.action == "show":
        if args.draft:
            result = load(root, args.draft)
        else:
            result = [
                {"draft_id": d["draft_id"], "topic": d["topic"], "created_at": d["created_at"], "handoff": d.get("handoff", {})}
                for d in dna_lib.load_drafts(root) if not d.get("finalized_as")
            ]
    elif args.action == "note":
        result = note(root, args.draft, {"progress": args.progress, "requirements": args.requirements, "todo": args.todo})
    elif args.action == "revise":
        if not args.draft or args.file is None:
            parser.error("revise必须提供--draft和--file")
        result = revise(root, args.draft, args.file.read_text(encoding="utf-8"), args.author_line)
    else:
        result = {"expired": expire(root, args.days)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
