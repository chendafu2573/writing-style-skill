import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ContractTests(unittest.TestCase):
    def test_bootstrap_lists_existing_full_files(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        block = text.split("### full 模式（默认）", 1)[1].split("### lite 模式", 1)[0]
        paths = re.findall(r"^(?:SKILL\.md|reference/[^`\n]+|assets/[^`\n]+|evals/[^`\n]+)$", block, re.MULTILINE)
        self.assertIn("SKILL.md", paths)
        self.assertIn("reference/scene-rules.md", paths)
        self.assertEqual(len(paths), len(set(paths)))
        for relative in paths:
            self.assertTrue((ROOT / relative).exists(), relative)

    def test_scene_rules_have_canonical_scenes_and_required_sections(self):
        text = (ROOT / "reference" / "scene-rules.md").read_text(encoding="utf-8")
        for scene in ("项目争议", "市场播报", "产品评测", "游戏体验", "个人吐槽"):
            self.assertIn(f"## {scene}", text)
        self.assertIn("主场景", text)
        self.assertIn("辅助标签", text)
        self.assertIn("未确认", text)

    def test_docs_do_not_claim_background_scheduler(self):
        for relative in ("README.md", "SKILL.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            if relative == "SKILL.md":
                self.assertIn("不会自动运行 scheduler", text)
            self.assertNotIn("自动生成。", text)


if __name__ == "__main__":
    unittest.main()
