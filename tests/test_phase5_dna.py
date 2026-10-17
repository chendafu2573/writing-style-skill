import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import load, make_root, payload, run, save_draft, stats

finalize = load("finalize")
dna_update = load("dna_update")
dna_lib = dna_update.dna_lib


def update(root, *args):
    return run("dna_update", args[0], "--root", root, *args[1:])


def confirm(root, topic, source="A", at=None):
    result = finalize.finalize(root, payload(topic, input_source=source, review=[
        {"id": "#002", "tag": "印证", "basis": "我试了三次"}]))
    if at:
        path = root / "assets" / "review-log.md"
        text = path.read_text(encoding="utf-8")
        text = re.sub(rf"(?m)^\| [^|]+ \| {re.escape(result['filename'])} \|", f"| {at} | {result['filename']} |", text)
        path.write_text(text, encoding="utf-8")
    return result["filename"]


def sleep_at(root, at="2026-10-18 10:00"):
    result = update(root, "status", "--id", "#002", "--to", "休眠", "--confirmed")
    if result.returncode:
        raise AssertionError(result.stderr)
    path = root / "assets" / "diff-log.md"
    text = re.sub(r"(?m)^\| [^|]+ \| 休眠 \|", f"| {at} | 休眠 |", path.read_text(encoding="utf-8"))
    path.write_text(text, encoding="utf-8")


def wake_tips(root):
    return [tip for tip in dna_lib.suggest(root) if "是否唤醒" in tip]


def counted_sample(root):
    return finalize.finalize(root, payload(draft_id=save_draft(root),
        final="我试了三次。\n车还在，灯也还在，车没动。\n补一句作者话。"))["filename"]


class Phase5IndexAndLogTests(unittest.TestCase):
    def test_audit_rejects_two_active_files_with_same_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            finalize.finalize(root, payload("第一篇"))
            second = finalize.finalize(root, payload("第二篇"))["filename"]
            index = root / "assets" / "style-index.md"
            index.write_text(index.read_text(encoding="utf-8").replace("| 第二篇 |", "| 第一篇 |"), encoding="utf-8")
            artifact = root / "assets" / second
            artifact.write_text(artifact.read_text(encoding="utf-8").replace("| 身份键 | 第二篇 |", "| 身份键 | 第一篇 |"), encoding="utf-8")
            result = run("audit_skill", "--root", root)
            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("第一篇", result.stdout)
            self.assertIn("active", result.stdout)

    def test_audit_rejects_duplicate_active_index_row_for_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            filename = finalize.finalize(root, payload("重复登记"))["filename"]
            index = root / "assets" / "style-index.md"
            text = index.read_text(encoding="utf-8")
            row = next(line for line in text.splitlines() if f"| {filename} |" in line)
            index.write_text(text + row + "\n", encoding="utf-8")
            result = run("audit_skill", "--root", root)
            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("重复登记", result.stdout)
            self.assertIn("active", result.stdout)

    def test_log_rows_and_cursor_keep_same_minute_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = root / "assets" / "diff-log.md"
            path.write_text(path.read_text(encoding="utf-8") +
                "| 2026-10-18 10:00 | 修改 | — | — | #001 第一改 | 1 |\n"
                "| 2026-10-18 10:00 | 修改 | — | — | #002 第二改 | 1 |\n"
                "| 非数据 | 修改 | — | — | 忽略 | 1 |\n", encoding="utf-8")
            self.assertEqual(2, len(dna_lib.log_data_rows(root)))
            self.assertEqual(["2026-10-18 10:00 修改 #002 第二改"], dna_lib.changes_since(root,
                {"since": "2026-10-18 10:00", "log_rows": 1}))

    def test_changes_since_supports_old_baseline_dictionary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            path = root / "assets" / "diff-log.md"
            path.write_text(path.read_text(encoding="utf-8") +
                "| 2026-10-18 10:00 | 修改 | — | — | #001 旧改 | 1 |\n"
                "| 2026-10-18 10:01 | 修改 | — | — | #002 新改 | 1 |\n", encoding="utf-8")
            self.assertEqual(["2026-10-18 10:01 修改 #002 新改"],
                dna_lib.changes_since(root, {"since": "2026-10-18 10:00"}))

    def test_suggest_uses_baseline_row_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            folder = root / "evals" / "exam"
            folder.mkdir()
            (folder / "baseline.json").write_text(json.dumps({"since": "2026-10-18 10:00", "log_rows": 0}), encoding="utf-8")
            path = root / "assets" / "diff-log.md"
            path.write_text(path.read_text(encoding="utf-8") +
                "| 2026-10-18 10:00 | 修改 | — | — | #001 同分钟改动 | 1 |\n", encoding="utf-8")
            self.assertTrue(any("盲选" in tip for tip in dna_lib.suggest(root)))


class Phase5BackupTests(unittest.TestCase):
    def test_failed_write_marks_backup_and_cannot_rollback_over_later_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            args = SimpleNamespace(confirmed=True, id="#001", text="结尾停在具体结果上", type=None, scene=None)
            with patch.object(dna_lib, "write_all", side_effect=OSError("模拟写失败")):
                with self.assertRaises(OSError):
                    dna_update.cmd_edit(root, args)
            self.assertEqual(1, len(list((root / "backups").glob("*.failed"))))
            self.assertIsNone(dna_lib.latest_backup(root))
            counted_sample(root)
            before = {path: path.read_bytes() for path in dna_update._tracked(root)}
            with self.assertRaisesRegex(SystemExit, "没有可撤回"):
                dna_update.cmd_rollback(root, SimpleNamespace(confirmed=True))
            self.assertEqual(before, {path: path.read_bytes() for path in dna_update._tracked(root)})

    def test_latest_backup_skips_failed_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            folder = root / "backups"
            for name in ("001", "002.restored", "003.failed"):
                (folder / name).mkdir(parents=True)
            self.assertEqual(folder / "001", dna_lib.latest_backup(root))

    def test_rollback_refuses_missing_completion_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            dna_lib.backup(root, dna_update._tracked(root))
            with self.assertRaisesRegex(SystemExit, "这份备份没有写入完成标记，不能自动撤回"):
                dna_update.cmd_rollback(root, SimpleNamespace(confirmed=True))

    def test_completion_marker_uses_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            args = SimpleNamespace(confirmed=True, id="#001", text="结尾停在具体结果上", type=None, scene=None)
            with patch.object(dna_lib, "atomic_write", wraps=dna_lib.atomic_write) as write:
                dna_update.cmd_edit(root, args)
            self.assertTrue(any(call.args[0].name == "after.json" for call in write.call_args_list))


class Phase5WakeTests(unittest.TestCase):
    def test_same_day_confirmations_before_sleep_do_not_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            for i in (1, 2):
                confirm(root, f"休眠前{i}", at="2026-10-18 09:59")
            sleep_at(root)
            self.assertFalse(wake_tips(root))

    def test_mixed_confirmations_after_sleep_do_not_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            sleep_at(root)
            for i in (1, 2):
                confirm(root, f"混合{i}", source="D", at="2026-10-18 10:01")
            self.assertFalse(wake_tips(root))

    def test_two_distinct_eligible_confirmations_after_sleep_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            sleep_at(root)
            for i in (1, 2):
                confirm(root, f"合格{i}", at="2026-10-18 10:01")
            self.assertEqual(1, len(wake_tips(root)))

    def test_duplicate_confirmation_for_one_sample_does_not_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            sleep_at(root)
            sample = confirm(root, "只一篇", at="2026-10-18 10:01")
            path = root / "assets" / "review-log.md"
            path.write_text(path.read_text(encoding="utf-8") +
                dna_lib.review_row("2026-10-18 10:02", sample, "#002", "印证", "我试了三次") + "\n", encoding="utf-8")
            self.assertFalse(wake_tips(root))

    def test_legacy_sleep_date_excludes_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            dna = root / "reference" / "dna.md"
            head, prefs = dna_lib.split_ledger(dna.read_text(encoding="utf-8"))
            prefs[1].fields["状态"] = "休眠"
            prefs[1].fields["状态日期"] = "2026-10-18"
            dna.write_text(dna_lib.render_ledger(head, prefs), encoding="utf-8")
            for i in (1, 2):
                confirm(root, f"旧格式同日{i}", at="2026-10-18 20:00")
            self.assertFalse(wake_tips(root))


class Phase5ArchiveTests(unittest.TestCase):
    def test_reject_and_restore_recompute_archived_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            filename = counted_sample(root)
            self.assertEqual(0, update(root, "status", "--id", "#001", "--to", "淘汰", "--confirmed").returncode)
            rejected = run("reject_sample", "--root", root, "--artifact", root / "assets" / filename, "--reason", "不像本人")
            self.assertEqual(0, rejected.returncode, rejected.stderr)
            archive = (root / "reference" / "dna-archive.md").read_text(encoding="utf-8")
            self.assertIn("- 统计: 用上 0 / 守住 0 / 被改回 0 / 印证 0", archive)
            self.assertEqual(0, update(root, "status", "--id", "#001", "--to", "生效", "--confirmed").returncode)
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 0", stats(root, "#001"))
            checked = run("audit_skill", "--root", root)
            self.assertEqual(0, checked.returncode, checked.stdout)

    def test_commit_recomputes_stale_archived_stats_before_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            counted_sample(root)
            update(root, "status", "--id", "#001", "--to", "淘汰", "--confirmed")
            review = root / "assets" / "review-log.md"
            review.write_text(review.read_text(encoding="utf-8").replace("| 有效 |", "| 作废 |"), encoding="utf-8")
            result = update(root, "status", "--id", "#001", "--to", "生效", "--confirmed")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 0", stats(root, "#001"))

    def test_recompute_updates_archived_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            counted_sample(root)
            update(root, "status", "--id", "#001", "--to", "淘汰", "--confirmed")
            review = root / "assets" / "review-log.md"
            review.write_text(review.read_text(encoding="utf-8").replace("| 有效 |", "| 作废 |"), encoding="utf-8")
            self.assertEqual(0, update(root, "recompute").returncode)
            archive = (root / "reference" / "dna-archive.md").read_text(encoding="utf-8")
            self.assertIn("- 统计: 用上 0 / 守住 0 / 被改回 0 / 印证 0", archive)

    def test_audit_checks_archived_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            counted_sample(root)
            update(root, "status", "--id", "#001", "--to", "淘汰", "--confirmed")
            archive = root / "reference" / "dna-archive.md"
            archive.write_text(archive.read_text(encoding="utf-8").replace("用上 1", "用上 7"), encoding="utf-8")
            result = run("audit_skill", "--root", root)
            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("#001统计与复盘记录不一致", result.stdout)


if __name__ == "__main__":
    unittest.main()
