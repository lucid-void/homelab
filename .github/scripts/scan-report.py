#!/usr/bin/env python3
"""Normalise grype + osv-scanner output, compute the CVE delta, render the PR comment.

Two subcommands:

    analyze  -- one image pair. Reads the raw scanner JSON from a scan
                directory, writes meta.json, exits non-zero if the gate fails.
    comment  -- all image pairs. Reads every meta.json and renders the markdown
                posted to the PR.

Gate: when a baseline exists, fail only if the NET Critical or High count
increases. An upgrade that introduces some CVEs while fixing more is a net
improvement and passes -- the introduced ones are still listed in the comment.
With no baseline (a brand-new image) there is nothing to compare against, so an
absolute threshold applies instead: any Critical fails.

The two scanners disagree on how severity is expressed and both are normalised
to the same Critical/High/Medium/Low bands here:

  grype  -- `matches[].vulnerability.severity`, already a band name. Stable
            across the versions this repo has used.
  osv v2 -- no severity label at all. `database_specific.severity` (which
            osv-scanner v1 populated, and which the pre-2026 version of this
            workflow keyed off) is now consistently null, so the v1 filters
            silently counted zero for every image. v2 exposes severity only as
            a CVSS base score string on `groups[].max_severity`, which is
            banded here using the standard CVSS v3 ranges.
"""

import argparse
import glob
import json
import os
import sys

# Standard CVSS v3.1 qualitative severity ranges.
CVSS_BANDS = [(9.0, "Critical"), (7.0, "High"), (4.0, "Medium"), (0.0, "Low")]

GATING = ("Critical", "High")


def band(score):
    """CVSS base score -> qualitative band. None for an unscored advisory."""
    if score is None:
        return "Unknown"
    for threshold, name in CVSS_BANDS:
        if score >= threshold:
            return name
    return "Unknown"


class Finding:
    """One vulnerability occurrence, normalised across scanners."""

    __slots__ = ("vid", "package", "version", "severity", "ecosystem")

    def __init__(self, vid, package, version, severity, ecosystem):
        self.vid = vid
        self.package = package
        self.version = version
        self.severity = severity
        self.ecosystem = ecosystem

    def as_row(self):
        return [self.package, self.version, self.vid, self.severity, self.ecosystem]


def load(path):
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def parse_grype(path):
    data = load(path)
    if not data:
        return []
    findings = []
    for match in data.get("matches") or []:
        vuln = match.get("vulnerability") or {}
        artifact = match.get("artifact") or {}
        findings.append(
            Finding(
                vuln.get("id", "?"),
                artifact.get("name", "?"),
                artifact.get("version", "?"),
                (vuln.get("severity") or "Unknown").capitalize(),
                artifact.get("type", "?"),
            )
        )
    return findings


def _preferred_id(group):
    """Prefer the CVE alias so grype and osv IDs line up in the comment."""
    aliases = group.get("aliases") or []
    for alias in aliases:
        if alias.startswith("CVE-"):
            return alias
    ids = group.get("ids") or []
    return (aliases or ids or ["?"])[0]


def parse_osv(path):
    data = load(path)
    if not data:
        return []
    findings = []
    for result in data.get("results") or []:
        for entry in result.get("packages") or []:
            package = entry.get("package") or {}
            for group in entry.get("groups") or []:
                raw = group.get("max_severity")
                try:
                    score = float(raw) if raw not in (None, "") else None
                except (TypeError, ValueError):
                    score = None
                findings.append(
                    Finding(
                        _preferred_id(group),
                        package.get("name", "?"),
                        package.get("version", "?"),
                        band(score),
                        package.get("ecosystem", "?"),
                    )
                )
    return findings


def counts(findings):
    out = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Unknown": 0}
    for finding in findings:
        out[finding.severity if finding.severity in out else "Unknown"] += 1
    return out


def delta(old, new):
    """Introduced / fixed detail rows, keyed on gating-severity vulnerability ID."""
    old_ids = {f.vid for f in old if f.severity in GATING}
    new_ids = {f.vid for f in new if f.severity in GATING}

    def rows(findings, ids):
        seen, out = set(), []
        for finding in findings:
            if finding.severity in GATING and finding.vid in ids and finding.vid not in seen:
                seen.add(finding.vid)
                out.append(finding.as_row())
        return sorted(out, key=lambda r: (r[3] != "Critical", r[2]))

    return rows(new, new_ids - old_ids), rows(old, old_ids - new_ids)


def analyze(args):
    scan_dir = args.dir
    has_baseline = bool(args.old)

    tools = {}
    failed = False

    for name, parser, new_file, old_file in (
        ("grype", parse_grype, "grype.json", "grype-old.json"),
        ("osv", parse_osv, "osv.json", "osv-old.json"),
    ):
        new = parser(os.path.join(scan_dir, new_file))
        old = parser(os.path.join(scan_dir, old_file)) if has_baseline else []
        new_counts, old_counts = counts(new), counts(old)
        introduced, fixed = delta(old, new) if has_baseline else ([], [])

        if has_baseline:
            regressed = any(new_counts[s] > old_counts[s] for s in GATING)
        else:
            regressed = new_counts["Critical"] > 0
        failed = failed or regressed

        tools[name] = {
            "counts": new_counts,
            "old_counts": old_counts,
            "introduced": introduced,
            "fixed": fixed,
            "regressed": regressed,
        }

    meta = {
        "new_image": args.new,
        "old_image": args.old,
        "file": args.file,
        "has_baseline": has_baseline,
        "is_major_bump": is_major_bump(args.old, args.new),
        "passed": not failed,
        "tools": tools,
    }

    with open(os.path.join(scan_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    for name, tool in tools.items():
        print(
            f"{name}: {tool['old_counts']['Critical']}C/{tool['old_counts']['High']}H "
            f"-> {tool['counts']['Critical']}C/{tool['counts']['High']}H "
            f"({len(tool['fixed'])} fixed, {len(tool['introduced'])} introduced)"
            f"{'  REGRESSION' if tool['regressed'] else ''}"
        )

    return 1 if failed else 0


def is_major_bump(old, new):
    if not old or not new:
        return False

    def major(ref):
        tag = ref.split("@", 1)[0].rpartition(":")[2]
        digits = "".join(c if c.isdigit() else " " for c in tag).split()
        return int(digits[0]) if digits else None

    old_major, new_major = major(old), major(new)
    return old_major is not None and new_major is not None and new_major > old_major


def changelog_url(image):
    repo = image.split("@", 1)[0].rpartition(":")[0]
    repo = repo[len("docker.io/"):] if repo.startswith("docker.io/") else repo
    if repo.startswith("ghcr.io/immich-app/immich-"):
        return "https://github.com/immich-app/immich/releases"
    if repo.startswith("ghcr.io/"):
        return f"https://github.com/{repo[len('ghcr.io/'):]}/releases"
    if repo.startswith("lscr.io/linuxserver/"):
        name = repo[len("lscr.io/linuxserver/"):]
        return f"https://github.com/linuxserver/docker-{name}/releases"
    if "/" in repo:
        return f"https://hub.docker.com/r/{repo}/tags"
    return f"https://hub.docker.com/_/{repo}/tags"


def delta_cell(before, after):
    diff = after - before
    if diff > 0:
        return f"+{diff} ❌"
    if diff < 0:
        return f"{diff} ✅"
    return "0"


def render_table(out, rows, header):
    out.append(f"| {' | '.join(header)} |")
    out.append("|" + "|".join("---" for _ in header) + "|")
    for pkg, ver, vid, sev, eco in rows:
        out.append(f"| `{pkg}` | {ver} | {vid} | **{sev}** | {eco} |")


def render_detail(out, tool, label, columns):
    introduced, fixed = tool["introduced"], tool["fixed"]
    if introduced:
        icon = "❌" if tool["regressed"] else "⚠️"
        out.append("<details>")
        out.append(
            f"<summary>{label} — {icon} {len(introduced)} new Critical/High CVE(s) "
            f"introduced</summary>\n"
        )
        render_table(out, introduced, columns)
        out.append("\n</details>\n")
    else:
        out.append(f"**{label}:** no new Critical/High CVEs introduced. ✅\n")

    if fixed:
        out.append("<details>")
        out.append(
            f"<summary>{label} — ✅ {len(fixed)} Critical/High CVE(s) fixed by this "
            f"upgrade</summary>\n"
        )
        render_table(out, fixed, [columns[0], "Was"] + columns[2:])
        out.append("\n</details>\n")


def comment(args):
    metas = []
    for path in sorted(glob.glob(os.path.join(args.artifacts, "*", "meta.json"))):
        data = load(path)
        if data:
            metas.append(data)

    out = ["## 🔍 Image Security Scan Results", ""]

    if not metas:
        out.append("_No container image changes detected in this PR._")
        print("\n".join(out))
        return 0

    overall = all(m["passed"] for m in metas)

    for meta in metas:
        new, old = meta["new_image"], meta["old_image"]
        badge = "✅ Pass" if meta["passed"] else "❌ Fail"
        grype, osv = meta["tools"]["grype"], meta["tools"]["osv"]

        if meta["has_baseline"]:
            old_tag = old.rpartition(":")[2]
            new_tag = new.rpartition(":")[2]
            out.append(f"### `{old_tag}` → `{new_tag}` — {badge}")
            out.append(f"_`{new}`_ · <sub>{meta['file']}</sub>\n")

            if meta["is_major_bump"]:
                out.append(
                    f"> ⚠️ **Major version bump** `{old_tag}` → `{new_tag}` — review the "
                    f"[upstream changelog]({changelog_url(new)}) for breaking changes and "
                    f"configuration migrations before merging.\n"
                )

            gc, go = grype["counts"], grype["old_counts"]
            oc, oo = osv["counts"], osv["old_counts"]
            out.append("| | Critical | High | Medium | Low |")
            out.append("|-|----------|------|--------|-----|")
            out.append(
                f"| grype before (`{old_tag}`) | {go['Critical']} | {go['High']} | "
                f"{go['Medium']} | {go['Low']} |"
            )
            out.append(
                f"| grype after (`{new_tag}`) | {gc['Critical']} | {gc['High']} | "
                f"{gc['Medium']} | {gc['Low']} |"
            )
            out.append(
                f"| grype delta | **{delta_cell(go['Critical'], gc['Critical'])}** | "
                f"**{delta_cell(go['High'], gc['High'])}** | "
                f"{delta_cell(go['Medium'], gc['Medium'])} | "
                f"{delta_cell(go['Low'], gc['Low'])} |"
            )
            out.append(
                f"| osv before (`{old_tag}`) | {oo['Critical']} | {oo['High']} | "
                f"{oo['Medium']} | {oo['Low']} |"
            )
            out.append(
                f"| osv after (`{new_tag}`) | {oc['Critical']} | {oc['High']} | "
                f"{oc['Medium']} | {oc['Low']} |"
            )
            out.append(
                f"| osv delta | **{delta_cell(oo['Critical'], oc['Critical'])}** | "
                f"**{delta_cell(oo['High'], oc['High'])}** | "
                f"{delta_cell(oo['Medium'], oc['Medium'])} | "
                f"{delta_cell(oo['Low'], oc['Low'])} |"
            )
            out.append("")

            render_detail(
                out, grype, "grype",
                ["Package", "Installed", "Vulnerability", "Severity", "Type"],
            )
            render_detail(
                out, osv, "osv-scanner",
                ["Package", "Installed", "Vulnerability", "Severity", "Ecosystem"],
            )
        else:
            out.append(f"### `{new}` — {badge} _(no baseline)_")
            out.append(f"<sub>{meta['file']}</sub>\n")
            out.append(
                "_No prior version to compare against (new image, or the previous tag "
                "could not be pulled). Absolute threshold applied (fail on Critical)._\n"
            )
            gc, oc = grype["counts"], osv["counts"]
            out.append("| Tool | Critical | High | Medium | Low |")
            out.append("|------|----------|------|--------|-----|")
            out.append(
                f"| grype | {gc['Critical']} | {gc['High']} | {gc['Medium']} | {gc['Low']} |"
            )
            out.append(
                f"| osv | {oc['Critical']} | {oc['High']} | {oc['Medium']} | {oc['Low']} |"
            )
            out.append("")

        out.append("---\n")

    if overall:
        out.append("**Result: ✅ All image scans passed.**")
    else:
        out.append(
            "**Result: ❌ One or more images introduced new vulnerabilities — "
            "merge blocked.**"
        )

    print("\n".join(out))
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze")
    a.add_argument("--dir", required=True)
    a.add_argument("--new", required=True)
    a.add_argument("--old", default="")
    a.add_argument("--file", default="")
    a.set_defaults(func=analyze)

    c = sub.add_parser("comment")
    c.add_argument("--artifacts", default=".")
    c.set_defaults(func=comment)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
