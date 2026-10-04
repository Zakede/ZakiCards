"""ザキCards (ZakiCards): OLED-safe fullscreen flashcard slideshow.

Deck sources:
  * your Anki profiles (read from a *copy*, never written to)
  * imported .apkg / .colpkg files and CSV / TXT word lists
  * decks you write yourself inside the app
Loops question -> answer -> black.
"""
import csv
import ctypes
import html as htmllib
import io
import json
import mimetypes
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import webview

from anki_reader import AnkiFile, pb_fields

VERSION = "1.3.0"
# bundled files live next to the script, or in PyInstaller's unpack dir
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ANKI_ROOT = Path(os.environ["APPDATA"]) / "Anki2"
DATA_DIR = Path(os.environ["APPDATA"]) / "ZakiCards"
OLD_DATA_DIR = Path(os.environ["APPDATA"]) / "KuroCards"   # name before 1.2
DECK_DIR = DATA_DIR / "decks"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "profile": None,
    "decks": [],            # keys: "anki:<deck name>" or "local:<deck id>"
    "q": 5,
    "a": 5,
    "black": 10,
    "filter": "all",        # all | learned | new | due   (Anki decks only)
    "order": "shuffle",     # shuffle | inorder
    "brightness": 70,       # % text brightness
    "shift": True,          # move card around to avoid burn-in
    "awake": True,          # stop Windows from sleeping the display
    "clock": False,         # dim drifting clock during the black phase
    "lang": "en",           # en | ja (UI language)
    # display
    "ui_scale": 110,        # % size of menus and buttons
    "card_size": 100,       # % size of card text
    "card_width": 82,       # % of screen width a card may use
    "font": "gothic",       # gothic | mincho | ud
    "align": "center",      # center | left
    # playback
    "shift_amount": 6,      # how far cards wander (% of screen), 0 = never
    "progress": True,       # thin timer line under the card
    "anim": "smooth",       # smooth | fast | none
    "on_end": "loop",       # loop | stop
    "clock24": True,
    "sleep_min": 0,         # stop the slideshow after N minutes (0 = never)
    # colours
    "accent": "#F5A9C3",    # sakura pink
    "tone": "neutral",      # neutral | warm | cool  (card text colour)
    # window
    "start_full": True,     # open the app fullscreen
    "play_full": True,      # go fullscreen when a session starts
}


# ---------------------------------------------------------------- settings
def load_settings():
    if OLD_DATA_DIR.exists() and not DATA_DIR.exists():
        try:
            shutil.copytree(OLD_DATA_DIR, DATA_DIR)
        except OSError:
            pass
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    s["decks"] = [d if d.startswith(("anki:", "local:")) else "anki:" + d for d in s.get("decks", [])]
    return s


def save_settings(s):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- html cleanup
_RX = [
    (re.compile(r"<style.*?</style>", re.S | re.I), ""),
    (re.compile(r"<script.*?</script>", re.S | re.I), ""),
    (re.compile(r"\[sound:[^\]]*\]|\[anki:[^\]]*\]"), ""),
    (re.compile(r"""\s(style|bgcolor|color|on\w+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I), ""),
    (re.compile(r"<audio.*?</audio>|<video.*?</video>|<iframe.*?</iframe>", re.S | re.I), ""),
]
_CLASS = re.compile(r"""\sclass\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)


def clean_html(h):
    """Drop the deck's own colours/fonts (white backgrounds = OLED pain)."""
    for rx, rep in _RX:
        h = rx.sub(rep, h)
    return _CLASS.sub(lambda m: m.group(0) if "cloze" in m.group(1) else "", h).strip()


def media_urls(h, prefix):
    return re.sub(r'src="(?!https?:|data:|/)([^"]+)"', lambda m: f'src="{prefix}{m.group(1)}"', h)


# ---------------------------------------------------------------- anki profiles
def list_profiles():
    if not ANKI_ROOT.exists():
        return []
    return sorted(p.name for p in ANKI_ROOT.iterdir() if (p / "collection.anki2").exists())


def _fresh_workdir(base):
    """Own folder per run, so a second window never fights over a locked copy."""
    base.mkdir(parents=True, exist_ok=True)
    for old in base.glob("*"):  # locked leftovers belong to a live run
        try:
            shutil.rmtree(old) if old.is_dir() else old.unlink()
        except OSError:
            pass
    work = base / f"{os.getpid()}-{random.randrange(1 << 30)}"
    work.mkdir(parents=True)
    return work


class AnkiSource:
    def __init__(self):
        self.lock = threading.Lock()
        self.col = None
        self.profile = None
        self.media = None

    def open(self, profile):
        with self.lock:
            if self.col:
                self.col.close()
                self.col = None
            src = ANKI_ROOT / profile
            work = _fresh_workdir(Path(tempfile.gettempdir()) / "ZakiCards")
            # copy db + WAL so we see changes Anki hasn't checkpointed yet
            for suffix in ("", "-wal"):
                f = src / f"collection.anki2{suffix}"
                if f.exists():
                    shutil.copy2(f, work / f"c.anki2{suffix}")
            self.col = AnkiFile(work / "c.anki2")
            self.profile = profile
            self.media = src / "collection.media"

    def decks(self):
        if not self.col:
            return []
        with self.lock:
            return self.col.deck_list()

    def card_ids(self, names, filt):
        if not self.col or not names:
            return []
        with self.lock:
            return [f"a:{i}" for i in self.col.card_ids(names, filt)]

    def card(self, cid):
        with self.lock:
            c = self.col.card(cid)
        return {"q": media_urls(clean_html(c["q"]), "/media/"),
                "a": media_urls(clean_html(c["a"]), "/media/"), "deck": c["deck"]}


# ---------------------------------------------------------------- local decks
class LocalDecks:
    """Decks stored as JSON in %APPDATA%/ZakiCards/decks (imports + hand-written)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}

    def _path(self, did):
        if not re.fullmatch(r"[a-z0-9-]+", did or ""):
            raise ValueError("bad deck id")
        return DECK_DIR / f"{did}.json"

    def load(self, did):
        with self.lock:
            if did not in self.cache:
                self.cache[did] = json.loads(self._path(did).read_text(encoding="utf-8"))
            return self.cache[did]

    def list(self):
        out = []
        if DECK_DIR.exists():
            for f in sorted(DECK_DIR.glob("*.json")):
                try:
                    d = self.load(f.stem)
                    out.append({"id": d["id"], "name": d["name"], "count": len(d["cards"]),
                                "source": d.get("source", "manual")})
                except Exception:
                    pass
        return sorted(out, key=lambda d: d["name"].lower())

    def save(self, name, cards, source, did=None, text=None):
        DECK_DIR.mkdir(parents=True, exist_ok=True)
        did = did or f"{int(time.time())}-{random.randrange(1 << 20):x}"
        deck = {"id": did, "name": name.strip() or "Untitled deck", "source": source, "cards": cards}
        if text is not None:
            deck["text"] = text
        with self.lock:
            self._path(did).write_text(json.dumps(deck, ensure_ascii=False), encoding="utf-8")
            self.cache[did] = deck
        return deck

    def delete(self, did):
        with self.lock:
            self._path(did).unlink(missing_ok=True)
            shutil.rmtree(DECK_DIR / f"{did}_media", ignore_errors=True)
            self.cache.pop(did, None)

    def card_ids(self, dids):
        ids = []
        for did in dids:
            try:
                ids += [f"l:{did}:{i}" for i in range(len(self.load(did)["cards"]))]
            except Exception:
                pass
        return ids

    def card(self, did, i):
        d = self.load(did)
        c = d["cards"][i]
        prefix = f"/lmedia/{did}/"
        return {"q": media_urls(c["q"], prefix), "a": media_urls(c["a"], prefix), "deck": d["name"]}


# ---------------------------------------------------------------- importers
def _cell(s):
    s = s.strip()
    return s if re.search(r"<[a-zA-Z/][^>]*>", s) else htmllib.escape(s).replace("\\n", "<br>")


def parse_text_cards(text):
    """Word lists: one card per line, front and back split by tab, |, ;, or comma."""
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return []
    sep = next((s for s in ("\t", "|", ";") if sum(s in l for l in lines[:50]) >= max(1, len(lines[:50]) // 2)), ",")
    cards = []
    if sep == "|":
        rows = [l.split("|") for l in lines]
    else:
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=sep))
    for r in rows:
        r = [c for c in r if c.strip()] or [""]
        if not r[0].strip():
            continue
        q = _cell(r[0])
        a = "<br>".join(_cell(c) for c in r[1:]) if len(r) > 1 else ""
        # back side shows the front too, like Anki does
        cards.append({"q": q, "a": f"{q}<hr>{a}" if a else q})
    return cards


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _maybe_zstd(data):
    if data[:4] == _ZSTD_MAGIC:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    return data


def import_apkg(path):
    """Anki package -> one local deck. Cards are pre-rendered, media extracted."""
    work = _fresh_workdir(Path(tempfile.gettempdir()) / "ZakiCardsImport")
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        db_name = next((n for n in ("collection.anki21b", "collection.anki21", "collection.anki2") if n in names), None)
        if not db_name:
            raise ValueError("That file doesn't look like an Anki deck package.")
        (work / "c.db").write_bytes(_maybe_zstd(z.read(db_name)))
        # media map: legacy JSON {"0": "file.jpg"} or newer zstd-protobuf list
        media_map = {}
        if "media" in names:
            raw = _maybe_zstd(z.read("media"))
            try:
                media_map = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                for i, entry in enumerate(pb_fields(raw).get(1, [])):
                    f = pb_fields(entry)
                    if f.get(1):
                        media_map[str(i)] = f[1][0].decode("utf-8", "replace")

        col = AnkiFile(work / "c.db")
        try:
            decks = col.deck_list()
            if not decks:
                raise ValueError("That package has no cards in it.")
            tops = [d for d in decks if "::" not in d["name"]]
            cards = []
            for cid in col.card_ids([d["name"] for d in tops], "all"):
                c = col.card(cid)
                if c["q"].strip():
                    cards.append({"q": clean_html(c["q"]), "a": clean_html(c["a"])})
        finally:
            col.close()
        name = max(tops, key=lambda d: d["count"])["name"] if tops else Path(path).stem
        deck = LOCAL.save(name, cards, "apkg")

        used = set(re.findall(r'src="([^"]+)"', " ".join(c["q"] + c["a"] for c in cards)))
        mdir = DECK_DIR / f"{deck['id']}_media"
        for num, fname in media_map.items():
            if fname in used and num in names and "/" not in fname and "\\" not in fname:
                mdir.mkdir(exist_ok=True)
                (mdir / fname).write_bytes(_maybe_zstd(z.read(num)))
    shutil.rmtree(work, ignore_errors=True)
    return deck


def import_file(path):
    p = Path(path)
    if p.suffix.lower() in (".apkg", ".colpkg"):
        return import_apkg(p)
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "cp932", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    cards = parse_text_cards(text)
    if not cards:
        raise ValueError("Couldn't find any cards in that file. Use one card per line: front<tab>back")
    return LOCAL.save(p.stem, cards, "csv")


ANKI = AnkiSource()
WINDOW = {"full": True}
LOCAL = LocalDecks()
SETTINGS = load_settings()


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", code=200, cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, base, name):
        if base:
            f = (base / name).resolve()
            if f.is_file() and base.resolve() in f.parents:
                return self._send(f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                                  cache=True)
        return self._send(b"", "text/plain", 404)

    def _state(self, qs):
        profiles = list_profiles()
        prof = qs.get("profile") or SETTINGS.get("profile")
        if profiles:
            if prof not in profiles:
                prof = profiles[0]
            if qs.get("reload") or ANKI.profile != prof:
                ANKI.open(prof)
            SETTINGS["profile"] = prof
        return {"profiles": profiles, "profile": prof if profiles else None, "version": VERSION,
                "anki": ANKI.decks() if profiles else [], "local": LOCAL.list(), "settings": SETTINGS,
                "full": WINDOW["full"]}

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send((RES_DIR / "ui.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if u.path.startswith("/assets/"):
                return self._file(RES_DIR / "assets", unquote(u.path[len("/assets/"):]))
            if u.path.startswith("/media/"):
                return self._file(ANKI.media, unquote(u.path[len("/media/"):]))
            if u.path.startswith("/lmedia/"):
                did, _, name = unquote(u.path[len("/lmedia/"):]).partition("/")
                LOCAL._path(did)  # validates id
                return self._file(DECK_DIR / f"{did}_media", name)
            if u.path == "/api/init":
                return self._send(self._state(qs))
            if u.path == "/api/card":
                cid = qs["id"]
                if cid.startswith("a:"):
                    return self._send(ANKI.card(int(cid[2:])))
                _, did, i = cid.split(":")
                return self._send(LOCAL.card(did, int(i)))
            if u.path == "/api/deck":
                d = LOCAL.load(qs["id"])
                text = d.get("text")
                if text is None and d.get("source") not in ("apkg", "starred"):
                    text = "\n".join(f"{_plain(c['q'])} | {_plain(c['a'].split('<hr>', 1)[-1])}" for c in d["cards"])
                return self._send({"id": d["id"], "name": d["name"], "text": text, "source": d.get("source")})
            if u.path == "/api/quit":
                self._send({"ok": True})
                threading.Thread(target=lambda: [w.destroy() for w in webview.windows]).start()
                return
            self._send({"error": "not found"}, code=404)
        except Exception as e:
            self._send({"error": str(e)}, code=500)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path in ("/api/start", "/api/stop", "/api/save"):
                SETTINGS.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
                save_settings(SETTINGS)
            if self.path == "/api/start":
                set_awake(SETTINGS["awake"])
                keys = SETTINGS["decks"]
                ids = ANKI.card_ids([k[5:] for k in keys if k.startswith("anki:")], SETTINGS["filter"])
                ids += LOCAL.card_ids([k[6:] for k in keys if k.startswith("local:")])
                if SETTINGS["order"] == "shuffle":
                    random.shuffle(ids)
                return self._send({"ids": ids})
            if self.path == "/api/stop":
                set_awake(False)
                return self._send({"ok": True})
            if self.path == "/api/save":
                return self._send({"ok": True})
            if self.path == "/api/fullscreen":
                want = data.get("on")
                if want is None or bool(want) != WINDOW["full"]:
                    for w in webview.windows:
                        w.toggle_fullscreen()
                    WINDOW["full"] = not WINDOW["full"]
                return self._send({"full": WINDOW["full"]})
            if self.path == "/api/import":
                picked = webview.windows[0].create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=True,
                    file_types=("Flashcards (*.apkg;*.colpkg;*.csv;*.tsv;*.txt)", "All files (*.*)"))
                made, errors = [], []
                for p in picked or []:
                    try:
                        made.append(import_file(p)["name"])
                    except Exception as e:
                        errors.append(f"{Path(p).name}: {e}")
                return self._send({"made": made, "errors": errors, "local": LOCAL.list()})
            if self.path == "/api/deck/save":
                cards = parse_text_cards(data.get("text", ""))
                if not cards:
                    return self._send({"error": "Add at least one card: front | back"})
                old = LOCAL.load(data["id"]) if data.get("id") else None
                if old and old.get("source") in ("apkg", "starred"):   # HTML cards: rename only
                    deck = LOCAL.save(data.get("name", old["name"]), old["cards"], old["source"], old["id"])
                else:
                    deck = LOCAL.save(data.get("name", ""), cards, "manual", data.get("id"), text=data.get("text"))
                return self._send({"deck": {"id": deck["id"]}, "local": LOCAL.list()})
            if self.path == "/api/deck/rename":
                old = LOCAL.load(data["id"])
                LOCAL.save(data["name"], old["cards"], old.get("source", "manual"), old["id"], text=old.get("text"))
                return self._send({"local": LOCAL.list()})
            if self.path == "/api/star":
                # add the current card to the built-in "Starred" deck (no duplicates)
                card = ANKI.card(int(data["id"][2:])) if data["id"].startswith("a:") else                     LOCAL.card(*(lambda p: (p[1], int(p[2])))(data["id"].split(":")))
                try:
                    deck = LOCAL.load("starred")
                except (OSError, ValueError):
                    deck = {"cards": []}
                entry = {"q": card["q"], "a": card["a"]}
                cards = deck["cards"]
                added = entry not in cards
                if added:
                    cards.append(entry)
                LOCAL.save("★ Starred", cards, "starred", "starred")
                return self._send({"added": added, "count": len(cards), "local": LOCAL.list()})
            if self.path == "/api/deck/delete":
                LOCAL.delete(data["id"])
                SETTINGS["decks"] = [k for k in SETTINGS["decks"] if k != "local:" + data["id"]]
                save_settings(SETTINGS)
                return self._send({"local": LOCAL.list(), "settings": SETTINGS})
            self._send({"error": "not found"}, code=404)
        except Exception as e:
            self._send({"error": str(e)}, code=500)


def _plain(h):
    return htmllib.unescape(re.sub(r"<br\s*/?>", "\\\\n", re.sub(r"<(?!br)[^>]+>", "", h)))


# ---------------------------------------------------------------- keep awake
_awake_evt = threading.Event()


def _awake_loop():
    # SetThreadExecutionState is per-thread, so keep one thread holding it
    ES_CONTINUOUS, ES_SYSTEM, ES_DISPLAY = 0x80000000, 0x1, 0x2
    k32 = ctypes.windll.kernel32
    on = False
    while True:
        want = _awake_evt.is_set()
        if want != on:
            k32.SetThreadExecutionState(ES_CONTINUOUS | ((ES_SYSTEM | ES_DISPLAY) if want else 0))
            on = want
        threading.Event().wait(1)


def set_awake(flag):
    (_awake_evt.set if flag else _awake_evt.clear)()


# ---------------------------------------------------------------- main
def main():
    try:  # own taskbar icon instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ZakiCards.App")
    except Exception:
        pass
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=_awake_loop, daemon=True).start()
    WINDOW["full"] = bool(SETTINGS.get("start_full", True))
    webview.create_window("ザキCards", f"http://127.0.0.1:{srv.server_port}/", width=1280, height=800,
                          fullscreen=WINDOW["full"], background_color="#000000")
    webview.start(icon=str(RES_DIR / "assets" / "icon.ico"))
    set_awake(False)
    if ANKI.col:
        ANKI.col.close()
    srv.shutdown()


if __name__ == "__main__":
    main()
