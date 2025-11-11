#!/usr/bin/env python3
"""
arxiv_weekly.py
- Fetch recent arXiv submissions (last N days, default 7)
- Filter by categories and keywords (title/abstract)
- Print a plain-text email body with matching paper titles + links
Environment variables:
  ARXIV_CATEGORIES   comma-separated (e.g. "cs.LG,stat.ML,cs.AI,cs.CL,cs.CV")
  ARXIV_KEYWORDS     comma-separated (e.g. "federated learning,time series")
  ARXIV_DAYS         integer days back (default 7)
  MAX_RESULTS        max results to fetch before filtering (default 200)
  INCLUDE_ABSTRACTS  "true"/"false" (default "false") – add 1-line abstract snippet
"""

import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from typing import List

try:
    import arxiv  # pip install arxiv
except ImportError:
    print("Missing dependency 'arxiv'. Did you install requirements.txt?", file=sys.stderr)
    sys.exit(1)


def getenv_list(name: str, default_list: List[str]) -> List[str]:
    raw = os.getenv(name, "")
    if raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return default_list


def main():
    # Defaults: common ML-related categories + keywords
    default_categories = ["cs.LG", "stat.ML", "cs.AI", "cs.CL", "cs.CV"]
    default_keywords = ["federated learning", "time series"]

    categories = getenv_list("ARXIV_CATEGORIES", default_categories)
    keywords = [k.lower() for k in getenv_list("ARXIV_KEYWORDS", default_keywords)]
    days = int(os.getenv("ARXIV_DAYS", "7"))
    max_results = int(os.getenv("MAX_RESULTS", "200"))
    include_abstracts = os.getenv("INCLUDE_ABSTRACTS", "false").lower() == "true"

    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Build arXiv query: OR across categories; we’ll sort by submittedDate and then filter by date + keywords
    # Example query: (cat:cs.LG OR cat:stat.ML OR cat:cs.AI ...)
    cat_query = " OR ".join([f"cat:{c}" for c in categories])
    query = f"({cat_query})"

    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    matches = []
    for result in search.results():
        # Filter by submission date
        published = result.published  # timezone-aware datetime
        if published is None or published < since:
            continue

        title_l = (result.title or "").lower()
        summary_l = (result.summary or "").lower()

        # If no keywords specified, accept all; otherwise match any keyword in title or abstract
        if keywords:
            if not any((kw in title_l) or (kw in summary_l) for kw in keywords):
                continue

        primary_cat = result.primary_category or (result.categories[0] if result.categories else "N/A")
        pdf_url = result.pdf_url or result.entry_id  # fall back to entry page

        # Optional 1-line abstract snippet
        snippet = ""
        if include_abstracts and result.summary:
            s = " ".join(result.summary.split())  # single line
            snippet = f"\n  – {textwrap.shorten(s, width=180, placeholder='…')}"

        matches.append({
            "title": result.title.strip(),
            "url": pdf_url,
            "date": published.strftime("%Y-%m-%d"),
            "cat": primary_cat,
            "snippet": snippet
        })

    # Build plain text email body
    header = [
        f"arXiv weekly digest – last {days} day(s)",
        f"Categories: {', '.join(categories)}",
        ("Keywords: " + ", ".join(keywords)) if keywords else "Keywords: (none)",
        "",
    ]

    if not matches:
        body = "\n".join(header + ["No matching papers found this week."])
        print(body)
        return

    lines = header + [f"Found {len(matches)} paper(s):", ""]
    for i, m in enumerate(matches, start=1):
        lines.append(f"{i}. {m['title']}  [{m['cat']}]  ({m['date']})")
        lines.append(f"   {m['url']}{m['snippet']}")
        lines.append("")  # blank line between items

    print("\n".join(lines))


if __name__ == "__main__":
    main()
