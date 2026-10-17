"""测试共用：临时 skill 根目录和 payload。"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"


def load(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"ws_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(name, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), *map(str, args)], capture_output=True, text=True, check=False)


def pref(pid, text, status="生效", stats="用上 0 / 守住 0 / 被改回 0 / 印证 0", kind="说话"):
    return f"""### 偏好 {pid}
- 写法: {text}
- 类型: {kind}
- 适用场景: 全局
- 状态: {status}
- 来源: 用户口头指令 2026-05-24
- 证据: —
- 统计: {stats}
- 创建于: 2026-05-24
- 最近相关: 2026-05-24
- 状态日期: 2026-05-24
"""


def make_root(tmp, prefs=None):
    root = Path(tmp)
    for folder in ("assets", "reference", "evals"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "assets" / "state.md").write_text(
        "# state\n\n| `polish_count_total` | 整数 | 累计 | 2 |\n| `dna_active_count` | 整数 | DNA | 2 |\n"
        "| `dna_archive_count` | 整数 | 归档 | 0 |\n\n```yaml\npolish_count_total: 2\ndna_active_count: 2\n"
        "dna_archive_count: 0\nlast_updated: 2026-06-02\n```\n",
        encoding="utf-8",
    )
    (root / "assets" / "diff-log.md").write_text(
        "# diff-log\n\n| 修改时间 | 事件类型 | 样本文件 | 确认版本 | 详情 | 数量 |\n|---|---|---|---|---|---|\n", encoding="utf-8")
    (root / "assets" / "style-index.md").write_text(
        "# 索引\n\n| 身份键 | 样本文件 | 来源类型 | 风格资格 | 状态 |\n|---|---|---|---|---|\n", encoding="utf-8")
    (root / "assets" / "review-log.md").write_text(
        "# 复盘记录\n\n| 时间 | 样本文件 | 偏好 | 标签 | 依据 | 状态 |\n|---|---|---|---|---|---|\n", encoding="utf-8")
    body = prefs if prefs is not None else pref("#001", "结尾停在最后一个事实上") + "\n" + pref("#002", "保留自嘲和冷幽默")
    (root / "reference" / "dna.md").write_text("# 活跃 DNA\n\n" + body, encoding="utf-8")
    (root / "reference" / "dna-archive.md").write_text("# 冷藏 DNA\n", encoding="utf-8")
    (root / "evals" / "benchmark.md").write_text("# benchmark\n\n#### SNF-001 示例\n- 理由: x\n", encoding="utf-8")
    return root


# 虚构维修素材，不代表任何作者的真实经历。
FIRST = "我试了三次。\n车还在，灯也还在，车没动。"


def save_draft(root, first=FIRST, anchors=None, source="B", topic="测试主题"):
    draft = load("draft")
    data = {
        "topic": topic, "input_source": source, "scene": "项目争议", "original_material": "素材",
        "first_generated": first,
        "anchors": anchors if anchors is not None else [{"id": "#001", "text": "车还在，灯也还在，车没动"}],
        "handoff": {"progress": "初稿已出"},
    }
    return draft.save(Path(root), data)["draft_id"]


def payload(topic="测试主题", *, final=FIRST, draft_id=None, input_source=None, event_type="创作", review=None, **extra):
    data = {
        "topic": topic, "identity": topic, "event_type": event_type, "finalized": True, "scope": "正文",
        "scene": "项目争议", "audience": "玩家", "source_summary": "用户素材", "confirmed_final": final,
        "time_sensitive": False, "checked_at": None, "sources": ["用户提供素材"],
        "scores": {"natural": 5, "faithful": 5, "publishable": 5, "reason": "已发布"},
        "review": review or [], "revision_of": None,
    }
    if draft_id:
        data["draft_id"] = draft_id
    else:
        data["input_source"] = input_source or "A"
        data["original_material"] = "作者原文"
        data["first_generated"] = None
    data.update(extra)
    return data


def dna_text(root):
    return (Path(root) / "reference" / "dna.md").read_text(encoding="utf-8")


def stats(root, pid):
    text = dna_text(root)
    section = text.split(f"### 偏好 {pid}", 1)[1].split("### 偏好", 1)[0]
    return section.split("- 统计: ", 1)[1].splitlines()[0]


def dumps(data):
    return json.dumps(data, ensure_ascii=False)
