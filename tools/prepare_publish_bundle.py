#!/usr/bin/env python3
"""Create a deterministic, bounded bundle from staged Giljabi media changes.

The bundle deliberately contains data only.  It is safe to move across the
collector/publisher job boundary without carrying a Git checkout, executable
code, credentials, or untracked collector output with it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from validate_target_changes import DELETABLE, allowed, validate

SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 80 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_RECORDS = 4096
KINDS = frozenset({"hourly", "sweep"})
SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_KEYS = frozenset(
    {"schemaVersion", "baseSha", "kind", "files", "deletions"}
)
FILE_KEYS = frozenset({"path", "sha256", "size"})


class BundleError(RuntimeError):
    """A fail-closed bundle validation error."""


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], input=input_bytes, stderr=subprocess.PIPE
    )


def _canonical_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\0" in raw:
        raise BundleError("bundle path must be a non-empty canonical POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleError(f"unsafe bundle path: {raw!r}")
    if path.as_posix() != raw or not allowed(raw):
        raise BundleError(f"non-allowlisted or non-canonical bundle path: {raw!r}")
    return raw


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def parse_manifest(raw: bytes, expected_kind: str) -> dict[str, Any]:
    """Parse and strictly validate a canonical bundle manifest."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise BundleError("manifest exceeds the 1 MiB safety limit")
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"invalid manifest JSON: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise BundleError("manifest has an unexpected schema")
    version = manifest["schemaVersion"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise BundleError(f"unsupported schemaVersion: {version!r}")
    base_sha = manifest["baseSha"]
    if not isinstance(base_sha, str) or SHA1_RE.fullmatch(base_sha) is None:
        raise BundleError("baseSha must be a lowercase 40-character commit id")
    kind = manifest["kind"]
    if not isinstance(kind, str) or kind not in KINDS or kind != expected_kind:
        raise BundleError(f"bundle kind mismatch: expected {expected_kind!r}, got {kind!r}")

    files = manifest["files"]
    deletions = manifest["deletions"]
    if not isinstance(files, list) or not isinstance(deletions, list):
        raise BundleError("files and deletions must be arrays")
    if len(files) > MAX_FILE_RECORDS or len(deletions) > MAX_FILE_RECORDS:
        raise BundleError("manifest contains too many paths")

    seen: set[str] = set()
    previous = ""
    total = len(raw)
    for record in files:
        if not isinstance(record, dict) or set(record) != FILE_KEYS:
            raise BundleError("file record has an unexpected schema")
        path = _canonical_path(record["path"])
        if path in seen or (previous and path <= previous):
            raise BundleError("file records must be unique and sorted by path")
        previous = path
        seen.add(path)
        digest = record["sha256"]
        size = record["size"]
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise BundleError(f"invalid sha256 for {path}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise BundleError(f"invalid size for {path}")
        total += size
        if total > MAX_BUNDLE_BYTES:
            raise BundleError("bundle exceeds the 80 MiB safety limit")

    deletion_seen: set[str] = set()
    previous = ""
    for raw_path in deletions:
        path = _canonical_path(raw_path)
        if path not in DELETABLE:
            raise BundleError(f"deletion is forbidden: {path}")
        if path in seen or path in deletion_seen or (previous and path <= previous):
            raise BundleError("deletions must be disjoint, unique, and sorted")
        previous = path
        deletion_seen.add(path)
    if not files and not deletions:
        raise BundleError("empty publish bundles are forbidden")
    if raw != canonical_manifest_bytes(manifest):
        raise BundleError("manifest is not in canonical deterministic form")
    return manifest


def _staged_changes(repo: Path) -> tuple[list[str], list[str]]:
    raw = _git(repo, "diff", "--cached", "--name-status", "-z", "--no-renames", "--")
    fields = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    if len(fields) % 2:
        raise BundleError("cannot parse staged Git changes")
    files: list[str] = []
    deletions: list[str] = []
    for index in range(0, len(fields), 2):
        status_code, raw_path = fields[index], fields[index + 1]
        path = _canonical_path(raw_path)
        if status_code in {"A", "M"}:
            files.append(path)
        elif status_code == "D":
            if path not in DELETABLE:
                raise BundleError(f"deletion is forbidden: {path}")
            deletions.append(path)
        else:
            raise BundleError(f"unsupported staged status {status_code!r}: {path}")
    files.sort()
    deletions.sort()
    if len(set(files)) != len(files) or len(set(deletions)) != len(deletions):
        raise BundleError("duplicate staged path")
    return files, deletions


def _assert_regular(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BundleError(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BundleError(f"{label} must be a regular file")
    return metadata


def prepare_bundle(repo: Path, output: Path, kind: str) -> dict[str, Any]:
    """Build *output* from the exact blobs currently staged in *repo*."""
    if not isinstance(kind, str) or kind not in KINDS:
        raise BundleError(f"kind must be one of: {', '.join(sorted(KINDS))}")
    original_repo = Path(repo)
    if original_repo.is_symlink() or getattr(original_repo, "is_junction", lambda: False)():
        raise BundleError("repository root cannot be a symlink or junction")
    repo = original_repo.resolve(strict=True)
    if not (repo / ".git").exists():
        raise BundleError(f"not a Git checkout: {repo}")

    validated = validate(repo, staged_only=True)
    files, deletions = _staged_changes(repo)
    if sorted(validated) != sorted(files + deletions):
        raise BundleError("validator result does not match the staged change set")
    if not files and not deletions:
        raise BundleError("there are no staged media changes to publish")

    base_sha = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if SHA1_RE.fullmatch(base_sha) is None:
        raise BundleError("Git returned an invalid base commit id")

    output = Path(output)
    if output.exists() or output.is_symlink():
        raise BundleError(f"output path already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        payload_root = scratch / "files"
        payload_root.mkdir()
        records: list[dict[str, Any]] = []
        payload_bytes = 0
        for relative in files:
            size_raw = _git(repo, "cat-file", "-s", f":{relative}").decode("ascii").strip()
            try:
                declared_size = int(size_raw)
            except ValueError as exc:
                raise BundleError(f"invalid staged blob size for {relative}") from exc
            if declared_size < 0 or payload_bytes + declared_size > MAX_BUNDLE_BYTES:
                raise BundleError("bundle exceeds the 80 MiB safety limit")
            content = _git(repo, "cat-file", "blob", f":{relative}")
            if len(content) != declared_size:
                raise BundleError(f"staged blob changed while reading: {relative}")
            payload_bytes += len(content)
            destination = payload_root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            destination.chmod(0o644)
            _assert_regular(destination, f"bundle payload {relative}")
            records.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            )

        manifest: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "baseSha": base_sha,
            "kind": kind,
            "files": records,
            "deletions": deletions,
        }
        manifest_raw = canonical_manifest_bytes(manifest)
        if len(manifest_raw) > MAX_MANIFEST_BYTES:
            raise BundleError("manifest exceeds the 1 MiB safety limit")
        if payload_bytes + len(manifest_raw) > MAX_BUNDLE_BYTES:
            raise BundleError("bundle exceeds the 80 MiB safety limit")
        (scratch / "manifest.json").write_bytes(manifest_raw)
        (scratch / "manifest.json").chmod(0o644)
        parse_manifest(manifest_raw, kind)
        os.replace(scratch, output)
        return manifest
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kind", required=True, choices=sorted(KINDS))
    args = parser.parse_args()
    try:
        manifest = prepare_bundle(args.repo, args.output, args.kind)
    except (BundleError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"publish bundle preparation failed: {exc}", file=sys.stderr)
        return 2
    print(
        "publish bundle prepared "
        f"({len(manifest['files'])} files, {len(manifest['deletions'])} deletions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
