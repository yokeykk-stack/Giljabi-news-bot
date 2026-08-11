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


def exercise_target_allowlist() -> tuple[bool, bool]:
    scratch = ROOT / ".test_tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="allowlist-", dir=scratch) as raw:
        repo = Path(raw)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "bot-test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "bot@test.invalid"], check=True)
        (repo / "news" / "archive").mkdir(parents=True)
        for relative in ("news/feed.json", "news/feed.js", "news/headlines.json"):
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
        result = allowed_unstaged and allowed_staged, rogue_rejected
    try:
        scratch.rmdir()
    except OSError:
        pass
    return result


def main() -> int:
    all_paths = sorted(WORKFLOWS.glob("*.yml"))
    all_text = "\n".join(text(path) for path in all_paths)
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", all_text, re.M)
    check("all workflow actions are pinned to full commit SHAs",
          bool(uses) and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses))
    check("no pull_request_target trigger exists", "pull_request_target" not in all_text)
    check("no artifact or dependency cache action exists",
          "upload-artifact" not in all_text and "download-artifact" not in all_text
          and "actions/cache" not in all_text)
    check("no private key or broad token literal is committed",
          "BEGIN OPENSSH PRIVATE KEY" not in all_text
          and not re.search(r"\b(?:gho_|github_pat_)[A-Za-z0-9_]", all_text))
    check("SSH host verification is never disabled", "StrictHostKeyChecking=no" not in all_text)
    check("runtime host discovery and verbose credential logging are forbidden",
          "ssh-keyscan" not in all_text and "accept-new" not in all_text
          and "ssh -v" not in all_text and "set -x" not in all_text)

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
        check(f"{path.name}: target is a fixed shallow main clone",
              "git@github.com:hsgamsa00-netizen/Giljabi.git target" in source
              and "--depth 1 --single-branch --branch main --no-tags" in source)
        check(f"{path.name}: deploy key is limited to clone and publish steps",
              source.count("secrets.GILJABI_DEPLOY_KEY") == 2
              and source.index("Clone the private target")
              < source.index("prepare one")
              < source.index("Publish the validated"))
        check(f"{path.name}: official GitHub host key and strict SSH options are fixed",
              "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" in source
              and "StrictHostKeyChecking yes" in source
              and "IdentitiesOnly yes" in source
              and "BatchMode yes" in source)
        check(f"{path.name}: stale-base publication is fail-closed and non-force",
              "git rev-parse FETCH_HEAD" in source
              and "git rev-parse HEAD^" in source
              and "HEAD:refs/heads/main" in source
              and "--force" not in source and "git rebase" not in source)
        build = source[source.index("prepare one"):source.index("Publish the validated")]
        publish = source[source.index("Publish the validated"):]
        check(f"{path.name}: collection runs without deploy-key or remote Git access",
              "GILJABI_DEPLOY_KEY" not in build and "git fetch" not in build
              and "git push" not in build)
        check(f"{path.name}: publish step executes Git only, never target code",
              "git fetch" in publish and "git push" in publish
              and "python tools/" not in publish and "node tools/" not in publish)
        check(f"{path.name}: target changes pass allowlist checks before and after staging",
              source.count("validate_target_changes.py") == 2
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
          recovery.index("git commit -m") < recovery.index("Report recovery action required"))

    validate = text(WORKFLOWS / "validate.yml")
    check("public PR validation is separate and never references the deploy secret",
          "pull_request:" in validate and "GILJABI_DEPLOY_KEY" not in validate
          and "permissions:\n  contents: read" in validate)
    keepalive = text(WORKFLOWS / "keepalive.yml")
    check("weekly keepalive changes only the public heartbeat",
          "contents: write" in keepalive and "status/heartbeat.json" in keepalive
          and "GILJABI_DEPLOY_KEY" not in keepalive)

    allowlist_accepts, allowlist_rejects = exercise_target_allowlist()
    check("target allowlist accepts only approved regular media changes", allowlist_accepts)
    check("target allowlist rejects unexpected target files", allowlist_rejects)

    if failures:
        print(f"FAILED: {len(failures)}")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"public bot contracts PASS ({len(all_paths)} workflows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
