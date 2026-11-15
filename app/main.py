"""Házikönyvtár - FastAPI alkalmazás."""
from __future__ import annotations

import asyncio
import hmac
import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import db, enrich, library, metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

BASE = Path(__file__).parent
STATIC = BASE / "static"

# ------------------------------------------------------------------ beállítások

def _parse_users(raw: str) -> dict[str, str]:
    """APP_USERS="Tibi:jelszo1,Évi:jelszo2" """
    users = {}
    for part in raw.split(","):
        if ":" in part:
            name, pw = part.split(":", 1)
            if name.strip() and pw:
                users[name.strip()] = pw
    return users


USERS = _parse_users(os.environ.get("APP_USERS", ""))
OWNERS = [x.strip() for x in os.environ.get("OWNERS", "Tibi,Évi,Közös,Család").split(",") if x.strip()]
FORMATS = ["Papír", "EPUB", "AZW3", "AZW", "MOBI", "PRC", "PDF", "FB2", "DOC", "DOCX", "Hangoskönyv"]
# böngészőben olvasható formátumok (EPUB: beépített olvasó, PDF: a böngésző saját nézője)
READABLE = {"EPUB", "PDF"}
LIBRARY_SCAN_HOURS = float(os.environ.get("LIBRARY_SCAN_HOURS", "6"))
# "Szerveren" csoport: azok a könyvek, amelyeknek van fájlja a közös Könyvek mappában
ON_SERVER = "SELECT book_id FROM book_files WHERE source='library'"
EBOOK_EXT = {".pdf": "PDF", ".epub": "EPUB", ".azw": "AZW", ".azw3": "AZW3", ".mobi": "MOBI",
             ".kfx": "KFX", ".fb2": "FB2", ".djvu": "DJVU", ".cbz": "CBZ", ".cbr": "CBR",
             ".txt": "TXT", ".rtf": "RTF", ".doc": "DOC", ".docx": "DOCX",
             ".mp3": "Hangoskönyv", ".m4b": "Hangoskönyv"}

SECRET_KEY_FILE = db.DATA_DIR / ".secret_key"


def _secret_key() -> str:
    k = os.environ.get("SECRET_KEY")
    if k:
        return k
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_text().strip()
    k = secrets.token_urlsafe(48)
    SECRET_KEY_FILE.write_text(k)
    return k


app = FastAPI(title="Házikönyvtár", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=_secret_key(), session_cookie="hk_session",
                   max_age=60 * 60 * 24 * 180, same_site="lax",
                   https_only=os.environ.get("COOKIE_SECURE", "0") == "1")


@app.on_event("startup")
def _startup():
    db.init_db()
    (db.COVERS_DIR / "tmp").mkdir(parents=True, exist_ok=True)
    # régi ideiglenes képek takarítása
    for f in (db.COVERS_DIR / "tmp").glob("*"):
        if time.time() - f.stat().st_mtime > 86400:
            f.unlink(missing_ok=True)
    if not USERS:
        log.warning("Nincs APP_USERS beállítva - a bejelentkezés KI VAN KAPCSOLVA!")
    threading.Thread(target=_backup_loop, daemon=True).start()
    if library.enabled():
        threading.Thread(target=_scan_loop, daemon=True).start()
        log.info("Közös e-könyv mappa: %s", db.LIBRARY_DIR)


def _scan_loop():
    """A közös mappa rendszeres átvizsgálása (új fájlok megjelennek az import oldalon)."""
    time.sleep(90)
    while True:
        library.scan()
        time.sleep(max(0.5, LIBRARY_SCAN_HOURS) * 3600)


def _backup_loop():
    """Napi automatikus mentés a DATA_DIR/backups mappába."""
    time.sleep(60)
    while True:
        try:
            newest = max(db.BACKUP_DIR.glob("konyvtar_*.db"), default=None, key=lambda p: p.stat().st_mtime)
            if newest is None or time.time() - newest.stat().st_mtime > 23 * 3600:
                p = db.backup_db()
                if p:
                    log.info("Adatbázis mentve: %s", p)
        except Exception as e:
            log.error("Mentési hiba: %s", e)
        time.sleep(3600)


# ------------------------------------------------------------------ auth

def current_user(request: Request) -> str:
    if not USERS:
        return "Vendég"
    user = request.session.get("user")
    if not user or user not in USERS:
        raise HTTPException(401, "Bejelentkezés szükséges")
    return user


class LoginIn(BaseModel):
    name: str
    password: str


_failed: dict[str, list[float]] = {}


@app.post("/api/login")
async def login(data: LoginIn, request: Request):
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now = time.time()
    attempts = [t for t in _failed.get(ip, []) if now - t < 600]
    if len(attempts) >= 10:
        raise HTTPException(429, "Túl sok sikertelen próbálkozás, próbáld újra 10 perc múlva.")
    pw = USERS.get(data.name)
    if pw is None or not hmac.compare_digest(pw.encode(), data.password.encode()):
        attempts.append(now)
        _failed[ip] = attempts
        await asyncio.sleep(1)
        raise HTTPException(401, "Hibás név vagy jelszó")
    _failed.pop(ip, None)
    request.session["user"] = data.name
    return {"user": data.name}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    if not USERS:
        return {"user": "Vendég", "auth": False}
    user = request.session.get("user")
    return {"user": user if user in USERS else None, "auth": True, "users": list(USERS)}


# ------------------------------------------------------------------ segédek

HU_MAP = str.maketrans("áéíóöőúüűÁÉÍÓÖŐÚÜŰ", "aeiooouuuAEIOOOUUU")


def sort_key(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"^(a|az|the|der|die|das)\s+", "", s)
    return unicodedata.normalize("NFKD", s.translate(HU_MAP)).encode("ascii", "ignore").decode()


def fts_query(q: str) -> str | None:
    words = re.findall(r"[\w\-]+", q, re.UNICODE)
    terms = []
    for w in words:
        w = w.replace('"', "").strip("-")
        if w:
            terms.append(f'"{w}"*')
    return " AND ".join(terms) if terms else None


def _list(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def book_details(con, ids: list[int], full: bool = False, user: str | None = None) -> list[dict]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    cols = "*" if full else ("id,title,subtitle,isbn,publish_year,cover_path,formats,purchase_year,"
                             "wishlist,owner,wanted_by,location,price,currency,shop,link,updated_at")
    rows = {r["id"]: dict(r) for r in con.execute(f"SELECT {cols} FROM books WHERE id IN ({ph})", ids)}
    for r in rows.values():
        r["authors"], r["tags"], r["files"], r["ratings"] = [], [], [], []
        r["my_rating"] = r["my_status"] = r["avg_rating"] = None
        r["rating_count"] = 0
    for r in con.execute(f"SELECT book_id, user, rating, status FROM user_books WHERE book_id IN ({ph}) "
                         f"ORDER BY user", ids):
        b = rows[r["book_id"]]
        b["ratings"].append({"user": r["user"], "rating": r["rating"], "status": r["status"]})
        if r["user"] == user:
            b["my_rating"], b["my_status"] = r["rating"], r["status"]
    for b in rows.values():
        vals = [x["rating"] for x in b["ratings"] if x["rating"]]
        b["rating_count"] = len(vals)
        b["avg_rating"] = round(sum(vals) / len(vals), 2) if vals else None
        if not full:
            del b["ratings"]
    for r in con.execute(
            f"SELECT ba.book_id, a.name FROM book_authors ba JOIN authors a ON a.id=ba.author_id "
            f"WHERE ba.book_id IN ({ph}) ORDER BY ba.position", ids):
        rows[r["book_id"]]["authors"].append(r["name"])
    for r in con.execute(
            f"SELECT bt.book_id, t.name FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
            f"WHERE bt.book_id IN ({ph}) ORDER BY t.name", ids):
        rows[r["book_id"]]["tags"].append(r["name"])
    for r in con.execute(
            f"SELECT id, book_id, format, orig_name, size, source FROM book_files WHERE book_id IN ({ph}) ORDER BY id", ids):
        f = {k: r[k] for k in ("id", "format", "orig_name", "size", "source")}
        f["readable"] = r["format"] in READABLE
        rows[r["book_id"]]["files"].append(f)
    for r in rows.values():
        r["formats"] = [f for f in (r.get("formats") or "").split(",") if f]
        r["wishlist"] = bool(r["wishlist"])
        r["cover"] = f"/media/{r['cover_path']}" if r.get("cover_path") else None
        r["cover_thumb"] = f"/media/{_thumb_rel(r['cover_path'])}" if r.get("cover_path") else None
        if full:
            r["back"] = f"/media/{r['back_path']}" if r.get("back_path") else None
    return [rows[i] for i in ids if i in rows]


# ------------------------------------------------------------------ meta

@app.get("/api/meta")
def meta(user: str = Depends(current_user)):
    with db.connect() as con:
        tags = _list(con, "SELECT t.name, COUNT(bt.book_id) AS n FROM tags t "
                          "LEFT JOIN book_tags bt ON bt.tag_id=t.id GROUP BY t.id HAVING n>0")
        authors = [r["name"] for r in con.execute(
            "SELECT DISTINCT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id")]
        locations = [r[0] for r in con.execute(
            "SELECT DISTINCT location FROM books WHERE location IS NOT NULL AND location<>''")]
        shops = [r[0] for r in con.execute(
            "SELECT DISTINCT shop FROM books WHERE shop IS NOT NULL AND shop<>''")]
        owners_db = [r[0] for r in con.execute(
            "SELECT DISTINCT owner FROM books WHERE owner IS NOT NULL AND owner<>''")]
        publishers = [r[0] for r in con.execute(
            "SELECT DISTINCT publisher FROM books WHERE publisher IS NOT NULL AND publisher<>''")]
        c = con.execute(f"""SELECT COUNT(*),
              COALESCE(SUM(wishlist=0 AND (id NOT IN ({ON_SERVER}) OR (',' || formats || ',') LIKE '%,Papír,%')),0),
              COALESCE(SUM(wishlist=1),0), COALESCE(SUM(id IN ({ON_SERVER})),0) FROM books""").fetchone()
        counts = {"all": c[0], "owned": c[1], "wishlist": c[2], "server": c[3]}
    owners = list(dict.fromkeys(OWNERS + sorted(owners_db, key=sort_key)))
    return {
        "user": user,
        "tags": sorted(tags, key=lambda t: sort_key(t["name"])),
        "authors": sorted(authors, key=sort_key),
        "locations": sorted(locations, key=sort_key),
        "shops": sorted(shops, key=sort_key),
        "publishers": sorted(publishers, key=sort_key),
        "owners": owners,
        "formats": FORMATS,
        "counts": counts,
        "ai": bool(metadata.ANTHROPIC_API_KEY),
    }


# ------------------------------------------------------------------ könyvek

@app.get("/api/books")
def list_books(q: str = "", tags: str = "", tag_mode: str = "any", status: str = "all",
               owner: str = "", fmt: str = "", location: str = "", sort: str = "",
               read: str = "", min_rating: float = 0, rmode: str = "mine", author: str = "",
               limit: int = 60, offset: int = 0, user: str = Depends(current_user)):
    where, args = [], []
    rank = {}
    with db.connect() as con:
        ids_from_fts = None
        if q.strip():
            fq = fts_query(q)
            ids_from_fts = []
            if fq:
                try:
                    for i, r in enumerate(con.execute(
                            "SELECT rowid FROM books_fts WHERE books_fts MATCH ? "
                            "ORDER BY bm25(books_fts, 10.0, 6.0, 4.0, 1.0, 1.0, 1.0)", (fq,))):
                        ids_from_fts.append(r[0])
                        rank[r[0]] = i
                except Exception as e:
                    log.warning("FTS hiba: %s", e)
            # ISBN számjegyekkel is
            digits = re.sub(r"[^0-9Xx]", "", q)
            if len(digits) >= 6:
                for r in con.execute("SELECT id FROM books WHERE REPLACE(REPLACE(isbn,'-',''),' ','') LIKE ?",
                                     (f"%{digits}%",)):
                    if r[0] not in rank:
                        rank[r[0]] = -1
                        ids_from_fts.insert(0, r[0])
            if not ids_from_fts:
                return {"total": 0, "items": []}
            where.append(f"b.id IN ({','.join('?' * len(ids_from_fts))})")
            args += ids_from_fts
        tag_list = [t for t in tags.split("|") if t.strip()]
        if tag_list:
            ph = ",".join("?" * len(tag_list))
            if tag_mode == "all":
                where.append(f"b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
                             f"WHERE t.name IN ({ph}) GROUP BY bt.book_id HAVING COUNT(DISTINCT t.id)=?)")
                args += tag_list + [len(tag_list)]
            else:
                where.append(f"b.id IN (SELECT bt.book_id FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
                             f"WHERE t.name IN ({ph}))")
                args += tag_list
        if status == "wishlist":
            where.append("b.wishlist=1")
        elif status == "owned":
            # Polcon: a fizikai könyvek (a szerveren lévő e-könyvek külön csoport, kivéve ha papíron is megvan)
            where.append(f"b.wishlist=0 AND (b.id NOT IN ({ON_SERVER}) OR (',' || b.formats || ',') LIKE '%,Papír,%')")
        elif status == "server":
            where.append(f"b.id IN ({ON_SERVER})")
        if author.strip():
            where.append("b.id IN (SELECT ba.book_id FROM book_authors ba JOIN authors a ON a.id=ba.author_id "
                         "WHERE a.name = ? COLLATE NOCASE)")
            args.append(author.strip())
        if owner:
            where.append("(b.owner=? OR b.wanted_by=?)")
            args += [owner, owner]
        if fmt:
            where.append("(',' || b.formats || ',') LIKE ?")
            args.append(f"%,{fmt},%")
        if location:
            where.append("b.location=?")
            args.append(location)
        # saját olvasási állapot
        if read in ("igen", "folyamatban"):
            where.append("b.id IN (SELECT book_id FROM user_books WHERE user=? AND status=?)")
            args += [user, read]
        elif read == "nem":
            where.append("b.id NOT IN (SELECT book_id FROM user_books WHERE user=? "
                         "AND status IN ('igen','folyamatban'))")
            args.append(user)
        if min_rating:
            if rmode == "avg":
                where.append("b.id IN (SELECT book_id FROM user_books GROUP BY book_id HAVING AVG(rating)>=?)")
                args.append(min_rating)
            else:
                where.append("b.id IN (SELECT book_id FROM user_books WHERE user=? AND rating>=?)")
                args += [user, min_rating]
        sql = "SELECT b.id, b.title, b.created_at, b.publish_year, b.purchase_year FROM books b"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = con.execute(sql, args).fetchall()

        if sort == "rating":
            if rmode == "avg":
                rmap = {r[0]: r[1] for r in con.execute(
                    "SELECT book_id, AVG(rating) FROM user_books WHERE rating IS NOT NULL GROUP BY book_id")}
            else:
                rmap = {r[0]: r[1] for r in con.execute(
                    "SELECT book_id, rating FROM user_books WHERE user=? AND rating IS NOT NULL", (user,))}
            rows = sorted(rows, key=lambda r: (-(rmap.get(r["id"]) or 0), sort_key(r["title"])))
        elif sort == "new":
            rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
        elif sort == "year":
            rows = sorted(rows, key=lambda r: r["publish_year"] or 0, reverse=True)
        elif sort == "title" or not rank:
            rows = sorted(rows, key=lambda r: sort_key(r["title"]))
        else:
            rows = sorted(rows, key=lambda r: rank.get(r["id"], 1e9))
        total = len(rows)
        page_ids = [r["id"] for r in rows[offset:offset + max(1, min(limit, 500))]]
        items = book_details(con, page_ids, user=user)
    return {"total": total, "items": items}


@app.get("/api/books/{book_id}")
def get_book(book_id: int, user: str = Depends(current_user)):
    with db.connect() as con:
        res = book_details(con, [book_id], full=True, user=user)
    if not res:
        raise HTTPException(404, "Nincs ilyen könyv")
    return res[0]


class MineIn(BaseModel):
    """Saját értékelés / olvasási állapot. Csak a megadott mezők változnak."""
    rating: Optional[float] = None
    status: Optional[str] = None
    clear_rating: bool = False


@app.put("/api/books/{book_id}/me")
def set_mine(book_id: int, data: MineIn, user: str = Depends(current_user)):
    if data.status is not None and data.status not in ("nem", "folyamatban", "igen"):
        raise HTTPException(400, "Állapot: nem | folyamatban | igen")
    rating = None
    if data.rating is not None:
        rating = round(float(data.rating) * 2) / 2
        if not 1 <= rating <= 5:
            raise HTTPException(400, "Az értékelés 1 és 5 között lehet (fél csillagokkal)")
    with db.write() as con:
        if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
            raise HTTPException(404, "Nincs ilyen könyv")
        con.execute("INSERT OR IGNORE INTO user_books(book_id, user) VALUES (?,?)", (book_id, user))
        if rating is not None:
            con.execute("UPDATE user_books SET rating=?, updated_at=datetime('now') WHERE book_id=? AND user=?",
                        (rating, book_id, user))
        if data.clear_rating:
            con.execute("UPDATE user_books SET rating=NULL, updated_at=datetime('now') WHERE book_id=? AND user=?",
                        (book_id, user))
        if data.status is not None:
            con.execute("UPDATE user_books SET status=?, updated_at=datetime('now') WHERE book_id=? AND user=?",
                        (data.status, book_id, user))
        con.execute("DELETE FROM user_books WHERE rating IS NULL AND (status IS NULL OR status='nem')")
    return get_book(book_id, user)


class BookIn(BaseModel):
    title: str = Field(min_length=1)
    subtitle: Optional[str] = None
    authors: list[str] = []
    isbn: Optional[str] = None
    edition: Optional[str] = None
    publisher: Optional[str] = None
    publish_year: Optional[int] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    tags: list[str] = []
    formats: list[str] = []
    purchase_year: Optional[int] = None
    wishlist: bool = False
    shop: Optional[str] = None
    link: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = "HUF"
    owner: Optional[str] = None
    wanted_by: Optional[str] = None
    location: Optional[str] = None
    # borító forrása (opcionális): távoli URL vagy a /api/lookup/image által adott ideiglenes fájl
    cover_url: Optional[str] = None
    cover_tmp: Optional[str] = None
    back_tmp: Optional[str] = None


BOOK_COLS = ["title", "subtitle", "isbn", "edition", "publisher", "publish_year", "description",
             "notes", "formats", "purchase_year", "wishlist", "shop", "link", "price", "currency",
             "owner", "wanted_by", "location"]


def _clean_book(b: BookIn) -> dict:
    d = b.model_dump()
    for k, v in list(d.items()):
        if isinstance(v, str):
            d[k] = v.strip() or None
    d["title"] = d["title"] or "Névtelen"
    d["formats"] = ",".join(dict.fromkeys(f.strip() for f in b.formats if f.strip()))
    # Kívánságlista csak akkor, ha nincs vásárlási év
    if d["purchase_year"]:
        d["wishlist"] = False
    d["wishlist"] = 1 if d["wishlist"] else 0
    if d.get("isbn"):
        d["isbn"] = metadata.normalize_isbn(d["isbn"]) or d["isbn"]
    return d


def _set_relations(con, book_id: int, authors: list[str], tags: list[str]):
    con.execute("DELETE FROM book_authors WHERE book_id=?", (book_id,))
    seen = set()
    for pos, name in enumerate(a.strip() for a in authors):
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        con.execute("INSERT OR IGNORE INTO authors(name) VALUES (?)", (name,))
        aid = con.execute("SELECT id FROM authors WHERE name=?", (name,)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO book_authors(book_id, author_id, position) VALUES (?,?,?)",
                    (book_id, aid, pos))
    con.execute("DELETE FROM book_tags WHERE book_id=?", (book_id,))
    for name in {t.strip(): None for t in tags if t.strip()}:
        con.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
        tid = con.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO book_tags(book_id, tag_id) VALUES (?,?)", (book_id, tid))
    # árva szerzők/címkék törlése
    con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
    con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")


def _apply_images(con, book_id: int, d: dict):
    for field, tmpkey, side in (("cover_path", "cover_tmp", "front"), ("back_path", "back_tmp", "back")):
        tmp = d.get(tmpkey)
        if tmp and re.fullmatch(r"[0-9a-f]{32}\.jpg", tmp):
            src = db.COVERS_DIR / "tmp" / tmp
            if src.exists():
                _replace_image(con, book_id, side, src.read_bytes())
                src.unlink(missing_ok=True)
    if d.get("cover_url") and not d.get("cover_tmp"):
        data = metadata.download_image(d["cover_url"])
        if data:
            try:
                _replace_image(con, book_id, "front", data)
            except Exception as e:
                log.warning("Borító mentési hiba: %s", e)


@app.post("/api/books")
def create_book(b: BookIn, user: str = Depends(current_user)):
    d = _clean_book(b)
    with db.write() as con:
        cols = BOOK_COLS + ["created_by"]
        cur = con.execute(f"INSERT INTO books({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [d[c] for c in BOOK_COLS] + [user])
        book_id = cur.lastrowid
        _set_relations(con, book_id, b.authors, b.tags)
        _apply_images(con, book_id, d)
        db.reindex_book(con, book_id)
    return get_book(book_id, user)


@app.put("/api/books/{book_id}")
def update_book(book_id: int, b: BookIn, user: str = Depends(current_user)):
    d = _clean_book(b)
    with db.write() as con:
        if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
            raise HTTPException(404, "Nincs ilyen könyv")
        con.execute(f"UPDATE books SET {', '.join(c + '=?' for c in BOOK_COLS)}, updated_at=datetime('now') "
                    f"WHERE id=?", [d[c] for c in BOOK_COLS] + [book_id])
        _set_relations(con, book_id, b.authors, b.tags)
        _apply_images(con, book_id, d)
        db.reindex_book(con, book_id)
    return get_book(book_id, user)


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int, user: str = Depends(current_user)):
    with db.write() as con:
        b = con.execute("SELECT cover_path, back_path FROM books WHERE id=?", (book_id,)).fetchone()
        if not b:
            raise HTTPException(404, "Nincs ilyen könyv")
        files = con.execute("SELECT id, path, source FROM book_files WHERE book_id=?", (book_id,)).fetchall()
        # a közös mappában lévő fájlokat SOSEM töröljük, csak újra importálhatóvá tesszük
        library.release_files(con, [f["id"] for f in files if f["source"] == "library"])
        con.execute("DELETE FROM books WHERE id=?", (book_id,))
        con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
        con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
        db.reindex_book(con, book_id)
    for rel in [b["cover_path"], b["back_path"]] + [f["path"] for f in files if f["source"] == "upload"]:
        if rel:
            _delete_rel(rel)
    return {"ok": True}


# ------------------------------------------------------------------ képek

def _thumb_rel(rel: str) -> str:
    return re.sub(r"\.jpg$", "_t.jpg", rel)


def _delete_rel(rel: str):
    p = (db.DATA_DIR / rel).resolve()
    if db.DATA_DIR in p.parents:
        p.unlink(missing_ok=True)
        if rel.endswith(".jpg"):
            (db.DATA_DIR / _thumb_rel(rel)).unlink(missing_ok=True)


def _save_jpeg(data: bytes, dest: Path, max_size: int = 1400, thumb: bool = True):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
    full = img.copy()
    full.thumbnail((max_size, max_size))
    dest.parent.mkdir(parents=True, exist_ok=True)
    full.save(dest, "JPEG", quality=86, optimize=True)
    if thumb:
        t = img.copy()
        t.thumbnail((360, 540))
        t.save(dest.with_name(dest.stem + "_t.jpg"), "JPEG", quality=80, optimize=True)


def _replace_image(con, book_id: int, side: str, data: bytes):
    field = "cover_path" if side == "front" else "back_path"
    old = con.execute(f"SELECT {field} FROM books WHERE id=?", (book_id,)).fetchone()[0]
    rel = f"covers/{book_id}_{side}_{uuid.uuid4().hex[:8]}.jpg"
    _save_jpeg(data, db.DATA_DIR / rel)
    con.execute(f"UPDATE books SET {field}=?, updated_at=datetime('now') WHERE id=?", (rel, book_id))
    if old:
        _delete_rel(old)


@app.post("/api/books/{book_id}/image")
async def upload_image(book_id: int, side: str = Form("front"), file: Optional[UploadFile] = File(None),
                       url: Optional[str] = Form(None), user: str = Depends(current_user)):
    if side not in ("front", "back"):
        raise HTTPException(400, "side: front|back")
    if file is not None:
        data = await file.read()
    elif url:
        data = await asyncio.to_thread(metadata.download_image, url)
    else:
        raise HTTPException(400, "Nincs kép")
    if not data:
        raise HTTPException(400, "A képet nem sikerült betölteni")
    try:
        with db.write() as con:
            if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                raise HTTPException(404, "Nincs ilyen könyv")
            _replace_image(con, book_id, side, data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Hibás kép: {e}")
    return get_book(book_id, user)


@app.delete("/api/books/{book_id}/image")
def delete_image(book_id: int, side: str = "front", user: str = Depends(current_user)):
    field = "cover_path" if side == "front" else "back_path"
    with db.write() as con:
        r = con.execute(f"SELECT {field} FROM books WHERE id=?", (book_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        con.execute(f"UPDATE books SET {field}=NULL WHERE id=?", (book_id,))
    if r[0]:
        _delete_rel(r[0])
    return get_book(book_id, user)


@app.get("/media/{rel:path}")
def media(rel: str, user: str = Depends(current_user)):
    if not re.fullmatch(r"covers/(tmp/)?[\w\-]+\.jpg", rel):
        raise HTTPException(404)
    p = db.DATA_DIR / rel
    if not p.exists():
        if rel.endswith("_t.jpg"):  # nincs bélyegkép -> eredeti
            p = db.DATA_DIR / rel.replace("_t.jpg", ".jpg")
        if not p.exists():
            raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=2592000"})


# ------------------------------------------------------------------ e-könyv fájlok

def _safe_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w.\- ]+", "", name).strip().replace(" ", "_")
    return name[:120] or "konyv"


@app.post("/api/books/{book_id}/files")
async def upload_file(book_id: int, file: UploadFile = File(...), user: str = Depends(current_user)):
    ext = Path(file.filename or "").suffix.lower()
    fmt = EBOOK_EXT.get(ext)
    if not fmt:
        raise HTTPException(400, f"Nem támogatott fájltípus: {ext or '?'} "
                                 f"(engedélyezett: {', '.join(sorted(EBOOK_EXT))})")
    with db.connect() as con:
        if not con.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
            raise HTTPException(404, "Nincs ilyen könyv")
    rel = f"files/{book_id}/{uuid.uuid4().hex[:6]}_{_safe_name(Path(file.filename).stem)}{ext}"
    dest = db.DATA_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _copy():
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out, 1024 * 1024)
        return dest.stat().st_size

    size = await asyncio.to_thread(_copy)
    with db.write() as con:
        con.execute("INSERT INTO book_files(book_id, format, path, orig_name, size) VALUES (?,?,?,?,?)",
                    (book_id, fmt, rel, file.filename, size))
        # formátum automatikus hozzáadása
        cur = con.execute("SELECT formats FROM books WHERE id=?", (book_id,)).fetchone()[0] or ""
        fl = [f for f in cur.split(",") if f]
        if fmt not in fl:
            fl.append(fmt)
            con.execute("UPDATE books SET formats=?, updated_at=datetime('now') WHERE id=?",
                        (",".join(fl), book_id))
        db.reindex_book(con, book_id)
    return get_book(book_id, user)


def _file_path(r) -> Path | None:
    if r["source"] == "library":
        return library.abs_path(r["path"])
    p = (db.DATA_DIR / r["path"]).resolve()
    return p if db.DATA_DIR in p.parents else None


MIME = {"EPUB": "application/epub+zip", "PDF": "application/pdf", "MOBI": "application/x-mobipocket-ebook",
        "AZW3": "application/vnd.amazon.ebook", "AZW": "application/vnd.amazon.ebook"}


@app.get("/api/files/{file_id}")
def download_file(file_id: int, inline: int = 0, user: str = Depends(current_user)):
    """Letöltés; inline=1 esetén böngészőben megnyitható (PDF olvasás, EPUB olvasó)."""
    with db.connect() as con:
        r = con.execute("SELECT path, orig_name, source, format FROM book_files WHERE id=?", (file_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    p = _file_path(r)
    if not p or not p.exists():
        raise HTTPException(404, "A fájl nem található a lemezen")
    return FileResponse(p, filename=r["orig_name"] or p.name, media_type=MIME.get(r["format"]),
                        content_disposition_type="inline" if inline else "attachment")


@app.get("/api/files/{file_id}/info")
def file_info(file_id: int, user: str = Depends(current_user)):
    with db.connect() as con:
        r = con.execute("SELECT f.id, f.book_id, f.format, f.orig_name, b.title FROM book_files f "
                        "JOIN books b ON b.id=f.book_id WHERE f.id=?", (file_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        pos = con.execute("SELECT position, percent FROM reading_positions WHERE file_id=? AND user=?",
                          (file_id, user)).fetchone()
    return {**dict(r), "position": pos["position"] if pos else None, "percent": pos["percent"] if pos else None}


class PositionIn(BaseModel):
    position: str
    percent: Optional[float] = None


@app.put("/api/files/{file_id}/position")
def save_position(file_id: int, data: PositionIn, user: str = Depends(current_user)):
    with db.write() as con:
        r = con.execute("SELECT book_id FROM book_files WHERE id=?", (file_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        con.execute("""INSERT INTO reading_positions(file_id, user, position, percent, updated_at)
                       VALUES (?,?,?,?, datetime('now'))
                       ON CONFLICT(file_id, user) DO UPDATE SET position=excluded.position,
                       percent=excluded.percent, updated_at=excluded.updated_at""",
                    (file_id, user, data.position[:2000], data.percent))
        # olvasás megkezdése -> "folyamatban" (ha még nem volt állapota)
        con.execute("INSERT OR IGNORE INTO user_books(book_id, user, status) VALUES (?,?, 'folyamatban')",
                    (r["book_id"], user))
        con.execute("UPDATE user_books SET status='folyamatban' WHERE book_id=? AND user=? "
                    "AND (status IS NULL OR status='nem')", (r["book_id"], user))
    return {"ok": True}


@app.delete("/api/files/{file_id}")
def delete_file(file_id: int, user: str = Depends(current_user)):
    with db.write() as con:
        r = con.execute("SELECT book_id, path, source FROM book_files WHERE id=?", (file_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        if r["source"] == "library":
            library.release_files(con, [file_id])
        con.execute("DELETE FROM book_files WHERE id=?", (file_id,))
    if r["source"] == "upload":
        _delete_rel(r["path"])
    return get_book(r["book_id"], user)


# ------------------------------------------------------------------ szerzők kezelése

@app.get("/api/authors")
def list_authors(user: str = Depends(current_user)):
    with db.connect() as con:
        rows = _list(con, "SELECT a.id, a.name, COUNT(ba.book_id) AS n FROM authors a "
                          "LEFT JOIN book_authors ba ON ba.author_id=a.id GROUP BY a.id")
    return {"items": sorted(rows, key=lambda r: sort_key(r["name"]))}


class AuthorIn(BaseModel):
    name: str = Field(min_length=1)


@app.put("/api/authors/{author_id}")
def rename_author(author_id: int, data: AuthorIn, user: str = Depends(current_user)):
    """Átnevezés. Ha már létezik ilyen nevű szerző, a kettőt összevonja."""
    name = re.sub(r"\s+", " ", data.name).strip()
    with db.write() as con:
        if not con.execute("SELECT 1 FROM authors WHERE id=?", (author_id,)).fetchone():
            raise HTTPException(404, "Nincs ilyen szerző")
        books = [r[0] for r in con.execute("SELECT book_id FROM book_authors WHERE author_id=?", (author_id,))]
        other = con.execute("SELECT id FROM authors WHERE name=? AND id<>?", (name, author_id)).fetchone()
        if other:
            for b in books:
                pos = con.execute("SELECT position FROM book_authors WHERE book_id=? AND author_id=?",
                                  (b, author_id)).fetchone()[0]
                con.execute("INSERT OR IGNORE INTO book_authors(book_id, author_id, position) VALUES (?,?,?)",
                            (b, other[0], pos))
            con.execute("DELETE FROM authors WHERE id=?", (author_id,))
        else:
            con.execute("UPDATE authors SET name=? WHERE id=?", (name, author_id))
        for b in books:
            con.execute("UPDATE books SET updated_at=datetime('now') WHERE id=?", (b,))
            db.reindex_book(con, b)
    return {"ok": True, "merged": bool(other), "books": len(books)}


@app.delete("/api/authors/{author_id}")
def delete_author(author_id: int, user: str = Depends(current_user)):
    """A szerző törlése (a könyvek megmaradnak, csak ez a név kerül le róluk)."""
    with db.write() as con:
        books = [r[0] for r in con.execute("SELECT book_id FROM book_authors WHERE author_id=?", (author_id,))]
        con.execute("DELETE FROM authors WHERE id=?", (author_id,))
        for b in books:
            con.execute("UPDATE books SET updated_at=datetime('now') WHERE id=?", (b,))
            db.reindex_book(con, b)
    return {"ok": True, "books": len(books)}


# ------------------------------------------------------------------ hiányzó adatok kiegészítése


@app.get("/api/enrich")
def enrich_status(user: str = Depends(current_user)):
    with db.connect() as con:
        left = con.execute("SELECT COUNT(*) FROM books WHERE (description IS NULL OR trim(description)='') "
                           "AND enriched_at IS NULL AND wishlist=0").fetchone()[0]
    return {**enrich.state, "pending": left}


@app.post("/api/enrich")
def enrich_start(user: str = Depends(current_user)):
    try:
        enrich.run(_replace_image, _set_relations)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# ------------------------------------------------------------------ közös e-könyv mappa

@app.get("/api/library/status")
def library_status(user: str = Depends(current_user)):
    if not library.enabled():
        return {"enabled": False}
    return {"enabled": True, **library.state, **library.counts()}


@app.post("/api/library/scan")
def library_scan(user: str = Depends(current_user)):
    if not library.enabled():
        raise HTTPException(400, "A könyvtár mappa nincs beállítva (LIBRARY_DIR)")
    library.start_scan()
    return {"ok": True}


@app.get("/api/library/groups")
def library_groups(which: str = "new", user: str = Depends(current_user)):
    if which not in ("new", "ignored"):
        raise HTTPException(400)
    return {"items": library.groups(which)}


class IgnoreIn(BaseModel):
    file_ids: list[int]
    ignored: bool = True


@app.post("/api/library/ignore")
def library_ignore(data: IgnoreIn, user: str = Depends(current_user)):
    library.set_ignored(data.file_ids, data.ignored)
    return library.counts()


class ImportItem(BaseModel):
    file_ids: list[int]
    title: Optional[str] = None
    authors: list[str] = []


class ImportIn(BaseModel):
    items: list[ImportItem]
    owner: Optional[str] = None


@app.post("/api/library/import")
def library_import(data: ImportIn, user: str = Depends(current_user)):
    if not data.items:
        raise HTTPException(400, "Nincs kijelölve semmi")
    try:
        # import után a hiányzó leírások automatikus kitöltése is elindul
        library.run_import([i.model_dump() for i in data.items], (data.owner or "").strip() or None, user,
                           _replace_image, _set_relations,
                           on_done=lambda: enrich.run(_replace_image, _set_relations))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# ------------------------------------------------------------------ automatikus adatkitöltés

class UrlIn(BaseModel):
    url: str


class IsbnIn(BaseModel):
    isbn: str


class SearchIn(BaseModel):
    q: str


@app.post("/api/lookup/url")
async def lookup_url(data: UrlIn, user: str = Depends(current_user)):
    url = data.url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    return await asyncio.to_thread(metadata.lookup_url, url)


@app.post("/api/lookup/isbn")
async def lookup_isbn(data: IsbnIn, user: str = Depends(current_user)):
    return await asyncio.to_thread(metadata.lookup_isbn, data.isbn)


@app.post("/api/lookup/search")
async def lookup_search(data: SearchIn, user: str = Depends(current_user)):
    return {"items": await asyncio.to_thread(metadata.search_title, data.q)}


@app.post("/api/lookup/image")
async def lookup_image(file: UploadFile = File(...), side: str = Form("front"), lookup: str = Form("1"),
                       user: str = Depends(current_user)):
    """Kép ideiglenes feltöltése; lookup=1 esetén a könyv azonosítását is megpróbálja."""
    raw = await file.read()
    tmp_name = f"{uuid.uuid4().hex}.jpg"
    try:
        _save_jpeg(raw, db.COVERS_DIR / "tmp" / tmp_name, thumb=False)
    except Exception as e:
        raise HTTPException(400, f"Hibás kép: {e}")
    if lookup == "1":
        res = await asyncio.to_thread(metadata.lookup_image, raw, file.content_type or "image/jpeg")
    else:
        res = {}
    res["_tmp"] = tmp_name
    res["_tmp_url"] = f"/media/covers/tmp/{tmp_name[:-4]}.jpg"
    res["_side"] = side
    return res


# ------------------------------------------------------------------ mentés / export

@app.get("/api/export")
def export(user: str = Depends(current_user)):
    with db.connect() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM books ORDER BY id")]
        books = book_details(con, ids, full=True, user=user)
    body = json.dumps({"exported_at": datetime.now().isoformat(), "books": books},
                      ensure_ascii=False, indent=1)
    name = f"hazikonyvtar_{datetime.now():%Y%m%d}.json"
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/healthz")
def health():
    return {"ok": True}


# ------------------------------------------------------------------ frontend

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/olvaso")
def reader():
    return FileResponse(STATIC / "reader.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
