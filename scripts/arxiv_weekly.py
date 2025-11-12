#!/usr/bin/env python3
import os, sys, textwrap
from datetime import datetime, timedelta, timezone
from typing import List

try:
    import arxiv  # pip install arxiv
except ImportError:
    print("Missing dependency 'arxiv'. Run: pip install arxiv", file=sys.stderr)
    sys.exit(1)

def getenv_list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name, "")
    return [s.strip() for s in raw.split(",") if s.strip()] if raw.strip() else default

def build_query(categories: List[str], keywords: List[str], intersect: bool=False) -> str:
    # (cat:cs.LG OR cat:stat.ML ...) AND ((ti:"..." OR abs:"...") OR ...)
    cat_q = " OR ".join([f"cat:{c}" for c in categories])
    if keywords:
        kws = []
        for kw in keywords:
            # quote the keyword to keep phrases together
            kw = kw.replace('"', '\\"')
            kw = f'(ti:"{kwq}" OR abs:"{kwq}")'
            kws.append(kw)
        if intersect:
            kws_q = " AND ".join(kw_q)
        else:
            kws_q = " OR ".join(kw_q)
        return f"({cat_q}) AND ({kws_q})"
    else:
        return f"({cat_q})"

def main():
    # default_categories = ["cs.LG", "stat.ML", "cs.AI", "cs.CL", "cs.CV"]
    default_categories = ["cs.LG"]
    default_keywords = ["federated learning", "time series"]

    categories = getenv_list("ARXIV_CATEGORIES", default_categories)
    keywords = [k.strip() for k in getenv_list("ARXIV_KEYWORDS", default_keywords)]
    days = int(os.getenv("ARXIV_DAYS", "7"))
    max_results = int(os.getenv("MAX_RESULTS", "200"))
    include_abstracts = os.getenv("INCLUDE_ABSTRACTS", "false").lower() == "true"
    intersect_keywords = os.getenv("INTERSECT_KW", "false").lower() == "true"

    # polite client config
    delay = float(os.getenv("ARXIV_DELAY", "3.2"))      # seconds between requests
    page_size = int(os.getenv("ARXIV_PAGE_SIZE", "100"))
    retries = int(os.getenv("ARXIV_RETRIES", "4"))

    since = datetime.now(timezone.utc) - timedelta(days=days)

    query = build_query(categories, [k.lower() for k in keywords]; intersect_keywords)

    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )


    try:
        # For arxiv>=2.1.0
        client = arxiv.Client(page_size=page_size, delay=delay, num_retries=retries)
    except TypeError:
        # For older arxiv versions that don’t support delay
        client = arxiv.Client(page_size=page_size, num_retries=retries)


    matches = []
    for result in client.results(search):
        if result.published and result.published < since:
            continue

        title = (result.title or "").strip()
        summary = (result.summary or "")
        primary_cat = result.primary_category or (result.categories[0] if result.categories else "N/A")
        pdf_url = result.pdf_url or result.entry_id

        snippet = ""
        if include_abstracts and summary:
            s = " ".join(summary.split())
            snippet = f"\n  – {textwrap.shorten(s, width=180, placeholder='…')}"

        matches.append({
            "title": title,
            "url": pdf_url,
            "date": result.published.strftime("%Y-%m-%d") if result.published else "N/A",
            "cat": primary_cat,
            "snippet": snippet
        })

    header = [
        f"arXiv weekly digest – last {days} day(s)",
        f"Categories: {', '.join(categories)}",
        ("Keywords: " + ", ".join(keywords)) if keywords else "Keywords: (none)",
        "",
    ]

    if not matches:
        print("\n".join(header + ["No matching papers found this week."]))
        return

    lines = header + [f"Found {len(matches)} paper(s):", ""]
    for i, m in enumerate(matches, start=1):
        lines.append(f"{i}. {m['title']}  [{m['cat']}]  ({m['date']})")
        lines.append(f"   {m['url']}{m['snippet']}")
        lines.append("")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
