# Házikönyvtár

Saját könyvtár-nyilvántartó webalkalmazás két felhasználónak (Tibi, Évi), mobilról, tabletről és gépről is.

- **Adatbázis:** SQLite (egyetlen fájl: `konyvtar.db`), FTS5 teljes szöveges kereséssel
- **Szerver:** Python / FastAPI, egy Docker-konténerben
- **Felület:** böngészőben fut, telefonon „Hozzáadás a kezdőképernyőhöz” után appként is

## Mit tud

| Funkció | Megjegyzés |
|---|---|
| Könyv adatai | cím, alcím, több szerző, ISBN, verzió/kiadás, kiadó, megjelenés éve, címkék, borító + hátoldal kép, leírás, megjegyzés, formátum(ok), vásárlás éve, kívánságlista, bolt, link, ár, kié, ki szeretné, hol található |
| Kívánságlista | csak akkor jelölhető be, ha nincs vásárlási év; a vásárlási év beírásakor automatikusan lekerül róla |
| Digitális fájlok | PDF, EPUB, AZW/AZW3, MOBI, FB2, DJVU, CBZ, hangoskönyv (MP3/M4B)… A fájl a lemezre kerül (`data/files/<könyv-id>/`), az adatbázisban csak az útvonala van. Letölthető az adatlapról. |
| Keresés | egyetlen keresőmező: cím, szerző, címke, leírás, megjegyzés, ISBN, kiadó, hely… Ékezetfüggetlen (`geza` = `Géza`), szókezdetre is talál (`gárd` → Gárdonyi). Több szó = mindegyiknek szerepelnie kell. |
| Szűrők | Mind / Polcon / Kívánságlista, címkék (bármelyik vagy mindegyik), kié, formátum, hely, rendezés |
| Automatikus kitöltés | **link** (Libri, Bookline, Moly, Líra, Alexandra, Amazon, bármely oldal szabványos adataiból), **ISBN**, **cím/szerző** keresés (találatlista), **borítófotó** vagy **hátoldal-fotó vonalkóddal** |
| Mentés | naponta automatikus adatbázis-mentés a `data/backups` mappába (30 napig), plusz JSON export a menüből |

### Honnan jönnek az automatikus adatok?

1. A megadott weboldal strukturált adatai (schema.org JSON-LD, microdata, OpenGraph, „Kiadó: …” sorok)
2. **moly.hu** – magyar könyveknél ez a legjobb forrás (leírás, borító, kiadás, címkék)
3. Google Books és Open Library (főleg idegen nyelvű könyvek)
4. Fotóból: **vonalkód → ISBN → fenti források** (ez a legmegbízhatóbb: fotózd a hátoldalt!).
   Ha nincs vonalkód: szövegfelismerés (tesseract) a borítón, majd keresés – ez bizonytalanabb.
5. *Opcionális:* ha megadsz `ANTHROPIC_API_KEY`-t, a borítófotót Claude AI ismeri fel, és szükség
   esetén a weben is rákeres. Ez kérésenként néhány forint költség; nélküle is működik minden más.

Az automatikus kitöltés **csak az üres mezőket** tölti ki, mentés előtt mindig ellenőrizhető.

## Telepítés a szerverre (192.168.31.152)

A kód a GitHubon van: https://github.com/tipcsy/hazikonyvtar. A szerver a repót egy csak
olvasásra jogosult **deploy key**-jel éri el (`~/.ssh/hazikonyvtar_deploy`, SSH alias:
`github-hazikonyvtar`), így privát repóval is működik.

### Frissítés (a szokásos menet)

1. A változtatásokat a gépeden commitold és pushold a GitHubra (`git push`).
2. A szerveren:

```bash
ssh tipcsy@192.168.31.152 /opt/hazikonyvtar/update.sh
```

Ez letölti a legújabb kódot, újraépíti és újraindítja a konténert. A `.env` és az adatok
(`/mnt/truenas_share/Program_Files/hazikonyvtar_data`) érintetlenek maradnak.

### Első telepítés (új szerverre)

```bash
sudo mkdir -p /opt/hazikonyvtar && sudo chown $USER /opt/hazikonyvtar
git clone git@github-hazikonyvtar:tipcsy/hazikonyvtar.git /opt/hazikonyvtar
cd /opt/hazikonyvtar
cp .env.example .env && nano .env          # jelszavak megadása (APP_USERS)!
mkdir -p /mnt/truenas_share/Program_Files/hazikonyvtar_data
docker compose up -d --build
```

Ezután helyi hálózatról: **http://192.168.31.152:8070**

## Külső elérés Cloudflare Tunnellel

A NAS-on már futó `cloudflared`-hez adj hozzá egy új „Public Hostname”-et
(Cloudflare Zero Trust → Networks → Tunnels → a tunneled → *Public Hostname* → *Add*):

- **Subdomain:** pl. `konyvtar`, **Domain:** a saját domained
- **Service:** `HTTP` → `192.168.31.152:8070`
  (ha a cloudflared ugyanazon a gépen, host hálózaton fut: `localhost:8070` is jó)

Ha konfigurációs fájlt használsz (`config.yml`):

```yaml
ingress:
  - hostname: konyvtar.sajatdomain.hu
    service: http://192.168.31.152:8070
  # ... a többi szabály ...
  - service: http_status:404
```

**Biztonság:** az alkalmazásnak saját bejelentkezése van (név + jelszó, 10 hibás próbálkozás után
10 perc tiltás). Extra védelemnek ajánlott a Cloudflare **Access** is (Zero Trust → Access →
Applications → Self-hosted, szabály: csak a ti két e-mail címetek) – így a belépési oldalt sem
látja más.

**Feltöltési korlát:** a Cloudflare ingyenes csomagja max. **100 MB** méretű kérést enged,
ennél nagyobb e-könyvet/hangoskönyvet helyi hálózatról (http://192.168.31.152:8070) tölts fel.

## Adatok a megosztáson

```
/mnt/truenas_share/Program_Files/hazikonyvtar_data/
├── konyvtar.db          ← az adatbázis
├── covers/              ← borítók (+ _t.jpg bélyegképek)
├── files/<id>/          ← e-könyv fájlok
├── backups/             ← napi adatbázis-mentések
└── .secret_key          ← munkamenet kulcs (automatikus)
```

> **Fontos:** a megosztás SMB/CIFS, ahol az SQLite fájlzárolása nem működik („database is
> locked”). Ezért az SQLite zárolása ki van kapcsolva (`SQLITE_NOLOCK=1`), és az alkalmazás
> maga rendezi sorba az adatbázis-műveleteket. Ez csak úgy biztonságos, ha **egyetlen**
> konténer fut (ne indíts el egy második példányt ugyanerre a mappára). A naplózási mód
> `DELETE` (a WAL mód hálózati fájlrendszeren nem biztonságos). Ne nyisd meg az adatbázist írásra máshonnan, amíg fut a
> konténer. Visszaállítás: állítsd le a konténert, másold a kívánt `backups/konyvtar_*.db`
> fájlt `konyvtar.db` néven a helyére, majd indítsd újra.

## Helyi fejlesztés (Windows)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
set DATA_DIR=./data
set APP_USERS=Tibi:teszt,Évi:teszt
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8070
```

(Windowson a vonalkód-olvasó és a szövegfelismerés nem elérhető, Dockerben igen.)
