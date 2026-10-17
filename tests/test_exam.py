import json
import tempfile
import unittest
from pathlib import Path

from fixtures import load, make_root, run

exam = load("exam")
dna_lib = load("dna_lib")


def make_exam(root):
    folder = Path(root) / "evals" / "exam"
    folder.mkdir(parents=True)
    items = [{"paper": f"paper-0{i}.md", "title": f"卷{i}", "scene": "项目争议", "original": "x"} for i in (1, 2, 3)]
    (folder / "manifest.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    for item in items:
        (folder / item["paper"]).write_text(f"# {item['title']}\n\n事实。", encoding="utf-8")
    (folder / "baseline-dna.md").write_text("# 旧 DNA\n", encoding="utf-8")
    (folder / "baseline.json").write_text(json.dumps({"since": "2026-01-01 00:00"}), encoding="utf-8")


def write_all_versions(root, name):
    folder = Path(root) / "evals" / "exam-runs" / name
    for side in ("old", "new"):
        for i in (1, 2, 3):
            (folder / f"{side}-paper-0{i}.md").write_text(f"{side} 写的第 {i} 篇", encoding="utf-8")


class ExamTests(unittest.TestCase):
    def test_prepare_builds_tasks_that_forbid_reading_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            result = exam.prepare(root)
            task = Path(result["tasks"]["new"]).read_text(encoding="utf-8")
            self.assertIn("new-dna.md", task)
            self.assertIn("不要读取 `assets/`", task)
            self.assertEqual(3, task.count("# 盲选考试写作任务"))
            folder = Path(root) / "evals" / "exam-runs" / result["run"]
            self.assertEqual("# 旧 DNA\n", (folder / "old-dna.md").read_text(encoding="utf-8"))

    def test_shuffle_requires_all_versions_and_hides_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            name = exam.prepare(root)["run"]
            with self.assertRaises(SystemExit):
                exam.shuffle(root, name)
            write_all_versions(root, name)
            blind = Path(exam.shuffle(root, name, seed=1)["blind"]).read_text(encoding="utf-8")
            self.assertIn("版本甲", blind)
            self.assertNotIn("old-paper", blind)

    def test_record_keeps_when_new_not_worse_and_moves_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            name = exam.prepare(root)["run"]
            write_all_versions(root, name)
            exam.shuffle(root, name, seed=1)
            key = json.loads((Path(root) / "evals" / "exam-runs" / name / "key.json").read_text(encoding="utf-8"))
            new_label = lambda paper: next(label for label, side in key[paper].items() if side == "new")  # noqa: E731
            old_label = lambda paper: next(label for label, side in key[paper].items() if side == "old")  # noqa: E731
            result = exam.record(root, name, [new_label("paper-01.md"), old_label("paper-02.md"), "都不像"])
            self.assertEqual((1, 1), (result["新版赢"], result["旧版赢"]))
            self.assertTrue(result["结论"].startswith("保留"))
            self.assertIn("偏好 #001", (Path(root) / "evals" / "exam" / "baseline-dna.md").read_text(encoding="utf-8"))
            self.assertIn("| 保留", (Path(root) / "evals" / "exam-log.md").read_text(encoding="utf-8"))

    def test_record_suggests_revert_when_old_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            name = exam.prepare(root)["run"]
            write_all_versions(root, name)
            exam.shuffle(root, name, seed=2)
            key = json.loads((Path(root) / "evals" / "exam-runs" / name / "key.json").read_text(encoding="utf-8"))
            picks = [next(label for label, side in key[f"paper-0{i}.md"].items() if side == "old") for i in (1, 2, 3)]
            result = exam.record(root, name, picks)
            self.assertIn("建议撤回", result["结论"])
            self.assertEqual("# 旧 DNA\n", (Path(root) / "evals" / "exam" / "baseline-dna.md").read_text(encoding="utf-8"))
            with self.assertRaises(SystemExit):
                exam.record(root, name, ["甲"])

    def test_rebase_needs_confirmation_and_clears_exam_tip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            run("dna_update", "edit", "--root", root, "--id", "#001", "--text", "结尾停在一个具体结果上", "--confirmed")
            self.assertTrue(any("盲选" in tip for tip in dna_lib.suggest(root)))
            self.assertNotEqual(0, run("exam", "rebase", "--root", root, "--reason", "x").returncode)
            done = run("exam", "rebase", "--root", root, "--reason", "按考试结论撤回后设基准", "--confirmed")
            self.assertEqual(0, done.returncode, done.stderr)
            self.assertFalse(any("盲选" in tip for tip in dna_lib.suggest(root)))
            self.assertIn("结尾停在一个具体结果上", (Path(root) / "evals" / "exam" / "baseline-dna.md").read_text(encoding="utf-8"))

    def test_suggest_asks_for_exam_only_after_behaviour_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            self.assertFalse(any("盲选" in tip for tip in dna_lib.suggest(root)))
            run("dna_update", "status", "--root", root, "--id", "#002", "--to", "休眠", "--confirmed")
            self.assertFalse(any("盲选" in tip for tip in dna_lib.suggest(root)))
            run("dna_update", "edit", "--root", root, "--id", "#001", "--text", "结尾停在一个具体结果上", "--confirmed")
            self.assertTrue(any("盲选" in tip for tip in dna_lib.suggest(root)))


if __name__ == "__main__":
    unittest.main()
