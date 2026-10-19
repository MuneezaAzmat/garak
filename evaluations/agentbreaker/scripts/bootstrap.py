#!/usr/bin/env python3
"""Download pinned sources when missing, or verify all provenance offline.

Tree SHA256 is the SHA256 of UTF-8 JSON for sorted [relative POSIX path,
file-content SHA256] pairs, using ensure_ascii=False and separators=(",", ":").
For symlinks, content SHA256 hashes b"symlink\\0" + the UTF-8 link target.
Python bytecode and __pycache__ directories are excluded; other changes fail.
No dependency installation or upstream code execution occurs here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
TREE_HASH_ALGORITHM = "sha256(sorted-json-[posix-path,file-sha256])-v1"


class SourceError(Exception):
    """Source content or provenance could not be verified."""


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _ignored(path: PurePosixPath) -> bool:
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}


def _digest(files: dict[str, str]) -> str:
    records = sorted(files.items())
    canonical = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _link_digest(target: str) -> str:
    return hashlib.sha256(b"symlink\0" + target.encode("utf-8")).hexdigest()


def _members(archive: tarfile.TarFile, archive_root: str):
    if not archive_root or PurePosixPath(archive_root).name != archive_root:
        raise SourceError("Invalid archive root in manifest")
    members, seen, expanded_bytes = [], set(), 0
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (path.is_absolute() or ".." in path.parts or "\\" in member.name or
                not path.parts or path.parts[0] != archive_root):
            raise SourceError("Unsafe archive member path")
        if not member.isfile() and not member.isdir() and not member.issym():
            raise SourceError("Archive hard links and special files are unsupported")
        relative = PurePosixPath(*path.parts[1:])
        if not path.parts[1:]:
            if not member.isdir():
                raise SourceError("Archive root must be a directory")
            continue
        key = relative.as_posix()
        if key in seen:
            raise SourceError("Archive contains duplicate paths")
        seen.add(key)
        expanded_bytes += member.size
        if member.size < 0 or expanded_bytes > MAX_EXPANDED_BYTES:
            raise SourceError("Archive exceeds the expanded size limit")
        members.append((member, relative))
    regular_files = {relative.as_posix() for member, relative in members if member.isfile()}
    for member, relative in members:
        if member.issym():
            target = PurePosixPath(member.linkname)
            resolved = posixpath.normpath((relative.parent / target).as_posix())
            if target.is_absolute() or "\\" in member.linkname or resolved not in regular_files:
                raise SourceError("Archive symlink must refer directly to an internal regular file")
    return members


def archive_tree(archive_path: Path, archive_root: str):
    """Return canonical tree hash, per-file hashes, and PAX commit provenance."""
    files = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member, relative in _members(archive, archive_root):
            if _ignored(relative):
                continue
            if member.issym():
                files[relative.as_posix()] = _link_digest(member.linkname)
            elif member.isfile():
                with archive.extractfile(member) as stream:
                    files[relative.as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
        commit = archive.pax_headers.get("comment")
    return _digest(files), files, commit


def _tree_files(directory: Path) -> dict[str, str]:
    if directory.is_symlink() or not directory.is_dir():
        raise SourceError("Source tree is missing or is a symbolic link")
    files = {}
    for path in directory.rglob("*"):
        relative = PurePosixPath(path.relative_to(directory).as_posix())
        if _ignored(relative):
            continue
        if path.is_symlink():
            target = os.readlink(path)
            direct_target = path.parent / target
            if (Path(target).is_absolute() or direct_target.is_symlink() or
                    not direct_target.is_file() or not direct_target.resolve().is_relative_to(directory.resolve())):
                raise SourceError("Source symlink must refer directly to an internal regular file")
            files[relative.as_posix()] = _link_digest(target)
        elif path.is_file():
            files[relative.as_posix()] = file_sha256(path)
        elif not path.is_dir():
            raise SourceError("Source tree contains a special file")
    return files


def tree_digest(directory: Path) -> str:
    return _digest(_tree_files(directory))


def safe_extract_archive(archive_path: Path, destination: Path, archive_root: str):
    """Validate every entry, then build a new tree without overwriting a checkout."""
    if destination.exists() or destination.is_symlink():
        raise SourceError("Refusing to overwrite an existing source tree")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = _members(archive, archive_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".source-extract-", dir=destination.parent) as temp:
            staged = Path(temp) / "source"
            staged.mkdir()
            for member, relative in sorted(members, key=lambda item: item[0].issym()):
                path = staged.joinpath(*relative.parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.issym():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(member.linkname)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    path.chmod(0o755 if member.mode & 0o111 else 0o644)
            if destination.exists() or destination.is_symlink():
                raise SourceError("Source destination appeared during extraction")
            staged.rename(destination)


def verify_source(archive_path: Path, tree_path: Path, lock: dict) -> dict:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise SourceError("Pinned source archive is missing or is a symbolic link")
    if file_sha256(archive_path) != lock["archive_sha256"]:
        raise SourceError("Source archive SHA256 differs from the manifest")
    digest, expected_files, commit = archive_tree(archive_path, lock["archive_root"])
    if digest != lock["tree_sha256"]:
        raise SourceError("Archive tree SHA256 differs from the manifest")
    if lock.get("commit") is not None and commit != lock["commit"]:
        raise SourceError("Archive commit differs from the manifest")
    if lock.get("file_count", len(expected_files)) != len(expected_files):
        raise SourceError("Archive file count differs from the manifest")
    actual_files = _tree_files(tree_path)
    if _digest(actual_files) != digest:
        changed = sorted(path for path in expected_files.keys() | actual_files.keys()
                         if expected_files.get(path) != actual_files.get(path))
        raise SourceError("Source tree differs from archive: " + ", ".join(changed[:12]))
    return {"archive_sha256": lock["archive_sha256"], "tree_sha256": digest,
            "commit": commit, "file_count": len(expected_files), "verified": True}


def _download_archive(url: str, destination: Path, expected_sha256: str):
    if not url.startswith("https://"):
        raise SourceError("Archive downloads require HTTPS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".source-download-", dir=destination.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            request = urllib.request.Request(url, headers={"User-Agent": "agentbreaker-eval-bootstrap"})
            with urllib.request.urlopen(request, timeout=30) as response:
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise SourceError("Download exceeds archive size limit")
                    output.write(chunk)
        if file_sha256(temp) != expected_sha256:
            raise SourceError("Downloaded archive SHA256 differs from the manifest")
        if destination.exists() or destination.is_symlink():
            raise SourceError("Refusing to overwrite an existing archive")
        temp.rename(destination)
    finally:
        temp.unlink(missing_ok=True)


def bootstrap(project_root: Path = PROJECT_ROOT, *, verify_only: bool = False) -> dict:
    manifest = json.loads((project_root / "sources.lock.json").read_text(encoding="utf-8"))
    results = {}
    for name, lock in manifest.items():
        if PurePosixPath(name).name != name or name in {".", ".."} or "\\" in name:
            raise SourceError("Invalid source name in manifest")
        archive = project_root / "vendor" / f"{name}.tar.gz"
        tree = project_root / "vendor" / name
        if not archive.exists():
            if verify_only:
                raise SourceError(f"{name}: archive is missing; verification is offline")
            _download_archive(lock["url"], archive, lock["archive_sha256"])
        if not tree.exists() and not tree.is_symlink():
            if verify_only:
                raise SourceError(f"{name}: extracted source tree is missing")
            if file_sha256(archive) != lock["archive_sha256"]:
                raise SourceError(f"{name}: source archive SHA256 differs from the manifest")
            digest, _, _ = archive_tree(archive, lock["archive_root"])
            if digest != lock["tree_sha256"]:
                raise SourceError(f"{name}: archive tree SHA256 differs from the manifest")
            safe_extract_archive(archive, tree, lock["archive_root"])
        results[name] = verify_source(archive, tree, lock)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Verify existing archives and trees without network access")
    args = parser.parse_args()
    try:
        results = bootstrap(verify_only=args.verify)
    except (SourceError, OSError, KeyError, ValueError, tarfile.TarError, urllib.error.URLError) as exc:
        print(f"Source verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
