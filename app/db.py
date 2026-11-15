"""SQLite adatbázis réteg + FTS5 teljes szöveges keresés."""
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "konyvtar.db"
COVERS_DIR = DATA_DIR / "covers"
FILES_DIR = DATA_DIR / "files"
# A közös e-könyv mappa (csak olvasásra csatolva). Üres = nincs bekapcsolva.
_lib = os.environ.get("LIBRARY_DIR", "").strip()
LIBRARY_DIR = Path(_lib).resolve() if _lib else None

# Hálózati megosztáson (NFS/SMB) a WAL mód nem biztonságos, ezért alapból DELETE.
JOURNAL_MODE = os.environ.get("SQLITE_JOURNAL_MODE", "DELETE").upper()

# SMB/CIFS megosztáson az SQLite fájlzárolása nem működik ("database is locked").
# Ezért alapból kikapcsoljuk (nolock=1), és helyette a folyamaton belül egy zárral
# rendezzük sorba az ÖSSZES adatbázis-műveletet. Ez csak akkor biztonságos, ha
# egyetlen folyamat (egy konténer, egy uvicorn worker) használja az adatbázist.
NOLOCK = os.environ.get("SQLITE_NOLOCK", "1") == "1"

_write_lock = threading.RLock()


def _sqlite_connect(path: Path, timeout: float = 30) -> sqlite3.Connection:
    if NOLOCK:
        uri = "file:" + path.as_posix() + "?nolock=1"
        return sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    return sqlite3.connect(path, timeout=timeout, check_same_thread=False)

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    subtitle        TEXT,
    isbn            TEXT,
    edition         TEXT,          -- verziószám / kiadás
    publisher       TEXT,          -- kiadó
    publish_year    INTEGER,
    cover_path      TEXT,          -- relatív út a DATA_DIR-hez
    back_path       TEXT,
    description     TEXT,          -- hátoldali hivatalos szöveg
    notes           TEXT,          -- saját megjegyzés
    formats         TEXT,          -- vesszővel elválasztva: Papír,EPUB,...
    purchase_year   INTEGER,
    wishlist        INTEGER NOT NULL DEFAULT 0,
    shop            TEXT,
    link            TEXT,
    price           REAL,
    currency        TEXT DEFAULT 'HUF',
    owner           TEXT,          -- kié
    wanted_by       TEXT,          -- ki szeretné (kívánságlista)
    location        TEXT,          -- hol található
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT
);

CREATE TABLE IF NOT EXISTS authors (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS book_authors (
    book_id   INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, author_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS book_tags (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (book_id, tag_id)
);

CREATE TABLE IF NOT EXISTS book_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    format     TEXT NOT NULL,
    path       TEXT NOT NULL,      -- relatív út a DATA_DIR-hez
    orig_name  TEXT,
    size       INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_books_wishlist ON books(wishlist);
CREATE INDEX IF NOT EXISTS idx_books_isbn ON books(isbn);

CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, authors, tags, description, notes, other,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Felhasználónkénti adatok: értékelés (1-5, fél csillagokkal) és olvasási állapot
CREATE TABLE IF NOT EXISTS user_books (
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    user       TEXT NOT NULL,
    rating     REAL,               -- 1.0 .. 5.0, 0.5-ös lépésekben
    status     TEXT,               -- 'nem' | 'folyamatban' | 'igen'
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (book_id, user)
);

-- Hol tart az olvasásban (fájlonként, felhasználónként)
CREATE TABLE IF NOT EXISTS reading_positions (
    file_id    INTEGER NOT NULL REFERENCES book_files(id) ON DELETE CASCADE,
    user       TEXT NOT NULL,
    position   TEXT,
    percent    REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (file_id, user)
);

-- A közös "Könyvek" mappa átvizsgált fájljai (állandó kapcsolat)
CREATE TABLE IF NOT EXISTS library_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,   -- relatív út a LIBRARY_DIR-hez
    dir          TEXT NOT NULL,
    name         TEXT NOT NULL,
    format       TEXT NOT NULL,
    size         INTEGER,
    mtime        REAL,
    title        TEXT,
    authors      TEXT,                   -- JSON lista
    meta         TEXT,                   -- JSON: leírás, kiadó, év, isbn, címkék...
    group_key    TEXT,
    state        TEXT NOT NULL DEFAULT 'new',   -- new | imported | ignored
    missing      INTEGER NOT NULL DEFAULT 0,
    book_file_id INTEGER,
    scanned_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_libfiles_state ON library_files(state);

-- egyszerű beállítások (pl. kizárt mappák listája)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_setting(key: str, default=None):
    import json
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r and r[0] is not None else default


def set_setting(key: str, value):
    import json
    with connect() as con:
        con.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))


def _migrate(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(book_files)")}
    if "source" not in cols:
        # 'upload' = feltöltött fájl a DATA_DIR-ben, 'library' = a közös Könyvek mappában
        con.execute("ALTER TABLE book_files ADD COLUMN source TEXT NOT NULL DEFAULT 'upload'")
    # a közös mappából importált könyvek helye: "Szerveren" (régebben "E-könyvtár")
    con.execute("UPDATE books SET location='Szerveren' WHERE location='E-könyvtár'")
    bcols = {r[1] for r in con.execute("PRAGMA table_info(books)")}
    if "enriched_at" not in bcols:
        # mikor próbálta az automatikus kiegészítő (csak egyszer próbálkozik könyvenként)
        con.execute("ALTER TABLE books ADD COLUMN enriched_at TEXT")


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        _migrate(con)


def _connect():
    con = _sqlite_connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}")
    con.execute("PRAGMA busy_timeout = 30000")
    return con


@contextmanager
def connect():
    """Minden adatbázis-művelet sorban, egyszerre csak egy kapcsolat (2 felhasználónál bőven elég)."""
    with _write_lock:
        con = _connect()
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


@contextmanager
def write():
    with connect() as con:
        yield con


def reindex_book(con, book_id: int):
    b = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    con.execute("DELETE FROM books_fts WHERE rowid=?", (book_id,))
    if not b:
        return
    authors = " ".join(r["name"] for r in con.execute(
        "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id=a.id WHERE ba.book_id=?",
        (book_id,)))
    tags = " ".join(r["name"] for r in con.execute(
        "SELECT t.name FROM tags t JOIN book_tags bt ON bt.tag_id=t.id WHERE bt.book_id=?",
        (book_id,)))
    other = " ".join(str(x) for x in (
        b["subtitle"], b["isbn"], b["publisher"], b["shop"], b["owner"],
        b["wanted_by"], b["location"], b["formats"], b["publish_year"]) if x)
    con.execute(
        "INSERT INTO books_fts(rowid, title, authors, tags, description, notes, other) VALUES (?,?,?,?,?,?,?)",
        (book_id, b["title"], authors, tags, b["description"] or "", b["notes"] or "", other))


BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "30"))


def backup_db() -> Path | None:
    """Konzisztens másolat az adatbázisról (SQLite backup API), a régiek törlésével."""
    import datetime
    if not DB_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"konyvtar_{datetime.datetime.now():%Y%m%d_%H%M}.db"
    with _write_lock:
        src = _connect()
        try:
            dst = _sqlite_connect(dest)
            with dst:
                src.backup(dst)
            dst.close()
        finally:
            src.close()
    olds = sorted(BACKUP_DIR.glob("konyvtar_*.db"))
    for f in olds[:-BACKUP_KEEP]:
        f.unlink(missing_ok=True)
    return dest


def rebuild_fts():
    with write() as con:
        con.execute("DELETE FROM books_fts")
        for r in con.execute("SELECT id FROM books").fetchall():
            reindex_book(con, r["id"])
