import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fixtures import make_root, run
from test_exam import exam, make_exam, write_all_versions


def prepare_versions(root):
    name = exam.prepare(root)["run"]
    write_all_versions(root, name)
    exam.shuffle(root, name, seed=1)
    folder = root / "evals" / "exam-runs" / name
    key = json.loads((folder / "key.json").read_text(encoding="utf-8"))
    picks = [next(label for label, side in key[f"paper-0{i}.md"].items() if side == "new") for i in (1, 2, 3)]
    return name, folder, picks


def append_log_row(root, event="修改"):
    path = root / "assets" / "diff-log.md"
    path.write_text(path.read_text(encoding="utf-8") + f"| 2026-10-18 12:00 | {event} | — | — | #001 [修改] 写法: 结尾落在事实 | 0 |\n", encoding="utf-8")


class Phase5ExamTests(unittest.TestCase):
    def test_prepare_captures_all_valid_log_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            append_log_row(root)
            append_log_row(root, "休眠")
            log = root / "assets" / "diff-log.md"
            log.write_text(log.read_text(encoding="utf-8") + "| 缺列 | 修改 |\n", encoding="utf-8")
            name = exam.prepare(root)["run"]
            meta = json.loads((root / "evals" / "exam-runs" / name / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(2, meta.get("log_rows"))

    def test_edit_during_exam_still_requires_another_exam(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            name, folder, picks = prepare_versions(root)
            edited = run("dna_update", "edit", "--root", root, "--id", "#001", "--text", "结尾落在新增的具体事实上", "--confirmed")
            self.assertEqual(0, edited.returncode, edited.stderr)
            exam.record(root, name, picks)
            base = json.loads((root / "evals" / "exam" / "baseline.json").read_text(encoding="utf-8"))
            self.assertEqual(0, base.get("log_rows"))
            self.assertNotIn("新增的具体事实", (root / "evals" / "exam" / "baseline-dna.md").read_text(encoding="utf-8"))
            self.assertTrue(any("盲选" in tip for tip in exam.dna_lib.suggest(root)))

    def test_all_neither_has_no_verdict_and_preserves_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            name, folder, picks = prepare_versions(root)
            paths = [root / "evals" / "exam" / file for file in ("baseline.json", "baseline-dna.md")]
            before = [path.read_bytes() for path in paths]
            result = exam.record(root, name, ["都不像"] * 3)
            self.assertEqual("无结论，保持旧基准", result["结论"])
            self.assertEqual(before, [path.read_bytes() for path in paths])
            saved = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            self.assertFalse(saved["passed"])

    def test_second_record_is_rejected_without_changing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            name, folder, picks = prepare_versions(root)
            exam.record(root, name, picks)
            paths = [folder / "result.json", root / "evals" / "exam-log.md", root / "evals" / "exam" / "baseline.json"]
            before = [path.read_bytes() for path in paths]
            with self.assertRaisesRegex(SystemExit, "这个考场已交过卷"):
                exam.record(root, name, ["都不像"] * 3)
            self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_rebase_saves_current_log_row_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            append_log_row(root)
            append_log_row(root, "休眠")
            exam.rebase(root, "作者确认接受当前偏好")
            base = json.loads((root / "evals" / "exam" / "baseline.json").read_text(encoding="utf-8"))
            self.assertEqual(2, base.get("log_rows"))
            self.assertFalse(any("盲选" in tip for tip in exam.dna_lib.suggest(root)))

    def test_old_baseline_without_log_rows_still_prepares_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            append_log_row(root)
            name, folder, picks = prepare_versions(root)
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(meta["changes"]))
            result = exam.record(root, name, picks)
            self.assertEqual(3, result["新版赢"])

    def test_prepare_record_and_rebase_mutations_hold_the_shared_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            make_exam(root)
            held = False
            entries = []

            @contextmanager
            def tracked_lock(actual_root):
                nonlocal held
                self.assertEqual(root, actual_root)
                self.assertFalse(held)
                held = True
                entries.append(actual_root)
                try:
                    yield
                finally:
                    held = False

            copy = exam.shutil.copy2
            write_all = exam.dna_lib.write_all

            def checked_copy(*args, **kwargs):
                self.assertTrue(held, "考试快照必须和日志游标在同一个锁内读取")
                return copy(*args, **kwargs)

            def checked_write(changes):
                self.assertTrue(held, "考试结果与基准写入必须持有共用锁")
                return write_all(changes)

            with patch.object(exam.dna_lib, "lock", tracked_lock), patch.object(exam.shutil, "copy2", checked_copy), patch.object(exam.dna_lib, "write_all", checked_write):
                name = exam.prepare(root)["run"]
                write_all_versions(root, name)
                exam.shuffle(root, name, seed=1)
                exam.record(root, name, ["都不像"] * 3)
                exam.rebase(root, "作者确认接受当前偏好")
            self.assertEqual(4, len(entries))


if __name__ == "__main__":
    unittest.main()
