#!/usr/bin/env python3
"""Safely persist a finalized writing sample and its review.

The agent writes content and the semantic review tags; this script owns
validation, source detection, anchor checks, versioning, locking, rollback,
state counters, diff-log, review-log and preference statistics.
It never adds, deletes, or rewrites a preference.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import dna_lib  # noqa: E402
from dna_lib import atomic_write as _atomic_write, lock as _lock  # noqa: E402,F401  (reject_sample 复用)

DRY_RUN = os.getenv("WRITING_STYLE_DRY_RUN", "false").lower() in {"true", "1", "yes"}
VALID_EVENTS = {"创作", "修订"}
VALID_SCOPES = {"正文", "发布包"}
AGENT_TAGS = {"守住了", "印证", "不相关"}
NO_DRAFT_SOURCES = {"A": "user_original", "C": "reference_text", "D": "mixed"}


def _slug(value: str) -> str:
    value = re.sub(r"\s+", "-", value.strip())
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "", value)
    value = re.sub(r"-+", "-", value).strip("-_")
    if not value:
        raise ValueError("identity清理后为空")
    return value[:60]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _loose(text: str) -> str:
    return re.sub(r"[\W_]+", "", text)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("checked_at必须是ISO 8601时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("checked_at必须包含时区")
    return parsed.astimezone(timezone.utc)


def _sentence_stats(text: str) -> tuple[int, float]:
    parts = [
        re.sub(r"\s+", "", part)
        for part in re.split(r"[。！？!?；;]+|\n+", text)
        if re.sub(r"\s+", "", part)
    ]
    if not parts:
        return 0, 0.0
    lengths = [len(part) for part in parts]
    mean = sum(lengths) / len(lengths)
    variance = sum((length - mean) ** 2 for length in lengths) / len(lengths)
    return len(lengths), round(math.sqrt(variance), 1)


def _resolve(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    """合并待定区初稿，推出来源类型。返回本次存档用到的派生信息。"""
    draft = None
    versions = []
    if data.get("draft_id"):
        path = dna_lib.paths(root)["drafts"] / f"{data['draft_id']}.json"
        if not path.is_file():
            raise ValueError(f"待定区没有这份初稿: {data['draft_id']}")
        draft = json.loads(path.read_text(encoding="utf-8"))
        if draft.get("finalized_as") and data["event_type"] == "创作":
            raise ValueError(f"这份初稿已定稿为 {draft['finalized_as']}，再改请用修订")
        first, input_source, material = draft["first_generated"], draft["input_source"], draft["original_material"]
        anchors = draft.get("anchors", [])
        versions = draft.get("agent_versions", [])
    else:
        first = data.get("first_generated")
        input_source = data.get("input_source")
        material = data.get("original_material")
        anchors = []
        if input_source not in {"A", "B", "C", "D"}:
            raise ValueError("没有draft_id时必须提供input_source（A/B/C/D）")
        if first is not None and (not isinstance(first, str) or not first.strip()):
            raise ValueError("first_generated只能是非空字符串或null")
        if first is None and input_source == "B":
            raise ValueError("B类资料由Agent发挥，必须有Agent初稿（draft_id或first_generated）")
    if not isinstance(material, str) or not material.strip():
        raise ValueError("original_material不能为空")
    final = data["confirmed_final"]
    latest = versions[-1]["text"] if versions else first
    author_lines = [line for version in versions for line in version.get("author_lines", [])]
    agent_texts = ([first] if first is not None else []) + [version["text"] for version in versions]
    if first is not None:
        # 助手多轮重写仍是助手稿，只有作者亲改或口述才提供作者证据。
        source_type = "agent_draft" if _norm(latest) == _norm(final) and not author_lines else "user_revision"
    else:
        source_type = NO_DRAFT_SOURCES[input_source]
    return {
        "draft": draft,
        "first": first,
        "latest": latest,
        "agent_texts": agent_texts,
        "author_lines": author_lines,
        "versions": versions,
        "input_source": input_source,
        "material": material,
        "anchors": anchors,
        "source_type": source_type,
    }


def _review(root: Path, data: dict[str, Any], info: dict[str, Any]) -> list[dict[str, str]]:
    """锚点句由脚本判定；守住了/印证/不相关由 Agent 提供并在这里校验。"""
    _, prefs, _, _ = dna_lib.load_ledgers(root)
    final = _loose(data["confirmed_final"])
    rows = []
    for anchor in info["anchors"]:
        kept = _loose(anchor["text"]) in final
        rows.append({"pid": anchor["id"], "tag": "用上了" if kept else "被改回", "basis": anchor["text"], "by": "脚本"})
    anchor_ids = {row["pid"] for row in rows}
    overrides = data.get("anchor_overrides", [])
    if not isinstance(overrides, list):
        raise ValueError("anchor_overrides必须是列表")
    overridden = set()
    for item in overrides:
        if not isinstance(item, dict):
            raise ValueError("anchor_overrides每项必须是对象")
        pid, tag, reason = item.get("id"), item.get("tag"), item.get("reason")
        if pid not in anchor_ids:
            raise ValueError(f"纠正的偏好不属于本篇锚点: {pid}")
        if pid in overridden:
            raise ValueError(f"锚点纠正重复: {pid}")
        row = next(row for row in rows if row["pid"] == pid)
        if tag not in {*dna_lib.SCRIPT_TAGS, "不相关"} or tag == row["tag"]:
            raise ValueError(f"{pid}：纠正标签必须为用上了/被改回/不相关，且与脚本原判不同")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{pid}：作者纠正必须写明原因")
        overridden.add(pid)
        row["basis"] += f"（脚本原判 {row['tag']}，作者改为 {tag}：{reason.strip()}）"
        row.update(tag=tag, by="作者纠正")
    seen = set()
    review = data.get("review", [])
    if not isinstance(review, list):
        raise ValueError("review必须是列表")
    for item in review:
        pid, tag, basis = item.get("id"), item.get("tag"), (item.get("basis") or "").strip()
        pref = dna_lib.find(prefs, pid or "")
        if not pref:
            raise ValueError(f"复盘偏好不存在: {pid}")
        if tag in dna_lib.SCRIPT_TAGS:
            raise ValueError(f"{pid}：用上了/被改回只能由脚本按锚点句判定")
        if tag not in AGENT_TAGS:
            raise ValueError(f"{pid}标签无效: {tag}")
        if pid in seen or pid in anchor_ids:
            raise ValueError(f"复盘偏好重复: {pid}")
        seen.add(pid)
        if tag == "守住了":
            if info["first"] is None or pref.fields["状态"] != "生效":
                raise ValueError(f"{pid}：守住了只用于Agent初稿里的生效偏好")
            if not basis:
                raise ValueError(f"{pid}：守住了要写依据")
        if tag == "印证":
            if info["source_type"] in {"agent_draft", "reference_text"}:
                raise ValueError(f"{pid}：{info['source_type']}的文字不是作者写的，不能印证")
            if len(_norm(basis)) < 4 or _loose(basis) not in final:
                raise ValueError(f"{pid}：印证的依据必须是终稿里的原句（至少4个字）")
            in_agent = any(_loose(basis) in _loose(text) for text in info["agent_texts"])
            dictated = any(_loose(basis) in _loose(line) for line in info["author_lines"])
            if in_agent and not dictated:
                raise ValueError(f"{pid}：印证的依据必须是作者改动后的句子，不能是Agent稿原句")
        rows.append({"pid": pid, "tag": tag, "basis": basis, "by": "Agent"})
    return rows


def _diff_blocks(first: str | None, final: str) -> list[str]:
    if first is None:
        return []
    a, b = first.splitlines(), final.splitlines()
    blocks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag != "equal":
            blocks.append(f"原：{' '.join(a[i1:i2]).strip() or '（无）'}\n改：{' '.join(b[j1:j2]).strip() or '（无）'}")
    return blocks


def _validate(root: Path, data: dict[str, Any]) -> None:
    required = {
        "topic", "identity", "event_type", "finalized", "scope", "scene", "audience",
        "source_summary", "confirmed_final", "time_sensitive", "sources", "scores",
    }
    missing = sorted(key for key in required if key not in data)
    if missing:
        raise ValueError(f"payload缺字段: {', '.join(missing)}")
    if "preference_hits" in data or "source_type" in data:
        raise ValueError("payload已改版：去掉preference_hits和source_type，改用draft_id/input_source和review")
    if data["finalized"] is not True:
        raise ValueError("正文未明确进入FINALIZED状态")
    if data["event_type"] not in VALID_EVENTS:
        raise ValueError("event_type只能是创作或修订")
    if data["scope"] not in VALID_SCOPES:
        raise ValueError("scope只能是正文或发布包")
    for key in ("topic", "identity", "scene", "audience", "source_summary", "confirmed_final"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key}不能为空")
    if any(char in data["identity"] for char in "|\n\r"):
        raise ValueError("identity不能包含竖线或换行（会破坏样本索引表格）")

    sources = data["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources必须是非空列表")
    if any(not isinstance(source, str) or not source.strip() for source in sources):
        raise ValueError("sources每项必须是非空字符串")

    scores = data["scores"]
    if not isinstance(scores, dict) or set(scores) != {"natural", "faithful", "publishable", "reason"}:
        raise ValueError("scores必须包含natural、faithful、publishable、reason")
    for key in ("natural", "faithful", "publishable"):
        if isinstance(scores[key], bool) or not isinstance(scores[key], int) or not 1 <= scores[key] <= 5:
            raise ValueError(f"scores.{key}必须是1到5的整数")
    if not isinstance(scores["reason"], str) or not scores["reason"].strip() or len(scores["reason"].strip()) > 30:
        raise ValueError("scores.reason必须是1到30字")

    if not isinstance(data["time_sensitive"], bool):
        raise ValueError("time_sensitive必须是布尔值")
    checked_at = data.get("checked_at")
    if checked_at is not None:
        if not isinstance(checked_at, str) or not checked_at.strip():
            raise ValueError("checked_at必须是ISO 8601时间或null")
        if (datetime.now(timezone.utc) - _parse_time(checked_at)).total_seconds() < -300:
            raise ValueError("checked_at不能晚于当前时间")
    elif data["time_sensitive"]:
        raise ValueError("时效内容必须提供checked_at")

    if data["scope"] == "发布包":
        if data.get("publication_assets_checked") is not True:
            raise ValueError("发布包中的配图和附件尚未完成事实检查")
        for key in ("platform", "title", "asset_review"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"发布包必须提供{key}")
        asset_paths = data.get("asset_paths")
        if not isinstance(asset_paths, list) or not asset_paths:
            raise ValueError("发布包必须提供asset_paths")
        for raw_path in asset_paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("asset_paths每项必须是非空字符串")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = root / path
            if not path.is_file():
                raise ValueError(f"发布包附件不存在: {raw_path}")

    if data["event_type"] == "修订":
        revision_of = data.get("revision_of")
        if not revision_of:
            raise ValueError("修订必须提供revision_of")
        candidate = (root / "assets" / revision_of).resolve()
        if candidate.parent != (root / "assets").resolve() or not candidate.is_file():
            raise ValueError("revision_of不是assets下的现有文件")


def _next_version(assets: Path, identity: str) -> int:
    pattern = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(identity)}-v(\d+)\.md$")
    versions = [int(m.group(1)) for p in assets.glob(f"*-{identity}-v*.md") if (m := pattern.match(p.name))]
    return max(versions, default=0) + 1


def _validate_version_intent(data: dict[str, Any], version: int, identity: str) -> None:
    if data["event_type"] == "创作" and version > 1:
        raise ValueError("同一主题已有定稿；后续修改必须使用修订事件")
    if data["event_type"] == "修订":
        match = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)-v\d+\.md$", data["revision_of"])
        if not match or match.group(1) != identity:
            raise ValueError("修订主题必须与revision_of一致")


STATE_FIELDS = ("polish_count_total", "dna_active_count", "dna_archive_count")


def _state_values(text: str) -> dict[str, int]:
    result = {}
    for field in STATE_FIELDS:
        match = re.search(rf"(?m)^{field}:\s*(\d+)\s*$", text)
        table_match = re.search(rf"(?m)^\|\s*`{re.escape(field)}`\s*\|[^\n]*?\|\s*(\d+)\s*\|$", text)
        if not match or not table_match:
            raise ValueError(f"state.md缺少{field}")
        if int(table_match.group(1)) != int(match.group(1)):
            raise ValueError(f"state.md中{field}的YAML值与表格值不一致")
        result[field] = int(match.group(1))
    return result


def _build_state(state: str, prefs: int, archived: int, event_type: str, date_str: str) -> str:
    values = _state_values(state)
    if event_type == "创作":
        values["polish_count_total"] += 1
    values["dna_active_count"], values["dna_archive_count"] = prefs, archived
    for field, value in values.items():
        state = dna_lib.replace_state_number(state, field, value)
    state, count = re.subn(r"(?m)^(last_updated:\s*).*$", rf"\g<1>{date_str}", state, count=1)
    if count != 1:
        raise ValueError("state.md缺少last_updated")
    return state


def _table(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _summary(rows: list[dict[str, str]]) -> str:
    return ", ".join(f"{row['pid']} {row['tag']}" for row in rows) or "—"


def _build_artifact(data: dict[str, Any], info: dict[str, Any], rows: list[dict[str, str]], now: datetime, version: int) -> tuple[str, int, float]:
    sentence_count, stddev = _sentence_stats(data["confirmed_final"])
    first = info["first"].strip() if info["first"] is not None else "不适用：没有 Agent 初稿"
    version_detail = ""
    if info["versions"]:
        author_lines = "\n".join(f"- {line}" for line in info["author_lines"]) or "- —"
        version_detail = f"\n\n---\n\n## Agent 最后一版\n\n{info['latest'].strip()}\n\n## 作者口述句\n\n{author_lines}"
    sources = "\n".join(f"- {source.strip()}" for source in data["sources"])
    scores = data["scores"]
    package_assets = "\n".join(f"- {path}" for path in data.get("asset_paths") or []) or "- —"
    content = f"""# {data['topic']}

## 元数据

| 字段 | 值 |
|---|---|
| 资产格式 | v3 |
| 资产状态 | active |
| 身份键 | {_table(data['identity'])} |
| 定稿时间 | {now.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')} |
| 版本 | v{version} |
| 事件类型 | {_table(data['event_type'])} |
| 定稿范围 | {_table(data['scope'])} |
| 场景 | {_table(data['scene'])} |
| 目标读者 | {_table(data['audience'])} |
| 素材来源 | {_table(data['source_summary'])} |
| 输入类型 | {_table(info['input_source'])} |
| 来源类型 | {_table(info['source_type'])} |
| 待定区初稿 | {_table(data.get('draft_id') or '—')} |
| 复盘 | {_table(_summary(rows))} |
| 句子数 | {sentence_count} |
| 句长标准差 | {stddev} |
| 时效数据检查 | {_table(data.get('checked_at') or '不适用')} |
| 修订来源 | {_table(data.get('revision_of') or '—')} |
| 发布平台 | {_table(data.get('platform') or '—')} |
| 发布标题 | {_table(data.get('title') or '—')} |
| 附件检查 | {_table(data.get('asset_review') or '—')} |

---

## 定稿自评

| 维度 | 分数 |
|---|---|
| 自然 | {scores['natural']}/5 |
| 保真 | {scores['faithful']}/5 |
| 可直接发 | {scores['publishable']}/5 |
| 理由 | {_table(scores['reason'].strip())} |

---

## 原始素材

{info['material'].strip()}

---

## 首次生成版本

{first}{version_detail}

---

## 用户确认终稿

{data['confirmed_final'].strip()}

---

## 发布包附件

{package_assets}

---

## 资料来源

{sources}
"""
    return content, sentence_count, stddev


def _recover(journal: Path) -> bool:
    if not journal.exists():
        return False
    data = json.loads(journal.read_text(encoding="utf-8"))
    root = journal.parent.parent.resolve()

    def target(raw_path: str) -> Path:
        path = Path(raw_path)
        path = (path if path.is_absolute() else root / path).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError("恢复记录中的目标超出当前技能根目录，已保留记录")
        return path

    # 复制过来的旧记录可能仍指向原目录；先校验全部目标，再恢复任何文件。
    before = [(target(raw_path), base64.b64decode(encoded, validate=True))
              for raw_path, encoded in data["before"].items()]
    created = [target(raw_path) for raw_path in data["created"]]
    if data.get("phase") != "committed":
        for path, content in before:
            _atomic_write(path, content)
        for path in created:
            path.unlink(missing_ok=True)
    journal.unlink(missing_ok=True)
    return True


def finalize(
    root: Path | str,
    data: dict[str, Any],
    *,
    dry_run: bool = False,
    _test_fail_after: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    p = dna_lib.paths(root)
    assets = root / "assets"
    dry_run = dry_run or DRY_RUN
    with _lock(root):
        journal = assets / ".finalize-journal.json"
        if dry_run:
            # 预览也要先等账本完整，避免展示中断时写了一半的内容。
            dna_lib.require_no_pending_finalize(root)
        else:
            _recover(journal)
        for key in ("state", "log", "dna", "archive", "index", "review"):
            if not p[key].exists():
                raise FileNotFoundError(p[key])
        _validate(root, data)
        now = datetime.now().astimezone()
        date_str = now.strftime("%Y-%m-%d")
        identity = _slug(data["identity"])

        if dry_run:
            info = _resolve(root, data)
            rows = _review(root, data, info)
            review_table = [f"{row['pid']} | {row['tag']} | {row['basis'] or '—'} | {row['by']}" for row in rows]
            version = _next_version(assets, identity)
            _validate_version_intent(data, version, identity)
            artifact, sentence_count, stddev = _build_artifact(data, info, rows, now, version)
            return {
                "dry_run": True,
                "filename": f"{date_str}-{identity}-v{version}.md",
                "version": version,
                "source_type": info["source_type"],
                "style_eligible": info["source_type"] in dna_lib.ELIGIBLE_SOURCES,
                "review_table": review_table,
                "diff_blocks": _diff_blocks(info["latest"], data["confirmed_final"]),
                "sentence_count": sentence_count,
                "sentence_stddev": stddev,
                "preview_bytes": len(artifact.encode("utf-8")),
            }

        # 与 revise/note 共用锁，存档时保留刚完成的改稿和交接。
        info = _resolve(root, data)
        rows = _review(root, data, info)
        review_table = [f"{row['pid']} | {row['tag']} | {row['basis'] or '—'} | {row['by']}" for row in rows]
        version = _next_version(assets, identity)
        _validate_version_intent(data, version, identity)
        filename = f"{date_str}-{identity}-v{version}.md"
        artifact_path = assets / filename
        if artifact_path.exists():
            raise FileExistsError(artifact_path)

        stamp = now.strftime("%Y-%m-%d %H:%M")
        review_text = p["review"].read_text(encoding="utf-8")
        if data["event_type"] == "修订":
            # 同一篇文章只保留最新版本的复盘，避免重复计数
            review_text, _ = dna_lib.set_review_state(review_text, data["revision_of"], "有效", "被替代")
        new_rows = [dna_lib.review_row(stamp, filename, row["pid"], row["tag"], row["basis"]) for row in rows]
        review_after = review_text.rstrip() + ("\n" + "\n".join(new_rows) if new_rows else "") + "\n"

        head, prefs, archive_head, archived = dna_lib.load_ledgers(root)
        reviews = dna_lib.read_reviews_text(review_after)
        dna_lib.recompute(prefs, reviews)
        dna_lib.recompute(archived, reviews)
        dna_after = dna_lib.render_ledger(head, prefs)
        archive_after = dna_lib.render_ledger(archive_head, archived)

        log_text = p["log"].read_text(encoding="utf-8")
        log_row = f"| {stamp} | {data['event_type']} | {filename} | {filename} | {_summary(rows)} | {len(rows)} |"
        if log_row in log_text:
            raise ValueError("diff-log中已存在相同记录")
        log_after = log_text.rstrip() + "\n" + log_row + "\n"

        index_text = p["index"].read_text(encoding="utf-8")
        superseded = {}
        if data["event_type"] == "修订":
            # 旧版本退出学习，避免同一篇文章被当成两份证据
            old = data["revision_of"]
            index = dna_lib.read_index(p["index"])
            old_row = index.get(old)
            if not old_row or old_row["status"] != "active":
                current = [row["file"] for row in index.values()
                           if row["identity"] == data["identity"] and row["status"] == "active"]
                raise ValueError(f"revision_of不是active样本；当前active: {', '.join(current) or '无'}")
            index_text, count = re.subn(
                rf"(?m)^(\| [^|]+ \| {re.escape(old)} \| [^|]+ \| )[^|]+ \| active( \|)$", r"\g<1>否 | superseded\g<2>", index_text)
            if count != 1:
                raise ValueError(f"旧版本索引必须替代1行，实际{count}行")
            old_path = assets / old
            old_text, count = re.subn(r"(?m)^\| 资产状态 \| active \|$", "| 资产状态 | superseded |", old_path.read_text(encoding="utf-8"), count=1)
            if count:
                superseded[old_path] = old_text
        eligible = "是" if info["source_type"] in dna_lib.ELIGIBLE_SOURCES else "否"
        index_after = index_text.rstrip() + f"\n| {data['identity']} | {filename} | {info['source_type']} | {eligible} | active |\n"

        state_after = _build_state(p["state"].read_text(encoding="utf-8"), len(prefs), len(archived), data["event_type"], date_str)
        artifact, sentence_count, stddev = _build_artifact(data, info, rows, now, version)

        changes = {
            artifact_path: artifact,
            p["log"]: log_after,
            p["index"]: index_after,
            p["review"]: review_after,
            p["dna"]: dna_after,
            **({p["archive"]: archive_after} if archive_after != p["archive"].read_text(encoding="utf-8") else {}),
            p["state"]: state_after,
            **superseded,
        }
        if info["draft"] is not None:
            draft = dict(info["draft"])
            draft["finalized_as"] = filename
            changes[p["drafts"] / f"{data['draft_id']}.json"] = json.dumps(draft, ensure_ascii=False, indent=2)

        before = {str(path): base64.b64encode(path.read_bytes()).decode() for path in changes if path.exists()}
        journal_data = {"phase": "applying", "before": before, "created": [str(artifact_path)]}
        _atomic_write(journal, json.dumps(journal_data, ensure_ascii=False).encode("utf-8"))
        try:
            for path, content in changes.items():
                _atomic_write(path, content.encode("utf-8"))
                if _test_fail_after and path.name.startswith(_test_fail_after):
                    raise RuntimeError(f"模拟失败: {_test_fail_after}")
            journal_data["phase"] = "committed"
            _atomic_write(journal, json.dumps(journal_data, ensure_ascii=False).encode("utf-8"))
            journal.unlink(missing_ok=True)
        except Exception:
            for raw_path, encoded in before.items():
                _atomic_write(Path(raw_path), base64.b64decode(encoded))
            artifact_path.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            raise

    return {
        "dry_run": False,
        "filename": filename,
        "version": version,
        "event_type": data["event_type"],
        "source_type": info["source_type"],
        "review_table": review_table,
        "sentence_count": sentence_count,
        "sentence_stddev": stddev,
        "state_incremented": data["event_type"] == "创作",
        "suggestions": dna_lib.suggest(root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="保存已确认的写作终稿并记录复盘")
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[1])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--payload", type=Path, help="UTF-8 JSON payload")
    action.add_argument("--recover", action="store_true", help="恢复未完成的定稿存档")
    parser.add_argument("--dry-run", action="store_true", help="只校验并打印复盘表，不写文件")
    args = parser.parse_args()
    if args.recover:
        if args.dry_run:
            parser.error("--recover不能与--dry-run同时使用")
        root = args.root.resolve()
        with _lock(root):
            result = {"recovered": _recover(root / "assets" / ".finalize-journal.json")}
    else:
        data = json.loads(args.payload.read_text(encoding="utf-8"))
        result = finalize(args.root, data, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
