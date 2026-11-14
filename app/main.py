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

from . import db, metadata

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
FORMATS = ["Papír", "EPUB", "AZW3", "AZW", "MOBI", "PDF", "Hangoskönyv"]
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


def book_details(con, ids: list[int], full: bool = False) -> list[dict]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    cols = "*" if full else ("id,title,subtitle,isbn,publish_year,cover_path,formats,purchase_year,"
                             "wishlist,owner,wanted_by,location,price,currency,shop,link,updated_at")
    rows = {r["id"]: dict(r) for r in con.execute(f"SELECT {cols} FROM books WHERE id IN ({ph})", ids)}
    for r in rows.values():
        r["authors"], r["tags"], r["files"] = [], [], []
    for r in con.execute(
            f"SELECT ba.book_id, a.name FROM book_authors ba JOIN authors a ON a.id=ba.author_id "
            f"WHERE ba.book_id IN ({ph}) ORDER BY ba.position", ids):
        rows[r["book_id"]]["authors"].append(r["name"])
    for r in con.execute(
            f"SELECT bt.book_id, t.name FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
            f"WHERE bt.book_id IN ({ph}) ORDER BY t.name", ids):
        rows[r["book_id"]]["tags"].append(r["name"])
    for r in con.execute(
            f"SELECT id, book_id, format, orig_name, size FROM book_files WHERE book_id IN ({ph}) ORDER BY id", ids):
        rows[r["book_id"]]["files"].append({k: r[k] for k in ("id", "format", "orig_name", "size")})
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
        c = con.execute("SELECT COUNT(*), COALESCE(SUM(wishlist=0),0), COALESCE(SUM(wishlist=1),0) "
                        "FROM books").fetchone()
        counts = {"all": c[0], "owned": c[1], "wishlist": c[2]}
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
            where.append("b.wishlist=0")
        if owner:
            where.append("(b.owner=? OR b.wanted_by=?)")
            args += [owner, owner]
        if fmt:
            where.append("(',' || b.formats || ',') LIKE ?")
            args.append(f"%,{fmt},%")
        if location:
            where.append("b.location=?")
            args.append(location)
        sql = "SELECT b.id, b.title, b.created_at, b.publish_year, b.purchase_year FROM books b"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = con.execute(sql, args).fetchall()

        if sort == "new":
            rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
        elif sort == "year":
            rows = sorted(rows, key=lambda r: r["publish_year"] or 0, reverse=True)
        elif sort == "title" or not rank:
            rows = sorted(rows, key=lambda r: sort_key(r["title"]))
        else:
            rows = sorted(rows, key=lambda r: rank.get(r["id"], 1e9))
        total = len(rows)
        page_ids = [r["id"] for r in rows[offset:offset + max(1, min(limit, 500))]]
        items = book_details(con, page_ids)
    return {"total": total, "items": items}


@app.get("/api/books/{book_id}")
def get_book(book_id: int, user: str = Depends(current_user)):
    with db.connect() as con:
        res = book_details(con, [book_id], full=True)
    if not res:
        raise HTTPException(404, "Nincs ilyen könyv")
    return res[0]


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
        files = con.execute("SELECT path FROM book_files WHERE book_id=?", (book_id,)).fetchall()
        con.execute("DELETE FROM books WHERE id=?", (book_id,))
        con.execute("DELETE FROM authors WHERE id NOT IN (SELECT author_id FROM book_authors)")
        con.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM book_tags)")
        db.reindex_book(con, book_id)
    for rel in [b["cover_path"], b["back_path"]] + [f["path"] for f in files]:
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


@app.get("/api/files/{file_id}")
def download_file(file_id: int, user: str = Depends(current_user)):
    with db.connect() as con:
        r = con.execute("SELECT path, orig_name FROM book_files WHERE id=?", (file_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    p = (db.DATA_DIR / r["path"]).resolve()
    if db.DATA_DIR not in p.parents or not p.exists():
        raise HTTPException(404, "A fájl nem található a lemezen")
    return FileResponse(p, filename=r["orig_name"] or p.name)


@app.delete("/api/files/{file_id}")
def delete_file(file_id: int, user: str = Depends(current_user)):
    with db.write() as con:
        r = con.execute("SELECT book_id, path FROM book_files WHERE id=?", (file_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        con.execute("DELETE FROM book_files WHERE id=?", (file_id,))
    _delete_rel(r["path"])
    return get_book(r["book_id"], user)


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
        books = book_details(con, ids, full=True)
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


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
