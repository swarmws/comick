#!/usr/bin/env python3
"""
Comick Scraper Microservice
A FastAPI service for scraping Comick lists
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from curl_cffi import requests as curl_requests
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from urllib.parse import quote, urlparse
import uvicorn

_baka_rows: Optional[List[Dict[str, Any]]] = None
_baka_mtime: Optional[float] = None
_APOSTROPHE_CHARS = re.compile(r"[\u2018\u2019\u201a\u201b\u2032\u2035`´]")

_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_SOURCE_DOMAIN_MAP_PATH = _PACKAGE_DIR / "source_domain_map.json"


def _load_source_domain_map_bundle(path: Path) -> Tuple[
    Dict[str, Dict[str, str]],
    List[str],
    Dict[str, Dict[str, str]],
    Dict[str, str],
    Dict[str, str],
    Dict[str, str],
    List[str],
]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw_domains = data.get("domain_map")
    if not isinstance(raw_domains, dict):
        raise ValueError(f"{path}: missing or invalid 'domain_map' object")
    domain_map: Dict[str, Dict[str, str]] = {}
    track_by_id: Dict[str, Dict[str, str]] = {}
    for dom_key, meta in raw_domains.items():
        if not isinstance(meta, dict):
            continue
        iid = meta.get("id")
        if iid is None or iid == "":
            continue
        sid = str(iid)
        row = {
            "id": sid,
            "name": str(meta.get("name", sid)),
            "type": str(meta.get("type", "OTHER")),
        }
        domain_map[str(dom_key)] = row
        if sid not in track_by_id:
            track_by_id[sid] = dict(row)
    keys_sorted = sorted(domain_map.keys(), key=len, reverse=True)
    phrases_raw = data.get("publisher_name_phrases") or {}
    phrase_to_id: Dict[str, str] = {str(k): str(v) for k, v in phrases_raw.items()}
    phrase_keys_sorted = sorted(phrase_to_id.keys(), key=len, reverse=True)
    publisher_home = data.get("publisher_home_by_id") or {}
    if not isinstance(publisher_home, dict):
        publisher_home = {}
    publisher_home = {str(k): str(v) for k, v in publisher_home.items()}
    baka_null = data.get("baka_null_source_home") or {}
    if not isinstance(baka_null, dict):
        baka_null = {}
    baka_null = {str(k): str(v) for k, v in baka_null.items()}
    return (
        domain_map,
        keys_sorted,
        track_by_id,
        publisher_home,
        baka_null,
        phrase_to_id,
        phrase_keys_sorted,
    )


_src_map_env = os.environ.get("COMICK_SOURCE_DOMAIN_MAP")
_source_map_path = Path(_src_map_env) if _src_map_env else _DEFAULT_SOURCE_DOMAIN_MAP_PATH
if not _source_map_path.is_file():
    raise FileNotFoundError(
        f"Source map not found: {_source_map_path} "
        "(set COMICK_SOURCE_DOMAIN_MAP to a JSON path, or add source_domain_map.json next to comick.py)"
    )

(
    SOURCE_DOMAIN_MAP,
    _SOURCE_DOMAIN_KEYS_SORTED,
    _TRACK_BY_ID,
    PUBLISHER_HOME_BY_ID,
    BAKA_NULL_SOURCE_HOME,
    _PUBLISHER_NAME_PHRASES,
    _PUBLISHER_PHRASE_KEYS_SORTED,
) = _load_source_domain_map_bundle(_source_map_path)


def _baka_source_url(key: str, sid: Any) -> Optional[str]:
    if sid is None or sid == "":
        return None
    if key == "anime_planet":
        return f"https://www.anime-planet.com/manga/{sid}"
    if key == "manga_updates":
        return f"https://www.mangaupdates.com/series.html?id={sid}"
    if key == "anilist":
        return f"https://anilist.co/manga/{sid}"
    if key == "my_anime_list":
        return f"https://myanimelist.net/manga/{sid}"
    if key == "kitsu":
        return f"https://kitsu.io/manga/{sid}"
    if key == "shikimori":
        return f"https://shikimori.one/mangas/{sid}"
    if key == "anime_news_network":
        return f"https://www.animenewsnetwork.com/encyclopedia/manga.php?id={sid}"
    return None


def _track_id_slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s or "unknown"


def lookup_track_by_url(url: str) -> Dict[str, Any]:
    u = url.strip().lower()
    for domain_key in _SOURCE_DOMAIN_KEYS_SORTED:
        if domain_key in u:
            m = SOURCE_DOMAIN_MAP[domain_key]
            return {
                "id": m["id"],
                "name": m["name"],
                "type": m["type"],
                "url": url.strip(),
            }
    host = (urlparse(url).netloc or "").replace("www.", "") or "unknown"
    return {
        "id": _track_id_slug(host.split(".")[0] if "." in host else host),
        "name": host,
        "type": "OTHER",
        "url": url.strip(),
    }


def lookup_track_by_publisher_name(name: str) -> Dict[str, Any]:
    n = name.strip()
    nl = n.lower()
    for phrase in _PUBLISHER_PHRASE_KEYS_SORTED:
        pid = _PUBLISHER_NAME_PHRASES[phrase]
        if nl == phrase or phrase in nl:
            meta = _TRACK_BY_ID.get(pid)
            if meta:
                return {"id": meta["id"], "name": meta["name"], "type": meta["type"], "url": None}
            return {"id": pid, "name": n, "type": "OTHER", "url": None}
    candidates: List[Dict[str, str]] = []
    for meta in _TRACK_BY_ID.values():
        if not isinstance(meta, dict):
            continue
        mn = str(meta.get("name", "")).lower()
        if not mn:
            continue
        if mn == nl or mn in nl or nl in mn:
            candidates.append(meta)  # type: ignore[arg-type]
    if candidates:
        best = max(candidates, key=lambda m: len(m.get("name", "")))
        return {
            "id": best["id"],
            "name": best["name"],
            "type": best["type"],
            "url": None,
        }
    return {"id": _track_id_slug(n), "name": n, "type": "OTHER", "url": None}


def _comick_raw_link_to_url(key: str, raw: Any) -> Optional[str]:
    if raw is None or raw == "":
        return None
    v = raw.strip() if isinstance(raw, str) else str(raw).strip()
    if v.startswith("http://") or v.startswith("https://"):
        return v
    kl = str(key).lower()
    if kl == "ap":
        return f"https://www.anime-planet.com/manga/{quote(v, safe='')}"
    if kl == "mu":
        return f"https://www.mangaupdates.com/series.html?id={quote(v, safe='')}"
    if kl in ("kt", "kitsu"):
        return f"https://kitsu.io/manga/{quote(v, safe='')}"
    if kl in ("mal", "myanimelist"):
        return f"https://myanimelist.net/manga/{quote(v, safe='')}"
    if kl in ("al", "anilist", "ani"):
        return f"https://anilist.co/manga/{quote(v, safe='')}"
    if kl == "bw":
        path = v.lstrip("/")
        if path.startswith("series/"):
            return f"https://global.bookwalker.jp/{path}"
        return f"https://global.bookwalker.jp/series/{quote(v, safe='')}"
    if kl == "mb":
        return None
    return None


def comick_links_to_track_sources(comic: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not comic or not isinstance(comic, dict):
        return []
    links = comic.get("links")
    if not isinstance(links, dict):
        return []
    out: List[Dict[str, Any]] = []
    for k, raw in links.items():
        u = _comick_raw_link_to_url(k, raw)
        if u:
            out.append(lookup_track_by_url(u))
    return out


_URL_IN_TEXT = re.compile(r"https?://[^\s\)\]\"'<>]+", re.I)


def comic_desc_urls_to_track_sources(comic: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Comick often lists official/raw storefront URLs only in markdown `desc` / HTML `parsed`, not in `links`."""
    if not comic or not isinstance(comic, dict):
        return []
    chunks: List[str] = []
    for key in ("desc", "parsed"):
        v = comic.get(key)
        if isinstance(v, str) and v.strip():
            chunks.append(v)
    if not chunks:
        return []
    text = "\n".join(chunks)
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for raw in _URL_IN_TEXT.findall(text):
        u = raw.rstrip(").,;\\]\"'")
        if not u.startswith("http"):
            continue
        key = u.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(lookup_track_by_url(u))
    return out


def merge_and_finalize_sources(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_urls: set = set()
    out: List[Dict[str, Any]] = []
    for s in parts:
        iid = s.get("id")
        if not iid:
            continue
        row = dict(s)
        u = row.get("url")
        if isinstance(u, str) and u.strip():
            uk = u.strip().lower()
            if uk in seen_urls:
                continue
            seen_urls.add(uk)
        out.append(row)
    for s in out:
        if s.get("url") is None:
            hid = s.get("id")
            if hid and hid in PUBLISHER_HOME_BY_ID:
                s["url"] = PUBLISHER_HOME_BY_ID[hid]
    return out


COMICK_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://comick.dev/",
    "Origin": "https://comick.dev",
}

COMICK_STATUS_LABEL = {1: "ongoing", 2: "completed", 3: "hiatus"}


def comick_search(query: str) -> List[Dict[str, Any]]:
    """Search Comick API by title or keyword; returns raw result list (may be empty)."""
    q = (query or "").strip()
    if not q:
        return []
    search_url = (
        "https://api.comick.dev/v1.0/search"
        f"?q={quote(q)}&limit=49&page=1"
        "&content_rating=safe&content_rating=suggestive&content_rating=erotica&content_rating=pornographic"
    )
    resp = curl_requests.get(search_url, headers=COMICK_BROWSER_HEADERS, impersonate="chrome120", timeout=15)
    if resp.status_code != 200:
        return []
    data = resp.json()
    return data if isinstance(data, list) else []


def comick_status_label(code: Any) -> str:
    if isinstance(code, int) and code in COMICK_STATUS_LABEL:
        return COMICK_STATUS_LABEL[code]
    return "unknown"


def normalize_info_description(text: Any) -> Optional[str]:
    if text is None or not isinstance(text, str):
        return None
    s = text.replace("\r\n", "\n").replace("\r", "\n")

    # Keep synopsis only: drop link/translation appendix (see Comick markdown desc shape)
    _appendix_split = re.compile(
        r"(?is)"
        r"\n-{3,}\s*\n|"
        r"\n\*\*Original Webcomic:\*\*\s*|"
        r"\nOriginal Webcomic:\s+|"
        r"\n\*\*Official Translations:\*\*\s*|"
        r"\nOfficial Translations:\s+"
    )
    m = _appendix_split.search(s)
    if m:
        s = s[: m.start()]
    # If headers were glued on one line (no newline before them)
    s = re.sub(r"(?is)\s+-{3,}\s+(\*\*)?Original Webcomic:.*$", "", s)
    s = re.sub(r"(?is)\s+(\*\*)?Original Webcomic:\s+.*$", "", s)
    s = re.sub(r"(?is)\s+(\*\*)?Official Translations:\s+.*$", "", s)

    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"-{3,}", " ", s)
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"[ \t]+", " ", s).strip(" \t-")
    return s or None


def _people_names(rows: Any) -> List[str]:
    if not isinstance(rows, list):
        return []
    return [p["name"] for p in rows if isinstance(p, dict) and p.get("name")]


def get_comick_build_id() -> Optional[str]:
    home_resp = curl_requests.get("https://comick.dev", impersonate="chrome120", timeout=15)
    if home_resp.status_code != 200:
        return None
    match = re.search(r'<script id="__NEXT_DATA__".*?>(.*?)</script>', home_resp.text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1)).get("buildId")
    except Exception:
        return None


def comick_fetch_covers(comic_slug: str, build_id: Optional[str] = None) -> List[str]:
    if not build_id:
        build_id = get_comick_build_id()
    if not build_id:
        return []
    next_resp = curl_requests.get(
        f"https://comick.dev/_next/data/{build_id}/comic/{comic_slug}/cover.json",
        params={"slug": comic_slug},
        headers={
            **COMICK_BROWSER_HEADERS,
            "Referer": f"https://comick.dev/comic/{comic_slug}",
        },
        impersonate="chrome120",
        timeout=15,
    )
    if next_resp.status_code != 200:
        return []
    page_props = next_resp.json().get("pageProps", {})
    raw = page_props.get("md_covers") or page_props.get("comic", {}).get("md_covers", [])
    return [
        f"https://meo.comick.pictures/{c['b2key']}"
        for c in raw
        if isinstance(c, dict) and c.get("b2key")
    ]


def comick_fetch_comic_page(
    comic_slug: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    build_id = get_comick_build_id()
    if not build_id:
        return None, None, {}
    url = f"https://comick.dev/_next/data/{build_id}/comic/{comic_slug}.json"
    resp = curl_requests.get(
        url,
        params={"slug": comic_slug},
        headers={
            **COMICK_BROWSER_HEADERS,
            "Referer": f"https://comick.dev/comic/{comic_slug}",
        },
        impersonate="chrome120",
        timeout=15,
    )
    data = resp.json() if resp.status_code == 200 else {}
    page_props = data.get("pageProps") if isinstance(data, dict) else {}
    if not isinstance(page_props, dict):
        page_props = {}
    comic = page_props.get("comic")
    if not isinstance(comic, dict):
        return None, build_id, page_props
    return comic, build_id, page_props


def comick_map_comic(
    comic: Dict[str, Any], covers: List[str], page_props: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    pp = page_props or {}

    genres: List[str] = []
    for row in comic.get("md_comic_md_genres") or []:
        g = row.get("md_genres") or {}
        if g.get("group") == "Genre" and g.get("name"):
            genres.append(g["name"])

    mu = comic.get("mu_comics") or {}
    tags: List[str] = []
    for c in mu.get("mu_comic_categories") or []:
        mc = c.get("mu_categories") or {}
        tit = mc.get("title")
        if tit:
            tags.append(tit)

    publishers: List[str] = []
    for row in mu.get("mu_comic_publishers") or []:
        pub = row.get("mu_publishers") or {}
        pt = pub.get("title")
        if pt:
            publishers.append(pt)

    authors = _people_names(pp.get("authors") or comic.get("authors"))
    artists = _people_names(pp.get("artists") or comic.get("artists"))
    alt_titles = [
        t["title"] for t in comic.get("md_titles") or [] if isinstance(t, dict) and t.get("title")
    ]
    related: List[str] = []
    for rf in comic.get("relate_from") or []:
        rt = rf.get("relate_to") or {}
        tit = rt.get("title")
        if isinstance(tit, str) and tit.strip():
            related.append(tit.strip())

    cr = comic.get("content_rating")
    nsfw = cr != "safe" if cr is not None else False
    lang_list = comic.get("langList") or pp.get("langList") or []
    if not isinstance(lang_list, list):
        lang_list = []

    return {
        "description": normalize_info_description(comic.get("desc")),
        "status": comick_status_label(comic.get("status")),
        "year": comic.get("year"),
        "latestChapterNumber": comic.get("last_chapter"),
        "chapterCount": comic.get("chapter_count"),
        "translationComplete": comic.get("translation_completed"),
        "ended": comic.get("final_chapter"),
        "demographic": comic.get("demographic"),
        "origination": comic.get("country"),
        "nsfw": nsfw,
        "anime": comic.get("anime"),
        "altTitles": alt_titles,
        "availableLangs": lang_list,
        "authors": authors,
        "author": authors[0] if authors else None,
        "artists": artists,
        "artist": artists[0] if artists else None,
        "genres": genres,
        "tags": tags,
        "publishers": publishers,
        "relatedSeries": related,
        "covers": covers,
    }


def load_baka_data() -> List[Dict[str, Any]]:
    global _baka_rows, _baka_mtime
    path = Path(__file__).resolve().parent / "baka.json"
    if not path.is_file():
        _baka_rows = []
        _baka_mtime = None
        return _baka_rows
    mtime = path.stat().st_mtime
    if _baka_rows is not None and _baka_mtime == mtime:
        return _baka_rows
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    data = payload.get("data") if isinstance(payload, dict) else []
    _baka_rows = data if isinstance(data, list) else []
    _baka_mtime = mtime
    return _baka_rows


def _norm_title(s: str) -> str:
    s = (s or "").strip()
    s = _APOSTROPHE_CHARS.sub("'", s)
    s = s.lower()
    return re.sub(r"\s+", " ", s)


def slug_title_candidates(slug: str) -> List[str]:
    """Derive human title guesses from a Comick slug (e.g. 00-the-beginning-after-the-end-1 → the beginning after the end)."""
    if not (slug or "").strip():
        return []
    s = slug.strip()
    s = re.sub(r"^\d+-", "", s)
    s = re.sub(r"-\d+$", "", s)
    guess = re.sub(r"-+", " ", s).strip()
    return [guess] if guess else []


def _baka_entry_titles(entry: Dict[str, Any]) -> List[str]:
    titles: List[str] = []
    for key in ("title", "native_title", "romanized_title"):
        v = entry.get(key)
        if isinstance(v, str) and v.strip():
            titles.append(v.strip())
    for row in (entry.get("secondary_titles") or {}).get("unknown") or []:
        if isinstance(row, dict):
            tt = row.get("title")
            if isinstance(tt, str) and tt.strip():
                titles.append(tt.strip())
    return titles


def baka_find_match(*candidates: str) -> Optional[Dict[str, Any]]:
    rows = load_baka_data()
    if not rows:
        return None
    cands = [_norm_title(str(c)) for c in candidates if c and str(c).strip()]
    if not cands:
        return None

    for want in cands:
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            for t in _baka_entry_titles(entry):
                if _norm_title(t) == want:
                    return entry

    best_entry: Optional[Dict[str, Any]] = None
    best_len = 0
    for want in cands:
        if len(want) < 8:
            continue
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            for t in _baka_entry_titles(entry):
                tn = _norm_title(t)
                if want in tn or tn in want:
                    overlap = min(len(want), len(tn))
                    if overlap > best_len:
                        best_len = overlap
                        best_entry = entry
    if best_entry:
        return best_entry

    for want in cands:
        wt = set(re.findall(r"[a-z0-9]+", want))
        if len(wt) < 2:
            continue
        best_score = 0.0
        best_e: Optional[Dict[str, Any]] = None
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            for t in _baka_entry_titles(entry):
                tt = set(re.findall(r"[a-z0-9]+", _norm_title(t)))
                if not tt:
                    continue
                inter = len(wt & tt)
                uni = len(wt | tt)
                score = inter / uni if uni else 0.0
                if inter >= 3 and score >= 0.45 and score > best_score:
                    best_score = score
                    best_e = entry
        if best_e:
            return best_e
    return None


def baka_build_sources(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in entry.get("publishers") or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name:
            continue
        out.append(lookup_track_by_publisher_name(str(name)))
    for link in entry.get("links") or []:
        if isinstance(link, str) and link.strip():
            out.append(lookup_track_by_url(link.strip()))
    src = entry.get("source") or {}
    if isinstance(src, dict):
        for key, block in src.items():
            if not isinstance(block, dict):
                continue
            sid = block.get("id")
            if sid is not None and sid != "":
                url = _baka_source_url(key, sid)
                if url:
                    out.append(lookup_track_by_url(url))
            else:
                home = BAKA_NULL_SOURCE_HOME.get(key)
                if home:
                    out.append(lookup_track_by_url(home))
    return out


def merge_info_sources(
    comic_slug: Optional[str],
    baka_row: Optional[Dict[str, Any]],
    comic: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    if comic_slug:
        parts.append(
            {
                "id": "comick",
                "name": "Comick",
                "type": "DATABASE",
                "url": f"https://comick.dev/comic/{comic_slug}",
            }
        )
    if comic:
        parts.extend(comick_links_to_track_sources(comic))
        parts.extend(comic_desc_urls_to_track_sources(comic))
    if baka_row:
        parts.extend(baka_build_sources(baka_row))
    return merge_and_finalize_sources(parts)


def baka_map_to_info(entry: Dict[str, Any]) -> Dict[str, Any]:
    authors = entry.get("authors") or []
    artists = entry.get("artists") or []
    if not isinstance(authors, list):
        authors = []
    if not isinstance(artists, list):
        artists = []
    cr = entry.get("content_rating")
    nsfw = cr != "safe" if cr else False
    covers: List[str] = []
    raw = (entry.get("cover") or {}).get("raw") or {}
    u = raw.get("url")
    if isinstance(u, str) and u:
        covers.append(u)
    alt: List[str] = []
    for row in (entry.get("secondary_titles") or {}).get("unknown") or []:
        if isinstance(row, dict):
            tt = row.get("title")
            if isinstance(tt, str) and tt.strip():
                alt.append(tt.strip())
    pub_names = [
        p.get("name")
        for p in (entry.get("publishers") or [])
        if isinstance(p, dict) and p.get("name")
    ]

    return {
        "description": normalize_info_description(entry.get("description")),
        "status": entry.get("status"),
        "year": entry.get("year"),
        "latestChapterNumber": entry.get("total_chapters"),
        "chapterCount": None,
        "translationComplete": None,
        "ended": entry.get("final_volume"),
        "demographic": None,
        "origination": None,
        "nsfw": nsfw,
        "anime": entry.get("anime"),
        "altTitles": alt,
        "availableLangs": [],
        "authors": authors,
        "author": authors[0] if authors else None,
        "artists": artists,
        "artist": artists[0] if artists else None,
        "genres": list(entry.get("genres") or []),
        "tags": list(entry.get("tags") or []),
        "publishers": pub_names,
        "relatedSeries": [],
        "covers": covers,
    }


def resolve_info_by_title(t: str) -> Dict[str, Any]:
    """Same payload as GET /info: Comick-mapped comic + covers + merged sources, or baka-only fallback."""
    t = (t or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="title is required")

    search_items = comick_search(t)
    first_hit: Optional[Dict[str, Any]] = next(
        (item for item in search_items if isinstance(item, dict) and item.get("slug")),
        None,
    )

    baka_candidates: List[str] = [t]
    if first_hit:
        baka_candidates.append(str(first_hit.get("title") or ""))
        baka_candidates.extend(slug_title_candidates(str(first_hit["slug"])))

    comic: Optional[Dict[str, Any]] = None
    build_id: Optional[str] = None
    comic_slug: Optional[str] = None
    page_props: Dict[str, Any] = {}
    if first_hit:
        comic_slug = str(first_hit["slug"])
        comic, build_id, page_props = comick_fetch_comic_page(comic_slug)

    tracker_slug: Optional[str] = str(first_hit["slug"]) if first_hit else None

    if comic and comic_slug:
        covers = comick_fetch_covers(comic_slug, build_id)
        payload = comick_map_comic(comic, covers, page_props)
        baka_candidates.append(str(comic.get("title") or ""))
        baka_candidates.append(str(comic.get("englishTitle") or ""))
        baka_row = baka_find_match(*baka_candidates)
        payload["sources"] = merge_info_sources(comic_slug, baka_row, comic)
        return payload

    baka_row = baka_find_match(*baka_candidates)
    if not baka_row:
        raise HTTPException(status_code=404, detail="Not Found")
    payload = baka_map_to_info(baka_row)
    payload["sources"] = merge_info_sources(tracker_slug, baka_row, None)
    return payload


app = FastAPI(
    title="Comick Scraper API",
    description="Microservice for scraping Comick manga lists",
    version="1.0.0"
)

# Enable CORS for all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def scrape_comick_list(url: str) -> Dict[str, Any]:
    """Scrape a Comick user's manga list"""
    try:
        # Extract user ID from URL
        user_id_match = re.search(r"/user/([a-f0-9-]{36})", url)
        if not user_id_match:
            return {"error": "Invalid user ID format"}
        
        user_id = user_id_match.group(1)
        follows_url = f"https://api.comick.dev/user/{user_id}/follows"
        
        resp = curl_requests.get(follows_url, impersonate="chrome120")
        
        if resp.status_code != 200:
            return {"error": f"Failed to fetch data: {resp.status_code}"}
        
        data = resp.json()
        
        # Extract titles from the response
        titles = []
        if isinstance(data, list):
            for item in data:
                if "md_comics" in item and "title" in item["md_comics"]:
                    titles.append({
                        "title": item["md_comics"]["title"],
                        "comick_id": item["md_comics"].get("slug", item["md_comics"].get("id", "unknown"))
                    })
        
        return {"titles": titles}
        
    except Exception as e:
        return {"error": str(e)}

@app.get("/")
async def root():
    """Return full API discovery: all routes, methods, params, and schemas as JSON (OpenAPI)."""
    spec = app.openapi()
    return {
        **spec,
        "x-documentation": {
            "swagger_ui": "/docs",
            "redoc": "/redoc",
            "openapi_json": "/openapi.json",
        },
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "comick-scraper"}


@app.get("/baka")
async def baka_endpoint(title: str):
    """Required ?title= — identical response body to GET /info?title= (same keys, rows, sources)."""
    return resolve_info_by_title(title)


@app.get("/info")
async def info_endpoint(title: str):
    """Resolve by title: Comick Next.js comic payload (mapped) + covers; merge baka sources when matched. Falls back to baka.json."""
    return resolve_info_by_title(title)

@app.post("/scrape")
async def scrape_endpoint(request: Dict[str, str]):
    """Scrape a Comick user's manga list"""
    url = request.get("url")
    
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")
    
    # Validate Comick URL format
    comick_url_pattern = r'^https?://comick\.(io|dev)/user/[^/]+(/list)?$'
    if not re.match(comick_url_pattern, url):
        raise HTTPException(
            status_code=400, 
            detail="Invalid Comick URL format. Expected: https://comick.dev/user/[user-id] or https://comick.dev/user/[user-id]/list"
        )
    
    result = scrape_comick_list(url)
    
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    
    return result

@app.get("/scrape")
async def scrape_get(url: str):
    """Scrape a Comick user's manga list (GET endpoint for convenience)"""
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")
    
    # Validate Comick URL format
    comick_url_pattern = r'^https?://comick\.(io|dev)/user/[^/]+(/list)?$'
    if not re.match(comick_url_pattern, url):
        raise HTTPException(
            status_code=400, 
            detail="Invalid Comick URL format. Expected: https://comick.dev/user/[user-id] or https://comick.dev/user/[user-id]/list"
        )
    
    result = scrape_comick_list(url)
    
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    
    return result

@app.get("/cover")
async def cover_endpoint(slug: str):
    """Fetch all cover image URLs for a comic by title search"""
    items = comick_search(slug)
    print(f"Search slug={slug!r} -> {len(items)} raw hits")

    first = next((item for item in items if isinstance(item, dict) and item.get("slug")), None)
    if not first:
        raise HTTPException(status_code=400, detail="Not Found")

    comic_slug = first["slug"]
    covers = comick_fetch_covers(comic_slug)
    print(f"Covers fetched: {len(covers)}")

    if not covers:
        raise HTTPException(status_code=400, detail="Not Found")
    return covers


@app.get("/comments")
async def comments_endpoint(url: str):
    user_id_match = re.search(r"/user/([a-f0-9-]{36})", url)
    if not user_id_match:
        raise HTTPException(status_code=400, detail="Invalid user ID format")

    user_id = user_id_match.group(1)
    all_comments = []

    for page in range(1, 21):
        try:
            resp = curl_requests.get(
                f"https://api.comick.dev/user/{user_id}/comments?page={page}",
                impersonate="chrome120",
                timeout=15
            )
            if resp.status_code != 200:
                break

            data = resp.json()
            if not data:
                break

            for item in data:
                if item is None:
                    continue

                file_name = item.get("file")
                if file_name:
                    image_url = f"https://meo.comick.pictures/{file_name}"
                    has_image = True
                else:
                    image_url = None
                    has_image = False

                all_comments.append({
                    "content": item.get("parsed"),
                    "image": has_image,
                    "image_url": image_url,
                    "date": item.get("created_at"),
                    "manga_title": item.get("md_comics", {}).get("title")
                })

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return all_comments

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=4775,
        reload=False,
        log_level="info"
    )
