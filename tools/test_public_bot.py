#!/usr/bin/env python3
"""Static contracts for the public scheduler; no network or secret access."""
from __future__ import annotations

import ast
import math
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from validate_target_changes import validate

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = [WORKFLOWS / "hourly-media.yml", WORKFLOWS / "daily-media-recovery.yml"]
failures: list[str] = []

RECOVERY_CONTRACT = {
    "interval_hours": 6,
    "minute_utc": 47,
    "weeks": 6,
    "max_seconds": 420,
    "max_requests": 700,
    "minimum_timeout_margin_minutes": 8,
}


def check(label: str, condition: bool) -> None:
    print(f"{'OK' if condition else 'ERR'}  {label}")
    if not condition:
        failures.append(label)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def yaml_scalar(raw: str) -> str | int:
    """Decode the small scalar subset used by GitHub workflow contracts."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        decoded = ast.literal_eval(value)
        if not isinstance(decoded, str):
            raise ValueError(f"expected a quoted YAML string, got {raw!r}")
        return decoded
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def yaml_mapping_entry(
    source: str, path: tuple[str, ...]
) -> tuple[str, int, int, int, list[str]]:
    """Return a mapping value and the bounds of its indentation-defined subtree.

    This deliberately parses hierarchy instead of searching for a text fragment. It
    covers the plain mappings GitHub workflow metadata uses without adding a package
    installation to the offline contract test.
    """
    lines = source.splitlines()
    start, end, indent = 0, len(lines), 0
    for depth, key in enumerate(path):
        match_index = -1
        value = ""
        pattern = re.compile(rf"^{re.escape(key)}:\s*(.*?)\s*$")
        for index in range(start, end):
            line = lines[index]
            stripped = line.lstrip(" ")
            if not stripped or stripped.startswith("#"):
                continue
            if len(line) - len(stripped) != indent:
                continue
            match = pattern.fullmatch(stripped)
            if match:
                match_index = index
                value = match.group(1)
                break
        if match_index < 0:
            raise ValueError(f"missing YAML mapping path: {'.'.join(path[: depth + 1])}")

        subtree_end = end
        for index in range(match_index + 1, end):
            line = lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            line_indent = len(line) - len(line.lstrip(" "))
            if line_indent <= indent:
                subtree_end = index
                break

        if depth == len(path) - 1:
            return value, match_index + 1, subtree_end, indent, lines
        if value:
            raise ValueError(f"YAML mapping is not a block: {'.'.join(path[: depth + 1])}")
        start, end, indent = match_index + 1, subtree_end, indent + 2
    raise AssertionError("empty YAML path")


def yaml_scalar_at(source: str, path: tuple[str, ...]) -> str | int:
    value, _start, _end, _indent, _lines = yaml_mapping_entry(source, path)
    if not value or value.startswith(("|", ">")):
        raise ValueError(f"YAML path is not a scalar: {'.'.join(path)}")
    return yaml_scalar(value)


def yaml_sequence_scalars(source: str, path: tuple[str, ...], key: str) -> list[str | int]:
    value, start, end, parent_indent, lines = yaml_mapping_entry(source, path)
    if value:
        raise ValueError(f"YAML path is not a sequence block: {'.'.join(path)}")
    item_indent = parent_indent + 2
    pattern = re.compile(rf"^-\s+{re.escape(key)}:\s*(.*?)\s*$")
    result: list[str | int] = []
    for line in lines[start:end]:
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) != item_indent:
            continue
        match = pattern.fullmatch(stripped)
        if match:
            result.append(yaml_scalar(match.group(1)))
    return result


def workflow_step_script(source: str, job: str, step_name: str) -> str:
    value, start, end, parent_indent, lines = yaml_mapping_entry(
        source, ("jobs", job, "steps")
    )
    if value:
        raise ValueError(f"jobs.{job}.steps is not a sequence block")
    item_indent = parent_indent + 2
    starts = [
        index
        for index in range(start, end)
        if lines[index].strip()
        and len(lines[index]) - len(lines[index].lstrip(" ")) == item_indent
        and lines[index].lstrip(" ").startswith("- ")
    ]
    starts.append(end)
    for item_start, item_end in zip(starts, starts[1:]):
        first = lines[item_start].lstrip(" ")[2:]
        name_match = re.fullmatch(r"name:\s*(.*?)\s*", first)
        if not name_match or yaml_scalar(name_match.group(1)) != step_name:
            continue
        field_indent = item_indent + 2
        for index in range(item_start + 1, item_end):
            line = lines[index]
            stripped = line.lstrip(" ")
            if len(line) - len(stripped) != field_indent:
                continue
            run_match = re.fullmatch(r"run:\s*([|>][+-]?)\s*", stripped)
            if not run_match:
                continue
            block: list[str] = []
            for block_index in range(index + 1, item_end):
                block_line = lines[block_index]
                if block_line.strip():
                    block_indent = len(block_line) - len(block_line.lstrip(" "))
                    if block_indent <= field_indent:
                        break
                block.append(block_line)
            return textwrap.dedent("\n".join(block)).strip("\n")
        raise ValueError(f"workflow step has no block run script: {step_name}")
    raise ValueError(f"missing workflow step: {step_name}")


def cron_field_values(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        base, separator, step_text = part.partition("/")
        step = int(step_text) if separator else 1
        if step <= 0:
            raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron field outside {minimum}..{maximum}: {field}")
        values.update(range(start, end + 1, step))
    return values


def cron_matches_recovery_contract(expression: str) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    try:
        return (
            cron_field_values(minute, 0, 59) == {RECOVERY_CONTRACT["minute_utc"]}
            and cron_field_values(hour, 0, 23)
            == set(range(0, 24, RECOVERY_CONTRACT["interval_hours"]))
            and cron_field_values(day, 1, 31) == set(range(1, 32))
            and cron_field_values(month, 1, 12) == set(range(1, 13))
            and cron_field_values(weekday, 0, 6) == set(range(0, 7))
        )
    except (TypeError, ValueError):
        return False


def shell_invocation(script: str, program: str) -> list[str]:
    invocations: list[list[str]] = []
    pending = ""
    for raw_line in script.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        tokens = shlex.split(pending, posix=True)
        pending = ""
        if "|" in tokens:
            tokens = tokens[: tokens.index("|")]
        if len(tokens) >= 2 and tokens[0] in {"python", "python3"} and tokens[1] == program:
            invocations.append(tokens)
    if pending:
        raise ValueError("unterminated shell line continuation")
    if len(invocations) != 1:
        raise ValueError(f"expected one {program} invocation, found {len(invocations)}")
    return invocations[0]


def command_options(tokens: list[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            raise ValueError(f"unexpected positional command argument: {token}")
        if "=" in token:
            name, value = token.split("=", 1)
        else:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise ValueError(f"command option has no value: {token}")
            name, value = token, tokens[index + 1]
            index += 1
        if name in options:
            raise ValueError(f"duplicate command option: {name}")
        options[name] = value
        index += 1
    return options


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
    recovery_env = {
        "RECOVERY_INTERVAL_HOURS": yaml_scalar_at(
            recovery, ("jobs", "build", "env", "RECOVERY_INTERVAL_HOURS")
        ),
        "RECOVERY_MINUTE_UTC": yaml_scalar_at(
            recovery, ("jobs", "build", "env", "RECOVERY_MINUTE_UTC")
        ),
        "RECOVERY_WEEKS": yaml_scalar_at(
            recovery, ("jobs", "build", "env", "RECOVERY_WEEKS")
        ),
        "RECOVERY_MAX_SECONDS": yaml_scalar_at(
            recovery, ("jobs", "build", "env", "RECOVERY_MAX_SECONDS")
        ),
        "RECOVERY_MAX_REQUESTS": yaml_scalar_at(
            recovery, ("jobs", "build", "env", "RECOVERY_MAX_REQUESTS")
        ),
    }
    check("recovery job declares one six-hour 420-second 700-request contract",
          recovery_env == {
              "RECOVERY_INTERVAL_HOURS": str(RECOVERY_CONTRACT["interval_hours"]),
              "RECOVERY_MINUTE_UTC": str(RECOVERY_CONTRACT["minute_utc"]),
              "RECOVERY_WEEKS": str(RECOVERY_CONTRACT["weeks"]),
              "RECOVERY_MAX_SECONDS": str(RECOVERY_CONTRACT["max_seconds"]),
              "RECOVERY_MAX_REQUESTS": str(RECOVERY_CONTRACT["max_requests"]),
          })
    recovery_crons = yaml_sequence_scalars(recovery, ("on", "schedule"), "cron")
    check("recovery schedule is exactly every six hours at :47 UTC",
          len(recovery_crons) == 1
          and isinstance(recovery_crons[0], str)
          and cron_matches_recovery_contract(recovery_crons[0]))
    recovery_script = workflow_step_script(
        recovery, "build", "Recover, validate, and prepare a public-data checkpoint bundle"
    )
    recovery_command = shell_invocation(recovery_script, "tools/sweep_media_windows.py")
    recovery_options = command_options(recovery_command)
    check("recovery command implements the six-week 420-second 700-request contract",
          recovery_options == {
              "--weeks": "$RECOVERY_WEEKS",
              "--max-seconds": "$RECOVERY_MAX_SECONDS",
              "--max-requests": "$RECOVERY_MAX_REQUESTS",
              "--state-file": "news/archive/media-sweep-state.json",
          })
    recovery_timeout = yaml_scalar_at(recovery, ("jobs", "build", "timeout-minutes"))
    timeout_margin = (
        recovery_timeout - math.ceil(RECOVERY_CONTRACT["max_seconds"] / 60)
        if isinstance(recovery_timeout, int)
        else -1
    )
    check("recovery build timeout leaves at least eight minutes after the sweep deadline",
          timeout_margin >= RECOVERY_CONTRACT["minimum_timeout_margin_minutes"])
    check("six-hour recovery publishes action-required metadata before returning its status",
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
