#!/usr/bin/env python3
"""偏好账本、复盘记录、样本索引和待定区的共享读写工具。

所有改动偏好账本的脚本（finalize / dna_update / reject_sample）都经过这里，
保证格式、统计和提示规则只有一份实现。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

FIELDS = ("写法", "类型", "适用场景", "状态", "来源", "证据", "统计", "创建于", "最近相关", "状态日期")
TYPES = {"说话", "判断"}
SCENES = {"全局", "项目争议", "市场播报", "产品评测", "游戏体验", "个人吐槽"}
LEDGER_STATUSES = {"候选", "生效", "休眠"}  # reference/dna.md
ARCHIVE_STATUSES = {"淘汰"}  # reference/dna-archive.md
TAGS = ("用上了", "守住了", "被改回", "印证", "不相关")
SCRIPT_TAGS = {"用上了", "被改回"}  # 助手不能自填；作者纠正走 anchor_overrides
ELIGIBLE_SOURCES = {"user_original", "user_revision"}
VALID_SOURCE_TYPES = {"user_original", "agent_draft", "user_revision", "mixed", "reference_text"}
REVIEW_STATES = {"有效", "作废", "被替代", "仿写"}

# 门槛（2026-10-17 作者确认，见 PROJECT.md §2.3）
EVIDENCE_NEEDED = 2
RECENT_WINDOW = 5
REVERTS_TO_SLEEP = 2
CONFIRMS_TO_WAKE = 2
DRAFT_EXPIRE_DAYS = 30

HEADER_RE = re.compile(r"(?m)^### 偏好 (#\d{3,})\s*$")
STATS_RE = re.compile(r"^用上 (\d+) / 守住 (\d+) / 被改回 (\d+) / 印证 (\d+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 禁止命令式禁止和带引号的反面例句；「，不为整齐……」这类说明边界的半句允许（2026-10-17 盲选后放宽）
NEGATIVE_RE = re.compile(r"不要|禁止|不得|不准|别写|别用|别把|[“”\"「」『』]")
REVIEW_HEADER = "| 时间 | 样本文件 | 偏好 | 标签 | 依据 | 状态 |"


@dataclass
class Pref:
    pid: str
    fields: dict


def paths(root: Path) -> dict[str, Path]:
    return {
        "dna": root / "reference" / "dna.md",
        "archive": root / "reference" / "dna-archive.md",
        "benchmark": root / "evals" / "benchmark.md",
        "state": root / "assets" / "state.md",
        "log": root / "assets" / "diff-log.md",
        "index": root / "assets" / "style-index.md",
        "review": root / "assets" / "review-log.md",
        "drafts": root / "drafts",
        "backups": root / "backups",
    }


def check_voice(value) -> str | None:
    """仿写口吻名；None 表示不是仿写稿。不能含竖线或换行，以免破坏表格。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 30 or any(c in value for c in "|\n\r"):
        raise ValueError("voice必须是1到30字的口吻名，不能含竖线或换行")
    return value.strip()


def artifact_voice(text: str) -> str | None:
    match = re.search(r"(?m)^\| 仿写口吻 \| (.*?) \|$", text)
    return None if not match or match.group(1).strip() in {"", "—"} else match.group(1).strip()


def require_no_pending_finalize(root: Path) -> None:
    # 旧恢复快照会覆盖后来的修改，因此其他写入口先停下。
    if (root / "assets" / ".finalize-journal.json").exists():
        raise SystemExit("发现未完成的定稿存档记录（assets/.finalize-journal.json）。先运行 python3 scripts/finalize.py --recover 恢复，再继续。")


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def cell(value) -> str:
    return str(value).replace("|", "/").replace("\n", " ").strip()


# ---------- 偏好账本 ----------

def split_ledger(text: str) -> tuple[str, list[Pref]]:
    matches = list(HEADER_RE.finditer(text))
    head = text[: matches[0].start()] if matches else text
    prefs = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        fields = {}
        for line in text[match.end():end].splitlines():
            found = re.match(r"^- ([^:：]+): (.*)$", line)
            if found:
                fields[found.group(1).strip()] = found.group(2).strip()
        prefs.append(Pref(match.group(1), fields))
    return head, prefs


def render_ledger(head: str, prefs: list[Pref]) -> str:
    blocks = [head.rstrip() + "\n"]
    for pref in prefs:
        lines = [f"### 偏好 {pref.pid}"] + [f"- {key}: {pref.fields.get(key, '')}" for key in FIELDS]
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def load_ledgers(root: Path) -> tuple[str, list[Pref], str, list[Pref]]:
    p = paths(root)
    head, prefs = split_ledger(p["dna"].read_text(encoding="utf-8"))
    archive_head, archived = split_ledger(p["archive"].read_text(encoding="utf-8"))
    return head, prefs, archive_head, archived


def validate_pref(pref: Pref, archived: bool) -> list[str]:
    errors = []
    for key in FIELDS:
        if not pref.fields.get(key, "").strip():
            errors.append(f"{pref.pid}缺少字段: {key}")
    if errors:
        return errors
    f = pref.fields
    allowed = ARCHIVE_STATUSES if archived else LEDGER_STATUSES
    if f["状态"] not in allowed:
        errors.append(f"{pref.pid}状态无效: {f['状态']}")
    if f["类型"] not in TYPES:
        errors.append(f"{pref.pid}类型只能是说话或判断")
    if any(scene.strip() not in SCENES for scene in f["适用场景"].split("、")):
        errors.append(f"{pref.pid}适用场景无效: {f['适用场景']}")
    if not STATS_RE.match(f["统计"]):
        errors.append(f"{pref.pid}统计格式错误")
    for key in ("创建于", "最近相关", "状态日期"):
        if not DATE_RE.match(f[key]):
            errors.append(f"{pref.pid}{key}必须是YYYY-MM-DD")
    if len(f["写法"]) > 80:
        errors.append(f"{pref.pid}写法超过80字")
    if not archived and NEGATIVE_RE.search(f["写法"]):
        errors.append(f"{pref.pid}写法含命令式禁止或引号例句；可以写边界条件，例句放进 dna-examples.md")
    return errors


def next_id(prefs: list[Pref], archived: list[Pref]) -> str:
    numbers = [int(p.pid[1:]) for p in prefs + archived]
    return f"#{max(numbers, default=0) + 1:03d}"


def find(prefs: list[Pref], pid: str) -> Pref | None:
    return next((p for p in prefs if p.pid == pid), None)


def evidence_files(pref: Pref) -> list[str]:
    raw = pref.fields.get("证据", "—")
    return [] if raw in {"", "—"} else [item.strip() for item in raw.split(",") if item.strip()]


# ---------- 复盘记录 ----------

def read_reviews(path: Path) -> list[dict]:
    return read_reviews_text(path.read_text(encoding="utf-8")) if path.exists() else []


def read_reviews_text(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        parts = [item.strip() for item in line.strip().strip("|").split("|")]
        if len(parts) != 6 or parts[0] in {"时间", "---"} or not re.match(r"^\d{4}-\d{2}-\d{2}", parts[0]):
            continue
        rows.append(dict(zip(("time", "sample", "pid", "tag", "basis", "state"), parts)))
    return rows


def review_row(time: str, sample: str, pid: str, tag: str, basis: str, state: str = "有效") -> str:
    return f"| {time} | {sample} | {pid} | {tag} | {cell(basis) or '—'} | {state} |"


def set_review_state(text: str, sample: str, old: str, new: str) -> tuple[str, int]:
    pattern = rf"(?m)^(\| [^|]+ \| {re.escape(sample)} \| [^|]+ \| [^|]+ \| [^|]+ \| ){old}( \|)$"
    return re.subn(pattern, rf"\g<1>{new}\g<2>", text)


def recompute(prefs: list[Pref], reviews: list[dict]) -> list[Pref]:
    """按有效复盘记录重算统计；没有相关记录时保留原来的最近相关日期。"""
    for pref in prefs:
        rows = [r for r in reviews if r["pid"] == pref.pid and r["state"] == "有效"]
        count = {tag: sum(1 for r in rows if r["tag"] == tag) for tag in TAGS}
        pref.fields["统计"] = f"用上 {count['用上了']} / 守住 {count['守住了']} / 被改回 {count['被改回']} / 印证 {count['印证']}"
        related = [r["time"][:10] for r in rows if r["tag"] != "不相关"]
        if related:
            pref.fields["最近相关"] = max(related)
    return prefs


# ---------- 样本索引与待定区 ----------

def read_index(path: Path) -> dict[str, dict]:
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [item.strip() for item in line.strip().strip("|").split("|")]
        if len(parts) != 5 or parts[0] in {"身份键", "---"} or not parts[1].endswith(".md"):
            continue
        rows[parts[1]] = dict(zip(("identity", "file", "source_type", "eligible", "status"), parts))
    return rows


def eligible(index: dict[str, dict], sample: str) -> bool:
    row = index.get(sample)
    return bool(row) and row["eligible"] == "是" and row["status"] == "active"


def load_drafts(root: Path) -> list[dict]:
    drafts = []
    folder = paths(root)["drafts"]
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path)
        drafts.append(data)
    return drafts


# ---------- 提示规则 ----------

def suggest(root: Path) -> list[str]:
    p = paths(root)
    _, prefs, _, _ = load_ledgers(root)
    reviews = [r for r in read_reviews(p["review"]) if r["state"] == "有效"]
    index = read_index(p["index"])
    tips = []
    for pref in prefs:
        status = pref.fields.get("状态")
        rows = sorted((r for r in reviews if r["pid"] == pref.pid), key=lambda r: r["time"])
        if status == "候选":
            samples = set(evidence_files(pref)) | {r["sample"] for r in rows if r["tag"] == "印证"}
            ok = [s for s in samples if eligible(index, s)]
            originals = [s for s in ok if index[s]["source_type"] == "user_original"]
            if originals or len(ok) >= EVIDENCE_NEEDED:
                tips.append(f"{pref.pid} 候选已有 {len(ok)} 篇可用证据，建议问作者是否生效：{pref.fields['写法']}")
        elif status == "生效":
            recent = [r for r in rows if r["tag"] != "不相关"][-RECENT_WINDOW:]
            reverts = sum(1 for r in recent if r["tag"] == "被改回")
            if reverts >= REVERTS_TO_SLEEP:
                tips.append(f"{pref.pid} 最近 {len(recent)} 篇相关文章里被改回 {reverts} 次，建议问作者是否休眠：{pref.fields['写法']}")
        elif status == "休眠":
            sleeps = [r for r in log_data_rows(root) if r[1] == "休眠" and re.match(rf"^{re.escape(pref.pid)}(?:\s|$)", r[4])]
            since = sleeps[-1][0] if sleeps else pref.fields.get("状态日期", "0000-00-00")
            # 旧记录只有日期，无法区分同日先后，保守地从下一天开始算。
            confirms = {r["sample"] for r in rows
                        if r["tag"] == "印证" and eligible(index, r["sample"])
                        and (r["time"] > since if sleeps else r["time"][:10] > since)}
            if len(confirms) >= CONFIRMS_TO_WAKE:
                tips.append(f"{pref.pid} 休眠后又被作者原文印证 {len(confirms)} 次，建议问作者是否唤醒：{pref.fields['写法']}")
    limit = datetime.now().astimezone().timestamp() - DRAFT_EXPIRE_DAYS * 86400
    for draft in load_drafts(root):
        created = datetime.fromisoformat(draft["created_at"]).timestamp()
        if not draft.get("finalized_as") and created < limit:
            tips.append(f"初稿 {draft['draft_id']} 超过 {DRAFT_EXPIRE_DAYS} 天未定稿，建议问作者后运行 draft.py expire")
    base = root / "evals" / "exam" / "baseline.json"
    if base.exists():
        pending = changes_since(root, json.loads(base.read_text(encoding="utf-8")))
        if pending:
            tips.append(f"上次盲选考试之后有 {len(pending)} 次需要考试的偏好改动，建议问作者是否运行 exam.py 考一次")
    return tips


EXAM_EVENTS = {"生效", "唤醒", "恢复", "修改"}  # 会让 Agent 多做点什么的改动才需要考


def log_data_rows(root: Path) -> list[list[str]]:
    """读取修改日志的数据行，供考试游标和状态时间共用。"""
    rows = []
    for line in paths(root)["log"].read_text(encoding="utf-8").splitlines():
        parts = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(parts) == 6 and parts[0][:4].isdigit():
            rows.append(parts)
    return rows


def changes_since(root: Path, baseline: dict) -> list[str]:
    """按数据行游标找待考改动；旧基准保留按时间比较的兼容路径。"""
    data = log_data_rows(root)
    if "log_rows" in baseline:
        data = data[baseline["log_rows"]:]
    else:
        data = [parts for parts in data if parts[0] > baseline["since"]]
    rows = []
    for parts in data:
        if parts[1] in EXAM_EVENTS or (parts[1] == "新增" and "新增·生效" in parts[4]):
            rows.append(f"{parts[0]} {parts[1]} {parts[4]}")
    return rows


# ---------- 文件写入、锁、备份 ----------

def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def lock(root: Path):
    import hashlib

    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"writing-style-{digest}.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_all(changes: dict[Path, str]) -> None:
    """一次写入多个文件；任一失败就恢复全部原内容。"""
    before = {path: path.read_bytes() if path.exists() else None for path in changes}
    try:
        for path, content in changes.items():
            atomic_write(path, content.encode("utf-8"))
    except Exception:
        for path, data in before.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data)
        raise


def backup(root: Path, files: list[Path]) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    target = paths(root)["backups"] / stamp
    target.mkdir(parents=True)
    for path in files:
        if path.exists():
            relative = path.relative_to(root)
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / relative)
    return target


def latest_backup(root: Path) -> Path | None:
    folder = paths(root)["backups"]
    candidates = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.endswith((".failed", ".restored"))) if folder.exists() else []
    return candidates[-1] if candidates else None


def replace_state_number(text: str, field: str, value: int) -> str:
    text, yaml_count = re.subn(rf"(?m)^({re.escape(field)}:\s*)\d+(\s*)$", rf"\g<1>{value}\g<2>", text, count=1)
    table_pattern = rf"(?m)^(\|\s*`{re.escape(field)}`\s*\|[^\n]*?\|\s*)\d+(\s*\|\s*)$"
    text, table_count = re.subn(table_pattern, rf"\g<1>{value}\g<2>", text, count=1)
    if yaml_count != 1 or table_count != 1:
        raise ValueError(f"state.md中的{field}格式不符合契约")
    return text


def set_state_counts(text: str, active: int, archived: int) -> str:
    text = replace_state_number(text, "dna_active_count", active)
    text = replace_state_number(text, "dna_archive_count", archived)
    return re.sub(r"(?m)^(last_updated:\s*).*$", rf"\g<1>{today()}", text, count=1)


def file_hashes(files: list[Path]) -> dict[str, str]:
    import hashlib

    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "" for path in files}


def append_log(text: str, event: str, sample: str, detail: str, count: int) -> str:
    row = f"| {now_text()} | {event} | {sample} | {sample} | {cell(detail) or '—'} | {count} |"
    return text.rstrip() + "\n" + row + "\n"
