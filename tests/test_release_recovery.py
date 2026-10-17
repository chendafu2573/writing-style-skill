import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fixtures import load, run

finalize = load("finalize")


def snapshot(area):
    return {
        str(path.relative_to(area)): ("link", str(path.readlink())) if path.is_symlink()
        else ("file", path.read_bytes())
        for path in area.rglob("*") if path.is_symlink() or path.is_file()
    }


class ReleaseRecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.area = Path(self.tmp.name).resolve()
        self.root = self.area / "skill"
        self.assets = self.root / "assets"
        self.assets.mkdir(parents=True)
        self.old = self.assets / "state.md"
        self.old.write_bytes(b"keep internal state")
        self.created = self.assets / "created.md"
        self.created.write_bytes(b"keep internal sample")
        self.outside = self.area / "outside"
        self.outside.mkdir()
        self.external = self.outside / "state.md"
        self.external.write_bytes(b"keep external state")
        self.journal = self.assets / ".finalize-journal.json"

    def pending(self, *, before=None, created=None, phase="applying"):
        before = before if before is not None else {str(self.old): b"restore internal state"}
        created = created if created is not None else [str(self.created)]
        self.journal.write_text(json.dumps({
            "phase": phase,
            "before": {str(path): base64.b64encode(content).decode() for path, content in before.items()},
            "created": [str(path) for path in created],
        }), encoding="utf-8")

    def assert_refused_without_changes(self):
        before = snapshot(self.area)
        with self.assertRaisesRegex(ValueError, "当前技能根目录"):
            finalize._recover(self.journal)
        self.assertEqual(before, snapshot(self.area))
        self.assertTrue(self.journal.exists())

    def test_external_before_target_rejects_all_changes(self):
        self.pending(before={str(self.old): b"restore internal state", str(self.external): b"overwrite outside"})
        self.assert_refused_without_changes()

    def test_external_created_target_rejects_before_any_restore(self):
        self.pending(created=[self.created, self.external])
        self.assert_refused_without_changes()

    def test_parent_traversal_cannot_escape_root(self):
        for field in ("before", "created"):
            with self.subTest(field=field):
                escape = Path("..") / "outside" / "state.md"
                if field == "before":
                    self.pending(before={str(self.old): b"restore internal state", str(escape): b"overwrite outside"})
                else:
                    self.pending(created=[self.created, escape])
                self.assert_refused_without_changes()

    def test_symlink_cannot_escape_root(self):
        link = self.assets / "outside-link"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"platform cannot create symlinks: {exc}")
        for field in ("before", "created"):
            with self.subTest(field=field):
                escape = link / self.external.name
                if field == "before":
                    self.pending(before={str(self.old): b"restore internal state", str(escape): b"overwrite outside"})
                else:
                    self.pending(created=[self.created, escape])
                self.assert_refused_without_changes()

    def test_copied_journal_cannot_restore_or_delete_original_skill(self):
        self.pending()
        copied = self.area / "copied-skill"
        shutil.copytree(self.root, copied)
        before = snapshot(self.area)
        completed = run("finalize", "--root", copied, "--recover")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("当前技能根目录", completed.stderr)
        self.assertEqual(before, snapshot(self.area))

    def test_automatic_recovery_rejects_external_targets_before_finalize(self):
        self.pending(created=[self.created, self.external])
        before = snapshot(self.area)
        with self.assertRaisesRegex(ValueError, "当前技能根目录"):
            finalize.finalize(self.root, {})
        self.assertEqual(before, snapshot(self.area))

    def test_invalid_committed_record_is_kept_without_changes(self):
        self.pending(created=[self.created, self.external], phase="committed")
        self.assert_refused_without_changes()


if __name__ == "__main__":
    unittest.main()
