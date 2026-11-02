#!/usr/bin/env python3
"""Render role-targeted LaTeX sections from tagged content.

    python3 scripts/render.py --role ios [--explain] [--budget N]

Reads content/*.toml and roles/<role>.toml, scores every bullet against
the role's tag weights, keeps the strongest within each section's limits,
and writes sections/*.tex. Those files are build output, not source --
edit content/ and roles/ instead.

Scoring: a bullet's score is its `weight` scaled by the geometric mean of
the role `boost` for each of its tags. Tags the role does not mention
default to 1.0, so an untagged bullet keeps its base weight rather than
vanishing. An entry (job/project) blends its own score with the mean of
its kept bullets additively, at 60/40.

Both choices exist to stop tag-count from dominating substance: a plain
product is exponential in the number of tags, and multiplying an entry by
its bullets applies the same role weighting twice.

Ordering: projects and leadership are ranked by score; experience is not.
Jobs stay in authored (reverse-chronological) order, because moving a
current employer below an older one is what a CV reader treats as a red
flag. Relevance shows up in which bullets survive inside each job.

`--budget` trims the lowest-scoring bullets until at most N remain across
the whole document. The Makefile uses it to fit one page automatically.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
ROLES = ROOT / "roles"
SECTIONS = ROOT / "sections"

GENERATED_HEADER = (
    "% GENERATED FILE -- do not edit.\n"
    "% Produced by scripts/render.py for role: {role}\n"
    "% Edit content/*.toml or roles/*.toml instead.\n"
)


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"error: missing {path.relative_to(ROOT)}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


#: Suffix marking a per-role override table, e.g. `[entry.role_by_label]`
#: or a bullet's `text_by_label`. The pattern started as a way to give one
#: job two titles; it generalises to any field, which is what lets a
#: bullet read differently on the QA CV without forking the content file
#: or adding a bullet that every other role would then have to carry.
BY_LABEL = "_by_label"


def resolve_by_label(node: dict[str, Any], label: str) -> dict[str, Any]:
    """`node` with every `<field>_by_label` override for `label` applied."""
    out = dict(node)
    for key, table in node.items():
        if key.endswith(BY_LABEL) and isinstance(table, dict) and label in table:
            out[key[: -len(BY_LABEL)]] = table[label]
    return out


def resolve_entries(entries: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    """Apply label overrides to every entry and to every bullet under it."""
    out = []
    for entry in entries:
        item = resolve_by_label(entry, label)
        if entry.get("bullets"):
            item["bullets"] = [resolve_by_label(b, label) for b in entry["bullets"]]
        out.append(item)
    return out


def score_of(item: dict[str, Any], boost: dict[str, float]) -> float:
    """Base weight scaled by the *geometric mean* of its tag multipliers.

    A plain product is exponential in tag count -- four tags boosted 2.5x
    would multiply a bullet by ~39, so merely tagging something thoroughly
    outranks work that is substantively stronger. The geometric mean keeps
    the direction of each boost while bounding the result to roughly the
    range of the individual multipliers, which is what the numbers in
    roles/*.toml read as if they mean.
    """
    tags = item.get("tags", [])
    weight = float(item.get("weight", 5))
    if not tags:
        return weight
    product = 1.0
    for tag in tags:
        product *= float(boost.get(tag, 1.0))
    return weight * (product ** (1.0 / len(tags)))


def pick_bullets(
    entry: dict[str, Any], boost: dict[str, float], cap: int
) -> list[dict[str, Any]]:
    """Highest-scoring bullets, capped, restored to their authored order.

    Selection is by score, but presentation keeps the order the author
    wrote them in -- reordering bullets inside a job reads as arbitrary.
    """
    bullets = list(enumerate(entry.get("bullets", [])))
    ranked = sorted(bullets, key=lambda pair: -score_of(pair[1], boost))
    kept = sorted(ranked[:cap], key=lambda pair: pair[0])
    out = []
    for index, bullet in kept:
        item = dict(bullet)
        item["_score"] = score_of(bullet, boost)
        out.append(item)
    return out


def cap_at(position: int, bullet_cap: int, taper: list[int] | None) -> int:
    """Bullet cap for the entry at `position` (0 = first authored).

    `taper` gives older entries fewer lines: [3, 3, 2, 1] means the newest
    two jobs get three bullets, the third two, everything older one. The
    last value repeats. The flat cap still applies as a ceiling.
    """
    if not taper:
        return bullet_cap
    return min(bullet_cap, taper[min(position, len(taper) - 1)])


def rank_entries(
    entries: list[dict[str, Any]], boost: dict[str, float], bullet_cap: int,
    taper: list[int] | None = None,
) -> list[dict[str, Any]]:
    ranked = []
    for position, entry in enumerate(entries):
        kept = pick_bullets(entry, boost, cap_at(position, bullet_cap, taper))
        if not kept and entry.get("bullets"):
            continue
        mean = sum(b["_score"] for b in kept) / len(kept) if kept else 0.0
        item = dict(entry)
        item["_bullets"] = kept
        # Blend additively, not multiplicatively: the entry and its bullets
        # already carry the same tag boosts, so multiplying them squares the
        # role weighting and lets a narrowly-relevant trifle outrank
        # substantially stronger work.
        base = score_of(entry, boost)
        item["_score"] = 0.6 * base + 0.4 * mean if kept else base
        ranked.append(item)
    return ranked


#: Sections the budget will never take a bullet from.
PROTECTED_SECTIONS = frozenset({"education"})


def apply_budget(sections: dict[str, list[dict[str, Any]]], budget: int | None) -> int:
    """Drop the globally lowest-scoring bullets until `budget` remain.

    `budget` counts every bullet in the document, so it matches the total
    the renderer reports. Two protections apply: education is never
    trimmed, and no job is taken below two bullets -- a job showing a
    single line reads as filler, so the budget takes from projects and
    extras first.
    """
    if budget is None:
        return 0
    pool: list[tuple[float, str, int, list]] = []
    for name, entries in sections.items():
        for entry in entries:
            siblings = entry["_bullets"]
            for position, bullet in enumerate(siblings):
                pool.append((bullet["_score"], name, id(bullet), siblings))

    dropped = 0
    while len(pool) > budget:
        pool.sort(key=lambda row: row[0])
        for index, (_, name, ident, siblings) in enumerate(pool):
            if name in PROTECTED_SECTIONS:
                continue
            if name == "experience" and len(siblings) <= 2:
                continue
            # Remove by identity: two bullets can compare equal as dicts,
            # and list.remove() would then delete the wrong one.
            for position, candidate in enumerate(siblings):
                if id(candidate) == ident:
                    del siblings[position]
                    break
            pool.pop(index)
            dropped += 1
            break
        else:
            break  # everything left is protected
    return dropped


# --------------------------------------------------------------------- #
# LaTeX emission
# --------------------------------------------------------------------- #

BARE_PERCENT = re.compile(r"(?<!\\)%")


def tex(value: Any) -> str:
    """Emit authored text safely.

    Content is authored as LaTeX -- \\textbf{...} and friends are intended
    -- so this deliberately does not escape backslashes or braces. It only
    fixes the one character that is silently destructive: an unescaped `%`
    starts a LaTeX comment, so "cut load by 40% and shipped it" swallows
    the rest of the line including the closing brace. Percentages are
    common on a CV and nobody expects to escape them by hand.
    """
    return BARE_PERCENT.sub(r"\\%", str(value))


def esc_url(url: str) -> str:
    return url.replace("%", r"\%").replace("#", r"\#")


def render_heading(profile: dict[str, Any], role: str, headline: str = "") -> str:
    links = " $\\cdot$\n  ".join(
        f"\\underline{{\\href{{{esc_url(l['url'])}}}{{{l['label']}}}}}"
        for l in profile.get("links", [])
    )
    return (
        GENERATED_HEADER.format(role=role)
        + "{\\centering\n"
        f"  {{\\Huge \\scshape {tex(profile['name'])}}} \\\\[1.5pt]\n"
        # The target job title, for CVs applying outside the track the
        # bullets are written in. Omitted when the role does not set one.
        + (f"  {{\\large \\scshape {tex(headline)}}} \\\\[3pt]\n" if headline else "")
        + "  \\small\n"
        f"  {tex(profile['location'])} $\\cdot$\n"
        f"  \\underline{{\\href{{mailto:{profile['email']}}}{{{profile['email']}}}}} $\\cdot$\n"
        f"  {tex(profile['phone'])} \\\\\n"
        f"  {links}\\par}}\n"
        "\\vspace{-4pt}\n"
    )


def render_subheading_section(title: str, entries: list[dict[str, Any]], role: str,
                              tail_space: str = "-12pt") -> str:
    out = [GENERATED_HEADER.format(role=role), f"\\section{{{title}}}",
           "\\resumeSubHeadingListStart", ""]
    for entry in entries:
        out.append("  \\resumeSubheading")
        out.append(f"    {{{tex(entry['org'])}}}{{{tex(entry['location'])}}}")
        out.append(f"    {{{tex(entry['role'])}}}{{{tex(entry['dates'])}}}")
        if entry["_bullets"]:
            out.append("  \\resumeItemListStart")
            for bullet in entry["_bullets"]:
                out.append(f"    \\resumeItem{{{tex(bullet['text'])}}}")
            out.append("  \\resumeItemListEnd")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    out.append(f"\\vspace{{{tail_space}}}")
    return "\n".join(out) + "\n"


def render_education(entries: list[dict[str, Any]], role: str) -> str:
    """School, degree and dates, then the kept details as plain lines.

    No bullets: a degree has facts (thesis, grade), not achievements, and
    plain lines under it read as part of the entry. Same layout as the
    reference CV Anas chose to follow (Nov 2026).
    """
    out = [GENERATED_HEADER.format(role=role), "\\section{Education}",
           "\\resumeSubHeadingListStart", ""]
    for entry in entries:
        details = "".join(f"\\resumeEducationDetail{{{tex(b['text'])}}}"
                          for b in entry["_bullets"])
        out.append("  \\resumeEducation")
        out.append(f"    {{{tex(entry['org'])}}}{{{tex(entry['location'])}}}")
        out.append(f"    {{{tex(entry['role'])}}}{{{tex(entry['dates'])}}}")
        out.append(f"    {{{details}}}")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    out.append("\\vspace{-12pt}")
    return "\n".join(out) + "\n"


def render_projects(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role), "\\section{Projects}",
           "\\vspace{-5pt}", "\\resumeSubHeadingListStart", ""]
    for index, entry in enumerate(entries):
        links = " $|$ ".join(
            f"\\underline{{\\href{{{esc_url(l['url'])}}}{{{l['label']}}}}}"
            for l in entry.get("links", [])
        )
        stack = f" $|$ \\textit{{{tex(entry['stack'])}}}" if entry.get("stack") else ""
        out.append("\\resumeProjectHeading")
        out.append(f"  {{{tex(entry['name'])}{stack}}}")
        out.append(f"  {{{links}}}")
        # No bullets: one short paragraph per project, like the reference.
        if entry["_bullets"]:
            out.append("  \\par\\vspace{-10pt}")
        for bullet in entry["_bullets"]:
            # Plain text: no bold inside project descriptions (Anas, Nov 2026).
            plain = re.sub(r"\\textbf\{([^{}]*)\}", r"\1", bullet["text"])
            out.append(f"  \\resumeDescription{{{tex(plain)}}}")
        if index != len(entries) - 1:
            out.append("\\vspace{-14pt}")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    out.append("\\vspace{-14.5pt}")
    return "\n".join(out) + "\n"


def render_summary(entry: dict[str, Any], role: str) -> str:
    """A short targeted paragraph, for CVs that argue a change of track.

    The platform CVs deliberately have none: when the job title on the
    page already matches the job being applied for, a summary repeats the
    bullets underneath it. A QA application is the opposite case -- the
    reader has to be told why a mobile engineer's CV is in the QA pile,
    and that argument does not fit in a bullet under a job.
    """
    return (
        GENERATED_HEADER.format(role=role)
        + "\\section{Summary}\n"
        "\\vspace{2pt}\n"
        "\\begin{itemize}[leftmargin=0.2in, label={}]\n"
        "  \\small{\\item{\n"
        f"    {tex(entry['text'])}\n"
        "  }}\n\\end{itemize}\n\\vspace{-13pt}\n"
    )


def render_certifications(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role), "\\section{Certifications \\& Training}",
           "\\vspace{-4pt}", "\\resumeSubHeadingListStart", ""]
    for entry in entries:
        links = " $|$ ".join(
            f"\\underline{{\\href{{{esc_url(l['url'])}}}{{{l['label']}}}}}"
            for l in entry.get("links", [])
        )
        suffix = f" $|$ {{\\small {links}}}" if links else ""
        out.append("  \\resumeProjectHeading")
        out.append(f"    {{\\textbf{{{tex(entry['name'])}}}{suffix}}}")
        out.append(f"    {{{tex(entry['dates'])}}}")
        if entry["_bullets"]:
            out.append("  \\resumeItemListStart")
            for bullet in entry["_bullets"]:
                out.append(f"    \\resumeItem{{{tex(bullet['text'])}}}")
            out.append("  \\resumeItemListEnd")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    out.append("\\vspace{-14pt}")
    return "\n".join(out) + "\n"


def render_skills(categories: list[dict[str, Any]], role: str) -> str:
    """Two-column table, as in the reference CV: bold labels on the left,
    items starting one small gap after the LONGEST label (the reference's
    rule; its column only sits mid-page because its longest label is
    "Programming Languages:"). Each row should fit one line; anything
    longer wraps inside the item column, never under the labels."""
    rows = [f"    \\textbf{{{tex(cat['label'])}:}} & {tex(cat['items'])} \\\\"
            for cat in categories]
    return (
        GENERATED_HEADER.format(role=role)
        + "\\section{Technical Skills}\n\\vspace{-2pt}\n"
        "\\begin{itemize}[leftmargin=0.28in, label={}]\n"
        "  \\small\\item\n"
        "  \\begin{tabularx}{\\linewidth}[t]{@{}l@{\\hspace{6pt}}>{\\raggedright\\arraybackslash}X@{}}\n"
        + "\n".join(rows)
        + "\n  \\end{tabularx}\n\\end{itemize}\n\\vspace{-14pt}\n"
    )

def render_leadership(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role), "\\section{Leadership \\& Community}",
           "\\vspace{-5pt}", "\\resumeSubHeadingListStart", ""]
    for entry in entries:
        out.append("  \\resumeProjectHeading")
        out.append(f"    {{{tex(entry['name'])}}}")
        out.append(f"    {{{tex(entry['dates'])}}}")
        if entry["_bullets"]:
            out.append("    \\par\\vspace{-10pt}")
        for bullet in entry["_bullets"]:
            out.append(f"    \\resumeDescription{{{tex(bullet['text'])}}}")
        out.append("")
    out.append("\\resumeSubHeadingListEnd")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- #
# LaTeX emission -- tabular (German Lebenslauf) layout
#
# Selected by roles that set layout = "tabular". The difference is
# structural, not cosmetic: a Lebenslauf leads with a personal-data
# block, runs dates down a fixed left column, and carries a Languages
# section. Everything still comes from the same content/*.toml, so the
# two layouts cannot drift apart.
# --------------------------------------------------------------------- #

def render_personal_header(profile: dict[str, Any], role: str) -> str:
    """Name, then the personal-data block a German application expects.

    Uses `personal.legal_name` (the passport spelling) rather than the
    professional name: uni-assist matches the CV against the degree
    certificate, and a mismatch there is a documents query, not a
    formatting quibble.
    """
    personal = profile.get("personal", {})
    name = personal.get("legal_name") or profile["name"]

    links = " $\\cdot$ ".join(
        f"\\underline{{\\href{{{esc_url(l['url'])}}}{{{l['label']}}}}}"
        for l in profile.get("links", [])
    )

    def field(label: str, value: str, missing: str | None = None) -> str:
        if not value:
            if missing is None:
                return ""
            value = f"\\cvmissing{{{missing}}}"
        return f"\\cvfact{{{label}}}{{{value}}}\n"

    email = profile["email"]
    out = [
        GENERATED_HEADER.format(role=role),
        "\\begin{center}",
        f"  {{\\Huge \\scshape {tex(name)}}}",
        "\\end{center}",
        "\\vspace{-4pt}",
        "",
        "\\section{Personal Details}",
        "\\vspace{2pt}",
    ]
    out.append(field("Date of birth", tex(personal.get("birth_date", "")), "Date of birth"))
    out.append(field("Place of birth", tex(personal.get("birth_place", "")), "Place of birth"))
    out.append(field("Nationality", tex(personal.get("nationality", ""))))
    out.append(field("Address", tex(personal.get("address", "")) or tex(profile["location"])))
    out.append(field("Telephone", tex(profile["phone"])))
    out.append(field("Email", f"\\underline{{\\href{{mailto:{email}}}{{{email}}}}}"))
    out.append(field("Online", links))
    out.append("\\vspace{-6pt}")
    return "\n".join(part for part in out if part != "") + "\n"


def _cv_bullets(bullets: list[dict[str, Any]]) -> list[str]:
    if not bullets:
        return []
    out = ["    \\cvbullets"]
    out += [f"      \\item {tex(b['text'])}" for b in bullets]
    out.append("    \\cvbulletsend")
    return out


def render_tabular_entries(title: str, entries: list[dict[str, Any]], role: str) -> str:
    """Education and Experience: dates left, role/org/bullets right."""
    out = [GENERATED_HEADER.format(role=role), f"\\section{{{title}}}", ""]
    for entry in entries:
        out.append(f"\\cventry{{{tex(entry['dates'])}}}{{%")
        out.append(f"  \\textbf{{{tex(entry['role'])}}} \\\\")
        out.append(f"  \\textit{{\\small {tex(entry['org'])}, {tex(entry['location'])}}}")
        out += _cv_bullets(entry["_bullets"])
        out.append("}")
        out.append("")
    out.append("\\vspace{-4pt}")
    return "\n".join(out) + "\n"


def render_tabular_projects(entries: list[dict[str, Any]], role: str) -> str:
    """Projects have no dates, so the left column carries the stack.

    That keeps the left gutter flush with every other section -- an empty
    column here would read as a rendering fault -- and the technology
    list is the thing an admissions reader scans a project entry for.
    """
    out = [GENERATED_HEADER.format(role=role), "\\section{Projects}", ""]
    for entry in entries:
        links = " $|$ ".join(
            f"\\underline{{\\href{{{esc_url(l['url'])}}}{{{l['label']}}}}}"
            for l in entry.get("links", [])
        )
        out.append(f"\\cventry{{{tex(entry.get('stack', ''))}}}{{%")
        suffix = f" \\hfill {{\\small {links}}}" if links else ""
        out.append(f"  \\textbf{{{tex(entry['name'])}}}{suffix}")
        out += _cv_bullets(entry["_bullets"])
        out.append("}")
        out.append("")
    out.append("\\vspace{-4pt}")
    return "\n".join(out) + "\n"


def render_tabular_skills(categories: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role), "\\section{Technical Skills}",
           "\\vspace{2pt}", ""]
    for cat in categories:
        out.append(f"\\cvfact{{{tex(cat['label'])}}}{{{tex(cat['items'])}}}")
    out.append("\\vspace{-4pt}")
    return "\n".join(out) + "\n"


def render_tabular_certifications(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role),
           "\\section{Certifications \\& Training}", ""]
    for entry in entries:
        out.append(f"\\cventry{{{tex(entry['dates'])}}}{{%")
        out.append(f"  \\textbf{{{tex(entry['name'])}}}")
        out += _cv_bullets(entry["_bullets"])
        out.append("}")
        out.append("")
    out.append("\\vspace{-4pt}")
    return "\n".join(out) + "\n"


def render_languages(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role), "\\section{Languages}",
           "\\vspace{2pt}", ""]
    for entry in entries:
        out.append(f"\\cvfact{{{tex(entry['language'])}}}{{{tex(entry['level'])}}}")
    out.append("\\vspace{-4pt}")
    return "\n".join(out) + "\n"


def render_tabular_leadership(entries: list[dict[str, Any]], role: str) -> str:
    out = [GENERATED_HEADER.format(role=role),
           "\\section{Leadership \\& Community}", ""]
    for entry in entries:
        out.append(f"\\cventry{{{tex(entry['dates'])}}}{{%")
        out.append(f"  {tex(entry['name'])}")
        out += _cv_bullets(entry["_bullets"])
        out.append("}")
        out.append("")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--explain", action="store_true",
                        help="print the score behind every kept and dropped bullet")
    parser.add_argument("--budget", type=int,
                        help="keep at most N bullets document-wide")
    args = parser.parse_args()

    role_path = ROLES / f"{args.role}.toml"
    if not role_path.is_file():
        available = ", ".join(sorted(p.stem for p in ROLES.glob("*.toml")))
        raise SystemExit(f"error: unknown role '{args.role}'. Available: {available}")

    role = load(role_path)
    boost: dict[str, float] = role.get("boost", {})
    limits: dict[str, int] = role.get("limits", {})

    # A file only some roles render must not be mandatory for the rest:
    # languages is tabular-only, certifications and summary are for the
    # roles whose section order names them.
    def optional(filename: str, key: str = "entry") -> list[dict[str, Any]]:
        path = CONTENT / filename
        return load(path).get(key, []) if path.is_file() else []

    label = role.get("label", args.role)
    profile = load(CONTENT / "profile.toml")
    # Private, gitignored: birth date, address, legal name. Absent in CI.
    if (CONTENT / "personal.toml").exists():
        profile.update(load(CONTENT / "personal.toml"))
    # A job that spanned platforms can carry a title per role label
    # (`[entry.role_by_label]`), and a bullet can carry a wording per role
    # label (`text_by_label`). Both are resolved once, here, so every
    # layout downstream reads the plain field.
    education = resolve_entries(load(CONTENT / "education.toml").get("entry", []), label)
    experience = resolve_entries(load(CONTENT / "experience.toml").get("entry", []), label)
    projects = resolve_entries(load(CONTENT / "projects.toml").get("entry", []), label)
    leadership = resolve_entries(load(CONTENT / "leadership.toml").get("entry", []), label)
    certifications = resolve_entries(optional("certifications.toml"), label)
    # Categories take label overrides too: a QA CV wants its testing
    # lines at the top without those lines outranking a platform category
    # on the CVs that are actually about that platform.
    skills = [resolve_by_label(c, label)
              for c in load(CONTENT / "skills.toml").get("category", [])]
    languages = optional("languages.toml")
    # Summaries are per-role by nature: one entry per role label, matched
    # here. A role with no entry simply renders no summary section.
    summary = next((e for e in optional("summary.toml") if e.get("label") == label), None)

    order = role.get("sections", {}).get(
        "order", ["education", "experience", "skills", "projects", "leadership"]
    )

    # Education gets its own cap. A job CV wants one line under the degree;
    # an application CV wants the grade and credit breakdown as well, and
    # sharing the experience cap cannot express both.
    edu = rank_entries(education, boost,
                       limits.get("education_bullets_max",
                                  limits.get("experience_bullets_max", 3)))
    # Experience stays in authored (reverse-chronological) order. Ranking
    # jobs by relevance would push a current role below an older one, which
    # breaks the convention recruiters read for and looks like concealment.
    # Relevance is expressed by which bullets survive inside each job.
    # Older jobs get fewer lines (`experience_bullets_by_rank`), which is
    # what lets four jobs share one page without cutting the newest short.
    exp = rank_entries(experience, boost, limits.get("experience_bullets_max", 5),
                       limits.get("experience_bullets_by_rank"))
    prj = rank_entries(projects, boost, limits.get("project_bullets_max", 1))
    # `pin = true` keeps a project on every CV regardless of score, ahead
    # of the ranked ones -- the most recent work should never be crowded
    # out by a role's tag weighting.
    prj.sort(key=lambda e: (not e.get("pin", False), -e["_score"]))
    prj = prj[: limits.get("projects_max", 4)]
    # Shown in authored order (projects.toml), whatever the role picked:
    # RASM, InsideOut, Sound'sCool, CairoCrew (Anas, Nov 2026).
    authored = {e["name"]: i for i, e in enumerate(projects)}
    prj.sort(key=lambda e: authored[e["name"]])
    # Certifications are scored only for the roles that show the section.
    # Otherwise they would count against the bullet budget on every CV
    # that never renders them.
    if "certifications" in order:
        crt = rank_entries(certifications, boost,
                           limits.get("certification_bullets_max", 1))
        crt.sort(key=lambda e: -e["_score"])
        crt = crt[: limits.get("certifications_max", 2)]
    else:
        crt = []
    ldr = rank_entries(leadership, boost, 1)
    ldr.sort(key=lambda e: -e["_score"])
    ldr = ldr[: limits.get("leadership_max", 1)]

    # Categories marked `first` (Languages) lead every CV and are never
    # ranked out; the remaining slots go to the role's best categories.
    lead = [c for c in skills if c.get("first")]
    rest = sorted((c for c in skills if not c.get("first")),
                  key=lambda c: -score_of(c, boost))
    skl = (lead + rest)[: max(limits.get("skills_max", 5), len(lead))]

    dropped = apply_budget(
        {"education": edu, "experience": exp, "projects": prj,
         "certifications": crt, "leadership": ldr},
        args.budget,
    )

    SECTIONS.mkdir(exist_ok=True)
    if role.get("layout") == "tabular":
        written = {
            "heading.tex": render_personal_header(profile, args.role),
            "education.tex": render_tabular_entries("Education", edu, args.role),
            "experience.tex": render_tabular_entries("Practical Experience", exp, args.role),
            "skills.tex": render_tabular_skills(skl, args.role),
            "projects.tex": render_tabular_projects(prj, args.role),
            "languages.tex": render_languages(languages, args.role),
            "leadership.tex": render_tabular_leadership(ldr, args.role),
        }
        if crt:
            written["certifications.tex"] = render_tabular_certifications(crt, args.role)
    else:
        written = {
            "heading.tex": render_heading(profile, args.role,
                                          (summary or {}).get("headline", "")),
            "education.tex": render_education(edu, args.role),
            "experience.tex": render_subheading_section("Experience", exp, args.role),
            "skills.tex": render_skills(skl, args.role),
            "projects.tex": render_projects(prj, args.role),
            "leadership.tex": render_leadership(ldr, args.role),
        }
        if crt:
            written["certifications.tex"] = render_certifications(crt, args.role)
        if summary and summary.get("text"):
            written["summary.tex"] = render_summary(summary, args.role)
    for name, text in written.items():
        (SECTIONS / name).write_text(text, encoding="utf-8")
    # sections/ is build output this script owns entirely. Rendering a
    # role that emits fewer sections than the last one must not leave the
    # previous role's file behind: order.tex would not \input it, but a
    # later role that does name the section would pick up another role's
    # text without anything reporting it.
    for stale in SECTIONS.glob("*.tex"):
        if stale.name not in written and stale.name != "order.tex":
            stale.unlink()

    # The heading is not orderable: a CV without a name and contact block
    # is not a CV. Emit it first regardless of what the role lists, and
    # ignore it if a role names it explicitly.
    order = ["heading"] + [name for name in order if name != "heading"]
    # A role may list a section its layout does not produce (only tabular
    # emits languages.tex). Inputting a missing file is a hard LaTeX error,
    # so filter rather than trust the role file.
    order = [name for name in order if f"{name}.tex" in written]
    body = "\n\n".join(f"\\input{{sections/{name}}}" for name in order)
    (SECTIONS / "order.tex").write_text(
        GENERATED_HEADER.format(role=args.role) + body + "\n", encoding="utf-8"
    )

    kept = sum(len(e["_bullets"]) for e in exp + prj + ldr + edu + crt)
    print(f"role={args.role} ({role.get('name')})  bullets={kept}"
          + (f"  dropped_by_budget={dropped}" if dropped else ""))
    print("  experience: " + ", ".join(f"{e['org']}({len(e['_bullets'])})" for e in exp))
    print("  projects  : " + ", ".join(e["name"].split(" ---")[0] for e in prj))
    print("  skills    : " + ", ".join(c["label"] for c in skl))

    if args.explain:
        print("\n--- scores ---")
        for label, entries in (("EXPERIENCE", exp), ("PROJECTS", prj), ("LEADERSHIP", ldr)):
            print(f"\n{label}")
            for entry in entries:
                title = entry.get("org") or entry.get("name")
                print(f"  [{entry['_score']:7.1f}] {title}")
                for bullet in entry["_bullets"]:
                    tags = ",".join(bullet.get("tags", []))
                    ev = len(bullet.get("evidence", []))
                    flag = "" if ev else "   <- NO EVIDENCE"
                    print(f"      {bullet['_score']:7.1f}  [{tags}]{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
