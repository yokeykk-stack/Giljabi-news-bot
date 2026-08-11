#!/usr/bin/env python3
"""Validate and stage a data-only Giljabi publish bundle in a clean clone."""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from prepare_publish_bundle import (
    MAX_BUNDLE_BYTES,
    BundleError,
    KINDS,
    parse_manifest,
)
from validate_target_changes import validate


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], input=input_bytes, stderr=subprocess.PIPE
    )


def _reject_link_or_junction(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BundleError(f"cannot inspect {label}: {exc}") from exc
    is_junction = getattr(path, "is_junction", lambda: False)()
    if stat.S_ISLNK(metadata.st_mode) or path.is_symlink() or is_junction:
        raise BundleError(f"symlink or junction is forbidden: {label}")


def _inventory_bundle(bundle: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for current, dirnames, filenames in os.walk(bundle, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in dirnames:
            candidate = current_path / name
            relative = candidate.relative_to(bundle).as_posix()
            _reject_link_or_junction(candidate, f"bundle directory {relative}")
            if not candidate.is_dir():
                raise BundleError(f"non-directory in bundle tree: {relative}")
            directories.add(relative)
        for name in filenames:
            candidate = current_path / name
            relative = candidate.relative_to(bundle).as_posix()
            _reject_link_or_junction(candidate, f"bundle file {relative}")
            try:
                mode = candidate.lstat().st_mode
            except OSError as exc:
                raise BundleError(f"cannot inspect bundle file {relative}: {exc}") from exc
            if not stat.S_ISREG(mode):
                raise BundleError(f"non-regular bundle file: {relative}")
            files.add(relative)
    return files, directories


def _read_regular_file(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BundleError(f"cannot open {label}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BundleError(f"{label} is not a regular file")
        chunks: list[bytes] = []
        remaining_limit = MAX_BUNDLE_BYTES + 1
        while remaining_limit > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining_limit))
            if not chunk:
                break
            chunks.append(chunk)
            remaining_limit -= len(chunk)
        if remaining_limit <= 0 and os.read(descriptor, 1):
            raise BundleError(f"{label} exceeds the 80 MiB safety limit")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _safe_target(repo: Path, relative: str) -> Path:
    target = repo.joinpath(*PurePosixPath(relative).parts)
    cursor = repo
    for part in PurePosixPath(relative).parts[:-1]:
        cursor /= part
        if not cursor.exists():
            raise BundleError(f"target parent does not exist: {relative}")
        _reject_link_or_junction(cursor, f"target parent for {relative}")
        if not cursor.is_dir():
            raise BundleError(f"target parent is not a directory: {relative}")
    if target.exists() or target.is_symlink():
        _reject_link_or_junction(target, f"target {relative}")
        if not target.is_file():
            raise BundleError(f"target is not a regular file: {relative}")
    try:
        common = os.path.commonpath((str(repo), str(target.resolve(strict=False))))
    except (OSError, ValueError) as exc:
        raise BundleError(f"cannot resolve target path {relative}: {exc}") from exc
    if os.path.normcase(common) != os.path.normcase(str(repo)):
        raise BundleError(f"target path escapes the checkout: {relative}")
    return target


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix=".giljabi-publish-", dir=path.parent)
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _stage_blob(repo: Path, relative: str, content: bytes) -> None:
    object_id = _git(repo, "hash-object", "-w", "--stdin", input_bytes=content).decode("ascii").strip()
    if len(object_id) != 40 or any(character not in "0123456789abcdef" for character in object_id):
        raise BundleError(f"Git returned an invalid blob id for {relative}")
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{object_id},{relative}")


def apply_bundle(repo: Path, bundle: Path, kind: str) -> dict[str, Any]:
    """Validate *bundle*, apply it atomically per file, and stage exact changes."""
    if not isinstance(kind, str) or kind not in KINDS:
        raise BundleError(f"kind must be one of: {', '.join(sorted(KINDS))}")
    original_repo = Path(repo)
    original_bundle = Path(bundle)
    roots = (original_repo, original_bundle)
    if any(
        root.is_symlink() or getattr(root, "is_junction", lambda: False)()
        for root in roots
    ):
        raise BundleError("repository and bundle roots cannot be symlinks or junctions")
    repo = original_repo.resolve(strict=True)
    bundle = original_bundle.resolve(strict=True)
    _reject_link_or_junction(repo, "repository root")
    _reject_link_or_junction(bundle, "bundle root")
    if not repo.is_dir() or not (repo / ".git").exists():
        raise BundleError(f"not a Git checkout: {repo}")
    if not bundle.is_dir():
        raise BundleError(f"not a bundle directory: {bundle}")
    dirty = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    ignored = _git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--")
    if dirty or ignored:
        raise BundleError("target checkout must be completely clean")

    files_on_disk, directories_on_disk = _inventory_bundle(bundle)
    if "manifest.json" not in files_on_disk:
        raise BundleError("bundle is missing manifest.json")
    manifest_raw = _read_regular_file(bundle / "manifest.json", "manifest.json")
    manifest = parse_manifest(manifest_raw, kind)

    expected_files = {"manifest.json"}
    expected_directories = {"files"}
    payloads: dict[str, bytes] = {}
    total = len(manifest_raw)
    for record in manifest["files"]:
        relative = record["path"]
        bundle_relative = PurePosixPath("files", relative).as_posix()
        expected_files.add(bundle_relative)
        parent = PurePosixPath(bundle_relative).parent
        while parent.as_posix() not in {".", ""}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
        content = _read_regular_file(
            bundle.joinpath(*PurePosixPath(bundle_relative).parts), bundle_relative
        )
        total += len(content)
        if total > MAX_BUNDLE_BYTES:
            raise BundleError("bundle exceeds the 80 MiB safety limit")
        if len(content) != record["size"]:
            raise BundleError(f"payload size mismatch: {relative}")
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise BundleError(f"payload hash mismatch: {relative}")
        payloads[relative] = content
    if files_on_disk != expected_files or directories_on_disk != expected_directories:
        extras = sorted((files_on_disk - expected_files) | (directories_on_disk - expected_directories))
        missing = sorted((expected_files - files_on_disk) | (expected_directories - directories_on_disk))
        detail = []
        if extras:
            detail.append("extra=" + ",".join(extras))
        if missing:
            detail.append("missing=" + ",".join(missing))
        raise BundleError("bundle inventory mismatch: " + "; ".join(detail))

    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if head != manifest["baseSha"]:
        raise BundleError(f"base commit mismatch: expected {manifest['baseSha']}, got {head}")

    targets = {relative: _safe_target(repo, relative) for relative in payloads}
    deletion_targets = {
        relative: _safe_target(repo, relative) for relative in manifest["deletions"]
    }
    for relative, target in deletion_targets.items():
        if not target.exists():
            raise BundleError(f"deletion target is missing: {relative}")

    for relative in sorted(payloads):
        _atomic_write(targets[relative], payloads[relative])
    for relative in manifest["deletions"]:
        deletion_targets[relative].unlink()

    # Plumbing commands bypass attributes, clean filters, and hooks.  The index
    # therefore contains precisely the already-verified bytes from the bundle.
    for relative in sorted(payloads):
        _stage_blob(repo, relative, payloads[relative])
    for relative in manifest["deletions"]:
        _git(repo, "update-index", "--force-remove", "--", relative)

    expected_changes = sorted([*payloads, *manifest["deletions"]])
    validated = validate(repo, staged_only=True)
    if validated != expected_changes:
        raise BundleError(
            "target validator result does not match bundle: "
            f"expected {expected_changes}, got {validated}"
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=sorted(KINDS))
    args = parser.parse_args()
    try:
        manifest = apply_bundle(args.repo, args.bundle, args.kind)
    except (BundleError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"publish bundle application failed: {exc}", file=sys.stderr)
        return 2
    print(
        "publish bundle applied and validated "
        f"({len(manifest['files'])} files, {len(manifest['deletions'])} deletions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
