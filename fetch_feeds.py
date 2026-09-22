#!/usr/bin/env python3
"""
Build RSS feeds of recent arXiv papers for lists of authors.

For every file authors/<name>.txt, this script queries the arXiv API
one author at a time (politely spaced), keeps papers in the allowed
categories, merges and deduplicates them, and writes feeds/<name>.xml.
It also writes feeds/report.txt with the number of papers found per author.

Line format in authors/*.txt (lines starting with # are ignored):
    Display name | query | optional categories
Examples:
    Sergey Shadrin | Shadrin S
    Jun Li | Jun Li | math.AG
The query is sent to arXiv as au:"<query>".
"""

import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

ATOM = "{http://www.w3.org/2005/Atom}"

# Categories kept by default (an author line can override this).
DEFAULT_CATS = {
    "math.AG", "math-ph", "math.MP", "math.CO", "hep-th",
    "math.QA", "nlin.SI", "math.SG",
}

# Feed titles (file name without .txt -> title).
TITLES = {
    "ag": "arXiv – Algebraic Geometry",
    "mathphys": "arXiv – Mathematical Physics",
    "amplituhedron": "arXiv – Amplituhedron",
}

RESULTS_PER_AUTHOR = 25    # latest papers fetched per author
MAX_AGE_DAYS = 365         # papers older than this are dropped from the feed
MAX_ITEMS = 300            # maximum number of items per feed
DELAY = 3.5                # seconds between API calls (arXiv asks for >= 3)

ROOT = Path(__file__).resolve().parent


def read_authors(path):
    authors = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        name = parts[0]
        query = parts[1] if len(parts) > 1 and parts[1] else name
        cats = DEFAULT_CATS
        if len(parts) > 2 and parts[2]:
            cats = {c.strip() for c in parts[2].split(",") if c.strip()}
        authors.append((name, query, cats))
    return authors


HOSTS = ["https://arxiv.org/api/query", "https://export.arxiv.org/api/query"]
USER_AGENT = "arxiv-author-feeds/1.1 (personal RSS feed; github actions)"


def fetch(query):
    """Fetch one author query with curl (arXiv currently refuses Python urllib)."""
    params = urllib.parse.urlencode({
        "search_query": f'au:"{query}"',
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": RESULTS_PER_AUTHOR,
    })
    for attempt in range(2):
        for host in HOSTS:
            try:
                r = subprocess.run(
                    ["curl", "-sS", "-L", "--compressed", "--max-time", "60",
                     "-A", USER_AGENT,
                     "-H", "Accept: application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
                     "-w", "\n%{http_code}", f"{host}?{params}"],
                    capture_output=True, timeout=90)
                body, _, code = r.stdout.rpartition(b"\n")
                if r.returncode == 0 and code.strip() == b"200" and b"<feed" in body:
                    return body
                print(f"  {host}: HTTP {code.decode().strip() or '?'} {r.stderr.decode().strip()}",
                      file=sys.stderr)
            except Exception as e:
                print(f"  {host}: {e}", file=sys.stderr)
            time.sleep(5)
        time.sleep(20)
    return None


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    papers = []
    for e in root.findall(f"{ATOM}entry"):
        raw_id = e.findtext(f"{ATOM}id", "")
        if "/abs/" not in raw_id:
            continue
        arxiv_id = raw_id.split("/abs/")[1].rsplit("v", 1)[0]
        published = datetime.fromisoformat(
            e.findtext(f"{ATOM}published", "").replace("Z", "+00:00"))
        papers.append({
            "id": arxiv_id,
            "title": " ".join(e.findtext(f"{ATOM}title", "").split()),
            "summary": " ".join(e.findtext(f"{ATOM}summary", "").split()),
            "published": published,
            "authors": [a.findtext(f"{ATOM}name", "")
                        for a in e.findall(f"{ATOM}author")],
            "cats": [c.get("term") for c in e.findall(f"{ATOM}category")],
        })
    return papers


def build_rss(title, papers):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = title
    ET.SubElement(ch, "link").text = "https://arxiv.org"
    ET.SubElement(ch, "description").text = "Recent arXiv papers by followed authors"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    for p in papers:
        url = f"https://arxiv.org/abs/{p['id']}"
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = p["title"]
        ET.SubElement(it, "link").text = url
        ET.SubElement(it, "guid", isPermaLink="true").text = url
        ET.SubElement(it, "pubDate").text = format_datetime(p["published"])
        ET.SubElement(it, "author").text = ", ".join(p["authors"])
        ET.SubElement(it, "description").text = (
            f"<p><b>{escape(', '.join(p['authors']))}</b></p>"
            f"<p><i>Followed: {escape(', '.join(sorted(p['followed'])))}"
            f" · {escape(', '.join(p['cats']))}</i></p>"
            f"<p>{escape(p['summary'])}</p>"
            f"<p><a href=\"https://arxiv.org/pdf/{p['id']}\">PDF</a></p>"
        )
    ET.indent(rss)
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


def main():
    out_dir = ROOT / "feeds"
    out_dir.mkdir(exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    report = []
    total_failures = total_calls = 0
    consecutive_failures = 0

    for path in sorted((ROOT / "authors").glob("*.txt")):
        feed = path.stem
        merged = {}
        failures = 0
        authors = read_authors(path)
        print(f"== {feed}: {len(authors)} authors")
        for name, query, cats in authors:
            total_calls += 1
            data = fetch(query)
            time.sleep(DELAY)
            if data is None:
                failures += 1
                consecutive_failures += 1
                if consecutive_failures >= 8:
                    sys.exit("8 authors failed in a row: arXiv is refusing requests. "
                             "Feeds left unchanged; try again later.")
                report.append(f"{feed:14} | {name:30} | {query:25} | ERROR")
                continue
            consecutive_failures = 0
            papers = parse(data)
            kept = [p for p in papers if set(p["cats"]) & cats]
            report.append(f"{feed:14} | {name:30} | {query:25} | "
                          f"{len(papers):3} found | {len(kept):3} kept")
            print(f"  {name}: {len(papers)} found, {len(kept)} kept")
            for p in kept:
                merged.setdefault(p["id"], {**p, "followed": set()})
                merged[p["id"]]["followed"].add(name)

        total_failures += failures
        if authors and failures > len(authors) / 2:
            print(f"Too many failures for {feed}, feed not updated.", file=sys.stderr)
            continue
        papers = sorted((p for p in merged.values() if p["published"] >= cutoff),
                        key=lambda p: p["published"], reverse=True)[:MAX_ITEMS]
        xml = build_rss(TITLES.get(feed, f"arXiv – {feed}"), papers)
        (out_dir / f"{feed}.xml").write_text(xml, encoding="utf-8")
        print(f"  -> feeds/{feed}.xml ({len(papers)} items)")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = [f"Last run: {stamp}",
              "Authors with 0 found: check the query in authors/*.txt", ""]
    (out_dir / "report.txt").write_text("\n".join(header + report) + "\n",
                                        encoding="utf-8")
    if total_calls and total_failures == total_calls:
        sys.exit("All requests failed (arXiv unreachable?).")


if __name__ == "__main__":
    main()
