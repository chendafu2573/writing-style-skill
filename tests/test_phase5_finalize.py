import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fixtures import FIRST, load, make_root, payload, pref, run, save_draft, stats

draft = load("draft")
finalize = load("finalize")
AGENT_LINE = "这下好了，说明书又要重新读一遍了。"
AUTHOR_LINE = "我自己补一句，这回先把按钮修好。"


def add_version(root, draft_id, text, author_lines=None):
    """构造已保存的历史版本，直接检验定稿对来源的判断。"""
    path = root / "drafts" / f"{draft_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.setdefault("agent_versions", []).append({
        "text": text, "at": "2026-10-18T08:00:00+08:00", "author_lines": author_lines or [],
    })
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


class Phase5SourceTests(unittest.TestCase):
    def test_agent_rewrite_is_not_author_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            latest = FIRST + "\n" + AGENT_LINE
            add_version(root, draft_id, latest)
            result = finalize.finalize(root, payload(draft_id=draft_id, final=latest), dry_run=True)
            self.assertEqual("agent_draft", result["source_type"])
            with self.assertRaisesRegex(ValueError, "不能印证"):
                finalize.finalize(root, payload(draft_id=draft_id, final=latest,
                    review=[{"id": "#002", "tag": "印证", "basis": AGENT_LINE}]), dry_run=True)

    def test_dictated_lines_in_any_version_remain_author_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            latest = FIRST + "\n" + AUTHOR_LINE
            add_version(root, draft_id, latest, [AUTHOR_LINE])
            add_version(root, draft_id, latest + "\n再看一遍。")
            result = finalize.finalize(root, payload(draft_id=draft_id, final=latest + "\n再看一遍。",
                review=[{"id": "#002", "tag": "印证", "basis": "这回先把按钮修好"}]))
            self.assertEqual("user_revision", result["source_type"])
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 1", stats(root, "#002"))
            artifact = (root / "assets" / result["filename"]).read_text(encoding="utf-8")
            self.assertIn("## Agent 最后一版", artifact)
            self.assertIn("## 作者口述句", artifact)
            self.assertIn("- " + AUTHOR_LINE, artifact)
            self.assertIn("## 首次生成版本\n\n" + FIRST, artifact)

    def test_authors_new_sentence_can_confirm_but_agent_rewrite_cannot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            latest = FIRST + "\n" + AGENT_LINE
            add_version(root, draft_id, latest)
            final = latest + "\n" + AUTHOR_LINE
            with self.assertRaisesRegex(ValueError, "作者改动后的句子"):
                finalize.finalize(root, payload(draft_id=draft_id, final=final,
                    review=[{"id": "#002", "tag": "印证", "basis": AGENT_LINE}]), dry_run=True)
            result = finalize.finalize(root, payload(draft_id=draft_id, final=final,
                review=[{"id": "#002", "tag": "印证", "basis": AUTHOR_LINE}]), dry_run=True)
            self.assertEqual("user_revision", result["source_type"])
            self.assertEqual(["原：（无）\n改：" + AUTHOR_LINE], result["diff_blocks"])

    def test_sentence_from_an_earlier_agent_version_is_not_author_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            add_version(root, draft_id, FIRST + "\n" + AGENT_LINE)
            add_version(root, draft_id, FIRST)
            with self.assertRaisesRegex(ValueError, "作者改动后的句子"):
                finalize.finalize(root, payload(draft_id=draft_id, final=FIRST + "\n" + AGENT_LINE,
                    review=[{"id": "#002", "tag": "印证", "basis": AGENT_LINE}]), dry_run=True)

    def test_revise_appends_validated_versions_and_show_returns_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            body = root / "revision.txt"
            body.write_text(FIRST + "\n" + AUTHOR_LINE, encoding="utf-8")
            completed = run("draft", "revise", "--root", root, "--draft", draft_id, "--file", body,
                "--author-line", AUTHOR_LINE, "--author-line", "这回先把按钮修好")
            self.assertEqual(0, completed.returncode, completed.stderr)
            shown = run("draft", "show", "--root", root, "--draft", draft_id)
            record = json.loads(shown.stdout)
            self.assertEqual(FIRST, record["first_generated"])
            self.assertEqual([AUTHOR_LINE, "这回先把按钮修好"], record["agent_versions"][0]["author_lines"])
            self.assertEqual(body.read_text(encoding="utf-8"), record["agent_versions"][0]["text"])
            self.assertRegex(record["agent_versions"][0]["at"], r"T.*[+-]\d\d:\d\d$")
            draft.revise(root, draft_id, FIRST)
            self.assertEqual(2, len(draft.load(root, draft_id)["agent_versions"]))

    def test_revise_rejects_finalized_drafts_and_invalid_author_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            for line in ("短句", "这句话不在稿子里"):
                with self.subTest(line=line), self.assertRaises(ValueError):
                    draft.revise(root, draft_id, FIRST, [line])
            finalize.finalize(root, payload(draft_id=draft_id))
            with self.assertRaisesRegex(ValueError, "已定稿"):
                draft.revise(root, draft_id, FIRST)

    def test_note_reads_current_record_after_taking_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            path = root / "drafts" / f"{draft_id}.json"
            entered = []

            @contextmanager
            def lock_after_finalize(_root):
                record = json.loads(path.read_text(encoding="utf-8"))
                record["finalized_as"] = "已定稿-v1.md"
                path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                entered.append(True)
                yield

            with patch.object(draft.dna_lib, "lock", lock_after_finalize):
                draft.note(root, draft_id, {"todo": "补充交接"})
            self.assertEqual([True], entered)
            self.assertEqual("已定稿-v1.md", draft.load(root, draft_id)["finalized_as"])

    def test_finalize_reads_current_revision_after_taking_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            latest = FIRST + "\n" + AGENT_LINE

            @contextmanager
            def lock_after_revise(_root):
                add_version(root, draft_id, latest)
                path = root / "drafts" / f"{draft_id}.json"
                record = json.loads(path.read_text(encoding="utf-8"))
                record["handoff"]["todo"] = "刚更新的交接"
                path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
                yield

            with patch.object(finalize, "_lock", lock_after_revise):
                result = finalize.finalize(root, payload(draft_id=draft_id, final=latest))
            self.assertEqual("agent_draft", result["source_type"])
            record = draft.load(root, draft_id)
            self.assertEqual(latest, record["agent_versions"][-1]["text"])
            self.assertEqual("刚更新的交接", record["handoff"]["todo"])


class Phase5RevisionTests(unittest.TestCase):
    def test_revision_of_superseded_sample_names_current_active_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            first = finalize.finalize(root, payload())
            second = finalize.finalize(root, payload(event_type="修订", revision_of=first["filename"]))
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with self.assertRaisesRegex(ValueError, second["filename"]):
                finalize.finalize(root, payload(event_type="修订", revision_of=first["filename"]))
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_revision_requires_exactly_one_active_index_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            first = finalize.finalize(root, payload())
            index = root / "assets" / "style-index.md"
            text = index.read_text(encoding="utf-8")
            row = next(line for line in text.splitlines() if first["filename"] in line)
            index.write_text(text + row + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "1"):
                finalize.finalize(root, payload(event_type="修订", revision_of=first["filename"]))


class Phase5AnchorTests(unittest.TestCase):
    def test_anchor_kept_when_only_punctuation_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            result = finalize.finalize(root, payload(draft_id=draft_id, final=FIRST.replace("，", ",")), dry_run=True)
            self.assertIn("#001 | 用上了", result["review_table"][0])

    def test_author_can_override_deleted_anchor_as_unrelated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            data = payload(draft_id=draft_id, final="我试了三次。\n就这样吧。",
                anchor_overrides=[{"id": "#001", "tag": "不相关", "reason": "这篇没用这个结尾"}])
            preview = finalize.finalize(root, data, dry_run=True)
            self.assertIn("作者纠正", preview["review_table"][0])
            result = finalize.finalize(root, data)
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 0", stats(root, "#001"))
            review = (root / "assets" / "review-log.md").read_text(encoding="utf-8")
            self.assertIn("脚本原判 被改回，作者改为 不相关：这篇没用这个结尾", review)
            self.assertIn("| 不相关 |", review)
            self.assertEqual(preview["review_table"], result["review_table"])

    def test_invalid_anchor_overrides_are_rejected(self):
        invalid = [
            [{"id": "#002", "tag": "不相关", "reason": "没有这个锚点"}],
            [{"id": "#001", "tag": "用上了", "reason": "与原判相同"}],
            [{"id": "#001", "tag": "印证", "reason": "标签不允许"}],
            [{"id": "#001", "tag": "不相关", "reason": " "}],
            [{"id": "#001", "tag": "不相关", "reason": "一"}] * 2,
            "不是列表",
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                root = make_root(tmp)
                draft_id = save_draft(root)
                with self.assertRaises(ValueError):
                    finalize.finalize(root, payload(draft_id=draft_id, anchor_overrides=overrides), dry_run=True)

    def test_confirmation_matching_ignores_punctuation_in_final_and_agent_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            changed = FIRST.replace("，", ",") + "\n" + AUTHOR_LINE
            with self.assertRaisesRegex(ValueError, "作者改动后的句子"):
                finalize.finalize(root, payload(draft_id=draft_id, final=changed,
                    review=[{"id": "#002", "tag": "印证", "basis": "车还在,灯也还在"}]), dry_run=True)
            result = finalize.finalize(root, payload(draft_id=draft_id, final=changed,
                review=[{"id": "#002", "tag": "印证", "basis": AUTHOR_LINE.replace("，", ",")}]), dry_run=True)
            self.assertEqual("user_revision", result["source_type"])


class Phase5ArchiveTests(unittest.TestCase):
    def test_finalize_recomputes_archived_statistics_in_same_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            archived = root / "reference" / "dna-archive.md"
            archived.write_text("# 冷藏 DNA\n\n" + pref("#003", "保留有经历支撑的反问", status="淘汰",
                stats="用上 0 / 守住 0 / 被改回 0 / 印证 7"), encoding="utf-8")
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with self.assertRaises(RuntimeError):
                finalize.finalize(root, payload(), _test_fail_after="state")
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
            finalize.finalize(root, payload())
            self.assertIn("- 统计: 用上 0 / 守住 0 / 被改回 0 / 印证 0", archived.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
