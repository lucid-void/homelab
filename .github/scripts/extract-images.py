#!/usr/bin/env python3
"""Pair old/new container image references across a PR diff.

Renovate rarely changes a whole image reference in this repo. Two of the three
image-carrying forms split the repository and the tag onto separate lines, so a
bump shows up in the diff as a lone `tag:` line while the repository sits on an
unchanged context line. Diffing the raw `+`/`-` hunks therefore misses most
bumps. Instead we extract the full image set from the base and head versions of
every changed file and diff those two sets.

Recognised forms:

    image: repo/name:tag                 # plain manifests, initContainers
    repository: repo/name                # bjw-s app-template / Helm values
    tag: "1.2.3"

Helm chart coordinates (`chart:` + `version:`, OCIRepository `url:`) are
deliberately not matched -- they are chart artifacts, not runnable images, and
grype cannot scan them.

Output: a GitHub Actions matrix on stdout, e.g.
    {"include": [{"old": "alpine:3.21", "new": "alpine:3.24", "file": "..."}]}
An image present in head but absent from base is emitted with old == "",
meaning "no baseline" -- the caller falls back to an absolute threshold.
"""

import argparse
import json
import re
import subprocess
import sys

# A tag we can hand to a scanner. Rejects Helm templating and empty values.
IMAGE_RE = re.compile(r'^\s*image:\s*["\']?([^"\'\s#]+)["\']?\s*(?:#.*)?$')
REPOSITORY_RE = re.compile(r'^(\s*)repository:\s*["\']?([^"\'\s#]+)["\']?\s*(?:#.*)?$')
TAG_RE = re.compile(r'^(\s*)tag:\s*["\']?([^"\'\s#]+)["\']?\s*(?:#.*)?$')

# How far below a `repository:` line we will look for its `tag:`. Values files
# interleave `pullPolicy:`, so this cannot be strictly adjacent.
TAG_LOOKAHEAD = 6


def _repo_key(ref: str) -> str:
    """Pairing key for an image reference: everything before tag and digest.

    `repo:tag@sha256:...` must key off `repo`, or a digest change alone would
    look like a brand-new image and lose its baseline.
    """
    ref = ref.split("@", 1)[0]
    return ref.rpartition(":")[0]


def _is_scannable(ref: str) -> bool:
    """Reject templated, empty, or untagged references."""
    if not ref or "{{" in ref or "${" in ref:
        return False
    if ref in ("{}", "[]", "null", "~"):
        return False
    # Must have a tag; an untagged ref would resolve to :latest and compare
    # against nothing meaningful.
    name, sep, tag = ref.split("@", 1)[0].rpartition(":")
    if not sep or "/" in tag:
        return False
    return bool(name and tag)


def extract(text: str) -> dict:
    """Map image repository -> full reference for one file's contents."""
    found = {}
    lines = text.splitlines()

    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue

        m = IMAGE_RE.match(line)
        if m and _is_scannable(m.group(1)):
            ref = m.group(1)
            found[_repo_key(ref)] = ref
            continue

        m = REPOSITORY_RE.match(line)
        if not m:
            continue
        indent, repo = m.group(1), m.group(2)
        if "{{" in repo or "${" in repo:
            continue

        for follow in lines[idx + 1 : idx + 1 + TAG_LOOKAHEAD]:
            if REPOSITORY_RE.match(follow):
                break  # next image block started; this one has no tag
            tm = TAG_RE.match(follow)
            if not tm:
                continue
            if len(tm.group(1)) != len(indent):
                continue  # different nesting level, not this image's tag
            tag = tm.group(2)
            if "{{" in tag or "${" in tag:
                break
            found[repo] = f"{repo}:{tag}"
            break

    return found


def git_show(ref: str, path: str) -> str:
    """File contents at a git ref; empty string if the file does not exist."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def changed_files(base: str, head: str) -> list:
    result = subprocess.run(
        [
            "git", "diff", "--name-only", f"{base}...{head}",
            "--", "kubernetes/**.yml", "kubernetes/**.yaml",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.splitlines() if p.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()

    pairs = {}
    for path in changed_files(args.base, args.head):
        old_images = extract(git_show(args.base, path))
        new_images = extract(git_show(args.head, path))

        for repo, new_ref in new_images.items():
            old_ref = old_images.get(repo, "")
            if old_ref == new_ref:
                continue  # untouched by this PR
            # Same image in two files (e.g. a shared sidecar) scans once.
            pairs.setdefault(new_ref, {"old": old_ref, "new": new_ref, "file": path})

    include = sorted(pairs.values(), key=lambda p: p["new"])
    print(json.dumps({"include": include}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
