#!/usr/bin/env python3
"""Tabular curriculum vitae for a uni-assist master's application.

Layout follows the Hochschule Darmstadt guide for applicants
(mse.h-da.de, "CURRICULUM VITAE – How to apply in Germany") and the
uni-assist description of a Lebenslauf: dates on the left, stations on
the right, reverse chronological, two pages at most, no programme name
so one file can be reused. Facts come from content/*.toml. School is
not in those files and is not invented.
"""

from pathlib import Path

from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

OUT = Path(__file__).resolve().parents[1] / "output" / "Anas_Nasr_Nasr_Mostafa_Lebenslauf.pdf"
OLD = Path(__file__).resolve().parents[1] / "output" / "Anas_Nasr_Nasr_Mostafa_Europass_CV.pdf"

INK = black
MUTED = Color(0.25, 0.25, 0.25)
RULE = Color(0.15, 0.15, 0.15)
HAIR = Color(0.82, 0.82, 0.82)

PAGE_W, PAGE_H = A4
LEFT = 18 * mm
RIGHT = 18 * mm
TOP = 16 * mm
BOTTOM = 16 * mm
DATE_W = 34 * mm
GAP = 4 * mm
BODY_W = PAGE_W - LEFT - RIGHT - DATE_W - GAP


def styles():
    def s(name, **kw):
        base = dict(fontName="Helvetica", textColor=INK, leading=13)
        base.update(kw)
        return ParagraphStyle(name, **base)

    return {
        "name": s("name", fontName="Helvetica-Bold", fontSize=16, leading=19),
        "contact": s("contact", fontSize=9, leading=12),
        "section": s("section", fontName="Helvetica-Bold", fontSize=11, leading=13),
        "date": s("date", fontSize=9, leading=12),
        "title": s("title", fontName="Helvetica-Bold", fontSize=10, leading=13),
        "org": s("org", fontSize=9.5, leading=12),
        "body": s("body", fontSize=9.5, leading=12.5),
        "bullet": s(
            "bullet",
            fontSize=9.5,
            leading=12.5,
            leftIndent=11,
            firstLineIndent=-11,
        ),
        "label": s("label", fontName="Helvetica-Bold", fontSize=9.5, leading=12.5),
        "end": s("end", fontSize=10, leading=14),
    }


S = styles()


def P(text, style):
    return Paragraph(text, style)


def section(title):
    head = Table([[P(title, S["section"])]], colWidths=[PAGE_W - LEFT - RIGHT])
    head.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.6, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return head


def bullets(items):
    if not items:
        return items
    return [P("&bull;&nbsp;&nbsp;" + item, S["bullet"]) for item in items]


def station(dates, title, org, lines, items=None):
    right = [P(title, S["title"])]
    if org:
        right.append(Spacer(1, 1))
        right.append(P(org, S["org"]))
    for line in lines:
        right.append(P(line, S["body"]))
    right.extend(bullets(items or []))
    table = Table(
        [[P(dates, S["date"]), right]],
        colWidths=[DATE_W, BODY_W + GAP],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), GAP),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("LINEBELOW", (0, 0), (-1, -1), 0.2, HAIR),
            ]
        )
    )
    return KeepTogether(table)


def fact(label, value):
    """Label in the date column, so skills line up with the stations above."""
    table = Table(
        [[P(label, S["date"]), P(value, S["body"])]],
        colWidths=[DATE_W, BODY_W + GAP],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), GAP),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    return table


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(HAIR)
    canvas.setLineWidth(0.3)
    canvas.line(LEFT, 11 * mm, PAGE_W - RIGHT, 11 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(LEFT, 7 * mm, "Anas Nasr Nasr Mostafa")
    canvas.drawRightString(PAGE_W - RIGHT, 7 * mm, f"{doc.page}")
    canvas.restoreState()


def build():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(OUT),
        pagesize=A4,
        title="Curriculum vitae — Anas Nasr Nasr Mostafa",
        author="Anas Nasr Nasr Mostafa",
        subject="Tabular curriculum vitae for uni-assist",
        leftMargin=LEFT,
        rightMargin=RIGHT,
        topMargin=TOP,
        bottomMargin=BOTTOM,
    )
    frame = Frame(
        LEFT,
        BOTTOM,
        PAGE_W - LEFT - RIGHT,
        PAGE_H - TOP - BOTTOM,
        id="main",
        showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="cv", frames=[frame], onPage=footer)])

    story = [
        P("Anas Nasr Nasr Mostafa", S["name"]),
        Spacer(1, 3),
        P("23 Salahuddin St., Helwan, Cairo, Egypt", S["contact"]),
        P(
            "+20 102 479 3905&nbsp;&nbsp;·&nbsp;&nbsp;anmostafa03@gmail.com",
            S["contact"],
        ),
        P(
            "Born 1 December 2003 in Cairo&nbsp;&nbsp;·&nbsp;&nbsp;Egyptian",
            S["contact"],
        ),
        P(
            "anasnasr.vercel.app&nbsp;&nbsp;·&nbsp;&nbsp;"
            "github.com/AnasNasr-afk&nbsp;&nbsp;·&nbsp;&nbsp;"
            "linkedin.com/in/anasnasr",
            S["contact"],
        ),
        section("Education"),
        station(
            "10/2022 – 06/2026",
            "B.Sc. Computer Science",
            "Future University in Egypt, New Cairo, Egypt",
            [
                "Cumulative GPA 2.94 / 4.00 across 46 courses and eight semesters.",
                "133 credit hours, about 266 ECTS.",
                "96 in computer science, 15 mathematics, 9 science, 13 general.",
                "Graduation project <b>InsideOut</b>, grade A in both phases.",
                "Selected grades: Algorithms A; Mathematics I–III A / A / A−; Data Structures B.",
            ],
        ),
        section("Practical experience"),
        station(
            "07/2026 – 09/2026",
            "iOS Developer Intern",
            "4Sale International Co., Zamalek, Cairo",
            [],
            [
                "Save-search reminder in the iOS app, in three variants, switched with Remote Config and compared in an A/B test.",
                "Web client for 4Sale Reels (React, TypeScript): paginated feed, playback for low-end devices, Amplitude events for impressions, watch time and engagement.",
                "Same Reels feed as a server-driven UI component on iOS and Android, so layout changes ship without an app release.",
                "Internal landing-page builder for the marketing team: block editor with undo and redo, listings loaded from the search API.",
            ],
        ),
        station(
            "07/2025 – 08/2025",
            "iOS Developer Intern",
            "Banque Misr, Fifth Settlement, Cairo",
            [],
            [
                "SwiftUI and UIKit components used across four internal iOS apps.",
                "Fixed more than 11 interface and API defects, and profiled the slow paths in Xcode Instruments.",
                "Ran sprint planning for five interns with the senior mentors.",
            ],
        ),
        station(
            "07/2024 – 08/2024",
            "Flutter Developer Intern",
            "Hassan Allam Holding, Sheraton, Cairo",
            [],
            [
                "Built more than seven Flutter components from Figma designs for the shared interface layer.",
                "Introduced BLoC and Cubit state management and checked rebuilds in Flutter DevTools.",
                "Prototyped an augmented-reality feature with Vuforia and wrote the feasibility report.",
            ],
        ),
        section("Projects"),
        station(
            "Thesis",
            "InsideOut — Flutter, Firebase, SendBird",
            None,
            [
                "Client for an AI speech avatar (DOOBY) and chat between specialists and parents. github.com/AnasNasr-afk/inside-out",
            ],
        ),
        station(
            "3rd place",
            "RASM — Next.js, TypeScript, PostgreSQL",
            None,
            [
                "Cursor Build-a-thon. Commission workflow for freelance artists, built in four hours, with Paymob checkout confirmed by HMAC-verified webhooks. team-8-rasma.vercel.app",
            ],
        ),
        station(
            "Play Store",
            "Sound'sCool — Flutter, Gemini, Firebase",
            None,
            [
                "Scores pronunciation word by word, about two seconds per attempt, on iOS and Android. com.anasnasr.soundscool",
            ],
        ),
        station(
            "Play Store",
            "CairoCrew — Flutter, Google Maps, Firebase",
            None,
            [
                "Citizens report infrastructure problems on a live map, with separate admin and citizen roles. com.anasnasr.cairocrew",
            ],
        ),
        KeepTogether(
            [
                section("Further training"),
                station(
                    "2025",
                    "Software testing",
                    "Manual testing, Postman, Selenium (Java), Jira",
                    [
                        "Functional, regression and integration tests; test cases and bug reports; SDLC, STLC and Agile testing.",
                    ],
                ),
            ]
        ),
        section("Voluntary activity"),
        station(
            "10/2025 – 05/2026",
            "Vice Head, Flutter chapter",
            "GDG Al-Azhar, Cairo",
            [
                "Workshops on BLoC, Cubit and clean architecture for eight developers, and the chapter curriculum.",
            ],
        ),
        section("Languages"),
        fact(
            "Arabic",
            "Native.",
        ),
        fact(
            "English",
            "B2. IELTS Academic overall 6.0, British Council Egypt, October 2026. Medium of instruction of the B.Sc.",
        ),
        fact(
            "German",
            "About B1, self-assessed. No Goethe or telc certificate. Currently studying.",
        ),
        section("Computer skills"),
        fact(
            "Languages",
            "Swift, Dart, TypeScript, JavaScript, C#, Java, Python.",
        ),
        fact(
            "Mobile",
            "SwiftUI, UIKit, Flutter, BLoC, Cubit, Firebase, Xcode Instruments.",
        ),
        fact(
            "Web and data",
            "React, ASP.NET Core, SQL Server, PostgreSQL, Firestore, Supabase.",
        ),
        fact(
            "Tools",
            "Git, Docker, Kubernetes, GitHub Actions, AWS (S3, ECR, EKS), Jira, Amplitude.",
        ),
        Spacer(1, 12),
        P("Cairo, 7 October 2026", S["end"]),
        P("Anas Nasr Nasr Mostafa", S["end"]),
    ]

    doc.build(story)
    if OLD.exists():
        OLD.unlink()
    print(OUT)


if __name__ == "__main__":
    build()
