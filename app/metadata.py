"""Könyvadatok automatikus letöltése: link, ISBN, cím vagy borítókép alapján.

Források (sorrendben):
  1. A megadott weboldal strukturált adatai (JSON-LD, OpenGraph, meta tagek)
  2. Google Books API, Open Library (ISBN / cím alapján)
  3. Vonalkód felismerés a képen (ISBN)  - pyzbar
  4. Opcionális: Claude AI (ha ANTHROPIC_API_KEY be van állítva) - borító
     felismerés + webes keresés magyar könyvekhez
  5. Ha nincs AI: OCR (tesseract) a borítón, majd keresés a felismert szövegre
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("metadata")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8"}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-5-5")

SHOP_NAMES = {
    "libri.hu": "Libri", "bookline.hu": "Bookline", "lira.hu": "Líra",
    "alexandra.hu": "Alexandra", "moly.hu": "Moly", "antikvarium.hu": "Antikvarium.hu",
    "regikonyvek.hu": "Régikönyvek", "amazon.com": "Amazon", "amazon.de": "Amazon.de",
    "amazon.co.uk": "Amazon UK", "bookdepository.com": "Book Depository",
    "emag.hu": "eMAG", "dibook.hu": "Dibook", "kobo.com": "Kobo", "rukkola.hu": "Rukkola",
    "jatekbolt.hu": "Játékbolt", "pagony.hu": "Pagony", "szukits.hu": "Szukits",
    "openlibrary.org": "Open Library", "google.com": "Google Books",
}

FIELDS = ["title", "subtitle", "authors", "isbn", "publisher", "publish_year",
          "description", "cover_url", "price", "currency", "shop", "link", "tags",
          "edition", "pages", "language"]


# ---------------------------------------------------------------- segédek

def empty() -> dict:
    return {"authors": [], "tags": []}


def merge(base: dict, extra: dict | None) -> dict:
    """Csak a hiányzó mezőket tölti ki."""
    if not extra:
        return base
    for k, v in extra.items():
        if v in (None, "", [], {}):
            continue
        if k in ("authors", "tags"):
            if not base.get(k):
                base[k] = list(dict.fromkeys(v))
        elif not base.get(k):
            base[k] = v
    return base


def clean_text(s) -> str | None:
    if s is None:
        return None
    if isinstance(s, (list, tuple)):
        s = " ".join(str(x) for x in s if x)
    s = BeautifulSoup(str(s), "html.parser").get_text(" ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    return s or None


def isbn_valid(digits: str) -> bool:
    if len(digits) == 13 and digits.isdigit():
        s = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits[:12]))
        return (10 - s % 10) % 10 == int(digits[12])
    if len(digits) == 10 and digits[:9].isdigit() and (digits[9].isdigit() or digits[9] in "Xx"):
        s = sum((10 - i) * int(c) for i, c in enumerate(digits[:9]))
        chk = (11 - s % 11) % 11
        return (str(chk) if chk < 10 else "X") == digits[9].upper()
    return False


def normalize_isbn(s: str | None) -> str | None:
    if not s:
        return None
    d = re.sub(r"[^0-9Xx]", "", str(s)).upper()
    return d if isbn_valid(d) else None


ISBN_RE = re.compile(r"(?:ISBN(?:-1[03])?:?\s*)?((?:97[89][\-\s]?)?\d{1,5}[\-\s]?\d{1,7}[\-\s]?\d{1,7}[\-\s]?[\dXx])")


def find_isbn(text: str) -> str | None:
    if not text:
        return None
    # előbb az "ISBN" szó utáni számok
    for m in re.finditer(r"ISBN[^0-9]{0,15}([0-9][0-9\-\s]{8,20}[0-9Xx])", text, re.I):
        n = normalize_isbn(m.group(1))
        if n:
            return n
    for m in re.finditer(r"97[89][\-\s]?[0-9][0-9\-\s]{8,14}[0-9]", text):
        n = normalize_isbn(m.group(0))
        if n:
            return n
    return None


def parse_year(s) -> int | None:
    if s is None:
        return None
    m = re.search(r"(1[5-9]\d\d|20\d\d)", str(s))
    return int(m.group(1)) if m else None


def parse_price(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace("\xa0", " ")
    m = re.search(r"\d[\d .,]*", t)
    if not m:
        return None
    num = m.group(0).strip().replace(" ", "")
    # 4.999 / 4,999 (ezres) vs 12.50 / 12,50 (tizedes)
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", num):
        num = re.sub(r"[.,]", "", num)
    else:
        num = num.replace(",", ".")
        if num.count(".") > 1:
            num = num.replace(".", "", num.count(".") - 1)
    try:
        return float(num)
    except ValueError:
        return None


def shop_from_url(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    for dom, name in SHOP_NAMES.items():
        if host == dom or host.endswith("." + dom):
            return name
    return host or None


def _client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)


# ---------------------------------------------------------------- weboldal

def _iter_jsonld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_jsonld(x)
    elif isinstance(obj, dict):
        yield obj
        if "@graph" in obj:
            yield from _iter_jsonld(obj["@graph"])
        for key in ("mainEntity", "itemOffered", "workExample"):
            if key in obj:
                yield from _iter_jsonld(obj[key])


def _types(o) -> set:
    t = o.get("@type")
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return {str(t).lower()} if t else set()


def _names(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"\s*[;,&]\s*|\s+és\s+", v) if x.strip()]
    if isinstance(v, dict):
        return _names(v.get("name"))
    if isinstance(v, list):
        out = []
        for x in v:
            out += _names(x)
        return out
    return []


def _img(v) -> str | None:
    if not v:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return _img(v[0]) if v else None
    if isinstance(v, dict):
        return v.get("url") or v.get("contentUrl")
    return None


def parse_jsonld(soup: BeautifulSoup) -> dict:
    res = empty()
    for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        for o in _iter_jsonld(data):
            t = _types(o)
            if not t & {"book", "product", "creativework"}:
                continue
            d = empty()
            d["title"] = clean_text(o.get("name"))
            d["authors"] = _names(o.get("author") or o.get("creator"))
            d["isbn"] = normalize_isbn(o.get("isbn") or o.get("gtin13") or o.get("gtin") or o.get("sku"))
            d["publisher"] = (_names(o.get("publisher")) or [None])[0]
            d["publish_year"] = parse_year(o.get("datePublished") or o.get("copyrightYear"))
            d["description"] = clean_text(o.get("description"))
            d["cover_url"] = _img(o.get("image"))
            d["pages"] = o.get("numberOfPages")
            d["edition"] = clean_text(o.get("bookEdition"))
            d["language"] = o.get("inLanguage") if isinstance(o.get("inLanguage"), str) else None
            offers = o.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict):
                d["price"] = parse_price(offers.get("price") or offers.get("lowPrice"))
                d["currency"] = offers.get("priceCurrency")
            gen = o.get("genre")
            if gen:
                d["tags"] = _names(gen) if not isinstance(gen, list) else [str(g) for g in gen]
            # Book típus elsőbbséget kap
            if "book" in t:
                res = merge(d, res)
            else:
                res = merge(res, d)
    return res


def _meta(soup, *names) -> str | None:
    for n in names:
        el = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
            or soup.find("meta", attrs={"itemprop": n})
        if el and el.get("content"):
            return el["content"].strip()
    return None


def parse_microdata(soup: BeautifulSoup) -> dict:
    """schema.org microdata (itemprop) olvasása - pl. Libri, Bookline."""
    def val(el):
        if el is None:
            return None
        if el.get("content"):
            return el["content"].strip()
        inner = el.find(attrs={"itemprop": "name"})
        if inner is not None and inner is not el:
            return val(inner)
        return el.get_text(" ", strip=True) or None

    d = empty()
    d["price"] = parse_price(val(soup.find(attrs={"itemprop": "price"})))
    d["currency"] = val(soup.find(attrs={"itemprop": "priceCurrency"}))
    d["publisher"] = val(soup.find(attrs={"itemprop": "publisher"}))
    d["isbn"] = normalize_isbn(val(soup.find(attrs={"itemprop": "isbn"})) or
                               val(soup.find(attrs={"itemprop": "productID"})))
    d["publish_year"] = parse_year(val(soup.find(attrs={"itemprop": "datePublished"})))
    authors = [val(a) for a in soup.find_all(attrs={"itemprop": "author"})[:6]]
    d["authors"] = [a for a in authors if a and len(a) < 80]
    if d.get("publisher") and len(d["publisher"]) > 80:
        d["publisher"] = None
    return d


LABELS = {
    "price": r"Online [áa]r|Webshop [áa]r|Akci[óo]s [áa]r|Kiad[óo]i [áa]r|[ÁA]r",
    "publisher": r"Kiad[óo](?![a-zá])|Publisher",
    "publish_year": r"Kiad[áa]s [ée]ve|Megjelen[ée]s(?: [ée]ve| d[áa]tuma)?|Publication date|Published",
    "pages": r"Oldalsz[áa]m|Oldalak sz[áa]ma|Terjedelem|Pages",
    "isbn": r"ISBN",
}


def parse_labels(soup: BeautifulSoup) -> dict:
    """Gyakori 'Címke: érték' sorok (táblázat, dl, li) a termékoldalakon."""
    d = empty()
    texts = []
    for el in soup.find_all(["tr", "li", "dl", "div", "p"], limit=4000):
        t = el.get_text(" ", strip=True)
        if 3 < len(t) < 160:
            texts.append(t)
    for key, pat in LABELS.items():
        rx = re.compile(rf"(?:{pat})\s*[:：]?\s*(.+)", re.I)
        for t in texts:
            m = rx.match(t)
            if not m:
                continue
            val = m.group(1).strip()
            if key == "publish_year":
                d[key] = parse_year(val)
            elif key == "isbn":
                d[key] = normalize_isbn(val) or find_isbn(t)
            elif key == "pages":
                mm = re.search(r"\d+", val)
                d[key] = int(mm.group(0)) if mm else None
            elif key == "price":
                if not re.search(r"\d", val) or not re.search(r"Ft|HUF|€|EUR|\$", val):
                    continue
                d[key] = parse_price(val)
                d["currency"] = "EUR" if re.search(r"€|EUR", val) else ("USD" if "$" in val else "HUF")
            else:
                d[key] = val[:120]
            if d.get(key):
                break
    return d


def fetch_page(url: str) -> dict:
    with _client() as c:
        r = c.get(url)
        r.raise_for_status()
        html = r.text
        final_url = str(r.url)
    soup = BeautifulSoup(html, "html.parser")
    res = parse_jsonld(soup)

    og = empty()
    og["title"] = clean_text(_meta(soup, "og:title", "twitter:title"))
    og["description"] = clean_text(_meta(soup, "og:description", "description", "twitter:description"))
    og["cover_url"] = _meta(soup, "og:image", "twitter:image")
    og["isbn"] = normalize_isbn(_meta(soup, "book:isbn", "isbn"))
    a = _meta(soup, "book:author", "author")
    og["authors"] = _names(a) if a and not a.startswith("http") else []
    og["price"] = parse_price(_meta(soup, "product:price:amount", "og:price:amount", "price"))
    og["currency"] = _meta(soup, "product:price:currency", "og:price:currency", "priceCurrency")
    og["publish_year"] = parse_year(_meta(soup, "book:release_date"))
    res = merge(res, og)
    res = merge(res, parse_microdata(soup))
    res = merge(res, parse_labels(soup))

    if not res.get("isbn"):
        res["isbn"] = find_isbn(soup.get_text(" "))
    if not res.get("title") and soup.title:
        res["title"] = clean_text(soup.title.string)
    if res.get("title"):
        # "Cím - Szerző | Bolt" jellegű címek tisztítása
        res["title"] = re.split(r"\s+[|–—]\s+", res["title"])[0].strip()
        # "Cím (Szerző)" -> "Cím"
        m = re.match(r"^(.*\S)\s*\(([^()]+)\)$", res["title"])
        if m and (not res.get("authors") or any(a.lower() in m.group(2).lower() for a in res["authors"])):
            res["title"] = m.group(1)
            if not res.get("authors"):
                res["authors"] = _names(m.group(2))
        # "Szerző: Cím" -> "Cím"
        for a in res.get("authors") or []:
            if res["title"].lower().startswith(a.lower() + ":"):
                res["title"] = res["title"][len(a) + 1:].strip()
                break
    if res.get("publisher") and res["publisher"] == res["publisher"].lower():
        res["publisher"] = res["publisher"].title()
    if res.get("cover_url"):
        res["cover_url"] = urljoin(final_url, res["cover_url"])
    if res.get("currency") in ("Ft", "ft", "HUF "):
        res["currency"] = "HUF"
    res["link"] = url
    res["shop"] = (shop_from_url(final_url) or _meta(soup, "og:site_name"))
    return res


# ---------------------------------------------------------------- ISBN / cím

def google_books(query: str, max_results: int = 5) -> list[dict]:
    params = {"q": query, "maxResults": max_results, "printType": "books"}
    key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if key:
        params["key"] = key
    try:
        with _client() as c:
            r = c.get("https://www.googleapis.com/books/v1/volumes", params=params)
            r.raise_for_status()
            items = r.json().get("items") or []
    except Exception as e:
        log.warning("Google Books hiba: %s", e)
        return []
    out = []
    for it in items:
        v = it.get("volumeInfo", {})
        d = empty()
        d["title"] = v.get("title")
        d["subtitle"] = v.get("subtitle")
        d["authors"] = v.get("authors") or []
        d["publisher"] = v.get("publisher")
        d["publish_year"] = parse_year(v.get("publishedDate"))
        d["description"] = clean_text(v.get("description"))
        d["pages"] = v.get("pageCount")
        d["language"] = v.get("language")
        d["tags"] = v.get("categories") or []
        for ident in v.get("industryIdentifiers", []):
            if ident.get("type") == "ISBN_13":
                d["isbn"] = ident.get("identifier")
        if not d.get("isbn"):
            for ident in v.get("industryIdentifiers", []):
                if ident.get("type") == "ISBN_10":
                    d["isbn"] = ident.get("identifier")
        imgs = v.get("imageLinks") or {}
        img = imgs.get("extraLarge") or imgs.get("large") or imgs.get("medium") or imgs.get("thumbnail")
        if img:
            d["cover_url"] = img.replace("http://", "https://").replace("&edge=curl", "")
        d["link"] = v.get("infoLink")
        out.append(d)
    return out


def open_library_isbn(isbn: str) -> dict | None:
    try:
        with _client() as c:
            r = c.get("https://openlibrary.org/api/books",
                      params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"})
            r.raise_for_status()
            data = r.json().get(f"ISBN:{isbn}")
    except Exception as e:
        log.warning("Open Library hiba: %s", e)
        return None
    if not data:
        return None
    d = empty()
    d["title"] = data.get("title")
    d["subtitle"] = data.get("subtitle")
    d["authors"] = [a.get("name") for a in data.get("authors", []) if a.get("name")]
    d["publisher"] = (data.get("publishers") or [{}])[0].get("name")
    d["publish_year"] = parse_year(data.get("publish_date"))
    d["pages"] = data.get("number_of_pages")
    d["tags"] = [s.get("name") for s in (data.get("subjects") or [])[:5] if s.get("name")]
    cov = data.get("cover") or {}
    d["cover_url"] = cov.get("large") or cov.get("medium")
    d["isbn"] = isbn
    return d


def open_library_search(q: str, limit: int = 4) -> list[dict]:
    try:
        with _client() as c:
            r = c.get("https://openlibrary.org/search.json", params={
                "q": q, "limit": limit,
                "fields": "title,subtitle,author_name,isbn,first_publish_year,cover_i,publisher,subject"})
            r.raise_for_status()
            docs = r.json().get("docs") or []
    except Exception as e:
        log.warning("Open Library keresési hiba: %s", e)
        return []
    out = []
    for doc in docs:
        d = empty()
        d["title"] = doc.get("title")
        d["subtitle"] = doc.get("subtitle")
        d["authors"] = doc.get("author_name") or []
        isbns = [normalize_isbn(i) for i in doc.get("isbn") or []]
        isbns = [i for i in isbns if i]
        d["isbn"] = next((i for i in isbns if len(i) == 13), isbns[0] if isbns else None)
        d["publish_year"] = doc.get("first_publish_year")
        d["publisher"] = (doc.get("publisher") or [None])[0]
        if doc.get("cover_i"):
            d["cover_url"] = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"
        out.append(d)
    return out


# ---------------------------------------------------------------- moly.hu (magyar könyvek)

MOLY = "https://moly.hu"

# moly címkék -> általános műfaji címkék
GENRE_MAP = [
    (r"krimi|bűnügyi|thriller", "Krimi"),
    (r"sci-fi|science fiction|fantasy|disztópia", "Sci-fi / Fantasy"),
    (r"ezoteri|spiritual|asztrológ|tarot|meditáció|reiki|kabbal|okkult", "Ezoterikus"),
    (r"ismeretterjesztő|tudomány|fizika|biológia|kémia|csillagászat|matematika", "Tudományos"),
    (r"tankönyv|jegyzet|nyelvkönyv|munkafüzet", "Tankönyv"),
    (r"pszicholó", "Pszichológia"),
    (r"történelem|történeti", "Történelem"),
    (r"életrajz|memoár|önéletrajz", "Életrajz"),
    (r"szakácskönyv|gasztronóm|recept", "Szakácskönyv"),
    (r"gyerekkönyv|mesekönyv|mese|ifjúsági|lapozó", "Gyerek és ifjúsági"),
    (r"önsegítő|önfejlesztés|életvezetés", "Önfejlesztés"),
    (r"vallás|teológia|biblia", "Vallás"),
    (r"filozófia", "Filozófia"),
    (r"egészség|táplálkoz|orvos", "Egészség"),
    (r"képregény|manga", "Képregény"),
    (r"vers|költészet|novella|regény|klasszikus|dráma|elbeszélés", "Szépirodalom"),
]


def genre_tags(raw_tags: list[str], limit: int = 3) -> list[str]:
    out = []
    for t in raw_tags:
        for pat, genre in GENRE_MAP:
            if re.search(pat, t, re.I) and genre not in out:
                out.append(genre)
    return out[:limit]


def moly_search(q: str, limit: int = 4) -> list[str]:
    try:
        with _client() as c:
            r = c.get(f"{MOLY}/kereses", params={"query": q})
            r.raise_for_status()
    except Exception as e:
        log.warning("Moly keresési hiba: %s", e)
        return []
    urls = []
    for m in re.finditer(r'href="(/konyvek/[a-z0-9\-]+)"', r.text):
        u = MOLY + m.group(1)
        if u not in urls:
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def moly_book(url: str, isbn: str | None = None, html: str | None = None) -> dict:
    if html is None:
        with _client() as c:
            r = c.get(url)
            r.raise_for_status()
            html = r.text
    soup = BeautifulSoup(html, "html.parser")
    d = empty()
    og_title = _meta(soup, "og:title") or ""
    parts = [p.strip() for p in og_title.split("·")]
    if parts:
        d["title"] = parts[0]
    if len(parts) >= 3:
        d["authors"] = [a.strip() for a in re.split(r"\s+[–-]\s+|,\s*", parts[1]) if a.strip()]
    desc = _meta(soup, "og:description", "description") or ""
    dparts = [p.strip() for p in desc.split(" · ")]
    dparts = [p for p in dparts if not re.match(r"(Árak és vélemények|Legolcsóbb ár)", p)]
    d["description"] = clean_text(" · ".join(dparts))
    raw_tags = [a.get_text(strip=True) for a in soup.select("#book_tags a.tag")]
    d["tags"] = genre_tags(raw_tags)
    d["_moly_tags"] = raw_tags[:15]
    d["cover_url"] = _meta(soup, "og:image")

    editions = soup.select("[id^=edition_]")
    ed = None
    if isbn:
        for e in editions:
            if isbn in re.sub(r"[^0-9X]", "", e.get_text()):
                ed = e
                break
    if ed is not None:
        pub = ed.select_one('a[href^="/kiadok/"]')
        if pub:
            d["publisher"] = pub.get_text(strip=True)
        yr = ed.find("abbr")
        if yr:
            d["publish_year"] = parse_year(yr.get_text())
        m = re.search(r"(\d+)\s*oldal", ed.get_text(" "))
        if m:
            d["pages"] = int(m.group(1))
        img = ed.find("img", class_="screenshot")
        if img and img.get("src"):
            d["cover_url"] = img["src"].replace("/covers/normal/", "/covers/big/").split("?")[0]
        d["isbn"] = isbn
    d["link"] = None  # a moly nem bolt; a linket nem írjuk felül
    d["_moly_url"] = url
    return d


def _moly_by_isbn(isbn: str) -> dict | None:
    urls = moly_search(isbn, 1)
    if not urls:
        return None
    try:
        return moly_book(urls[0], isbn)
    except Exception as e:
        log.warning("Moly hiba: %s", e)
        return None


def lookup_isbn(isbn: str) -> dict:
    isbn = normalize_isbn(isbn) or re.sub(r"[^0-9Xx]", "", isbn or "")
    res = empty()
    res["isbn"] = isbn
    hungarian = isbn.startswith(("978963", "978615", "963", "615"))
    if hungarian:
        res = merge(res, _moly_by_isbn(isbn))
    g = google_books(f"isbn:{isbn}", 1)
    if g:
        res = merge(res, g[0])
    res = merge(res, open_library_isbn(isbn))
    if not hungarian and (not res.get("title") or not res.get("description")):
        res = merge(res, _moly_by_isbn(isbn))
    if not res.get("title") and ANTHROPIC_API_KEY:
        res = merge(res, ai_identify(text_hint=f"ISBN: {isbn}"))
    res["isbn"] = isbn
    return res


def search_title(q: str) -> list[dict]:
    """Találati lista választáshoz: moly.hu + Google Books + Open Library."""
    from concurrent.futures import ThreadPoolExecutor
    out: list[dict] = []
    urls = moly_search(q, 4)
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_moly = [ex.submit(moly_book, u) for u in urls]
        fut_g = ex.submit(google_books, q, 4)
        fut_ol = ex.submit(open_library_search, q, 4)
        for f in fut_moly:
            try:
                d = f.result()
                d["_source"] = "moly.hu"
                out.append(d)
            except Exception as e:
                log.warning("Moly hiba: %s", e)
        for d in fut_g.result():
            d["_source"] = "Google Books"
            out.append(d)
        for d in fut_ol.result():
            d["_source"] = "Open Library"
            out.append(d)
    return out[:10]


def lookup_url(url: str) -> dict:
    res = empty()
    if "moly.hu/konyvek/" in url:
        try:
            res = moly_book(url.split("?")[0])
            res["link"] = url
            res["shop"] = None
            return res
        except Exception as e:
            log.warning("Moly hiba: %s", e)
    try:
        res = fetch_page(url)
    except Exception as e:
        log.warning("Oldal letöltési hiba (%s): %s", url, e)
        res["link"] = url
        res["shop"] = shop_from_url(url)
        res["_warning"] = f"Az oldalt nem sikerült letölteni: {e}"
    if res.get("isbn"):
        extra = lookup_isbn(res["isbn"])
        extra.pop("link", None)
        res = merge(res, extra)
    elif res.get("title"):
        q = res["title"] + (" " + res["authors"][0] if res.get("authors") else "")
        g = google_books(q, 1)
        if g and g[0].get("title", "").lower()[:12] == res["title"].lower()[:12]:
            g[0].pop("link", None)
            res = merge(res, g[0])
    if (not res.get("title") or not res.get("description")) and ANTHROPIC_API_KEY:
        res = merge(res, ai_identify(text_hint=f"A könyv ezen a linken érhető el: {url}\n"
                                               f"Eddig ismert adatok: {json.dumps(res, ensure_ascii=False)}"))
    # "Szerző: Cím" -> "Cím" (ha a szerző csak később derült ki)
    for a in res.get("authors") or []:
        if (res.get("title") or "").lower().startswith(a.lower() + ":"):
            res["title"] = res["title"][len(a) + 1:].strip()
            break
    return res


# ---------------------------------------------------------------- kép

def decode_barcode(img_bytes: bytes) -> str | None:
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except Exception as e:  # pyzbar / libzbar hiányzik
        log.info("Vonalkód olvasó nem elérhető: %s", e)
        return None
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img = img.convert("L")
        for candidate in (img, img.rotate(90, expand=True), img.rotate(270, expand=True)):
            for sym in decode(candidate):
                n = normalize_isbn(sym.data.decode("ascii", "ignore"))
                if n and n.startswith("97"):
                    return n
    except Exception as e:
        log.warning("Vonalkód hiba: %s", e)
    return None


def ocr_text(img_bytes: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
        return pytesseract.image_to_string(img, lang="hun+eng")
    except Exception as e:
        log.info("OCR nem elérhető: %s", e)
        return ""


def lookup_image(img_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    res = empty()
    isbn = decode_barcode(img_bytes)
    if isbn:
        res = lookup_isbn(isbn)
        res["_source"] = "vonalkód"
        if res.get("title"):
            return res
    if ANTHROPIC_API_KEY:
        res = merge(res, ai_identify(image=(img_bytes, media_type)))
        res["_source"] = "AI"
        if res.get("isbn") and not res.get("description"):
            res = merge(res, lookup_isbn(res["isbn"]))
        return res
    text = ocr_text(img_bytes)
    isbn = isbn or find_isbn(text)
    if isbn:
        res = merge(res, lookup_isbn(isbn))
        res["_source"] = "OCR + ISBN"
        return res
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿŐőŰű]{3,}", text)][:8]
    if words:
        q = " ".join(words)
        urls = moly_search(q, 1)
        hit = None
        if urls:
            try:
                hit = moly_book(urls[0])
            except Exception:
                hit = None
        if not hit:
            g = google_books(q, 1) or open_library_search(q, 1)
            hit = g[0] if g else None
        if hit:
            res = merge(res, hit)
            res["_source"] = "szövegfelismerés (bizonytalan, ellenőrizd!)"
    if not res.get("title"):
        res["_warning"] = ("A képről nem sikerült azonosítani a könyvet. Fotózd le a hátoldali "
                           "vonalkódot, vagy add meg az ISBN-t / címet.")
    return res


# ---------------------------------------------------------------- AI (opcionális)

AI_PROMPT = """Egy magyar házikönyvtár-nyilvántartó alkalmazásnak segítesz. Azonosítsd a könyvet \
a megadott kép és/vagy információk alapján. Ha kell, keress rá a weben (pl. moly.hu, libri.hu, \
bookline.hu, antikvarium.hu), hogy pontos adatokat adj. A leírás a könyv hivatalos fülszövege / \
hátoldali szövege legyen, eredeti nyelven. Ne találj ki adatot: amit nem tudsz, hagyd null-on.

Válaszod KIZÁRÓLAG egyetlen JSON objektum legyen, ezekkel a kulcsokkal:
{"title": str|null, "subtitle": str|null, "authors": [str], "isbn": str|null, \
"publisher": str|null, "publish_year": int|null, "description": str|null, \
"price": number|null, "currency": str|null, "tags": [str], "cover_url": str|null, \
"link": str|null, "shop": str|null}
A tags mezőbe 1-3 magyar műfaji címkét írj (pl. Szépirodalom, Krimi, Ezoterikus, Tudományos, \
Tankönyv, Gyerekkönyv, Történelem, Pszichológia, Szakács)."""


def ai_identify(image: tuple[bytes, str] | None = None, text_hint: str | None = None) -> dict:
    if not ANTHROPIC_API_KEY:
        return {}
    try:
        import anthropic
    except ImportError:
        return {}
    content = []
    if image:
        data, mt = image
        data, mt = _shrink_for_ai(data, mt)
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": mt,
            "data": base64.standard_b64encode(data).decode("ascii")}})
    content.append({"type": "text", "text": AI_PROMPT + ("\n\n" + text_hint if text_hint else "")})
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120)
        resp = client.messages.create(
            model=AI_MODEL,
            max_tokens=16000,
            output_config={"effort": "low"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        log.warning("AI hiba: %s", e)
        return {"_warning": f"AI felismerés sikertelen: {e}"}
    if resp.stop_reason == "refusal":
        return {}
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {}
    out = empty()
    for k in ("title", "subtitle", "isbn", "publisher", "description", "currency",
              "cover_url", "link", "shop"):
        if d.get(k):
            out[k] = str(d[k]).strip()
    out["authors"] = [str(a) for a in d.get("authors") or [] if a]
    out["tags"] = [str(a) for a in d.get("tags") or [] if a]
    out["publish_year"] = parse_year(d.get("publish_year"))
    out["price"] = parse_price(d.get("price"))
    if out.get("isbn"):
        out["isbn"] = normalize_isbn(out["isbn"]) or out["isbn"]
    return out


def _shrink_for_ai(data: bytes, mt: str) -> tuple[bytes, str]:
    try:
        from PIL import Image, ImageOps
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        img.thumbnail((1568, 1568))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, mt


def download_image(url: str) -> bytes | None:
    try:
        with _client() as c:
            r = c.get(url)
            r.raise_for_status()
            if len(r.content) < 500:
                return None
            ctype = r.headers.get("content-type", "")
            if "image" not in ctype and not url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                return None
            return r.content
    except Exception as e:
        log.warning("Kép letöltési hiba (%s): %s", url, e)
        return None
