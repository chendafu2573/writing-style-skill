"""指定仿写口吻：照常存档，但不参与风格学习。素材为虚构。"""
import tempfile
import unittest
from pathlib import Path

from fixtures import FIRST, load, make_root, payload, run, save_draft, stats

finalize = load("finalize")
draft = load("draft")

ZERO = "用上 0 / 守住 0 / 被改回 0 / 印证 0"


def save_voice_draft(root, voice="鲁迅·梦境体", anchors=None):
    data = {
        "topic": "鲁迅口吻", "input_source": "A", "scene": "个人吐槽", "original_material": "作者原文",
        "first_generated": FIRST, "anchors": anchors or [], "voice": voice,
    }
    return draft.save(Path(root), data)["draft_id"]


def index_text(root):
    return (Path(root) / "assets" / "style-index.md").read_text(encoding="utf-8")


class VoiceDraftTests(unittest.TestCase):
    def test_voice_draft_records_voice_and_refuses_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_voice_draft(root)
            self.assertEqual("鲁迅·梦境体", draft.load(Path(root), draft_id)["voice"])
            with self.assertRaisesRegex(ValueError, "仿写口吻.*锚点"):
                save_voice_draft(root, anchors=[{"id": "#001", "text": "车还在，灯也还在，车没动"}])

    def test_voice_name_cannot_break_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with self.assertRaisesRegex(ValueError, "voice"):
                save_voice_draft(root, voice="鲁迅|王小波")


class VoiceFinalizeTests(unittest.TestCase):
    def test_voice_sample_is_archived_but_not_learned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_voice_draft(root)
            draft.revise(Path(root), draft_id, FIRST + "\n我大抵是修车修出了毛病。", ["我大抵是修车修出了毛病。"])
            final = FIRST + "\n我大抵是修车修出了毛病。"
            result = finalize.finalize(root, payload("鲁迅口吻", draft_id=draft_id, final=final))
            self.assertEqual([], result["review_table"])
            self.assertIn("| user_revision | 否 | active |", index_text(root))
            artifact = (Path(root) / "assets" / result["filename"]).read_text(encoding="utf-8")
            self.assertIn("| 仿写口吻 | 鲁迅·梦境体 |", artifact)
            self.assertEqual(ZERO, stats(root, "#001"))
            self.assertIn("仿写口吻：鲁迅·梦境体", (Path(root) / "assets" / "diff-log.md").read_text(encoding="utf-8"))
            self.assertEqual(0, run("audit_skill", "--root", root).returncode)

    def test_voice_sample_refuses_review_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_voice_draft(root)
            with self.assertRaisesRegex(ValueError, "仿写稿不写复盘"):
                finalize.finalize(root, payload("鲁迅口吻", draft_id=draft_id,
                                                review=[{"id": "#002", "tag": "守住了", "basis": "保留口语"}]))

    def test_voice_without_draft_comes_from_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = finalize.finalize(root, payload("王小波", voice="王小波"))
            self.assertIn("| user_original | 否 | active |", index_text(root))
            self.assertTrue(result["filename"])

    def test_payload_voice_must_match_draft_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_voice_draft(root)
            with self.assertRaisesRegex(ValueError, "voice.*不一致"):
                finalize.finalize(root, payload("鲁迅口吻", draft_id=draft_id, voice="王小波"))

    def test_revision_of_voice_sample_stays_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            first = finalize.finalize(root, payload("鲁迅口吻", draft_id=save_voice_draft(root)))
            second = finalize.finalize(root, payload("鲁迅口吻", event_type="修订", revision_of=first["filename"]))
            artifact = (Path(root) / "assets" / second["filename"]).read_text(encoding="utf-8")
            self.assertIn("| 仿写口吻 | 鲁迅·梦境体 |", artifact)
            self.assertIn(f"| {second['filename']} | user_original | 否 | active |", index_text(root))


class MarkVoiceTests(unittest.TestCase):
    def test_mark_existing_sample_voids_reviews_and_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            draft_id = save_draft(root)
            final = FIRST + "\n我自己加一句。"
            result = finalize.finalize(root, payload(draft_id=draft_id, final=final,
                                                     review=[{"id": "#002", "tag": "印证", "basis": "我自己加一句"}]))
            self.assertNotEqual(ZERO, stats(root, "#002"))
            artifact = Path(root) / "assets" / result["filename"]
            done = run("mark_voice", "--root", root, "--artifact", artifact, "--voice", "鲁迅·梦境体")
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertEqual(ZERO, stats(root, "#001"))
            self.assertEqual(ZERO, stats(root, "#002"))
            self.assertIn("| user_revision | 否 | active |", index_text(root))
            self.assertIn("| 仿写口吻 | 鲁迅·梦境体 |", artifact.read_text(encoding="utf-8"))
            self.assertIn("| 仿写 |", (Path(root) / "assets" / "review-log.md").read_text(encoding="utf-8"))
            self.assertIn("| 仿写 |", (Path(root) / "assets" / "diff-log.md").read_text(encoding="utf-8"))
            self.assertEqual(0, run("audit_skill", "--root", root).returncode)
            again = run("mark_voice", "--root", root, "--artifact", artifact, "--voice", "鲁迅·梦境体")
            self.assertNotEqual(0, again.returncode)

    def test_audit_catches_voice_sample_still_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            result = finalize.finalize(root, payload("鲁迅口吻", draft_id=save_voice_draft(root)))
            index = Path(root) / "assets" / "style-index.md"
            index.write_text(index.read_text(encoding="utf-8").replace("| 否 | active |", "| 是 | active |"), encoding="utf-8")
            audit = run("audit_skill", "--root", root)
            self.assertEqual(1, audit.returncode)
            self.assertIn("仿写稿", audit.stdout)
            self.assertTrue(result["filename"])


if __name__ == "__main__":
    unittest.main()
