"""Rule-based contribution-strategy generator.

Takes a normalized OSS opportunity dict (from either the trending scraper
or the good-first-issues scraper) and produces:

- ``difficulty`` — one of ``"easy" | "medium" | "hard"`` based on star
  count, presence of good-first-issues, and repo activity.
- ``reachout_strategy`` — a 3-step plan tailored to whether the repo has
  concrete good-first-issues picked out.
- ``reachout_subject`` + ``reachout_body`` — a maintainer-DM template the
  user can copy and edit.

The function deliberately does not call any LLM. It is deterministic so
the front-end UI is consistent and tests stay trivial.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

LANGUAGE_RUNTIMES = {
    "python": "Python",
    "javascript": "Node",
    "typescript": "Node",
    "go": "Go",
    "rust": "Cargo",
    "ruby": "Bundler",
    "java": "Maven",
    "kotlin": "Gradle",
    "swift": "Xcode",
    "cpp": "CMake/Clang",
    "c": "GCC/Clang",
}


def _owner(repo_path: str) -> str:
    return repo_path.split("/", 1)[0] if "/" in repo_path else repo_path


def _months_since(iso_str: str | None) -> float:
    if not iso_str:
        return 0.0
    try:
        when = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).days / 30.0)


def classify_difficulty(opp: dict[str, Any]) -> str:
    """Easy / Medium / Hard using stars, GFI presence, and activity.

    - **Easy:** under 5k stars AND at least one good-first-issue surfaced.
    - **Hard:** over 50k stars OR last-activity older than 12 months.
    - **Medium:** everything else.
    """
    stars = int(opp.get("stars") or 0)
    has_gfi = bool(opp.get("top_issues"))
    months_idle = _months_since(opp.get("last_activity") or opp.get("published"))

    if stars >= 50_000 or months_idle >= 12:
        return "hard"
    if stars < 5_000 and has_gfi:
        return "easy"
    return "medium"


def _runtime_for(opp: dict[str, Any]) -> str:
    """Pick the runtime hint for ``opp``.

    Prefer ``primary_language`` (set by the trending scraper explicitly),
    fall back to the first tag, and finally to a generic default.
    """
    preferred = (opp.get("primary_language") or "").strip()
    if not preferred and opp.get("tags"):
        first_tag = opp["tags"][0]
        if isinstance(first_tag, str):
            preferred = first_tag
    return LANGUAGE_RUNTIMES.get(preferred.lower(), "the project's tooling")


def build_strategy(opp: dict[str, Any], has_gfi: bool) -> str:
    """3-step contribution plan tailored to whether GFIs are picked out."""
    repo_path = opp.get("title") or opp.get("organization") or "owner/repo"
    runtime = _runtime_for(opp)

    if has_gfi and opp.get("top_issues"):
        issue_chips = ", ".join(
            f"#{iss['number']} {iss['title'][:60]}"
            for iss in opp["top_issues"][:3]
            if iss.get("number") is not None
        ) or "the labeled issues"
        return (
            f"1) Skim these Good First Issues: {issue_chips}. "
            f"2) Comment 'I'd like to work on this' on whichever you pick — that signals the maintainer. "
            f"3) Fork `{repo_path}`, branch as `fix/issue-<n>`, run the `{runtime}` test suite, and open a draft PR linking the issue."
        )
    return (
        f"1) Read `{repo_path}`'s CONTRIBUTING.md and skim recent merged PRs to learn conventions. "
        f"2) Set up `{runtime}` locally and run the existing test suite — verify it passes before changing anything. "
        f"3) Search the open issues for `bug`, `TODO`, or `documentation`; if nothing fits, open an issue proposing a small enhancement before writing code."
    )


def build_outreach(opp: dict[str, Any], has_gfi: bool) -> tuple[str, str]:
    """Maintainer outreach message — subject + body."""
    repo_path = opp.get("title") or "owner/repo"
    owner = _owner(repo_path)
    if has_gfi and opp.get("top_issues"):
        first_issue = opp["top_issues"][0]
        subject = f"Contribution inquiry: {repo_path} - issue #{first_issue.get('number', '?')}"
        issue_line = (
            f"I saw issue #{first_issue.get('number')} ({first_issue.get('') or 'open good-first-issue'}); "
            "is it still open for a PR?"
        )
    else:
        subject = f"Contribution inquiry: {repo_path}"
        issue_line = "Are there any undocumented areas or beginner-friendly issues you need help with?"

    body = (
        f"Hi @{owner},\n\n"
        f"I've been working through `{repo_path}` locally and would love to contribute. "
        f"{issue_line}\n\n"
        "I have the repo running and the test suite green, and I'm ready to open a draft PR if "
        "you can point me at the right direction. If contributors are asked to discuss before "
        "coding, I'm happy to start there.\n\n"
        "Thanks for your time!"
    )
    return subject, body


def attach_strategy(opp: dict[str, Any]) -> dict[str, Any]:
    """Mutates-and-returns the opportunity with all derived fields."""
    has_gfi = bool(opp.get("top_issues"))
    opp = dict(opp)  # don't mutate the caller's reference
    opp["difficulty"] = classify_difficulty(opp)
    opp["reachout_strategy"] = build_strategy(opp, has_gfi)
    opp["reachout_subject"], opp["reachout_body"] = build_outreach(opp, has_gfi)

    # If the opportunity came from the GFI source it has no stars/forks; leave them.
    if has_gfi:
        # Lightly increase score so GFI rows sort above generic trending rows.
        opp["score"] = min(1.0, float(opp.get("score") or 0.0) + 0.05)
    return opp
