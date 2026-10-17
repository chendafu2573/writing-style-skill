import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fixtures import dna_text, load, make_root, payload, pref, run, save_draft

finalize = load("finalize")
dna_lib = load("dna_lib")
draft = load("draft")


def update(root, *args):
    return run("dna_update", args[0], "--root", root, *args[1:])


class DnaUpdateTests(unittest.TestCase):
    def test_changes_require_author_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = update(root, "status", "--id", "#001", "--to", "休眠")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("--confirmed", result.stderr + result.stdout)

    def test_add_active_needs_snf_and_syncs_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            base = ["add", "--text", "先写自己试了几次", "--type", "判断", "--status", "生效", "--source", "用户口头指令 2026-10-17", "--confirmed"]
            self.assertNotEqual(0, update(root, *base).returncode)
            snf = ["--snf-title", "保留尝试次数", "--snf-context", "项目争议", "--snf-text", "我试了三次", "--snf-reason", "亲身尝试。"]
            result = update(root, *base, *snf)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("### 偏好 #003", dna_text(root))
            self.assertIn("#### SNF-002 保留尝试次数", (root / "evals" / "benchmark.md").read_text(encoding="utf-8"))
            self.assertIn("dna_active_count: 3", (root / "assets" / "state.md").read_text(encoding="utf-8"))
            self.assertEqual(0, run("audit_skill", "--root", root).returncode)

    def test_negative_wording_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = update(root, "edit", "--id", "#001", "--text", "结尾停住，不要补总结", "--confirmed")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("命令式禁止", result.stderr + result.stdout)
            quoted = update(root, "edit", "--id", "#001", "--text", "结尾停在事实上，比如“车没动”", "--confirmed")
            self.assertNotEqual(0, quoted.returncode)
            boundary = update(root, "edit", "--id", "#001", "--text", "口语优先保留，不为整齐改成书面语", "--confirmed")
            self.assertEqual(0, boundary.returncode, boundary.stderr)

    def test_retire_moves_to_archive_and_illegal_transition_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            self.assertEqual(0, update(root, "status", "--id", "#001", "--to", "淘汰", "--confirmed").returncode)
            self.assertNotIn("### 偏好 #001", dna_text(root))
            archive = (root / "reference" / "dna-archive.md").read_text(encoding="utf-8")
            self.assertIn("### 偏好 #001", archive)
            self.assertIn("- 状态: 淘汰", archive)
            self.assertIn("dna_archive_count: 1", (root / "assets" / "state.md").read_text(encoding="utf-8"))
            self.assertNotEqual(0, update(root, "status", "--id", "#002", "--to", "生效", "--confirmed").returncode)

    def test_retired_preference_can_be_restored_with_same_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            update(root, "status", "--id", "#001", "--to", "淘汰", "--confirmed")
            result = update(root, "status", "--id", "#001", "--to", "生效", "--confirmed")
            self.assertEqual(0, result.returncode, result.stderr)
            text = dna_text(root)
            self.assertLess(text.index("### 偏好 #001"), text.index("### 偏好 #002"))
            self.assertNotIn("### 偏好 #001", (root / "reference" / "dna-archive.md").read_text(encoding="utf-8"))
            self.assertIn("| 恢复 |", (root / "assets" / "diff-log.md").read_text(encoding="utf-8"))
            self.assertEqual(0, run("audit_skill", "--root", root).returncode)

    def test_rollback_restores_and_refuses_after_later_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            before = dna_text(root)
            update(root, "edit", "--id", "#001", "--text", "结尾停在一个具体结果上", "--confirmed")
            self.assertEqual(0, update(root, "rollback", "--confirmed").returncode)
            self.assertEqual(before, dna_text(root))
            update(root, "edit", "--id", "#001", "--text", "结尾停在一个具体结果上", "--confirmed")
            finalize.finalize(root, payload())
            result = update(root, "rollback", "--confirmed")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("不能自动撤回", result.stderr + result.stdout)


class SuggestTests(unittest.TestCase):
    def test_candidate_with_original_evidence_is_suggested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, prefs=pref("#001", "先写自己试了几次", status="候选"))
            finalize.finalize(root, payload(review=[{"id": "#001", "tag": "印证", "basis": "我试了三次"}]))
            self.assertTrue(any("#001 候选" in tip for tip in dna_lib.suggest(root)))

    def test_candidate_needs_two_revision_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, prefs=pref("#001", "结尾停在事实上") + "\n" + pref("#002", "先写自己试了几次", status="候选"))
            for index in (1, 2):
                draft_id = save_draft(root, topic=f"篇{index}", anchors=[])
                final = f"开头写我试了{index}次。\n" + "我试了三次。\n车还在，灯也还在，车没动。"
                finalize.finalize(root, payload(f"篇{index}", draft_id=draft_id, final=final,
                                                review=[{"id": "#002", "tag": "印证", "basis": f"开头写我试了{index}次"}]))
                tips = [tip for tip in dna_lib.suggest(root) if "#002 候选" in tip]
                self.assertEqual(index == 2, bool(tips), index)

    def test_two_reverts_in_recent_five_suggest_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            for index in (1, 2):
                draft_id = save_draft(root, topic=f"篇{index}")
                finalize.finalize(root, payload(f"篇{index}", draft_id=draft_id, final="我试了三次。\n算了。"))
            self.assertTrue(any("#001 最近" in tip for tip in dna_lib.suggest(root)))


class DraftTests(unittest.TestCase):
    def test_anchor_must_be_in_draft_and_preference_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, prefs=pref("#001", "结尾停在事实上") + "\n" + pref("#002", "保留口语", status="候选"))
            with self.assertRaisesRegex(ValueError, "初稿里的原句"):
                save_draft(root, anchors=[{"id": "#001", "text": "初稿里没有这句话"}])
            with self.assertRaisesRegex(ValueError, "未生效"):
                save_draft(root, anchors=[{"id": "#002", "text": "车还在，灯也还在"}])

    def test_handoff_note_and_expire(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            draft.note(Path(root), draft_id, {"todo": "等作者补亏损金额"})
            self.assertEqual("等作者补亏损金额", draft.load(Path(root), draft_id)["handoff"]["todo"])
            path = Path(root) / "drafts" / f"{draft_id}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["created_at"] = (datetime.now().astimezone() - timedelta(days=31)).isoformat()
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(any("超过 30 天" in tip for tip in dna_lib.suggest(Path(root))))
            self.assertEqual([draft_id], draft.expire(Path(root), 30))
            self.assertTrue((Path(root) / "drafts" / "_expired" / f"{draft_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
