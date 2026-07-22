#!/usr/bin/env python3
"""Create a reproducible snapshot of AFK Global character list and detail pages.

The crawler deliberately keeps both structured extraction and compressed raw HTML.
That way a future parser fix does not require hammering the source site again.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.afk.global"
GAMES = {
    "afk-arena": {
        "label": "AFK Arena",
        "list_url": f"{BASE_URL}/afk-arena/characters",
        "expected_min": 240,
    },
    "afk-journey": {
        "label": "AFK Journey",
        "list_url": f"{BASE_URL}/afk-journey/characters",
        "expected_min": 110,
    },
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36 AFKGlobalSnapshot/1.0"
)

DETAIL_LABELS = [
    "Faction",
    "Class",
    "Type",
    "Role",
    "Rarity",
    "Race",
    "Gender",
    "Damage Type",
    "Range",
]

MODE_NAMES = {
    "Campaign",
    "Tower",
    "Cursed Realm",
    "Nightmare Corridor",
    "Treasure Scramble",
    "Temporal Rift",
    "PvP",
    "Dream Realm",
    "Battle Drills",
    "Supreme Arena",
    "Arcane Labyrinth",
    "Guild Hunt",
    "AFK Stages",
    "Dura's Trials",
}

GRADE_RE = re.compile(r"^(?:S\+|S|A\+|A|B\+|B|C\+|C|D|N/A)$", re.I)
SKILL_MARKER_RE = re.compile(
    r"^(?:SKILL\s*(?:I{1,4}|\d+)|ULTIMATE|EXCLUSIVE\s+EQUIPMENT|HERO\s+FOCUS|ENHANCE\s+FORCE|SEASON\s+SKILL)$",
    re.I,
)
LEVEL_RE = re.compile(r"^(?:Lv\.?\s*\+?\d+|Level\s*\d+|\+\d+|E\d+|\d+/\d+)$", re.I)
PROGRESSION_MARKERS = {
    "SIGNATURE ITEM": "signature_item",
    "FURNITURE": "furniture",
    "ENGRAVING": "engraving",
    "ETERNAL ENGRAVING": "engraving",
}
STOP_WORDS = {
    "OVERVIEW",
    "SKILLS",
    "PROGRESSION SYSTEMS",
    "LORE",
    "STORY",
    "MODE RATINGS",
    "DETAILS",
    "ADVANCED",
}

_thread_local = threading.local()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def slug_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        status=6,
        backoff_factor=0.8,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
    )
    return session


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()
    return _thread_local.session


def fetch(url: str, timeout: int = 45) -> requests.Response:
    # Light jitter avoids turning a snapshot into an accidental load test.
    time.sleep(random.uniform(0.10, 0.32))
    response = get_session().get(url, timeout=timeout)
    response.raise_for_status()
    return response


def absolute_asset_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("data:"):
        return None
    return urljoin(BASE_URL, value)


def clean_soup_for_text(soup: BeautifulSoup) -> Tag:
    root = soup.find("main") or soup.find("article") or soup.body or soup
    for node in list(root.select("script,style,noscript,svg,template")):
        node.decompose()
    return root


def visible_lines(soup: BeautifulSoup) -> list[str]:
    root = clean_soup_for_text(soup)
    result: list[str] = []
    for value in root.stripped_strings:
        text = normalize_space(str(value))
        if text and (not result or result[-1] != text):
            result.append(text)
    return result


def heading_sections(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Capture heading-centered DOM sections without assuming CSS classes."""
    root = soup.find("main") or soup.find("article") or soup.body or soup
    headings = root.find_all(re.compile(r"^h[1-6]$"))
    sections: list[dict[str, Any]] = []
    for heading in headings:
        title = normalize_space(heading.get_text(" ", strip=True))
        if not title:
            continue
        level = int(heading.name[1])
        values: list[str] = []
        node = heading.next_element
        while node:
            if isinstance(node, Tag) and re.fullmatch(r"h[1-6]", node.name or ""):
                next_level = int(node.name[1])
                if node is not heading and next_level <= level:
                    break
            if isinstance(node, str):
                value = normalize_space(node)
                if value and value != title and (not values or values[-1] != value):
                    values.append(value)
            node = node.next_element
        # Avoid massive nav/footer capture while retaining useful card text.
        sections.append({"heading": title, "level": level, "lines": values[:600]})
    return sections


def first_meta(soup: BeautifulSoup, *selectors: tuple[str, str]) -> str | None:
    for attr, value in selectors:
        node = soup.find("meta", attrs={attr: value})
        if node and node.get("content"):
            return normalize_space(node["content"])
    return None


def find_exact_index(lines: list[str], names: Iterable[str], start: int = 0) -> int | None:
    wanted = {normalize_space(name).upper() for name in names}
    for i in range(start, len(lines)):
        if lines[i].upper() in wanted:
            return i
    return None


def section_slice(lines: list[str], start_names: Iterable[str], end_names: Iterable[str]) -> list[str]:
    start = find_exact_index(lines, start_names)
    if start is None:
        return []
    end = find_exact_index(lines, end_names, start + 1)
    return lines[start + 1 : end if end is not None else len(lines)]


def extract_details(lines: list[str]) -> dict[str, str]:
    details: dict[str, str] = {}
    start = find_exact_index(lines, {"DETAILS"})
    scan = lines[start + 1 :] if start is not None else lines
    lower_labels = {label.lower(): label for label in DETAIL_LABELS}

    for i, line in enumerate(scan):
        key = lower_labels.get(line.lower().rstrip(":"))
        if key and i + 1 < len(scan):
            value = scan[i + 1]
            if value.lower().rstrip(":") not in lower_labels and value.upper() not in STOP_WORDS:
                details[key.lower().replace(" ", "_")] = value

    # Some layouts render label and value in one text node.
    joined = "\n".join(scan)
    for label in DETAIL_LABELS:
        key = label.lower().replace(" ", "_")
        if key in details:
            continue
        match = re.search(rf"(?im)^{re.escape(label)}\s*:?\s*\n?\s*([^\n]+)$", joined)
        if match:
            value = normalize_space(match.group(1))
            if value and value.lower() != label.lower():
                details[key] = value
    return details


def trim_card_noise(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        text = normalize_space(value)
        if not text:
            continue
        if text.upper() in {"READ MORE", "SHOW MORE", "SHOW LESS", "COPY"}:
            continue
        if not output or output[-1] != text:
            output.append(text)
    return output


def split_by_markers(lines: list[str], marker_re: re.Pattern[str]) -> list[dict[str, Any]]:
    starts = [i for i, line in enumerate(lines) if marker_re.match(line)]
    records: list[dict[str, Any]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        chunk = trim_card_noise(lines[start + 1 : end])
        while chunk and chunk[0].upper() in STOP_WORDS:
            chunk.pop(0)
        # Tiny labels such as S1/S2/SKI/ULT are visual abbreviations, not names.
        while chunk and re.fullmatch(r"(?:S\d+|SKI|ULT|EE|HF|EF)", chunk[0], re.I):
            chunk.pop(0)
        name = chunk[0] if chunk else None
        body = chunk[1:] if len(chunk) > 1 else []
        levels: list[dict[str, Any]] = []
        intro: list[str] = []
        current: dict[str, Any] | None = None
        for value in body:
            if LEVEL_RE.match(value):
                if current:
                    levels.append(current)
                current = {"level": value, "text": []}
            elif current is not None:
                current["text"].append(value)
            else:
                intro.append(value)
        if current:
            levels.append(current)
        records.append(
            {
                "marker": lines[start],
                "name": name,
                "description": " ".join(intro).strip() or None,
                "levels": levels,
                "raw_lines": chunk,
            }
        )
    return records


def extract_skills(lines: list[str]) -> list[dict[str, Any]]:
    # Restrict to the skill portion where possible so progression labels are not mixed in.
    start = find_exact_index(lines, {"SKILLS"})
    if start is None:
        candidate = lines
    else:
        ends = [
            find_exact_index(lines, {"PROGRESSION SYSTEMS", "ADVANCED", "LORE", "STORY", "MODE RATINGS", "DETAILS"}, start + 1)
        ]
        end = next((value for value in ends if value is not None), len(lines))
        candidate = lines[start + 1 : end]
    return split_by_markers(candidate, SKILL_MARKER_RE)


def extract_progression(lines: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    indices: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        key = PROGRESSION_MARKERS.get(line.upper())
        if key:
            indices.append((i, key, line))
    for position, (start, key, marker) in enumerate(indices):
        end = indices[position + 1][0] if position + 1 < len(indices) else len(lines)
        hard_end = find_exact_index(lines, {"LORE", "STORY", "MODE RATINGS", "DETAILS"}, start + 1)
        if hard_end is not None:
            end = min(end, hard_end)
        raw = trim_card_noise(lines[start + 1 : end])
        while raw and raw[0].upper() in STOP_WORDS:
            raw.pop(0)
        levels: list[dict[str, Any]] = []
        intro: list[str] = []
        current: dict[str, Any] | None = None
        for value in raw:
            if LEVEL_RE.match(value):
                if current:
                    levels.append(current)
                current = {"level": value, "text": []}
            elif current is not None:
                current["text"].append(value)
            else:
                intro.append(value)
        if current:
            levels.append(current)
        result[key] = {
            "marker": marker,
            "description": " ".join(intro).strip() or None,
            "levels": levels,
            "raw_lines": raw,
        }
    return result


def extract_lore(lines: list[str]) -> str | None:
    values = section_slice(lines, {"LORE", "STORY"}, {"MODE RATINGS", "DETAILS"})
    values = [v for v in trim_card_noise(values) if v.upper() not in STOP_WORDS]
    return "\n\n".join(values).strip() or None


def extract_mode_ratings(lines: list[str]) -> dict[str, str]:
    values = section_slice(lines, {"MODE RATINGS"}, {"DETAILS"})
    values = trim_card_noise(values)
    ratings: dict[str, str] = {}
    for i, value in enumerate(values):
        if value in MODE_NAMES:
            neighbors = values[max(0, i - 2) : i] + values[i + 1 : i + 3]
            grade = next((item for item in neighbors if GRADE_RE.match(item)), None)
            if grade:
                ratings[value] = grade
        elif GRADE_RE.match(value) and i + 1 < len(values) and values[i + 1] in MODE_NAMES:
            ratings[values[i + 1]] = value
    return ratings


def extract_images(soup: BeautifulSoup, page_url: str, game: str) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in soup.find_all("img"):
        candidates = [node.get("src"), node.get("data-src"), node.get("data-lazy-src")]
        srcset = node.get("srcset") or node.get("data-srcset")
        if srcset:
            candidates.extend(item.strip().split(" ")[0] for item in srcset.split(","))
        for candidate in candidates:
            url = absolute_asset_url(candidate)
            if not url or url in seen:
                continue
            if f"/characters/{game}/" not in url and "/characters/" not in url:
                continue
            seen.add(url)
            images.append(
                {
                    "url": url,
                    "alt": normalize_space(node.get("alt") or "") or None,
                    "width": node.get("width"),
                    "height": node.get("height"),
                    "kind": (
                        "portrait"
                        if "/portrait/" in url
                        else "skill"
                        if "/skills/" in url
                        else "character_asset"
                    ),
                }
            )
    return images


def extract_title(soup: BeautifulSoup, name: str | None, lines: list[str]) -> str | None:
    h1 = soup.find("h1")
    if h1:
        # Prefer a nearby short heading/paragraph after the H1.
        for node in h1.find_all_next(["h2", "h3", "p", "span"], limit=12):
            value = normalize_space(node.get_text(" ", strip=True))
            if value and value != name and 2 <= len(value) <= 100 and value.upper() not in STOP_WORDS:
                return value
    if name and name in lines:
        i = lines.index(name)
        for value in lines[i + 1 : i + 8]:
            if value != name and value.upper() not in STOP_WORDS and len(value) <= 100:
                return value
    return None


def parse_character(html: bytes, url: str, game: str, fetched_at: str, status_code: int) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    name = normalize_space(h1.get_text(" ", strip=True)) if h1 else None
    if not name:
        og_title = first_meta(soup, ("property", "og:title"), ("name", "twitter:title"))
        if og_title:
            name = re.split(r"\s*[|–—-]\s*", og_title)[0].strip()
    lines = visible_lines(soup)
    canonical = soup.find("link", rel="canonical")
    images = extract_images(soup, url, game)
    portrait = next((image["url"] for image in images if image["kind"] == "portrait"), None)

    return {
        "game": game,
        "game_label": GAMES[game]["label"],
        "name": name,
        "slug": slug_from_url(url),
        "title": extract_title(soup, name, lines),
        "overview": first_meta(soup, ("name", "description"), ("property", "og:description")),
        "details": extract_details(lines),
        "skills": extract_skills(lines),
        "progression": extract_progression(lines),
        "lore": extract_lore(lines),
        "mode_ratings": extract_mode_ratings(lines),
        "portrait_url": portrait,
        "images": images,
        "heading_sections": heading_sections(soup),
        "raw_text_lines": lines,
        "source": {
            "url": url,
            "canonical_url": canonical.get("href") if canonical else None,
            "fetched_at": fetched_at,
            "http_status": status_code,
            "content_type": None,
            "html_sha256": sha256_bytes(html),
            "html_bytes": len(html),
        },
    }


def discover_character_urls(html: bytes, list_url: str, game: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    pattern = re.compile(rf"^/{re.escape(game)}/characters/[^/?#]+/?$")
    urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url = urljoin(list_url, anchor["href"])
        path = urlparse(url).path
        if pattern.match(path):
            urls.add(f"{BASE_URL}{path.rstrip('/')}")
    return sorted(urls, key=lambda item: slug_from_url(item))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "game",
        "name",
        "slug",
        "title",
        "faction",
        "class",
        "type",
        "role",
        "rarity",
        "race",
        "gender",
        "portrait_url",
        "skill_count",
        "progression_systems",
        "has_lore",
        "source_url",
        "fetched_at",
        "html_sha256",
        "skills_json",
        "mode_ratings_json",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            details = record.get("details") or {}
            writer.writerow(
                {
                    "game": record.get("game"),
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "title": record.get("title"),
                    "faction": details.get("faction"),
                    "class": details.get("class"),
                    "type": details.get("type"),
                    "role": details.get("role"),
                    "rarity": details.get("rarity"),
                    "race": details.get("race"),
                    "gender": details.get("gender"),
                    "portrait_url": record.get("portrait_url"),
                    "skill_count": len(record.get("skills") or []),
                    "progression_systems": ",".join(sorted((record.get("progression") or {}).keys())),
                    "has_lore": bool(record.get("lore")),
                    "source_url": (record.get("source") or {}).get("url"),
                    "fetched_at": (record.get("source") or {}).get("fetched_at"),
                    "html_sha256": (record.get("source") or {}).get("html_sha256"),
                    "skills_json": json.dumps(record.get("skills") or [], ensure_ascii=False),
                    "mode_ratings_json": json.dumps(record.get("mode_ratings") or {}, ensure_ascii=False),
                }
            )


def save_raw_html(path: Path, html: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb", compresslevel=9) as handle:
        handle.write(html)


def safe_asset_filename(url: str, fallback: str) -> str:
    path = Path(urlparse(url).path)
    name = path.name or fallback
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name or fallback


def download_portrait(record: dict[str, Any], game_dir: Path) -> dict[str, Any] | None:
    url = record.get("portrait_url")
    if not url:
        return None
    target_dir = game_dir / "images" / "portraits"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_asset_filename(url, f"{record['slug']}.webp")
    target = target_dir / filename
    try:
        response = fetch(url, timeout=60)
        target.write_bytes(response.content)
        return {
            "url": url,
            "path": target.relative_to(game_dir).as_posix(),
            "bytes": len(response.content),
            "sha256": sha256_bytes(response.content),
            "content_type": response.headers.get("content-type"),
        }
    except Exception as exc:  # noqa: BLE001
        logging.warning("portrait failed %s: %s", url, exc)
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}


@dataclass
class CrawlResult:
    url: str
    record: dict[str, Any] | None
    error: str | None


def crawl_one(url: str, game: str, game_dir: Path, portraits: bool) -> CrawlResult:
    fetched_at = utc_now()
    try:
        response = fetch(url)
        html = response.content
        slug = slug_from_url(url)
        save_raw_html(game_dir / "raw" / f"{slug}.html.gz", html)
        record = parse_character(html, url, game, fetched_at, response.status_code)
        record["source"]["content_type"] = response.headers.get("content-type")
        if portraits:
            record["portrait_download"] = download_portrait(record, game_dir)
        write_json(game_dir / "heroes" / f"{slug}.json", record)
        logging.info("OK %-12s %-42s %s", game, slug, record.get("name"))
        return CrawlResult(url=url, record=record, error=None)
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        logging.exception("FAILED %s %s", url, message)
        return CrawlResult(url=url, record=None, error=message)


def crawl_game(game: str, root: Path, workers: int, portraits: bool) -> dict[str, Any]:
    config = GAMES[game]
    game_dir = root / game
    game_dir.mkdir(parents=True, exist_ok=True)
    list_url = config["list_url"]
    fetched_at = utc_now()
    response = fetch(list_url)
    list_html = response.content
    save_raw_html(game_dir / "raw" / "_characters-list.html.gz", list_html)
    urls = discover_character_urls(list_html, list_url, game)
    write_json(
        game_dir / "character_urls.json",
        {
            "game": game,
            "list_url": list_url,
            "fetched_at": fetched_at,
            "count": len(urls),
            "urls": urls,
        },
    )
    logging.info("DISCOVERED %s: %d detail URLs", game, len(urls))
    if len(urls) < config["expected_min"]:
        raise RuntimeError(
            f"{game}: discovered only {len(urls)} character URLs; expected at least {config['expected_min']}"
        )

    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(crawl_one, url, game, game_dir, portraits): url for url in urls
        }
        for future in as_completed(futures):
            result = future.result()
            if result.record is not None:
                records.append(result.record)
            else:
                failures.append({"url": result.url, "error": result.error or "unknown error"})

    records.sort(key=lambda item: (item.get("name") or item.get("slug") or "").casefold())
    write_json(game_dir / "heroes.json", records)
    write_csv(game_dir / "heroes.csv", records)
    write_json(game_dir / "failures.json", failures)

    missing = {
        "name": [r["slug"] for r in records if not r.get("name")],
        "title": [r["slug"] for r in records if not r.get("title")],
        "details": [r["slug"] for r in records if not r.get("details")],
        "skills": [r["slug"] for r in records if not r.get("skills")],
        "portrait_url": [r["slug"] for r in records if not r.get("portrait_url")],
    }
    report = {
        "game": game,
        "game_label": config["label"],
        "list_url": list_url,
        "discovered": len(urls),
        "succeeded": len(records),
        "failed": len(failures),
        "missing_fields": {key: {"count": len(value), "slugs": value} for key, value in missing.items()},
        "generated_at": utc_now(),
    }
    write_json(game_dir / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("afk-global-snapshot"))
    parser.add_argument("--games", nargs="+", choices=sorted(GAMES), default=sorted(GAMES))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--portraits", action="store_true")
    args = parser.parse_args()

    root: Path = args.output
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "crawl.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )

    started_at = utc_now()
    reports: list[dict[str, Any]] = []
    fatal_errors: list[dict[str, str]] = []
    for game in args.games:
        try:
            reports.append(crawl_game(game, root, args.workers, args.portraits))
        except Exception as exc:  # noqa: BLE001
            logging.exception("Fatal game crawl error: %s", game)
            fatal_errors.append({"game": game, "error": f"{type(exc).__name__}: {exc}"})

    manifest = {
        "dataset": "AFK Global character snapshot",
        "source": BASE_URL,
        "started_at": started_at,
        "completed_at": utc_now(),
        "games": reports,
        "fatal_errors": fatal_errors,
        "format_version": 1,
        "notes": [
            "Each hero has a structured JSON record and compressed raw HTML cache.",
            "CSV files are UTF-8 with BOM for direct use in Excel.",
            "All source URLs, fetch timestamps, hashes, image URLs, and parser diagnostics are retained.",
        ],
    }
    write_json(root / "manifest.json", manifest)
    write_json(
        root / "README.json",
        {
            "files": {
                "manifest.json": "Snapshot-level counts and status.",
                "<game>/heroes.json": "All parsed character detail records.",
                "<game>/heroes.csv": "Flattened character table.",
                "<game>/heroes/<slug>.json": "One parsed record per character.",
                "<game>/raw/*.html.gz": "Compressed source HTML for reproducibility.",
                "<game>/images/portraits": "Downloaded portraits when enabled.",
                "<game>/report.json": "Completeness report.",
                "crawl.log": "Request and parser execution log.",
            }
        },
    )

    total_failed = sum(report.get("failed", 0) for report in reports)
    if fatal_errors or total_failed:
        logging.error("Snapshot completed with failures: fatal=%d pages=%d", len(fatal_errors), total_failed)
        return 2
    logging.info("Snapshot completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
