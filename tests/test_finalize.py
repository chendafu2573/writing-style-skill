import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fixtures import FIRST, SCRIPTS, load, make_root, payload, run, save_draft, stats

finalize = load("finalize")


class SourceAndAnchorTests(unittest.TestCase):
    def test_unchanged_draft_is_agent_draft_and_anchor_counts_as_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            result = finalize.finalize(root, payload(draft_id=draft_id))
            self.assertEqual("agent_draft", result["source_type"])
            self.assertEqual("用上 1 / 守住 0 / 被改回 0 / 印证 0", stats(root, "#001"))
            index = (root / "assets" / "style-index.md").read_text(encoding="utf-8")
            self.assertIn("| agent_draft | 否 | active |", index)
            draft = json.loads(next((root / "drafts").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(result["filename"], draft["finalized_as"])

    def test_edited_draft_is_user_revision_and_removed_anchor_is_reverted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            result = finalize.finalize(root, payload(draft_id=draft_id, final="我试了三次。\n就这样吧。"))
            self.assertEqual("user_revision", result["source_type"])
            self.assertEqual("用上 0 / 守住 0 / 被改回 1 / 印证 0", stats(root, "#001"))
            self.assertIn("| user_revision | 是 | active |", (root / "assets" / "style-index.md").read_text(encoding="utf-8"))

    def test_no_draft_sources_follow_input_type(self):
        cases = {"A": ("user_original", "是"), "C": ("reference_text", "否"), "D": ("mixed", "否")}
        for code, (source, eligible) in cases.items():
            with tempfile.TemporaryDirectory() as tmp:
                root = make_root(tmp)
                result = finalize.finalize(root, payload(input_source=code))
                self.assertEqual(source, result["source_type"])
                self.assertIn(f"| {source} | {eligible} | active |", (root / "assets" / "style-index.md").read_text(encoding="utf-8"))

    def test_material_b_without_agent_draft_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaisesRegex(ValueError, "B类资料"):
                finalize.finalize(root, payload(input_source="B"))

    def test_old_payload_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaisesRegex(ValueError, "payload已改版"):
                finalize.finalize(root, payload(preference_hits=["#001"]))

    def test_finalized_draft_cannot_be_created_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            finalize.finalize(root, payload(draft_id=draft_id))
            with self.assertRaisesRegex(ValueError, "已定稿"):
                finalize.finalize(root, payload("另一个", draft_id=draft_id))


class ReviewTagTests(unittest.TestCase):
    def test_agent_cannot_claim_script_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaisesRegex(ValueError, "只能由脚本"):
                finalize.finalize(root, payload(review=[{"id": "#002", "tag": "用上了", "basis": "x"}]))

    def test_confirmation_needs_sentence_from_authors_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaisesRegex(ValueError, "终稿里的原句"):
                finalize.finalize(root, payload(review=[{"id": "#002", "tag": "印证", "basis": "终稿里没有这句"}]))
            finalize.finalize(root, payload(review=[{"id": "#002", "tag": "印证", "basis": "车还在，灯也还在"}]))
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 1", stats(root, "#002"))

    def test_confirmation_on_revision_must_be_authors_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root, anchors=[])
            final = FIRST + "\n反正我先不用了，哈哈。"
            with self.assertRaisesRegex(ValueError, "作者改动后的句子"):
                finalize.finalize(root, payload(draft_id=draft_id, final=final, review=[{"id": "#002", "tag": "印证", "basis": "我试了三次"}]))
            finalize.finalize(root, payload(draft_id=draft_id, final=final, review=[{"id": "#002", "tag": "印证", "basis": "反正我先不用了"}]))
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 1", stats(root, "#002"))

    def test_reference_text_and_agent_draft_cannot_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaisesRegex(ValueError, "不能印证"):
                finalize.finalize(root, payload(input_source="C", review=[{"id": "#002", "tag": "印证", "basis": "车还在，灯也还在"}]))

    def test_kept_requires_agent_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaisesRegex(ValueError, "守住了只用于"):
                finalize.finalize(root, payload(review=[{"id": "#002", "tag": "守住了", "basis": "保留了口语"}]))
            draft_id = save_draft(root)
            finalize.finalize(root, payload(draft_id=draft_id, review=[{"id": "#002", "tag": "守住了", "basis": "保留了口语"}]))
            self.assertEqual("用上 0 / 守住 1 / 被改回 0 / 印证 0", stats(root, "#002"))

    def test_duplicate_or_anchor_overlap_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            with self.assertRaisesRegex(ValueError, "重复"):
                finalize.finalize(root, payload(draft_id=draft_id, review=[{"id": "#001", "tag": "不相关"}]))

    def test_dry_run_prints_review_table_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            result = finalize.finalize(root, payload(draft_id=draft_id, final="我试了三次。\n就这样吧。"), dry_run=True)
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})
            self.assertIn("#001 | 被改回 | 车还在，灯也还在，车没动 | 脚本", result["review_table"])
            self.assertTrue(result["diff_blocks"])


class PersistenceTests(unittest.TestCase):
    def test_creation_writes_sample_log_index_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = finalize.finalize(root, payload())
            artifact = (root / "assets" / result["filename"]).read_text(encoding="utf-8")
            self.assertIn("| 资产格式 | v3 |", artifact)
            self.assertIn("| 输入类型 | A |", artifact)
            self.assertIn("polish_count_total: 3", (root / "assets" / "state.md").read_text(encoding="utf-8"))
            self.assertIn("| 创作 |", (root / "assets" / "diff-log.md").read_text(encoding="utf-8"))

    def test_revision_supersedes_previous_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            first = finalize.finalize(root, payload(draft_id=save_draft(root)))
            revised = payload(draft_id=save_draft(root), event_type="修订", revision_of=first["filename"])
            second = finalize.finalize(root, revised)
            self.assertEqual(2, second["version"])
            self.assertEqual("用上 1 / 守住 0 / 被改回 0 / 印证 0", stats(root, "#001"))
            self.assertIn("polish_count_total: 3", (root / "assets" / "state.md").read_text(encoding="utf-8"))
            self.assertIn("| 被替代 |", (root / "assets" / "review-log.md").read_text(encoding="utf-8"))
            index = (root / "assets" / "style-index.md").read_text(encoding="utf-8")
            self.assertIn(f"| {first['filename']} | agent_draft | 否 | superseded |", index)
            self.assertIn("| 资产状态 | superseded |", (root / "assets" / first["filename"]).read_text(encoding="utf-8"))
            self.assertEqual(0, run("audit_skill", "--root", root).returncode)

    def test_duplicate_creation_requires_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            finalize.finalize(root, payload())
            with self.assertRaisesRegex(ValueError, "必须使用修订事件"):
                finalize.finalize(root, payload())

    def test_failed_transaction_restores_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            for step in ("diff-log", "review-log", "dna", "state"):
                with self.assertRaises(RuntimeError):
                    finalize.finalize(root, payload(draft_id=draft_id), _test_fail_after=step)
                self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}, step)

    def test_concurrent_creations_are_serialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            processes = []
            for index in (1, 2):
                path = root / f"payload-{index}.json"
                path.write_text(json.dumps(payload(f"并发{index}"), ensure_ascii=False), encoding="utf-8")
                processes.append(subprocess.Popen([sys.executable, str(SCRIPTS / "finalize.py"), "--root", str(root), "--payload", str(path)],
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            results = [proc.communicate(timeout=20) + (proc.returncode,) for proc in processes]
            self.assertEqual([0, 0], [r[2] for r in results], results)
            self.assertIn("polish_count_total: 4", (root / "assets" / "state.md").read_text(encoding="utf-8"))


class ValidationTests(unittest.TestCase):
    def test_published_live_data_older_than_24h_is_still_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            stale = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
            result = finalize.finalize(root, payload(time_sensitive=True, checked_at=stale))
            self.assertIn(f"| 时效数据检查 | {stale} |", (root / "assets" / result["filename"]).read_text(encoding="utf-8"))

    def test_live_data_still_requires_checked_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "必须提供checked_at"):
                finalize.finalize(make_root(tmp), payload(time_sensitive=True))

    def test_scores_sources_and_flags_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            bad = [
                ({"scores": {"natural": 6, "faithful": 5, "publishable": 5, "reason": "x"}}, "scores.natural"),
                ({"sources": [""]}, "sources每项"),
                ({"time_sensitive": "no"}, "布尔值"),
                ({"finalized": False}, "FINALIZED"),
            ]
            for extra, message in bad:
                with self.assertRaisesRegex(ValueError, message):
                    finalize.finalize(root, payload(**extra))

    def test_publish_package_requires_real_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            extra = {"scope": "发布包", "publication_assets_checked": True, "platform": "X", "title": "t",
                     "asset_review": "已核对", "asset_paths": ["assets/none.png"]}
            with self.assertRaisesRegex(ValueError, "附件不存在"):
                finalize.finalize(root, payload(**extra))
            (root / "assets" / "cover.png").write_bytes(b"png")
            extra["asset_paths"] = ["assets/cover.png"]
            result = finalize.finalize(root, payload(**extra))
            self.assertIn("| 发布平台 | X |", (root / "assets" / result["filename"]).read_text(encoding="utf-8"))

    def test_state_yaml_and_table_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            state = root / "assets" / "state.md"
            state.write_text(state.read_text(encoding="utf-8").replace("polish_count_total: 2", "polish_count_total: 9"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不一致"):
                finalize.finalize(root, payload())


class RejectAndAuditTests(unittest.TestCase):
    def test_reject_voids_reviews_and_keeps_audit_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = finalize.finalize(root, payload(draft_id=save_draft(root), final="我试了三次。\n车还在，灯也还在，车没动。\n我自己加一句。"))
            artifact = root / "assets" / result["filename"]
            completed = run("reject_sample", "--root", root, "--artifact", artifact, "--reason", "不像本人")
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("voided_reviews: 1", completed.stdout)
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 0", stats(root, "#001"))
            self.assertIn("| 资产状态 | rejected |", artifact.read_text(encoding="utf-8"))
            self.assertIn("| 撤销 |", (root / "assets" / "diff-log.md").read_text(encoding="utf-8"))
            audit = run("audit_skill", "--root", root)
            self.assertEqual(0, audit.returncode, audit.stdout)

    def test_user_original_cannot_be_auto_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = finalize.finalize(root, payload())
            completed = run("reject_sample", "--root", root, "--artifact", root / "assets" / result["filename"], "--reason", "x")
            self.assertNotEqual(0, completed.returncode)

    def test_audit_catches_stats_out_of_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            finalize.finalize(root, payload(draft_id=save_draft(root)))
            dna = root / "reference" / "dna.md"
            dna.write_text(dna.read_text(encoding="utf-8").replace("用上 1", "用上 7"), encoding="utf-8")
            audit = run("audit_skill", "--root", root)
            self.assertEqual(1, audit.returncode)
            self.assertIn("统计与复盘记录不一致", audit.stdout)


if __name__ == "__main__":
    unittest.main()
