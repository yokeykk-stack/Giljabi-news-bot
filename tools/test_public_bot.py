#!/usr/bin/env python3
"""Static contracts for the public scheduler; no network or secret access."""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from validate_target_changes import validate

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = [WORKFLOWS / "hourly-media.yml", WORKFLOWS / "daily-media-recovery.yml"]
failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'OK' if condition else 'ERR'}  {label}")
    if not condition:
        failures.append(label)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def exercise_target_allowlist() -> tuple[bool, bool, bool, bool]:
    scratch = ROOT / ".test_tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="allowlist-", dir=scratch) as raw:
        repo = Path(raw)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "bot-test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "bot@test.invalid"], check=True)
        (repo / "news" / "archive").mkdir(parents=True)
        for relative in (
            "news/feed.json",
            "news/feed.js",
            "news/headlines.json",
            "news/archive/manifest.json",
            "news/archive/media_2026-07.json",
        ):
            (repo / relative).write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "--", "news"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

        (repo / "news" / "feed.json").write_text('{"ok": true}\n', encoding="utf-8")
        allowed_unstaged = validate(repo, staged_only=False) == ["news/feed.json"]
        subprocess.run(["git", "-C", str(repo), "add", "--", "news/feed.json"], check=True)
        allowed_staged = validate(repo, staged_only=True) == ["news/feed.json"]
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", "-q", "HEAD"], check=True)

        (repo / "unexpected.txt").write_text("must fail\n", encoding="utf-8")
        try:
            validate(repo, staged_only=False)
        except RuntimeError:
            rogue_rejected = True
        else:
            rogue_rejected = False
        (repo / "unexpected.txt").unlink()
        subprocess.run(
            ["git", "-C", str(repo), "rm", "-q", "--", "news/archive/manifest.json"], check=True
        )
        try:
            validate(repo, staged_only=True)
        except RuntimeError:
            archive_deletion_rejected = True
        else:
            archive_deletion_rejected = False
        subprocess.run(["git", "-C", str(repo), "reset", "--hard", "-q", "HEAD"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "mv",
                "news/archive/media_2026-07.json",
                "news/archive/media_2026-08.json",
            ],
            check=True,
        )
        try:
            validate(repo, staged_only=True)
        except RuntimeError:
            archive_rename_rejected = True
        else:
            archive_rename_rejected = False
        result = (
            allowed_unstaged and allowed_staged,
            rogue_rejected,
            archive_deletion_rejected,
            archive_rename_rejected,
        )
    try:
        scratch.rmdir()
    except OSError:
        pass
    return result


def main() -> int:
    all_paths = sorted({*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")})
    expected_workflows = {
        "daily-media-recovery.yml",
        "hourly-media.yml",
        "keepalive.yml",
        "validate.yml",
    }
    check("workflow inventory is exact across .yml and .yaml",
          {path.name for path in all_paths} == expected_workflows)
    all_text = "\n".join(text(path) for path in all_paths)
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", all_text, re.M)
    check("all workflow actions are pinned to full commit SHAs",
          bool(uses) and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses))
    check("no pull_request_target trigger exists", "pull_request_target" not in all_text)
    check("no dependency cache action exists", "actions/cache" not in all_text)
    check("artifact transfer uses only pinned official v4 actions",
          all_text.count("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02") == 2
          and all_text.count("actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093") == 2)
    check("artifact lifetime and scope are bounded to the public-data bundle",
          all_text.count("retention-days: 1") == 2
          and all_text.count("include-hidden-files: false") == 2
          and all_text.count("publish-bundle") >= 8)
    check("no private key or broad token literal is committed",
          "BEGIN OPENSSH PRIVATE KEY" not in all_text
          and not re.search(r"\b(?:gho_|github_pat_)[A-Za-z0-9_]", all_text))
    check("SSH host verification is never disabled", "StrictHostKeyChecking=no" not in all_text)
    check("runtime host discovery and verbose credential logging are forbidden",
          "ssh-keyscan" not in all_text and "accept-new" not in all_text
          and "ssh -v" not in all_text and "set -x" not in all_text)
    deploy_text = "\n".join(text(path) for path in DEPLOY)
    check("only the two fixed deploy workflows can reference target secrets",
          all_text.count("secrets.GILJABI_READ_KEY") == 2
          and all_text.count("secrets.GILJABI_DEPLOY_KEY") == 4
          and all_text.count("environment: private-target") == 4
          and all_text.count("secrets.GILJABI_READ_KEY") == deploy_text.count("secrets.GILJABI_READ_KEY")
          and all_text.count("secrets.GILJABI_DEPLOY_KEY") == deploy_text.count("secrets.GILJABI_DEPLOY_KEY"))

    groups: list[str] = []
    for path in DEPLOY:
        source = text(path)
        check(f"{path.name}: only trusted schedule/dispatch triggers",
              "schedule:" in source and "workflow_dispatch:" in source
              and "pull_request:" not in source and "push:" not in source)
        check(f"{path.name}: repository token is read-only", "permissions:\n  contents: read" in source)
        check(f"{path.name}: deploy secret is scoped through the private-target environment",
              "environment: private-target" in source)
        check(f"{path.name}: exact public repository and trusted-main event guard",
              "github.repository == 'yokeykk-stack/Giljabi-news-bot'" in source
              and "github.ref == 'refs/heads/main'" in source
              and "github.event_name == 'schedule'" in source
              and "github.event_name == 'workflow_dispatch'" in source)
        check(f"{path.name}: public checkout never persists credentials",
              "persist-credentials: false" in source and "persist-credentials: true" not in source)
        check(f"{path.name}: target is a fixed partial shallow main clone",
              "git@github.com:hsgamsa00-netizen/Giljabi.git target" in source
              and "--filter=blob:none --sparse --depth 1 --single-branch --branch main --no-tags" in source)
        check(f"{path.name}: read and write credentials have separate scopes",
              source.count("secrets.GILJABI_READ_KEY") == 1
              and source.count("secrets.GILJABI_DEPLOY_KEY") == 2
              and "Read the private target with its read-only deploy key" in source
              and "Clone a clean private target without persisting its write key" in source)
        check(f"{path.name}: official GitHub host key and strict SSH options are fixed",
              "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" in source
              and "StrictHostKeyChecking yes" in source
              and "IdentitiesOnly yes" in source
              and "BatchMode yes" in source)
        check(f"{path.name}: stale-base publication is fail-closed and non-force",
              "rev-parse FETCH_HEAD" in source
              and "rev-parse HEAD^" in source
              and "HEAD:refs/heads/main" in source
              and "--force" not in source and "git rebase" not in source)
        build = source[source.index("  build:"):source.index("\n  publish:")]
        publish = source[source.index("\n  publish:"):]
        check(f"{path.name}: target code and write key run on different jobs",
              "GILJABI_DEPLOY_KEY" not in build
              and "GILJABI_READ_KEY" in build
              and "python tools/" in build
              and "node tools/" in build
              and "GILJABI_READ_KEY" not in publish)
        check(f"{path.name}: clean publish job never executes target code",
              "fetch --depth=1" in publish and "push --porcelain" in publish
              and "python tools/" not in publish and "node tools/" not in publish
              and "apply_publish_bundle.py" in publish)
        check(f"{path.name}: only a one-day public-data bundle crosses jobs",
              "actions/upload-artifact@" in build and "retention-days: 1" in build
              and "actions/download-artifact@" in publish)
        check(f"{path.name}: failed-job reruns reuse the exact successful build artifact",
              "artifact_name: ${{ steps.build.outputs.artifact_name }}" in build
              and "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in build
              and "name: ${{ steps.build.outputs.artifact_name }}" in build
              and "name: ${{ needs.build.outputs.artifact_name }}" in publish)
        check(f"{path.name}: target changes pass allowlist checks before and after staging",
              source.count("validate_target_changes.py") == 3
              and source.index("validate_target_changes.py") < source.index("git add -A -- news/feed.json")
              < source.rindex("validate_target_changes.py"))
        match = re.search(r"group:\s*([^\s]+)", source)
        groups.append(match.group(1) if match else "")
    check("hourly and recovery writers share one concurrency group",
          groups == ["giljabi-private-main-writer", "giljabi-private-main-writer"])

    hourly = text(WORKFLOWS / "hourly-media.yml")
    check("hourly media freshness is independently gated at 50 minutes",
          "mediaCollectedAt" in hourly and "timedelta(minutes=50)" in hourly)
    check("hourly collection uses bounded concurrency and deadline",
          "--media-concurrency=3" in hourly and "--media-deadline-seconds=300" in hourly
          and "git reset --hard" not in hourly)
    check("hourly publish validates feed and archive before exact-path staging",
          hourly.index("node tools/test_issue_monitor.js")
          < hourly.index("python tools/accumulate_media_archive.py")
          < hourly.index("git add -A -- news/feed.json"))

    recovery = text(WORKFLOWS / "daily-media-recovery.yml")
    check("daily recovery is a bounded six-week checkpoint",
          "--weeks 6 --max-seconds 240 --max-requests 350" in recovery)
    check("daily recovery publishes action-required metadata before returning its status",
          recovery.index("git commit -m") < recovery.index("Report recovery action required")
          and "needs.build.result == 'success'" in recovery
          and "needs.build.outputs.sweep_status != ''" in recovery)

    validate = text(WORKFLOWS / "validate.yml")
    check("public PR validation is separate and never references the deploy secret",
          "pull_request:" in validate and "GILJABI_DEPLOY_KEY" not in validate
          and "GILJABI_READ_KEY" not in validate and "environment: private-target" not in validate
          and "permissions:\n  contents: read" in validate
          and "test_publish_bundle.py" in validate)
    keepalive = text(WORKFLOWS / "keepalive.yml")
    check("weekly keepalive changes only the public heartbeat",
          "contents: write" in keepalive and "status/heartbeat.json" in keepalive
          and "GILJABI_DEPLOY_KEY" not in keepalive)
    check("weekly keepalive is main-only and does not hide commit failures",
          "github.repository == 'yokeykk-stack/Giljabi-news-bot'" in keepalive
          and "github.ref == 'refs/heads/main'" in keepalive
          and "git commit -m \"chore: refresh public bot heartbeat\" ||" not in keepalive)

    allowlist_accepts, allowlist_rejects, deletion_rejected, rename_rejected = exercise_target_allowlist()
    check("target allowlist accepts only approved regular media changes", allowlist_accepts)
    check("target allowlist rejects unexpected target files", allowlist_rejects)
    check("target allowlist rejects archive/core deletion", deletion_rejected)
    check("target allowlist rejects archive rename-as-delete", rename_rejected)

    if failures:
        print(f"FAILED: {len(failures)}")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"public bot contracts PASS ({len(all_paths)} workflows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
