#!/usr/bin/env python3
"""Security and integrity tests for the cross-job publish bundle."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from apply_publish_bundle import apply_bundle
from prepare_publish_bundle import BundleError, prepare_bundle

SCRATCH_ROOT = Path(__file__).resolve().parents[1] / ".test_tmp"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.STDOUT, text=True
    ).strip()


class PublishBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH_ROOT.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="giljabi-bundle-", dir=SCRATCH_ROOT
        )
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "config", "user.name", "bundle-test")
        git(self.source, "config", "user.email", "bundle@test.invalid")
        fixture = {
            "news/feed.json": b'{"version":1}\n',
            "news/feed.js": b'window.NEWS={"version":1};\n',
            "news/headlines.json": b"[]\n",
            "news/archive/manifest.json": b'{"months":[]}\n',
            "news/archive/media_stats.json": b"{}\n",
            "news/archive/media-sweep-state.json": b'{"cursor":1}\n',
        }
        for relative, content in fixture.items():
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        git(self.source, "add", "--", "news")
        git(self.source, "commit", "-qm", "fixture")

    def tearDown(self) -> None:
        self.temporary.cleanup()
        try:
            SCRATCH_ROOT.rmdir()
        except OSError:
            pass

    def stage_normal_update(self) -> None:
        (self.source / "news/feed.json").write_text('{"version":2}\n', encoding="utf-8")
        monthly = self.source / "news/archive/media_2026-08.json"
        monthly.write_text('[{"id":1}]\n', encoding="utf-8")
        git(self.source, "add", "--", "news/feed.json", "news/archive/media_2026-08.json")

    def clone_base(self, name: str) -> Path:
        clone = self.root / name
        subprocess.run(
            ["git", "clone", "-q", "--no-local", str(self.source), str(clone)], check=True
        )
        git(clone, "config", "user.name", "bundle-test")
        git(clone, "config", "user.email", "bundle@test.invalid")
        return clone

    def make_bundle(self, name: str = "bundle", kind: str = "hourly") -> Path:
        bundle = self.root / name
        prepare_bundle(self.source, bundle, kind)
        return bundle

    def test_happy_path_and_deterministic_output(self) -> None:
        self.stage_normal_update()
        first = self.make_bundle("bundle-a")
        second = self.make_bundle("bundle-b")
        first_files = {
            path.relative_to(first).as_posix(): path.read_bytes()
            for path in first.rglob("*")
            if path.is_file()
        }
        second_files = {
            path.relative_to(second).as_posix(): path.read_bytes()
            for path in second.rglob("*")
            if path.is_file()
        }
        self.assertEqual(first_files, second_files)

        target = self.clone_base("target-happy")
        manifest = apply_bundle(target, first, "hourly")
        self.assertEqual(manifest["kind"], "hourly")
        self.assertEqual(
            git(target, "diff", "--cached", "--name-only", "--").splitlines(),
            ["news/archive/media_2026-08.json", "news/feed.json"],
        )
        self.assertEqual((target / "news/feed.json").read_text(encoding="utf-8"), '{"version":2}\n')

    def test_rogue_extra_file_is_rejected(self) -> None:
        self.stage_normal_update()
        bundle = self.make_bundle()
        (bundle / "rogue.txt").write_text("not allowed\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "inventory mismatch"):
            apply_bundle(self.clone_base("target-rogue"), bundle, "hourly")

    def test_hash_tamper_is_rejected(self) -> None:
        self.stage_normal_update()
        bundle = self.make_bundle()
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        payload = bundle / "files" / manifest["files"][0]["path"]
        content = bytearray(payload.read_bytes())
        content[0] ^= 1
        payload.write_bytes(content)
        with self.assertRaisesRegex(BundleError, "hash mismatch"):
            apply_bundle(self.clone_base("target-tamper"), bundle, "hourly")

    def test_base_advanced_on_a_bundle_path_is_rejected(self) -> None:
        self.stage_normal_update()
        bundle = self.make_bundle()
        target = self.clone_base("target-base")
        # The target's own hourly official update rewrote the feed after the bundle was built.
        (target / "news/feed.json").write_text('{"version":3}\n', encoding="utf-8")
        git(target, "add", "--", "news/feed.json")
        git(target, "commit", "-qm", "advance base on a bundle path")
        with self.assertRaisesRegex(BundleError, "base commit mismatch.*news/feed.json"):
            apply_bundle(target, bundle, "hourly")

    def test_base_advanced_on_unrelated_paths_is_applied(self) -> None:
        # 2026-09-05: the daily recovery was refused because a status stamp commit landed first.
        self.stage_normal_update()
        bundle = self.make_bundle()
        target = self.clone_base("target-advanced")
        (target / "status").mkdir()
        (target / "status/pap.json").write_text('{"ok":true}\n', encoding="utf-8")
        git(target, "add", "--", "status/pap.json")
        git(target, "commit", "-qm", "data: record portal check status")
        manifest = apply_bundle(target, bundle, "hourly")
        staged = git(target, "diff", "--cached", "--name-only", "--").splitlines()
        self.assertEqual(sorted(staged), sorted(record["path"] for record in manifest["files"]))
        self.assertTrue((target / "status/pap.json").exists())

    def test_base_unknown_to_a_shallow_clone_is_rejected(self) -> None:
        self.stage_normal_update()
        bundle = self.make_bundle()
        target = self.clone_base("target-shallow")
        (target / "status").mkdir()
        (target / "status/pap.json").write_text('{"ok":true}\n', encoding="utf-8")
        git(target, "add", "--", "status/pap.json")
        git(target, "commit", "-qm", "advance base")
        shallow = self.root / "target-shallow-1"
        subprocess.run(
            ["git", "clone", "-q", "--no-local", "--depth", "1", str(target), str(shallow)], check=True
        )
        git(shallow, "config", "user.name", "bundle-test")
        git(shallow, "config", "user.email", "bundle@test.invalid")
        with self.assertRaisesRegex(BundleError, "base commit mismatch.*not a known ancestor"):
            apply_bundle(shallow, bundle, "hourly")

    def test_archive_deletion_is_rejected(self) -> None:
        git(self.source, "rm", "-q", "--", "news/archive/manifest.json")
        with self.assertRaisesRegex(RuntimeError, "cannot be deleted"):
            prepare_bundle(self.source, self.root / "forbidden-delete", "sweep")

    def test_state_deletion_is_allowed(self) -> None:
        git(self.source, "rm", "-q", "--", "news/archive/media-sweep-state.json")
        bundle = self.make_bundle(kind="sweep")
        target = self.clone_base("target-delete")
        manifest = apply_bundle(target, bundle, "sweep")
        self.assertEqual(manifest["deletions"], ["news/archive/media-sweep-state.json"])
        self.assertFalse((target / "news/archive/media-sweep-state.json").exists())
        self.assertEqual(
            git(target, "diff", "--cached", "--diff-filter=D", "--name-only", "--"),
            "news/archive/media-sweep-state.json",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
