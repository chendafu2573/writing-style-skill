import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fixtures import make_root
from test_exam import exam, make_exam, write_all_versions


PENDING_MESSAGE = "发现未完成的定稿存档记录（assets/.finalize-journal.json）。先运行 python3 scripts/finalize.py --recover 恢复，再继续。"
CHANGED_MESSAGE = "考卷正文在洗牌后被改过，这次成绩不能用"
STALE_MESSAGE = "建考场之后基准已被其他考场或 rebase 改过，这个考场过期了，请重新建考场"


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class Phase6ExamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = make_root(tmp.name)
        make_exam(self.root)

    def prepare_run(self, name="run"):
        created = exam.prepare(self.root)["run"]
        folder = self.root / "evals" / "exam-runs" / created
        target = folder.with_name(name)
        folder.rename(target)
        return name, target

    def ready_run(self, name="run"):
        name, folder = self.prepare_run(name)
        write_all_versions(self.root, name)
        exam.shuffle(self.root, name, seed=1)
        return name, folder

    def labels(self, folder, side="new"):
        key = json.loads((folder / "key.json").read_text(encoding="utf-8"))
        return [next(label for label, value in key[f"paper-0{i}.md"].items() if value == side) for i in (1, 2, 3)]

    def reverse_global_manifest(self):
        path = self.root / "evals" / "exam" / "manifest.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(list(reversed(items)), ensure_ascii=False), encoding="utf-8")

    def test_prepare_freezes_manifest(self):
        name, folder = self.prepare_run()
        source = self.root / "evals" / "exam"
        self.assertEqual((source / "manifest.json").read_bytes(), (folder / "manifest.json").read_bytes())

    def test_prepare_freezes_papers(self):
        name, folder = self.prepare_run()
        source = self.root / "evals" / "exam"
        for item in exam.manifest(self.root):
            self.assertEqual((source / item["paper"]).read_bytes(), (folder / "papers" / item["paper"]).read_bytes())

    def test_prepare_captures_baseline_hash(self):
        name, folder = self.prepare_run()
        source = self.root / "evals" / "exam"
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256((source / "baseline-dna.md").read_bytes()).hexdigest(), meta.get("baseline_hash"))

    def test_prepare_can_create_two_runs_in_quick_succession(self):
        first = exam.prepare(self.root)["run"]
        second = exam.prepare(self.root)["run"]
        self.assertNotEqual(first, second)

    def test_task_text_uses_frozen_paper_after_global_paper_changes(self):
        name, folder = self.prepare_run()
        item = exam.manifest(self.root)[0]
        source = self.root / "evals" / "exam" / item["paper"]
        original = source.read_text(encoding="utf-8")
        source.write_text("全局题目后来改过。", encoding="utf-8")
        task = exam.task_text(self.root, folder, "new", item)
        self.assertIn(original, task)
        self.assertNotIn("全局题目后来改过。", task)

    def test_shuffle_uses_frozen_manifest_order(self):
        name, folder = self.prepare_run()
        write_all_versions(self.root, name)
        self.reverse_global_manifest()
        result = exam.shuffle(self.root, name, seed=1)
        blind = Path(result["blind"]).read_text(encoding="utf-8")
        self.assertLess(blind.index("## 卷1"), blind.index("## 卷2"))
        self.assertLess(blind.index("## 卷2"), blind.index("## 卷3"))

    def test_record_uses_frozen_manifest_order(self):
        name, folder = self.ready_run()
        self.reverse_global_manifest()
        result = exam.record(self.root, name, self.labels(folder))
        self.assertEqual(3, result["新版赢"])
        self.assertEqual(["卷1", "卷2", "卷3"], [row.split("：", 1)[0] for row in result["每张卷"]])

    def test_second_shuffle_is_rejected_without_any_file_change(self):
        name, folder = self.ready_run()
        before = snapshot(self.root)
        with self.assertRaisesRegex(SystemExit, "已经洗过牌；作者可能已看过盲选页，重洗会让答案对不上"):
            exam.shuffle(self.root, name, seed=2)
        self.assertEqual(before, snapshot(self.root))

    def test_completed_run_cannot_shuffle_again(self):
        name, folder = self.ready_run()
        exam.record(self.root, name, ["都不像"] * 3)
        before = snapshot(self.root)
        with self.assertRaisesRegex(SystemExit, "这个考场已交过卷，不能再洗牌"):
            exam.shuffle(self.root, name, seed=2)
        self.assertEqual(before, snapshot(self.root))

    def test_shuffle_hashes_all_bodies_and_blind_without_counting_hashes_as_a_paper(self):
        name, folder = self.prepare_run()
        write_all_versions(self.root, name)
        result = exam.shuffle(self.root, name, seed=1)
        key = json.loads((folder / "key.json").read_text(encoding="utf-8"))
        files = [folder / f"{side}-paper-0{i}.md" for side in ("old", "new") for i in (1, 2, 3)]
        files.append(folder / "blind.md")
        self.assertEqual({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}, key.get("_hashes"))
        self.assertEqual(3, result["papers"])

    def test_record_rejects_changed_old_or_new_body_without_any_file_change(self):
        for side in ("old", "new"):
            with self.subTest(side=side):
                name, folder = self.ready_run(side)
                picks = self.labels(folder)
                body = folder / f"{side}-paper-01.md"
                original = body.read_bytes()
                body.write_bytes(original + b"\nchanged")
                before = snapshot(self.root)
                with self.assertRaisesRegex(SystemExit, CHANGED_MESSAGE):
                    exam.record(self.root, name, picks)
                self.assertEqual(before, snapshot(self.root))
                body.write_bytes(original)

    def test_record_rejects_changed_blind_page_without_any_file_change(self):
        name, folder = self.ready_run()
        blind = folder / "blind.md"
        blind.write_text(blind.read_text(encoding="utf-8") + "\n后来改过。", encoding="utf-8")
        before = snapshot(self.root)
        with self.assertRaisesRegex(SystemExit, CHANGED_MESSAGE):
            exam.record(self.root, name, self.labels(folder))
        self.assertEqual(before, snapshot(self.root))

    def test_record_rejects_missing_body(self):
        name, folder = self.ready_run()
        (folder / "new-paper-01.md").unlink()
        before = snapshot(self.root)
        with self.assertRaisesRegex(SystemExit, CHANGED_MESSAGE):
            exam.record(self.root, name, self.labels(folder))
        self.assertEqual(before, snapshot(self.root))

    def test_second_run_cannot_overwrite_baseline_after_first_run_passes(self):
        first, first_folder = self.ready_run("first")
        second, second_folder = self.ready_run("second")
        exam.record(self.root, first, self.labels(first_folder))
        before = snapshot(self.root)
        with self.assertRaisesRegex(SystemExit, STALE_MESSAGE):
            exam.record(self.root, second, self.labels(second_folder))
        self.assertEqual(before, snapshot(self.root))

    def test_record_rejects_run_after_rebase_changes_baseline(self):
        name, folder = self.ready_run()
        exam.rebase(self.root, "作者确认设新基准")
        before = snapshot(self.root)
        with self.assertRaisesRegex(SystemExit, STALE_MESSAGE):
            exam.record(self.root, name, self.labels(folder))
        self.assertEqual(before, snapshot(self.root))

    def test_legacy_run_without_frozen_files_or_hash_fields_still_shuffles_and_records(self):
        name = "legacy"
        folder = self.root / "evals" / "exam-runs" / name
        folder.mkdir(parents=True)
        (folder / "old-dna.md").write_text("# 旧 DNA\n", encoding="utf-8")
        (folder / "new-dna.md").write_text("# 旧考场的新 DNA\n", encoding="utf-8")
        (folder / "meta.json").write_text(json.dumps({"baseline_since": "2026-01-01 00:00", "changes": [], "log_rows": 0}), encoding="utf-8")
        write_all_versions(self.root, name)
        self.assertEqual(3, exam.shuffle(self.root, name, seed=1)["papers"])
        key_file = folder / "key.json"
        key = json.loads(key_file.read_text(encoding="utf-8"))
        key.pop("_hashes", None)
        key_file.write_text(json.dumps(key, ensure_ascii=False), encoding="utf-8")
        (folder / "new-paper-01.md").write_text("旧考场没有正文校验。", encoding="utf-8")
        (self.root / "evals" / "exam" / "baseline-dna.md").write_text("旧考场没有基准校验。", encoding="utf-8")
        result = exam.record(self.root, name, self.labels(folder))
        self.assertEqual(3, result["新版赢"])
        self.assertIn("保留", result["结论"])

    def test_pending_finalize_blocks_all_exam_writes_without_any_file_change(self):
        name, folder = self.ready_run()
        picks = self.labels(folder)
        created = self.root / "assets" / "partial.md"
        created.write_text("中断前新建的文件。", encoding="utf-8")
        journal = self.root / "assets" / ".finalize-journal.json"
        journal.write_text(json.dumps({"phase": "applying", "before": {"assets/state.md": "旧内容"}, "created": ["assets/partial.md"]}), encoding="utf-8")
        actions = {
            "prepare": lambda: exam.prepare(self.root),
            "shuffle": lambda: exam.shuffle(self.root, name),
            "record": lambda: exam.record(self.root, name, picks),
            "rebase": lambda: exam.rebase(self.root, "作者确认"),
        }
        before = snapshot(self.root)
        for action, call in actions.items():
            with self.subTest(action=action):
                with self.assertRaises(SystemExit) as raised:
                    call()
                self.assertEqual(PENDING_MESSAGE, str(raised.exception))
                self.assertEqual(before, snapshot(self.root))

    def test_all_exam_writes_guard_under_lock_before_reading_any_state(self):
        actions = {
            "prepare": lambda: exam.prepare(self.root),
            "shuffle": lambda: exam.shuffle(self.root, "absent"),
            "record": lambda: exam.record(self.root, "absent", ["甲"] * 3),
            "rebase": lambda: exam.rebase(self.root, "作者确认"),
        }
        for action, call in actions.items():
            with self.subTest(action=action):
                held = False

                @contextmanager
                def checked_lock(root):
                    nonlocal held
                    self.assertEqual(self.root, root)
                    held = True
                    try:
                        yield
                    finally:
                        held = False

                def guard(root):
                    self.assertTrue(held, "先拿共用锁，再检查未完成存档")
                    self.assertEqual(self.root, root)
                    raise SystemExit(PENDING_MESSAGE)

                with patch.object(exam.dna_lib, "lock", checked_lock), patch.object(exam.dna_lib, "require_no_pending_finalize", guard, create=True), patch.object(Path, "read_text", side_effect=AssertionError("保护前读取了状态")), patch.object(Path, "read_bytes", side_effect=AssertionError("保护前读取了状态")), patch.object(Path, "exists", side_effect=AssertionError("保护前读取了状态")):
                    with self.assertRaises(SystemExit) as raised:
                        call()
                    self.assertEqual(PENDING_MESSAGE, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
