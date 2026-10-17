import base64
import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fixtures import load, make_root, payload, run, save_draft, stats

finalize = load("finalize")
PENDING_MESSAGE = (
    "发现未完成的定稿存档记录（assets/.finalize-journal.json）。"
    "先运行 python3 scripts/finalize.py --recover 恢复，再继续。"
)
IDENTITY_MESSAGE = "identity不能包含竖线或换行（会破坏样本索引表格）"


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def pending(root, old_path, old_content, created, *, phase="applying"):
    journal = root / "assets" / ".finalize-journal.json"
    journal.write_text(json.dumps({
        "phase": phase,
        "before": {str(old_path): base64.b64encode(old_content).decode()},
        "created": [str(created)],
    }), encoding="utf-8")
    return journal


class Phase6RecoveryTests(unittest.TestCase):
    def test_recover_without_payload_restores_bytes_and_deletes_created_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 恢复入口只需要记录，其他账本暂时缺失也必须能救回来。
            root = Path(tmp)
            (root / "assets").mkdir()
            old = root / "assets" / "old.md"
            original = b"old content\n\x00\xff"
            old.write_bytes(b"partial write")
            created = root / "assets" / "created.md"
            created.write_text("写了一半的新文件", encoding="utf-8")
            journal = pending(root, old, original, created)
            completed = run("finalize", "--root", root, "--recover")
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual({"recovered": True}, json.loads(completed.stdout))
            self.assertEqual(original, old.read_bytes())
            self.assertFalse(created.exists())
            self.assertFalse(journal.exists())

    def test_recover_without_journal_reports_false_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            before = snapshot(root)
            completed = run("finalize", "--root", root, "--recover")
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual({"recovered": False}, json.loads(completed.stdout))
            self.assertEqual(before, snapshot(root))

    def test_recover_keeps_committed_content_and_only_removes_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            old = root / "assets" / "state.md"
            before = old.read_bytes()
            old.write_bytes(b"committed content")
            created = root / "assets" / "created.md"
            created.write_text("已提交的新文件", encoding="utf-8")
            journal = pending(root, old, before, created, phase="committed")
            completed = run("finalize", "--root", root, "--recover")
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual({"recovered": True}, json.loads(completed.stdout))
            self.assertEqual(b"committed content", old.read_bytes())
            self.assertTrue(created.exists())
            self.assertFalse(journal.exists())

    def test_recover_calls_existing_recovery_inside_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            held = []

            @contextmanager
            def checked_lock(actual_root):
                self.assertEqual(root, actual_root)
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()

            def checked_recover(journal):
                self.assertEqual([True], held)
                self.assertEqual(root / "assets" / ".finalize-journal.json", journal)
                return False

            output = io.StringIO()
            with patch.object(finalize, "_lock", checked_lock), patch.object(finalize, "_recover", checked_recover), \
                    patch("sys.argv", ["finalize.py", "--root", str(root), "--recover"]), redirect_stdout(output):
                self.assertEqual(0, finalize.main())
            self.assertEqual({"recovered": False}, json.loads(output.getvalue()))
            self.assertEqual([], held)

    def test_recover_cannot_be_combined_with_payload_or_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            data = root / "payload.json"
            data.write_text(json.dumps(payload()), encoding="utf-8")
            for extra in (("--payload", data), ("--dry-run",)):
                with self.subTest(extra=extra):
                    before = snapshot(root)
                    completed = run("finalize", "--root", root, "--recover", *extra)
                    self.assertEqual(2, completed.returncode, completed.stderr)
                    self.assertEqual(before, snapshot(root))

    def test_dry_run_blocks_pending_recovery_before_reading_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            old = root / "assets" / "state.md"
            created = root / "assets" / "created.md"
            created.write_text("未完成的新样本", encoding="utf-8")
            pending(root, old, old.read_bytes(), created)
            before = snapshot(root)
            with patch.object(finalize, "_resolve", side_effect=AssertionError("不能读未恢复的草稿")) as resolve:
                with self.assertRaises(SystemExit) as raised:
                    finalize.finalize(root, payload(draft_id="不存在"), dry_run=True)
            self.assertEqual(PENDING_MESSAGE, str(raised.exception))
            resolve.assert_not_called()
            self.assertEqual(before, snapshot(root))

    def test_dry_run_checks_pending_record_after_taking_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            old = root / "assets" / "state.md"
            created = root / "assets" / "created.md"
            before = None

            @contextmanager
            def pending_at_lock(_root):
                nonlocal before
                created.write_text("另一个定稿刚中断", encoding="utf-8")
                pending(root, old, old.read_bytes(), created)
                before = snapshot(root)
                yield

            with patch.object(finalize, "_lock", pending_at_lock):
                with self.assertRaises(SystemExit) as raised:
                    finalize.finalize(root, payload(), dry_run=True)
            self.assertEqual(PENDING_MESSAGE, str(raised.exception))
            self.assertEqual(before, snapshot(root))

    def test_formal_finalize_still_recovers_before_reading_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            old = root / "assets" / "state.md"
            original = old.read_bytes()
            old.write_text("未完成的状态写入", encoding="utf-8")
            created = root / "assets" / "created.md"
            created.write_text("未完成的新样本", encoding="utf-8")
            journal = pending(root, old, original, created)
            result = finalize.finalize(root, payload("恢复后的定稿"))
            self.assertFalse(journal.exists())
            self.assertFalse(created.exists())
            self.assertTrue((root / "assets" / result["filename"]).exists())
            self.assertIn("polish_count_total: 3", old.read_text(encoding="utf-8"))

    def test_interrupted_b_blocks_reject_a_until_recovery_then_c_keeps_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            first = finalize.finalize(root, payload("样本A", draft_id=save_draft(root)))
            artifact = root / "assets" / first["filename"]
            before_b = snapshot(root)
            atomic_write = finalize._atomic_write

            class SimulatedCrash(BaseException):
                pass

            def interrupt_after_index(path, content):
                atomic_write(path, content)
                if path.name == "style-index.md":
                    # 绕过普通异常回滚，保留真实中断会留下的记录与半成品。
                    raise SimulatedCrash()

            with patch.object(finalize, "_atomic_write", interrupt_after_index):
                with self.assertRaises(SimulatedCrash):
                    finalize.finalize(root, payload("样本B"))
            journal = root / "assets" / ".finalize-journal.json"
            record = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual("applying", record["phase"])
            self.assertTrue(all(Path(path).exists() for path in record["created"]))
            interrupted = snapshot(root)
            blocked = run("reject_sample", "--root", root, "--artifact", artifact, "--reason", "不像作者")
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn(PENDING_MESSAGE, blocked.stderr)
            self.assertEqual(interrupted, snapshot(root))

            recovered = run("finalize", "--root", root, "--recover")
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            self.assertEqual({"recovered": True}, json.loads(recovered.stdout))
            self.assertEqual(before_b, snapshot(root))
            rejected = run("reject_sample", "--root", root, "--artifact", artifact, "--reason", "不像作者")
            self.assertEqual(0, rejected.returncode, rejected.stderr)
            self.assertEqual("用上 0 / 守住 0 / 被改回 0 / 印证 0", stats(root, "#001"))
            rejection = (root / "assets" / "rejection-log.md").read_bytes()
            finalize.finalize(root, payload("样本C"))
            self.assertIn("| 资产状态 | rejected |", artifact.read_text(encoding="utf-8"))
            self.assertEqual(rejection, (root / "assets" / "rejection-log.md").read_bytes())
            self.assertFalse(journal.exists())
            audit = run("audit_skill", "--root", root)
            self.assertEqual(0, audit.returncode, audit.stdout)


class Phase6IdentityTests(unittest.TestCase):
    def test_identity_rejects_pipe_lf_and_cr_without_writing(self):
        for identity in ("坏|身份", "坏\n身份", "坏\r身份"):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as tmp:
                root = make_root(tmp)
                before = snapshot(root)
                with self.assertRaises(ValueError) as raised:
                    finalize.finalize(root, payload(identity=identity))
                self.assertEqual(IDENTITY_MESSAGE, str(raised.exception))
                self.assertEqual(before, snapshot(root))

    def test_pipe_and_newline_in_other_text_fields_are_still_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            topic = "带|竖线的主题"
            final = "正文里可以写 A|B。\n也可以换一行。"
            result = finalize.finalize(root, payload(topic, identity="正常身份", final=final,
                source_summary="来源|说明\n第二行", sources=["来源|条目\n附加说明"]))
            artifact = (root / "assets" / result["filename"]).read_text(encoding="utf-8")
            self.assertIn("# " + topic, artifact)
            self.assertIn(final, artifact)
            self.assertIn("| 素材来源 | 来源\\|说明<br>第二行 |", artifact)
            audit = run("audit_skill", "--root", root)
            self.assertEqual(0, audit.returncode, audit.stdout)


if __name__ == "__main__":
    unittest.main()
