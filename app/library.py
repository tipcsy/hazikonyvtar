"""A közös e-könyv mappa (pl. /mnt/truenas_share/Könyvek) kezelése.

- Átvizsgálás: a könyvformátumú fájlok felderítése, beágyazott adatok kiolvasása
  (EPUB: OPF, MOBI/AZW3: EXTH fejléc, PDF: dokumentum-adatok, egyébként fájlnév).
- Csoportosítás: ugyanazon könyv több formátuma (azonos mappa + cím) egy könyv lesz.
- Importálás: a kijelölt csoportokból könyv lesz, a fájlok a HELYÜKÖN maradnak
  (book_files.source = 'library'), borító kinyerése a fájlból.
A mappát a program soha nem írja.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import db, metadata

log = logging.getLogger("library")

FORMATS = {".epub": "EPUB", ".mobi": "MOBI", ".azw": "AZW", ".azw3": "AZW3", ".prc": "PRC",
           ".pdf": "PDF", ".fb2": "FB2", ".djvu": "DJVU", ".doc": "DOC", ".docx": "DOCX",
           ".rtf": "RTF", ".cbz": "CBZ", ".cbr": "CBR"}
# Ezeknél a nevű mappáknál lévő fájlokat kihagyjuk (rendszerfájlok)
# Ha az adatkiolvasás javul, ezt növeld: a következő átvizsgálás mindent újraolvas
EXTRACT_VERSION = 5
SKIP_DIRS = {"@eaDir", ".AppleDouble", "#recycle", ".Trash", "$RECYCLE.BIN", ".git"}

state = {
    "scanning": False, "scan_done": 0, "scan_total": 0, "last_scan": None, "scan_error": None,
    "importing": False, "import_done": 0, "import_total": 0, "import_error": None,
}
_scan_lock = threading.Lock()
_import_lock = threading.Lock()


def enabled() -> bool:
    return bool(db.LIBRARY_DIR and db.LIBRARY_DIR.is_dir())


def abs_path(rel: str) -> Path | None:
    """Biztonságos teljes útvonal a könyvtár mappán belül."""
    if not db.LIBRARY_DIR:
        return None
    p = (db.LIBRARY_DIR / rel).resolve()
    if db.LIBRARY_DIR != p and db.LIBRARY_DIR not in p.parents:
        return None
    return p


# ------------------------------------------------------------------ szöveg segédek

def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


JUNK_RE = re.compile(r"(-?up\s*by\s*\w+|-?olvas\s*om|\(\d\)|\[[^\]]*\]|\b(prc|doc|epub|mobi|pdf)\s*valtozat\b)", re.I)


def _clean_title(t: str) -> str:
    t = t.replace("_", " ")
    t = JUNK_RE.sub(" ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -.,")
    return t


def _looks_like_name(s: str) -> bool:
    words = s.split()
    return 1 <= len(words) <= 5 and all(w[:1].isupper() or w[:1] in ".-" for w in words if w)


def parse_filename(rel: str) -> tuple[str, list[str]]:
    """Cím és szerző kitalálása az útvonalból, ha a fájlban nincs adat."""
    p = Path(rel)
    stem = p.stem
    top = p.parts[0] if len(p.parts) > 1 else ""
    folder_author = top if top and not top.startswith("--") else ""
    # " - " elválasztóval írt szerzőnevek a mappanévben ("Dan Greenburg - Marcia Jacobs")
    folder_authors = [a.strip() for a in folder_author.split(" - ")] if folder_author else []

    # MEK stílus: "Kategoria.Szerzo_Nev.Cim"
    parts = stem.split(".")
    if len(parts) >= 3 and "_" in parts[1] and " " not in parts[1]:
        author = parts[1].replace("_", " ")
        return _clean_title(" ".join(parts[2:])), [author]

    s = _clean_title(stem)
    s = re.sub(r"^\d{1,4}\s*[-.]\s*", "", s)          # "04 - Cím", "1992 - Cím"
    chunks = [c.strip() for c in re.split(r"\s+-\s+|_-_", s) if c.strip()]
    chunks = [c for c in chunks if not re.fullmatch(r"\d{1,4}", c)]
    authors: list[str] = []
    title = s
    if len(chunks) >= 2:
        known = [norm(a) for a in folder_authors]
        if known and norm(chunks[0]) in known:
            authors, title = [chunks[0]], " - ".join(chunks[1:])
        elif known and norm(chunks[-1]) in known:
            authors, title = [chunks[-1]], " - ".join(chunks[:-1])
        elif known and any(w in norm(chunks[0]).split() for a in known for w in a.split() if len(w) > 3):
            # "Hawking-Mlodinow - Cím" a "Stephen Hawking" mappában -> a mappa szerint
            authors, title = folder_authors[:1], " - ".join(chunks[1:])
        elif _looks_like_name(chunks[0]) and len(chunks[0].split()) <= 4:
            authors, title = [chunks[0]], " - ".join(chunks[1:])
        else:
            title = " - ".join(chunks)
    if not authors and folder_authors:
        authors = folder_authors[:1]
    # "Rejtő Jenő_Pokol a hegyek között" -> a szerző a mappa nevéből
    for a in authors:
        if norm(title).startswith(norm(a) + " "):
            title = title[len(a):].strip(" -_")
    title = re.sub(r"^\d{1,3}\s*[-.]\s*", "", title).strip() or stem
    return title, authors


# ------------------------------------------------------------------ EPUB

NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/",
      "c": "urn:oasis:names:tc:opendocument:xmlns:container"}


def _epub_opf(z: zipfile.ZipFile):
    container = ET.fromstring(z.read("META-INF/container.xml"))
    rootfile = container.find(".//c:rootfile", NS).get("full-path")
    return rootfile, ET.fromstring(z.read(rootfile))


def epub_meta(path: Path) -> dict:
    d: dict = {}
    with zipfile.ZipFile(path) as z:
        rootfile, opf = _epub_opf(z)
        md = opf.find("opf:metadata", NS)
        if md is None:
            return d
        txt = lambda tag: [(e.text or "").strip() for e in md.findall(f"dc:{tag}", NS) if (e.text or "").strip()]
        t = txt("title")
        d["title"] = t[0] if t else None
        # csak a szerzők (a fordító, szerkesztő, illusztrátor nem)
        roles = {}
        for m in md.iter("{http://www.idpf.org/2007/opf}meta"):   # EPUB3: <meta refines="#id" property="role">
            if m.get("property") == "role" and m.get("refines"):
                roles[m.get("refines").lstrip("#")] = (m.text or "").strip()
        authors = []
        for e in md.findall("dc:creator", NS):
            name = (e.text or "").strip()
            role = e.get("{http://www.idpf.org/2007/opf}role") or roles.get(e.get("id") or "", "")
            if name and (not role or role == "aut"):
                authors.append(name)
        d["authors"] = authors[:6]
        desc = txt("description")
        d["description"] = metadata.clean_text(desc[0]) if desc else None
        pub = txt("publisher")
        d["publisher"] = pub[0] if pub else None
        dates = txt("date")
        d["publish_year"] = metadata.parse_year(dates[0]) if dates else None
        lang = txt("language")
        d["language"] = lang[0] if lang else None
        for ident in txt("identifier"):
            n = metadata.normalize_isbn(re.sub(r"^(urn:)?isbn:?", "", ident, flags=re.I))
            if n:
                d["isbn"] = n
                break
        d["subjects"] = txt("subject")[:10]
        d["has_cover"] = _epub_cover_name(z, rootfile, opf) is not None
    return d


def _epub_cover_name(z, rootfile, opf) -> str | None:
    base = rootfile.rsplit("/", 1)[0] + "/" if "/" in rootfile else ""
    manifest = opf.find("opf:manifest", NS)
    if manifest is None:
        return None
    items = manifest.findall("opf:item", NS)
    cover_id = None
    for m in opf.iter("{http://www.idpf.org/2007/opf}meta"):
        if m.get("name") == "cover":
            cover_id = m.get("content")
    cand = None
    for it in items:
        mt = it.get("media-type", "")
        if not mt.startswith("image/"):
            continue
        if "cover-image" in (it.get("properties") or "") or (cover_id and it.get("id") == cover_id):
            cand = it
            break
        if cand is None and "cover" in (it.get("id", "") + it.get("href", "")).lower():
            cand = it
    if cand is None:
        return None
    from urllib.parse import unquote
    name = base + unquote(cand.get("href"))
    # "../" feloldása
    parts = []
    for seg in name.split("/"):
        if seg == "..":
            parts and parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    name = "/".join(parts)
    return name if name in z.namelist() else None


def epub_cover(path: Path) -> bytes | None:
    with zipfile.ZipFile(path) as z:
        rootfile, opf = _epub_opf(z)
        name = _epub_cover_name(z, rootfile, opf)
        return z.read(name) if name else None


# ------------------------------------------------------------------ MOBI / AZW3 / PRC

def _mobi_records(f) -> tuple[list[int], bytes]:
    f.seek(0)
    head = f.read(78)
    if len(head) < 78:
        raise ValueError("túl rövid")
    n = struct.unpack(">H", head[76:78])[0]
    offs = [struct.unpack(">I", f.read(8)[:4])[0] for _ in range(n)]
    return offs, head


def mobi_meta(path: Path, want_cover: bool = False) -> dict:
    d: dict = {}
    with path.open("rb") as f:
        offs, head = _mobi_records(f)
        d["title"] = head[:32].split(b"\0")[0].decode("latin-1").replace("_", " ").strip() or None
        f.seek(offs[0])
        rec0 = f.read((offs[1] - offs[0]) if len(offs) > 1 else 65536)
        if rec0[16:20] != b"MOBI":
            return d  # régi PalmDOC (PRC) - csak a belső név
        mobi_len, enc = struct.unpack(">I", rec0[20:24])[0], struct.unpack(">I", rec0[28:32])[0]
        codec = "utf-8" if enc == 65001 else "cp1252"
        name_off, name_len = struct.unpack(">II", rec0[84:92])
        full = rec0[name_off:name_off + name_len].decode(codec, "ignore").strip()
        if full:
            d["title"] = full
        first_img = struct.unpack(">I", rec0[108:112])[0]
        exth_flag = struct.unpack(">I", rec0[128:132])[0]
        cover_off = None
        authors = []
        if exth_flag & 0x40:
            pos = 16 + mobi_len
            if rec0[pos:pos + 4] == b"EXTH":
                count = struct.unpack(">I", rec0[pos + 8:pos + 12])[0]
                p = pos + 12
                for _ in range(count):
                    if p + 8 > len(rec0):
                        break
                    typ, ln = struct.unpack(">II", rec0[p:p + 8])
                    data = rec0[p + 8:p + ln]
                    p += ln
                    if typ in (201,) and len(data) >= 4:
                        cover_off = struct.unpack(">I", data[:4])[0]
                        continue
                    val = data.decode(codec, "ignore").strip()
                    if typ == 100 and val:
                        authors += [a.strip() for a in re.split(r";|&", val) if a.strip()]
                    elif typ == 101:
                        d["publisher"] = val
                    elif typ == 103:
                        d["description"] = metadata.clean_text(val)
                    elif typ == 104:
                        d["isbn"] = metadata.normalize_isbn(val)
                    elif typ == 105:
                        d.setdefault("subjects", []).append(val)
                    elif typ == 106:
                        d["publish_year"] = metadata.parse_year(val)
                    elif typ == 503 and val:
                        d["title"] = val
                    elif typ == 524:
                        d["language"] = val
        d["authors"] = authors[:6]
        d["has_cover"] = cover_off is not None and first_img not in (0, 0xFFFFFFFF)
        if want_cover and d["has_cover"]:
            idx = first_img + cover_off
            if idx + 1 < len(offs):
                f.seek(offs[idx])
                d["_cover"] = f.read(offs[idx + 1] - offs[idx])
    return d


# ------------------------------------------------------------------ PDF

def pdf_meta(path: Path) -> dict:
    d: dict = {}
    try:
        from pypdf import PdfReader
        r = PdfReader(str(path), strict=False)
        info = r.metadata or {}
        t = (info.get("/Title") or "").strip()
        a = (info.get("/Author") or "").strip()
        if t and len(t) > 2 and not re.search(r"\.(docx?|indd|qxd|pdf|tex)\b|^untitled|microsoft word|^\d+$", t, re.I):
            d["title"] = t
        if a and len(a) < 80 and not re.search(r"^(admin|user|owner|unknown)", a, re.I):
            # vesszőnél NEM vágunk ("Karinthy, Frigyes 1887-1938" egy név), csak ; & és "és" mentén
            d["authors"] = [x.strip() for x in re.split(r";|&|\s+és\s+", a) if x.strip()][:4]
        d["has_cover"] = True
    except Exception as e:
        log.debug("PDF meta hiba %s: %s", path, e)
        d["has_cover"] = True
    return d


def pdf_cover(path: Path) -> bytes | None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "c"
        try:
            subprocess.run(["pdftoppm", "-jpeg", "-f", "1", "-l", "1", "-scale-to", "800", str(path), str(out)],
                           check=True, timeout=60, capture_output=True)
        except Exception as e:
            log.info("PDF borító hiba %s: %s", path.name, e)
            return None
        files = list(Path(td).glob("c*.jpg"))
        return files[0].read_bytes() if files else None


# ------------------------------------------------------------------ átvizsgálás

def extract(rel: str, path: Path, fmt: str) -> dict:
    meta: dict = {}
    try:
        if fmt == "EPUB":
            meta = epub_meta(path)
        elif fmt in ("MOBI", "AZW", "AZW3", "PRC"):
            meta = mobi_meta(path)
        elif fmt == "PDF":
            meta = pdf_meta(path)
    except Exception as e:
        log.info("Adatkiolvasási hiba (%s): %s", rel, e)
    ftitle, fauthors = parse_filename(rel)
    title = (meta.get("title") or "").strip()
    # értelmetlen beágyazott címek kiszűrése
    if not title or len(title) < 2 or re.fullmatch(r"[\w\-]+\.(\w{2,4})", title) or norm(title) in ("unknown", "untitled"):
        title = ftitle
    authors = [clean_author(a) for a in (meta.get("authors") or [])]
    authors = [a for a in authors
               if a and len(norm(a)) > 2 and re.search(r"[A-Za-zÀ-ž]", a) and not JUNK_AUTHOR.search(norm(a))]
    if fmt not in ("EPUB", "MOBI", "AZW", "AZW3"):
        # PDF/DOC/RTF: a "szerző" mező gyakran a gép/felhasználó neve (Asus, Rendszergazda...):
        # egyszavas nevet ezekből nem fogadunk el
        authors = [a for a in authors if " " in a.strip()]
    authors = authors or fauthors
    # A beágyazott "szerző" gyakran a fordító/szkennelő: ha a mappa egy szerző nevét viseli,
    # és nincs átfedés, a mappa/fájlnév szerinti szerző nyer
    top = rel.split("/")[0] if "/" in rel else ""
    if top and not top.startswith("--") and fauthors:
        fw = {w for a in fauthors for w in norm(a).split() if len(w) > 2}
        if not any(fw & set(norm(a).split()) for a in authors):
            authors = fauthors
    # takarítás: "- R. A. Salvatore", szerzőként szereplő cím, "-upByOM" utótagok
    authors = [a.strip(" -–_.,;") for a in authors]
    authors = [a for a in authors if a and norm(a) != norm(title)] or [a for a in fauthors if norm(a) != norm(title)]
    title = _clean_title(title)
    # "Szerző - Cím" / "asimov isaac utikalauz" alakú cím: a szerző nevének levágása (szórendtől függetlenül)
    words = title.split()
    for a in authors:
        aw = set(norm(a).split())
        n = len(norm(a).split())
        if n and len(words) > n and {norm(w) for w in words[:n]} - {""} == aw:
            title = " ".join(words[n:]).lstrip(" -–:,.")
            break
    if title and title == title.lower():
        title = title[0].upper() + title[1:]
    meta["title"], meta["authors"] = title, authors
    return meta


JUNK_AUTHOR = re.compile(r"^(unknown|ismeretlen|verypdf|calibre|administrator|admin|rendszergazda|user|owner|"
                         r"author|szerzo|anonymous|abbyy|adobe|microsoft|hp|dell|lenovo|asus|acer|pc|"
                         r"felhasznalo|tulajdonos)\b|^(n a|na)$|^www |^http|\b(hu|com|org)$", re.I)


def clean_author(a: str) -> str:
    """'Arany, János 1817-1882' -> 'Arany János'; '(1835-1914)', 'szerk.' és hasonlók le."""
    a = re.sub(r"\(?\b1[0-9]{3}\s*-\s*(1[0-9]{3}|20[0-9]{2})?\s*\)?", " ", a or "")
    a = re.sub(r"\s+\b(1[5-9]|20)\d{2}\b\s*-?$", " ", a)
    a = _flip_name(re.sub(r"\s{2,}", " ", a).strip(" ,;-–_."))
    return a.strip()


def _flip_name(a: str) -> str:
    """'Gárdonyi, Géza' -> 'Gárdonyi Géza' (magyar sorrend marad)."""
    if "," in a and len(a.split(",")) == 2:
        x, y = [s.strip() for s in a.split(",")]
        return f"{x} {y}"
    return a.strip()


def group_key(dir_: str, title: str, authors: list[str]) -> str:
    return f"{dir_}|{norm(title)}"


def scan(force_missing: bool = False):
    """A teljes mappa átvizsgálása (háttérszálon). Csak a változott fájlokat olvassa újra."""
    if not enabled():
        state["scan_error"] = "A könyvtár mappa nincs beállítva vagy nem elérhető (LIBRARY_DIR)."
        return
    if not _scan_lock.acquire(blocking=False):
        return
    state.update(scanning=True, scan_done=0, scan_total=0, scan_error=None)
    try:
        root = db.LIBRARY_DIR
        found: list[tuple[str, Path, str]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in FORMATS and not fn.startswith("._"):
                    p = Path(dirpath) / fn
                    found.append((p.relative_to(root).as_posix(), p, FORMATS[ext]))
        state["scan_total"] = len(found)
        with db.connect() as con:
            rows = con.execute("SELECT path, size, mtime, meta, authors, state, book_file_id FROM library_files").fetchall()
            known = {r["path"]: (r["size"], r["mtime"], (json.loads(r["meta"] or "{}") or {}).get("v")) for r in rows}
            imported = {r["path"]: (r["book_file_id"], json.loads(r["authors"] or "[]"))
                        for r in rows if r["state"] == "imported" and r["book_file_id"]}
        seen = set()
        batch = []
        author_fixes = []   # (book_file_id, régi szerzők, új szerzők)
        for i, (rel, p, fmt) in enumerate(found):
            seen.add(rel)
            state["scan_done"] = i + 1
            try:
                st = p.stat()
            except OSError:
                continue
            if rel in known and known[rel] == (st.st_size, st.st_mtime, EXTRACT_VERSION):
                continue
            meta = extract(rel, p, fmt)
            meta["v"] = EXTRACT_VERSION
            if rel in imported and [norm(a) for a in imported[rel][1]] != [norm(a) for a in meta["authors"]]:
                author_fixes.append((imported[rel][0], imported[rel][1], meta["authors"]))
            dir_ = str(Path(rel).parent.as_posix()) if "/" in rel else ""
            batch.append((rel, dir_, Path(rel).name, fmt, st.st_size, st.st_mtime, meta["title"],
                          json.dumps(meta["authors"], ensure_ascii=False),
                          json.dumps({k: v for k, v in meta.items() if k not in ("title", "authors") and not k.startswith("_")},
                                     ensure_ascii=False),
                          group_key(dir_, meta["title"], meta["authors"])))
            if len(batch) >= 50:
                _save_batch(batch)
                batch = []
        _save_batch(batch)
        if author_fixes:
            n = apply_author_fixes(author_fixes)
            log.info("Javított szerzők %d importált könyvnél", n)
        handle_missing(seen, force=force_missing)
        state["last_scan"] = time.strftime("%Y-%m-%d %H:%M")
        log.info("Könyvtár átvizsgálva: %d fájl", len(found))
    except Exception as e:
        log.exception("Átvizsgálási hiba")
        state["scan_error"] = str(e)
    finally:
        state["scanning"] = False
        _scan_lock.release()


MAX_MISSING_RATIO = 0.2   # ennél több eltűnt fájlnál (pl. nem elérhető megosztás) nem törlünk semmit


def handle_missing(seen: set[str], force: bool = False):
    """A mappából eltűnt fájlok kezelése.
    - Áthelyezett/átnevezett fájl (azonos név+méret, vagy azonos méret+mappa): a könyv átkerül az új útvonalra.
    - Törölt fájl: lekerül a könyvről; ha a könyvnek így nem marad fájlja és papíron sincs meg, a könyv is törlődik.
    - Biztonsági fék: ha a könyvtár üresnek látszik vagy túl sok fájl tűnt el, semmit nem töröl."""
    removed_books = 0
    with db.write() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM library_files")]
        gone = [r for r in rows if r["path"] not in seen]
        imported_total = sum(1 for r in rows if r["state"] == "imported")
        gone_imported = [r for r in gone if r["state"] == "imported"]
        # be nem importált, eltűnt fájlok egyszerűen kikerülnek a listából
        for r in gone:
            if r["state"] != "imported":
                con.execute("DELETE FROM library_files WHERE id=?", (r["id"],))
        if not gone_imported:
            con.execute("UPDATE library_files SET missing=0")
            return
        if not seen or (not force and len(gone_imported) > max(5, imported_total * MAX_MISSING_RATIO)):
            for r in gone_imported:
                con.execute("UPDATE library_files SET missing=1 WHERE id=?", (r["id"],))
            state["scan_error"] = (f"{len(gone_imported)} importált fájl nem található - a megosztás talán nem "
                                   f"elérhető, ezért nem töröltem semmit. Ha szándékosan törölted őket, nyomd meg a Törlés jóváhagyása gombot.")
            log.warning(state["scan_error"])
            return
        # áthelyezés/átnevezés: új (még nem importált) fájl azonos névvel+mérettel, vagy azonos mérettel ugyanott
        fresh = [r for r in rows if r["path"] in seen and r["state"] == "new"]
        for r in gone_imported:
            match = next((f for f in fresh if f["name"] == r["name"] and f["size"] == r["size"]), None)
            if not match:
                # átnevezve és/vagy áthelyezve: bájtra azonos méret + formátum, ha egyértelmű
                same = [f for f in fresh if f["size"] == r["size"] and f["format"] == r["format"]]
                match = same[0] if len(same) == 1 else None
            if match:
                fresh.remove(match)
                con.execute("UPDATE book_files SET path=?, orig_name=? WHERE id=?",
                            (match["path"], match["name"], r["book_file_id"]))
                con.execute("UPDATE library_files SET state='imported', book_file_id=? WHERE id=?",
                            (r["book_file_id"], match["id"]))
                con.execute("DELETE FROM library_files WHERE id=?", (r["id"],))
                log.info("Áthelyezett fájl: %s -> %s", r["path"], match["path"])
                continue
            # tényleg törölték
            con.execute("DELETE FROM library_files WHERE id=?", (r["id"],))
            bf = con.execute("SELECT book_id FROM book_files WHERE id=?", (r["book_file_id"],)).fetchone()
            if not bf:
                continue
            book_id = bf[0]
            con.execute("DELETE FROM book_files WHERE id=?", (r["book_file_id"],))
            b = con.execute("SELECT formats, cover_path, back_path FROM books WHERE id=?", (book_id,)).fetchone()
            if not b:
                continue
            left = [x[0] for x in con.execute("SELECT format FROM book_files WHERE book_id=?", (book_id,))]
            fmts = [f for f in (b["formats"] or "").split(",") if f]
            if not left and "Papír" not in fmts:
                con.execute("DELETE FROM books WHERE id=?", (book_id,))
                db.reindex_book(con, book_id)
                for rel in (b["cover_path"], b["back_path"]):
                    if rel:
                        for p in (db.DATA_DIR / rel, db.DATA_DIR / rel.replace(".jpg", "_t.jpg")):
                            p.unlink(missing_ok=True)
                removed_books += 1
            else:
                keep = [f for f in fmts if f == "Papír" or f in left or f not in FORMATS.values()]
                con.execute("UPDATE books SET formats=?, updated_at=datetime('now') WHERE id=?",
                            (",".join(keep), book_id))
                db.reindex_book(con, book_id)
        con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
        con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
        con.execute("UPDATE library_files SET missing=0")
    if removed_books:
        log.info("A mappából törölt fájlok miatt %d könyv lekerült a listáról", removed_books)


def set_book_authors(con, book_id: int, authors: list[str]):
    con.execute("DELETE FROM book_authors WHERE book_id=?", (book_id,))
    seen = set()
    for pos, name in enumerate(a.strip() for a in authors):
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        con.execute("INSERT OR IGNORE INTO authors(name) VALUES (?)", (name,))
        aid = con.execute("SELECT id FROM authors WHERE name=?", (name,)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO book_authors(book_id, author_id, position) VALUES (?,?,?)", (book_id, aid, pos))
    con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")


def apply_author_fixes(fixes) -> int:
    """Javított szerzők átvezetése az importált könyvekre - CSAK ha a könyv szerzőit
    az import óta senki nem módosította (különben a kézi javítás marad)."""
    n = 0
    with db.write() as con:
        for bf_id, old, new in fixes:
            r = con.execute("SELECT book_id FROM book_files WHERE id=?", (bf_id,)).fetchone()
            if not r:
                continue
            book_id = r[0]
            cur = [x[0] for x in con.execute(
                "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id WHERE ba.book_id=? "
                "ORDER BY ba.position", (book_id,))]
            if [norm(a) for a in cur] != [norm(a) for a in old]:
                continue
            set_book_authors(con, book_id, new)
            # ha még nincs leírása, a kiegészítő az új szerzővel újra megpróbálhatja
            con.execute("UPDATE books SET enriched_at=NULL, updated_at=datetime('now') "
                        "WHERE id=? AND (description IS NULL OR trim(description)='')", (book_id,))
            db.reindex_book(con, book_id)
            n += 1
    return n


def _save_batch(batch):
    if not batch:
        return
    with db.connect() as con:
        for row in batch:
            con.execute("""
                INSERT INTO library_files(path, dir, name, format, size, mtime, title, authors, meta, group_key, scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'))
                ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, title=excluded.title,
                    authors=excluded.authors, meta=excluded.meta, group_key=excluded.group_key,
                    scanned_at=excluded.scanned_at, missing=0
            """, row)


def start_scan(force_missing: bool = False):
    threading.Thread(target=scan, kwargs={"force_missing": force_missing}, daemon=True).start()


def counts() -> dict:
    with db.connect() as con:
        r = {row["state"]: row["n"] for row in con.execute(
            "SELECT state, COUNT(*) AS n FROM library_files WHERE missing=0 GROUP BY state")}
        miss = con.execute("SELECT COUNT(*) FROM library_files WHERE missing=1").fetchone()[0]
    return {"new": r.get("new", 0), "imported": r.get("imported", 0), "ignored": r.get("ignored", 0), "missing": miss}


def groups(which: str = "new") -> list[dict]:
    with db.connect() as con:
        rows = con.execute("SELECT * FROM library_files WHERE state=? AND missing=0 ORDER BY dir, name",
                           (which,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        g = out.get(r["group_key"])
        meta = json.loads(r["meta"] or "{}")
        if g is None:
            g = out[r["group_key"]] = {
                "key": r["group_key"], "dir": r["dir"], "title": r["title"],
                "authors": json.loads(r["authors"] or "[]"), "files": [], "size": 0,
                "has_meta": bool(meta.get("description") or meta.get("isbn"))}
        g["files"].append({"id": r["id"], "name": r["name"], "format": r["format"], "size": r["size"]})
        g["size"] += r["size"] or 0
        if not g["authors"]:
            g["authors"] = json.loads(r["authors"] or "[]")
    return list(out.values())


def set_ignored(file_ids: list[int], ignored: bool):
    with db.connect() as con:
        ph = ",".join("?" * len(file_ids))
        if ignored:
            con.execute(f"UPDATE library_files SET state='ignored' WHERE state='new' AND id IN ({ph})", file_ids)
        else:
            con.execute(f"UPDATE library_files SET state='new' WHERE state='ignored' AND id IN ({ph})", file_ids)


# ------------------------------------------------------------------ importálás

def folder_tags(rel: str) -> list[str]:
    top = rel.split("/")[0] if "/" in rel else ""
    if top.startswith("--"):
        t = top.lstrip("- ").strip()
        return [t] if t else []
    return []


def cover_for(path: Path, fmt: str) -> bytes | None:
    try:
        if fmt == "EPUB":
            return epub_cover(path)
        if fmt in ("MOBI", "AZW", "AZW3"):
            return mobi_meta(path, want_cover=True).get("_cover")
        if fmt == "PDF":
            return pdf_cover(path)
    except Exception as e:
        log.info("Borító hiba (%s): %s", path.name, e)
    return None


def run_import(items: list[dict], owner: str | None, user: str, save_image, set_relations, on_done=None):
    """items: [{file_ids:[..], title:str, authors:[..]}]; on_done: import után hívódik (pl. leírás-kiegészítés)"""
    if not _import_lock.acquire(blocking=False):
        raise RuntimeError("Már fut egy importálás")
    state.update(importing=True, import_done=0, import_total=len(items), import_error=None)

    def work():
        try:
            for it in items:
                try:
                    _import_one(it, owner, user, save_image, set_relations)
                except Exception as e:
                    log.exception("Import hiba")
                    state["import_error"] = f"{it.get('title')}: {e}"
                state["import_done"] += 1
        finally:
            state["importing"] = False
            _import_lock.release()
        if on_done:
            try:
                on_done()
            except Exception as e:
                log.info("Import utáni lépés: %s", e)

    threading.Thread(target=work, daemon=True).start()


FORMAT_ORDER = ["EPUB", "AZW3", "MOBI", "AZW", "PDF", "PRC", "FB2", "DJVU", "DOCX", "DOC", "RTF", "CBZ", "CBR"]


def _import_one(it: dict, owner, user, save_image, set_relations):
    ids = [int(i) for i in it.get("file_ids") or []]
    if not ids:
        return
    with db.connect() as con:
        ph = ",".join("?" * len(ids))
        files = [dict(r) for r in con.execute(
            f"SELECT * FROM library_files WHERE id IN ({ph}) AND state<>'imported'", ids)]
    if not files:
        return
    files.sort(key=lambda f: FORMAT_ORDER.index(f["format"]) if f["format"] in FORMAT_ORDER else 99)
    meta: dict = {}
    for f in files:
        metadata.merge(meta, json.loads(f["meta"] or "{}"))
    title = (it.get("title") or files[0]["title"] or files[0]["name"]).strip()
    authors = [a.strip() for a in (it.get("authors") or json.loads(files[0]["authors"] or "[]")) if a.strip()]
    tags = folder_tags(files[0]["path"]) + metadata.genre_tags(meta.get("subjects") or [])
    fmts = list(dict.fromkeys(f["format"] for f in files))

    with db.write() as con:
        # Már létező könyv (pl. papíralapú példány) azonos címmel és szerzővel?
        book_id = None
        for r in con.execute("SELECT id, title FROM books"):
            if norm(r["title"]) == norm(title):
                a = [x["name"] for x in con.execute(
                    "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id WHERE ba.book_id=?", (r["id"],))]
                if not authors or not a or {norm(x) for x in a} & {norm(x) for x in authors}:
                    book_id = r["id"]
                    break
        if book_id is None:
            cur = con.execute("""INSERT INTO books(title, isbn, publisher, publish_year, description, formats,
                                 wishlist, owner, location, created_by) VALUES (?,?,?,?,?,?,0,?,?,?)""",
                              (title, meta.get("isbn"), meta.get("publisher"), meta.get("publish_year"),
                               meta.get("description"), ",".join(fmts), owner, "Szerveren", user))
            book_id = cur.lastrowid
            set_relations(con, book_id, authors, tags)
            created = True
        else:
            cur_f = con.execute("SELECT formats FROM books WHERE id=?", (book_id,)).fetchone()[0] or ""
            fl = [x for x in cur_f.split(",") if x]
            fl += [x for x in fmts if x not in fl]
            con.execute("UPDATE books SET formats=?, updated_at=datetime('now') WHERE id=?", (",".join(fl), book_id))
            created = False
        for f in files:
            c2 = con.execute("INSERT INTO book_files(book_id, format, path, orig_name, size, source) "
                             "VALUES (?,?,?,?,?, 'library')", (book_id, f["format"], f["path"], f["name"], f["size"]))
            con.execute("UPDATE library_files SET state='imported', book_file_id=? WHERE id=?", (c2.lastrowid, f["id"]))
        has_cover = con.execute("SELECT cover_path FROM books WHERE id=?", (book_id,)).fetchone()[0]
        db.reindex_book(con, book_id)

    if not has_cover:
        for f in files:
            p = abs_path(f["path"])
            data = cover_for(p, f["format"]) if p and p.exists() else None
            if data:
                try:
                    with db.write() as con:
                        save_image(con, book_id, "front", data)
                    break
                except Exception as e:
                    log.info("Borító mentési hiba: %s", e)


def release_files(con, book_file_ids: list[int]):
    """Ha egy könyvet/fájlt törölnek, a könyvtár-fájl az "Elrejtett" fülre kerül
    (nem jelenik meg újra az új fájlok között, de onnan visszaállítható és újra importálható)."""
    if not book_file_ids:
        return
    ph = ",".join("?" * len(book_file_ids))
    con.execute(f"UPDATE library_files SET state='ignored', book_file_id=NULL WHERE book_file_id IN ({ph})",
                book_file_ids)
