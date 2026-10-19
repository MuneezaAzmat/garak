"""Source provenance checks use tiny real archives and on-disk source trees."""

import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location("source_bootstrap", Path(__file__).parents[1] / "scripts/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "source.tar.gz"

    def tearDown(self):
        self.temp.cleanup()

    def archive_with(self, path, payload=b"original\n", *, symlink=False):
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo(path)
            if symlink:
                member.type = tarfile.SYMTYPE
                member.linkname = "../outside"
                archive.addfile(member)
            else:
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

    def test_traversal_or_links_rejected_before_any_file_is_written(self):
        for path, symlink in (("repo/../../outside", False), ("/outside", False),
                              ("repo/link", True), ("other/file.py", False)):
            with self.subTest(path=path):
                self.archive_with(path, symlink=symlink)
                destination = self.root / "extracted"
                with self.assertRaises(bootstrap.SourceError):
                    bootstrap.safe_extract_archive(self.archive, destination, "repo")
                self.assertFalse(destination.exists())
                self.assertFalse((self.root / "outside").exists())

    def test_altered_or_extra_files_fail_verification_without_overwrite(self):
        self.archive_with("repo/file.py")
        destination = self.root / "source"
        bootstrap.safe_extract_archive(self.archive, destination, "repo")
        digest, files, _ = bootstrap.archive_tree(self.archive, "repo")
        lock = {"archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                "archive_root": "repo", "tree_sha256": digest, "file_count": len(files)}
        bootstrap.verify_source(self.archive, destination, lock)
        (destination / "file.py").write_text("changed\n")
        with self.assertRaises(bootstrap.SourceError):
            bootstrap.verify_source(self.archive, destination, lock)
        self.assertEqual((destination / "file.py").read_text(), "changed\n")
        (destination / "file.py").write_text("original\n")
        (destination / "extra.py").write_text("extra\n")
        with self.assertRaises(bootstrap.SourceError):
            bootstrap.verify_source(self.archive, destination, lock)

    def test_generated_python_cache_does_not_change_source_hash(self):
        self.archive_with("repo/file.py")
        destination = self.root / "source"
        bootstrap.safe_extract_archive(self.archive, destination, "repo")
        digest, _, _ = bootstrap.archive_tree(self.archive, "repo")
        (destination / "__pycache__").mkdir()
        (destination / "__pycache__/file.cpython-313.pyc").write_bytes(b"cache")
        self.assertEqual(bootstrap.tree_digest(destination), digest)

    def test_internal_file_symlink_is_preserved_and_verified(self):
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo("repo/data/version.json")
            member.size = 2
            archive.addfile(member, io.BytesIO(b"{}"))
            link = tarfile.TarInfo("repo/data/current.json")
            link.type = tarfile.SYMTYPE
            link.linkname = "version.json"
            archive.addfile(link)
        destination = self.root / "source"
        bootstrap.safe_extract_archive(self.archive, destination, "repo")
        self.assertTrue((destination / "data/current.json").is_symlink())
        self.assertEqual((destination / "data/current.json").read_text(), "{}")
        digest, _, _ = bootstrap.archive_tree(self.archive, "repo")
        self.assertEqual(bootstrap.tree_digest(destination), digest)


if __name__ == "__main__":
    unittest.main()
