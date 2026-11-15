"""Hiányzó leírások (és borítók, műfaji címkék) automatikus kitöltése.

- Csak az ÜRES mezőket tölti ki, meglévő adatot sosem ír felül.
- Könyvenként egyszer próbálkozik (books.enriched_at), így a későbbi kézi
  módosításokat nem írja felül, és a sikertelen könyveket sem kérdezi újra.
- Forrás: ISBN esetén ISBN-keresés; egyébként moly.hu keresés cím + szerző alapján,
  szigorú egyezés-ellenőrzéssel (rossz leírás rosszabb, mint semmi).
"""
from __future__ import annotations

import logging
import threading
import time

from . import db, metadata
from .library import norm

log = logging.getLogger("enrich")

state = {"running": False, "done": 0, "total": 0, "filled": 0, "covers": 0, "error": None, "finished_at": None}
_lock = threading.Lock()
DELAY = 1.0  # mp a kérések között (kíméljük a moly.hu-t)


def _title_ok(ours: str, theirs: str) -> bool:
    a, b = norm(ours), norm(theirs)
    if not a or not b:
        return False
    if a == b:
        return True
    # sorozatjelölés, alcím eltérés: az egyik a másik eleje (legalább 2 szó / 8 karakter)
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 8 and len(short.split()) >= 2 and long_.startswith(short)


def _author_ok(ours: list[str], theirs: list[str]) -> bool:
    if not ours:
        return True
    ow = {w for a in ours for w in norm(a).split() if len(w) > 2}
    tw = {w for a in theirs for w in norm(a).split() if len(w) > 2}
    return bool(ow & tw)


def find(title: str, authors: list[str], isbn: str | None) -> dict | None:
    if isbn:
        r = metadata.lookup_isbn(isbn)
        if r.get("description") and (not r.get("title") or _title_ok(title, r["title"])):
            return r
    query = f"{authors[0]} {title}" if authors else title
    urls = metadata.moly_search(query, 6)
    time.sleep(DELAY)
    if not urls and authors:
        urls = metadata.moly_search(title, 6)
        time.sleep(DELAY)
    # a címet tartalmazó URL-ek előre (pl. /konyvek/gardonyi-geza-egri-csillagok)
    slug = norm(title).replace(" ", "-")
    urls.sort(key=lambda u: 0 if slug and slug in u else 1)
    for u in urls[:3]:
        try:
            d = metadata.moly_book(u)
        except Exception as e:
            log.info("moly hiba %s: %s", u, e)
            continue
        finally:
            time.sleep(DELAY)
        if _title_ok(title, d.get("title") or "") and _author_ok(authors, d.get("authors") or []) \
                and (authors or norm(title) == norm(d.get("title") or "")):
            return d
    # "Gergő 01. - Gergő és az álomfogók": sorozatjelölés nélkül még egyszer
    if " - " in title:
        rest = title.rsplit(" - ", 1)[1].strip()
        if len(norm(rest)) >= 4:
            return find(rest, authors, None)
    return None


def run(save_image, set_relations):
    if not _lock.acquire(blocking=False):
        raise RuntimeError("Már fut a kiegészítés")
    state.update(running=True, done=0, total=0, filled=0, covers=0, error=None)

    def work():
        try:
            with db.connect() as con:
                rows = [dict(r) for r in con.execute(
                    "SELECT id, title, isbn, cover_path FROM books "
                    "WHERE (description IS NULL OR trim(description)='') AND enriched_at IS NULL AND wishlist=0 "
                    "ORDER BY id")]
                for r in rows:
                    r["authors"] = [x[0] for x in con.execute(
                        "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id "
                        "WHERE ba.book_id=? ORDER BY ba.position", (r["id"],))]
            state["total"] = len(rows)
            for r in rows:
                try:
                    _one(r, save_image, set_relations)
                except Exception as e:
                    log.warning("Kiegészítés hiba (%s): %s", r["title"], e)
                state["done"] += 1
        except Exception as e:
            log.exception("Kiegészítés hiba")
            state["error"] = str(e)
        finally:
            state["running"] = False
            state["finished_at"] = time.strftime("%Y-%m-%d %H:%M")
            _lock.release()

    threading.Thread(target=work, daemon=True).start()


def _one(r: dict, save_image, set_relations):
    d = find(r["title"], r["authors"], r["isbn"])
    cover = None
    if d and not r["cover_path"] and d.get("cover_url"):
        cover = metadata.download_image(d["cover_url"])
    with db.write() as con:
        cur = con.execute("SELECT * FROM books WHERE id=?", (r["id"],)).fetchone()
        if not cur:
            return
        con.execute("UPDATE books SET enriched_at=datetime('now') WHERE id=?", (r["id"],))
        if not d:
            return
        sets, args = [], []
        for col, key in (("description", "description"), ("publisher", "publisher"),
                         ("publish_year", "publish_year"), ("isbn", "isbn"), ("subtitle", "subtitle")):
            if not cur[col] and d.get(key):
                sets.append(f"{col}=?")
                args.append(d[key])
        if sets:
            con.execute(f"UPDATE books SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?",
                        args + [r["id"]])
            if any(s.startswith("description") for s in sets):
                state["filled"] += 1
        # szerző pótlása, ha a könyvnek egyáltalán nem volt (ilyenkor csak pontos címegyezést fogadtunk el),
        # és műfaji címkék hozzáadása (a meglévőket megtartja)
        authors = r["authors"] or [a for a in (d.get("authors") or []) if a][:4]
        have = [x[0] for x in con.execute(
            "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id WHERE bt.book_id=?", (r["id"],))]
        genres = {g for _, g in metadata.GENRE_MAP}
        add = [t for t in (d.get("tags") or []) if t in genres and t not in have]
        if add or authors != r["authors"]:
            set_relations(con, r["id"], authors, have + add)
        if cover and not cur["cover_path"]:
            try:
                save_image(con, r["id"], "front", cover)
                state["covers"] += 1
            except Exception as e:
                log.info("Borító hiba: %s", e)
        db.reindex_book(con, r["id"])
