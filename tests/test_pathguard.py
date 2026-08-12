"""tests.test_pathguard：kernel.pathguard 安全基座测试（路径穿越/敏感/备份/原子写）。"""

import os
import unittest
from pathlib import Path

from kernel import pathguard
from kernel.pathguard import PathGuardError


class TestResolveProjectPath(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "a.py").write_text("a", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_happy_relative(self):
        p = pathguard.resolve_project_path(self.project, "src/a.py")
        self.assertEqual(p, (self.project / "src" / "a.py").resolve())

    def test_dot_returns_root(self):
        p = pathguard.resolve_project_path(self.project, ".")
        self.assertEqual(p, self.project.resolve())

    def test_empty_returns_root(self):
        p = pathguard.resolve_project_path(self.project, "")
        self.assertEqual(p, self.project.resolve())

    def test_nonexistent_ok_for_write(self):
        p = pathguard.resolve_project_path(self.project, "new/dir/f.py")
        self.assertEqual(p, (self.project / "new" / "dir" / "f.py").resolve())

    def test_absolute_rejected(self):
        with self.assertRaises(PathGuardError):
            pathguard.resolve_project_path(self.project, "/etc/passwd")

    def test_traversal_rejected(self):
        with self.assertRaises(PathGuardError):
            pathguard.resolve_project_path(self.project, "../../etc/passwd")

    def test_tilde_rejected(self):
        with self.assertRaises(PathGuardError):
            pathguard.resolve_project_path(self.project, "~/x")

    def test_symlink_escape_rejected(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.project / "link.txt"
        link.symlink_to(outside)
        with self.assertRaises(PathGuardError):
            pathguard.resolve_project_path(self.project, "link.txt")

    def test_sensitive_marker_rejected(self):
        (self.project / ".env").write_text("KEY=x", encoding="utf-8")
        with self.assertRaises(PathGuardError):
            pathguard.resolve_project_path(self.project, ".env", ("config.json", ".env"))

    def test_missing_project_dir_rejected(self):
        with self.assertRaises(PathGuardError):
            pathguard.resolve_project_path("", "a.py")
        with self.assertRaises(PathGuardError):
            pathguard.resolve_project_path("/no/such/dir-xyz", "a.py")


class TestBackup(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.backups = Path(self.tmp.name) / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_created(self):
        target = self.project / "a.txt"
        target.write_text("v1", encoding="utf-8")
        backup = pathguard.backup_before_write(self.project, "a.txt", self.backups)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), "v1")
        # 备份不影响原文件
        self.assertEqual(target.read_text(encoding="utf-8"), "v1")

    def test_new_file_no_backup(self):
        self.assertIsNone(
            pathguard.backup_before_write(self.project, "new.txt", self.backups)
        )

    def test_directory_target_rejected(self):
        (self.project / "sub").mkdir()
        with self.assertRaises(PathGuardError):
            pathguard.backup_before_write(self.project, "sub", self.backups)

    def test_nested_relative_path_backup(self):
        (self.project / "pkg").mkdir()
        target = self.project / "pkg" / "mod.py"
        target.write_text("x", encoding="utf-8")
        backup = pathguard.backup_before_write(self.project, "pkg/mod.py", self.backups)
        self.assertTrue(backup.is_file())
        self.assertIn("pkg", backup.parts)

    def test_prune_by_count(self):
        # 时间片按名字排序：制造超限数量，最旧的被清理
        old = pathguard.BACKUP_MAX_COUNT
        try:
            pathguard.BACKUP_MAX_COUNT = 3
            for i in range(5):
                target = self.project / "a.txt"
                target.write_text(f"v{i}", encoding="utf-8")
                pathguard.backup_before_write(self.project, "a.txt", self.backups)
            dirs = sorted(p for p in self.backups.iterdir() if p.is_dir())
            self.assertEqual(len(dirs), 3)
        finally:
            pathguard.BACKUP_MAX_COUNT = old


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "sub" / "f.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_parents_and_writes(self):
        pathguard.atomic_write_text(self.target, "hello")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hello")
        self.assertFalse((Path(self.tmp.name) / "sub" / "f.txt.hb.tmp").exists())

    def test_overwrites(self):
        self.target.parent.mkdir()
        self.target.write_text("old", encoding="utf-8")
        pathguard.atomic_write_text(self.target, "new")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new")

    def test_tmp_cleaned_on_failure(self):
        # 目标是目录 → os.replace 失败 → tmp 被清理
        self.target.mkdir(parents=True)
        with self.assertRaises(PathGuardError):
            pathguard.atomic_write_text(self.target, "x")
        leftovers = [p for p in self.target.parent.iterdir() if p.name.endswith(".hb.tmp")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
