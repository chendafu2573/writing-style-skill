import base64
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fixtures import FIRST, load, make_root, payload, run, save_draft

dna_update = load("dna_update")
dna_lib = dna_update.dna_lib
draft = load("draft")
finalize = load("finalize")
reject_sample = load("reject_sample")

PENDING_MESSAGE = "发现未完成的定稿存档记录（assets/.finalize-journal.json）。先运行 python3 scripts/finalize.py --recover 恢复，再继续。"


def snapshot(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def pending_finalize(root):
    before = root / "assets" / "diff-log.md"
    created = root / "assets" / "interrupted-v1.md"
    created.write_text("只写了一半的新样本\n", encoding="utf-8")
    journal = root / "assets" / ".finalize-journal.json"
    journal.write_text(json.dumps({
        "phase": "applying",
        "before": {str(before): base64.b64encode(b"old log\n").decode("ascii")},
        "created": [str(created)],
    }), encoding="utf-8")
    return journal


class Phase6PendingFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(self.tmp.name)
        self.draft_id = save_draft(self.root)
        self.filename = finalize.finalize(self.root, payload(draft_id=self.draft_id))["filename"]
        self.open_draft = save_draft(self.root, topic="尚未定稿")
        dna_update.cmd_edit(self.root, SimpleNamespace(
            confirmed=True, id="#001", text="结尾停在具体事实上", type=None, scene=None))

    def reject(self):
        with patch("sys.argv", ["reject_sample.py", "--root", str(self.root), "--artifact",
                str(self.root / "assets" / self.filename), "--reason", "不像本人"]), redirect_stdout(StringIO()):
            return reject_sample.main()

    def operations(self):
        return {
            "reject": self.reject,
            "add": lambda: dna_update.cmd_add(self.root, SimpleNamespace(confirmed=True,
                status="候选", text="保留亲身经历", type="判断", scene="全局", source="用户口头指令", evidence=[])),
            "edit": lambda: dna_update.cmd_edit(self.root, SimpleNamespace(confirmed=True,
                id="#001", text="结尾停在最后结果上", type=None, scene=None)),
            "status": lambda: dna_update.cmd_status(self.root, SimpleNamespace(confirmed=True,
                id="#001", to="休眠", reason="作者确认")),
            "recompute": lambda: dna_update.cmd_recompute(self.root, None),
            "rollback": lambda: dna_update.cmd_rollback(self.root, SimpleNamespace(confirmed=True)),
            "note": lambda: draft.note(self.root, self.open_draft, {"todo": "补充交接"}),
            "revise": lambda: draft.revise(self.root, self.open_draft, FIRST + "\n新的一版。"),
            "expire": lambda: draft.expire(self.root, -1),
        }

    def assert_pending_blocks(self, action):
        pending_finalize(self.root)
        before = snapshot(self.root)
        # 阻断前连状态正文也不能读，避免使用半写入的数据。
        with patch.object(Path, "read_text", side_effect=AssertionError("阻断前读取了状态文件")):
            with self.assertRaises(SystemExit) as caught:
                self.operations()[action]()
        self.assertEqual(PENDING_MESSAGE, str(caught.exception))
        self.assertEqual(before, snapshot(self.root))

    def test_guard_reports_exact_recovery_instruction(self):
        pending_finalize(self.root)
        with self.assertRaises(SystemExit) as caught:
            dna_lib.require_no_pending_finalize(self.root)
        self.assertEqual(PENDING_MESSAGE, str(caught.exception))

    def test_reject_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("reject")

    def test_add_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("add")

    def test_edit_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("edit")

    def test_status_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("status")

    def test_recompute_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("recompute")

    def test_rollback_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("rollback")

    def test_note_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("note")

    def test_revise_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("revise")

    def test_expire_blocks_pending_without_reading_or_changing_files(self):
        self.assert_pending_blocks("expire")

    def test_every_guard_checks_after_taking_lock(self):
        for action, operation in self.operations().items():
            with self.subTest(action=action):
                entered = []

                @contextmanager
                def lock_with_pending(_root):
                    entered.append(True)
                    pending_finalize(self.root)
                    yield

                with patch.object(dna_lib, "lock", lock_with_pending):
                    with self.assertRaises(SystemExit) as caught:
                        operation()
                self.assertEqual([True], entered)
                self.assertEqual(PENDING_MESSAGE, str(caught.exception))
                (self.root / "assets" / ".finalize-journal.json").unlink()

    def test_expire_holds_lock_while_reading_and_moving_drafts(self):
        held = []
        original_load = dna_lib.load_drafts
        original_replace = Path.replace

        @contextmanager
        def tracked_lock(_root):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def locked_load(root):
            self.assertTrue(held, "expire读取初稿时没有拿锁")
            return original_load(root)

        def locked_move(source, target):
            self.assertTrue(held, "expire移动初稿时没有拿锁")
            return original_replace(source, target)

        with patch.object(dna_lib, "lock", tracked_lock), patch.object(dna_lib, "load_drafts", locked_load), \
                patch.object(Path, "replace", locked_move):
            self.assertEqual([self.open_draft], draft.expire(self.root, -1))

    def test_reject_reads_asset_after_taking_lock(self):
        artifact = self.root / "assets" / self.filename

        @contextmanager
        def lock_after_rejection(_root):
            artifact.write_text(artifact.read_text(encoding="utf-8").replace(
                "| 资产状态 | active |", "| 资产状态 | rejected |"), encoding="utf-8")
            yield

        with patch.object(dna_lib, "lock", lock_after_rejection):
            with self.assertRaisesRegex(SystemExit, "只有active资产可以拒绝，当前状态: rejected"):
                self.reject()


class Phase6AuditSafetyTests(unittest.TestCase):
    def assert_audit_fails(self, root, finding):
        before = snapshot(root)
        checked = run("audit_skill", "--root", root)
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        report = json.loads(checked.stdout)
        self.assertEqual("FAIL", report["status"])
        self.assertIn(finding, report["findings"])
        self.assertEqual(before, snapshot(root))

    def test_audit_reports_pending_finalize_and_keeps_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            pending_finalize(root)
            self.assert_audit_fails(root, "有未完成的定稿存档记录，先运行 finalize.py --recover")

    def test_audit_rejects_malformed_index_rows(self):
        for row in ("| 身份 | 破损-v1.md | agent_draft | active |",
                "| 身份 | 破损-v1.md | agent_draft | 否 | active | 多余列 |",
                "|身份|破损-v1.md|agent_draft|active|"):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                root = make_root(tmp)
                index = root / "assets" / "style-index.md"
                index.write_text(index.read_text(encoding="utf-8") + row + "\n", encoding="utf-8")
                self.assert_audit_fails(root, "样本索引有格式错误的行：" + row[:40])

    def test_audit_rejects_malformed_review_rows(self):
        for row in ("| 2026-10-18 10:00 | 样本.md | #001 | 用上了 | 有效 |",
                "| 2026-10-18 | 样本.md | #001 | 用上了 | 依据 | 有效 | 多余列 |"):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                root = make_root(tmp)
                review = root / "assets" / "review-log.md"
                review.write_text(review.read_text(encoding="utf-8") + row + "\n", encoding="utf-8")
                self.assert_audit_fails(root, "复盘记录有格式错误的行：" + row[:40])


if __name__ == "__main__":
    unittest.main()
