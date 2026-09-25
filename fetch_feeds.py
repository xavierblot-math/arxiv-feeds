#!/usr/bin/env python3
"""
Personal arXiv feeds, built from arXiv's daily announcement RSS.

Every run downloads the daily announcement feed of each category in
CATEGORIES (one request each, from rss.arxiv.org), keeps "new" and "cross"
announcements, and matches every announced paper against:
  - authors/<feed>.txt : "Full name | (ignored) | optional categories"
  - keywords/<feed>.txt: "expression | optional categories"
Matches are added to data/papers.json (the store, which accumulates over
time), and feeds/<feed>.xml is rebuilt from the store at every run.

The arXiv search API (export.arxiv.org/api/query) is no longer used: since
mid-September 2026 it answers HTTP 406 to any query that misses its cache.
"""

import json
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from pathlib import Path

# Categories whose daily announcements are downloaded.
CATEGORIES = [
    "math.AG", "math-ph", "math.CO", "hep-th",
    "math.QA", "nlin.SI", "math.SG",
]

# Announcement types kept: "new", "cross", "replace", "replace-cross".
ACCEPT_TYPES = {"new", "cross"}

# Categories kept by default (a line in authors/ or keywords/ can override).
DEFAULT_CATS = {
    "math.AG", "math-ph", "math.MP", "math.CO", "hep-th",
    "math.QA", "nlin.SI", "math.SG",
}

# Feed titles (file name without .txt -> title).
TITLES = {
    "ag": "arXiv – Algebraic Geometry",
    "mathphys": "arXiv – Mathematical Physics",
    "amplituhedron": "arXiv – Amplituhedron",
    "topics": "arXiv – Topics",
    "broad": "arXiv – Broad",
}

RSS_HOSTS = ["https://rss.arxiv.org/rss/", "https://export.arxiv.org/rss/"]
USER_AGENT = "personal-arxiv-feeds/2.0 (daily RSS reader)"
MAX_AGE_DAYS = 365     # papers older than this leave the feeds and the store
MAX_ITEMS = 300        # maximum number of items per feed
DELAY = 3.0            # seconds between downloads

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "data" / "papers.json"
DC = "{http://purl.org/dc/elements/1.1/}"
ARXIV = "{http://arxiv.org/schemas/atom}"


# ---------------------------------------------------------------- matching

def norm(text):
    """Lowercase, strip accents and punctuation: 'Lewański' -> ['lewanski']."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in text.lower()).split()


def name_matches(full_name, paper_author):
    """True if paper_author is the same person as full_name.

    Same surname, and:
      - if the paper spells a given name out, one of them must match the
        full name's given names ("Youjin Zhang" is not "Yong Zhang"),
      - if the paper only uses initials, the first one must match
        ("S. V. Shadrin" yes, "O. S. Shadrin" no).
    'Ran Tessler' matches 'Ran J. Tessler' and 'R. Tessler';
    'Melissa Liu' matches 'Chiu-Chu Melissa Liu'."""
    want, got = norm(full_name), norm(paper_author)
    if not want or not got:
        return False
    surname = want[-1]
    if surname not in got:
        return False
    want_given, got_given = want[:-1], got[:got.index(surname)]
    if not want_given or not got_given:
        return False
    spelled = [g for g in got_given if len(g) > 1]
    if not spelled:                       # the paper only has initials
        return got_given[0][0] == want_given[0][0]
    for g in spelled:
        for w in want_given:
            if g == w:
                return True
            if min(len(g), len(w)) >= 4 and (g.startswith(w) or w.startswith(g)):
                return True               # Jérémy / Jeremie-style variants
    return False


def read_lines(path, kind):
    """Read an authors/ or keywords/ file -> [(label, categories), ...]."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        cats = DEFAULT_CATS
        # authors: "name | query | cats" (query is no longer used)
        # keywords: "expression | cats"
        cat_field = parts[2] if kind == "authors" and len(parts) > 2 else (
            parts[1] if kind == "keywords" and len(parts) > 1 else "")
        if cat_field:
            cats = {c.strip() for c in cat_field.split(",") if c.strip()}
        out.append((parts[0], cats))
    return out


# ---------------------------------------------------------------- download

def fetch(url):
    """Download one URL with curl (Python's urllib is refused by arXiv)."""
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["curl", "-sS", "-L", "--compressed", "--max-time", "90",
                 "-A", USER_AGENT,
                 "-H", "Accept: application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
                 "-w", "\n%{http_code}", url],
                capture_output=True, timeout=120)
            body, _, code = r.stdout.rpartition(b"\n")
            if r.returncode == 0 and code.strip() == b"200" and b"<rss" in body:
                return body
            print(f"  {url}: HTTP {code.decode().strip() or '?'} "
                  f"{r.stderr.decode().strip()}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  {url}: {e}", file=sys.stderr, flush=True)
        time.sleep(10 * (attempt + 1))
    return None


def parse_announcements(xml_bytes):
    """Parse one daily announcement feed -> list of papers."""
    root = ET.fromstring(xml_bytes)
    papers = []
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "")
        link = (item.findtext("link") or "")
        arxiv_id = ""
        if ":" in guid:
            arxiv_id = guid.rsplit(":", 1)[1]
        elif "/abs/" in link:
            arxiv_id = link.split("/abs/")[1]
        arxiv_id = arxiv_id.split("v")[0] if arxiv_id[:1].isdigit() else arxiv_id
        if not arxiv_id:
            continue

        description = item.findtext("description") or ""
        announce = item.findtext(f"{ARXIV}announce_type") or ""
        if not announce and "Announce Type:" in description:
            announce = description.split("Announce Type:")[1].split("\n")[0]
        announce = announce.strip().lower()

        summary = description
        if "Abstract:" in summary:
            summary = summary.split("Abstract:", 1)[1]
        summary = " ".join(summary.split())

        authors = [a.strip() for a in
                   (item.findtext(f"{DC}creator") or "").split(",") if a.strip()]
        cats = []
        for c in item.findall("category"):
            cats += [t.strip() for t in (c.text or "").split() if t.strip()]

        papers.append({
            "id": arxiv_id,
            "title": " ".join((item.findtext("title") or "").split()),
            "summary": summary,
            "authors": authors,
            "cats": cats,
            "announce": announce,
            "link": link or f"https://arxiv.org/abs/{arxiv_id}",
        })
    return papers


# ------------------------------------------------------------------- store

def load_store():
    if STORE.exists():
        try:
            return json.loads(STORE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"store unreadable ({e}), starting a new one", file=sys.stderr)
    return {}


def save_store(store):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(store, ensure_ascii=False, indent=1,
                                sort_keys=True), encoding="utf-8")


# ------------------------------------------------------------------- feeds

def build_rss(title, papers):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = title
    ET.SubElement(ch, "link").text = "https://arxiv.org"
    ET.SubElement(ch, "description").text = "arXiv announcements (personal feed)"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(
        datetime.now(timezone.utc))
    for p in papers:
        url = p.get("link") or f"https://arxiv.org/abs/{p['id']}"
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = p["title"]
        ET.SubElement(it, "link").text = url
        ET.SubElement(it, "guid", isPermaLink="true").text = url
        ET.SubElement(it, "pubDate").text = format_datetime(
            datetime.fromisoformat(p["seen"]))
        ET.SubElement(it, "author").text = ", ".join(p["authors"])
        matched = ", ".join(p.get("matched", []))
        ET.SubElement(it, "description").text = (
            f"<p><b>{escape(', '.join(p['authors']))}</b></p>"
            f"<p><i>Matched: {escape(matched)}"
            f" · {escape(', '.join(p['cats']))}"
            f" · {escape(p.get('announce', ''))}</i></p>"
            f"<p>{escape(p['summary'])}</p>"
            f"<p><a href=\"https://arxiv.org/pdf/{p['id']}\">PDF</a></p>"
        )
    ET.indent(rss)
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


# -------------------------------------------------------------------- main

def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    # 1. the rules: feed name -> (kind, [(label, categories)])
    rules = {}
    for kind in ("authors", "keywords"):
        for path in sorted((ROOT / kind).glob("*.txt")):
            rules[path.stem] = (kind, read_lines(path, kind))
            print(f"{path}: {len(rules[path.stem][1])} lines")

    # 2. today's announcements
    announced, failures = [], 0
    for cat in CATEGORIES:
        data = None
        for host in RSS_HOSTS:
            data = fetch(host + cat)
            if data:
                break
        time.sleep(DELAY)
        if data is None:
            failures += 1
            print(f"== {cat}: FAILED", flush=True)
            continue
        papers = [p for p in parse_announcements(data)
                  if p["announce"] in ACCEPT_TYPES]
        announced += papers
        print(f"== {cat}: {len(papers)} new/cross announcements", flush=True)

    if failures == len(CATEGORIES):
        sys.exit("All category feeds failed; store and feeds left unchanged.")

    # 3. match against the rules, merge into the store
    store = load_store()
    added, per_feed_new = 0, {}
    for paper in {p["id"]: p for p in announced}.values():
        hits = {}
        text = " ".join(norm(paper["title"] + " " + paper["summary"]))
        for feed, (kind, lines) in rules.items():
            for label, cats in lines:
                if not set(paper["cats"]) & cats:
                    continue
                if kind == "authors":
                    ok = any(name_matches(label, a) for a in paper["authors"])
                else:
                    ok = " ".join(norm(label)) in text
                if ok:
                    hits.setdefault(feed, []).append(label)
        if not hits:
            continue
        entry = store.get(paper["id"])
        if entry is None:
            entry = dict(paper, seen=now.isoformat(), feeds={}, matched=[])
            added += 1
        for feed, labels in hits.items():
            entry["feeds"][feed] = sorted(set(entry["feeds"].get(feed, []))
                                          | set(labels))
            per_feed_new[feed] = per_feed_new.get(feed, 0) + 1
        entry["matched"] = sorted({l for ls in entry["feeds"].values() for l in ls})
        store[paper["id"]] = entry

    # 4. drop old papers, save, rebuild every feed
    store = {i: e for i, e in store.items()
             if datetime.fromisoformat(e["seen"]) >= cutoff}
    save_store(store)

    out_dir = ROOT / "feeds"
    out_dir.mkdir(exist_ok=True)
    report = [f"Last run: {now.strftime('%Y-%m-%d %H:%M UTC')}",
              f"Announcements downloaded: {len(announced)}"
              f" ({failures} category feeds failed)",
              f"New papers stored this run: {added}", ""]
    for feed in sorted(rules):
        papers = sorted((e for e in store.values() if feed in e["feeds"]),
                        key=lambda e: e["seen"], reverse=True)[:MAX_ITEMS]
        xml = build_rss(TITLES.get(feed, f"arXiv – {feed}"),
                        [dict(p, matched=p["feeds"][feed]) for p in papers])
        (out_dir / f"{feed}.xml").write_text(xml, encoding="utf-8")
        report.append(f"{feed:14} | {len(papers):4} items"
                      f" | {per_feed_new.get(feed, 0):3} new this run")
        print(f"-> feeds/{feed}.xml ({len(papers)} items)", flush=True)

    # which lines matched something today (helps tuning)
    if added:
        report += ["", "Matched this run:"]
        for entry in sorted(store.values(), key=lambda e: e["seen"],
                            reverse=True)[:60]:
            if entry["seen"] >= now.isoformat()[:10]:
                report.append(f"  {', '.join(entry['matched']):40} | "
                              f"{entry['title'][:70]}")
    (out_dir / "report.txt").write_text("\n".join(report) + "\n",
                                        encoding="utf-8")


if __name__ == "__main__":
    main()
