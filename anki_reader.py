"""Reads Anki collection files (.anki2): modern and legacy schemas."""
import html
import json
import re
import sqlite3
import time

# protobuf


def _varint(buf, i):
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def pb_fields(buf):
    """Decode a protobuf message into {field_number: [values]} without a schema."""
    out, i = {}, 0
    buf = bytes(buf or b"")
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 1:
            val, i = buf[i:i + 8], i + 8
        elif wt == 2:
            n, i = _varint(buf, i)
            val, i = buf[i:i + n], i + n
        elif wt == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            break
        out.setdefault(num, []).append(val)
    return out


def _pb_str(fields, num):
    v = fields.get(num)
    return v[0].decode("utf-8", "replace") if v else ""


# template engine

_TAG = re.compile(r"\{\{(.*?)\}\}", re.S)
_EMPTY_FIELD = re.compile(r"^(?:\s|&nbsp;| |</?(?:br|div)\s?/?>)*$", re.I)
_FURI = re.compile(r" ?([^ >]+?)\[(.+?)\]")
_CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)
_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s):
    return html.unescape(_HTML_TAG.sub("", s))


def _furigana(s):
    return _FURI.sub(lambda m: m.group(0) if m.group(2).startswith("sound:")
                     else f"<ruby><rb>{m.group(1)}</rb><rt>{m.group(2)}</rt></ruby>", s)


def _cloze(text, ordinal, question):
    def rep(m):
        n, body, hint = int(m.group(1)), m.group(2), m.group(3)
        if n != ordinal:
            return body
        if question:
            return f'<span class="cloze">[{hint or "..."}]</span>'
        return f'<span class="cloze">{body}</span>'
    return _CLOZE.sub(rep, text)


_FILTERS = {
    "text": _strip_html,
    "furigana": _furigana,
    "kana": lambda s: _FURI.sub(lambda m: m.group(2), s),
    "kanji": lambda s: _FURI.sub(lambda m: m.group(1), s),
    "type": lambda s: "",         # typing box: nothing to show
}


def _parse(tpl):
    """Template string -> nested list of ('text', s) / ('field', name) / ('if'|'unless', name, children)."""
    root, stack, pos = [], [], 0
    cur = root
    for m in _TAG.finditer(tpl):
        if m.start() > pos:
            cur.append(("text", tpl[pos:m.start()]))
        pos = m.end()
        tag = m.group(1).strip()
        if tag[:1] in "#^":
            node = ("if" if tag[0] == "#" else "unless", tag[1:].strip(), [])
            cur.append(node)
            stack.append(cur)
            cur = node[2]
        elif tag[:1] == "/":
            if stack:
                cur = stack.pop()
        else:
            cur.append(("field", tag))
    if pos < len(tpl):
        cur.append(("text", tpl[pos:]))
    return root


def render(tpl, fields, ctx, question):
    """Render one side of a card. fields: {name: html}. ctx: specials + cloze ordinal."""
    lower = {k.lower(): v for k, v in fields.items()}

    def lookup(name):
        if name in ctx:
            return ctx[name]
        if name in fields:
            return fields[name]
        return lower.get(name.lower())

    def walk(nodes):
        out = []
        for node in nodes:
            kind = node[0]
            if kind == "text":
                out.append(node[1])
            elif kind in ("if", "unless"):
                val = lookup(node[1])
                filled = val is not None and not _EMPTY_FIELD.match(val)
                if filled == (kind == "if"):
                    out.append(walk(node[2]))
            else:
                parts = node[1].split(":")
                name, filters = parts[-1].strip(), [p.strip() for p in parts[:-1]]
                val = lookup(name)
                if val is None:
                    continue
                for f in reversed(filters):          # filter nearest the field runs first
                    if f in ("cloze", "cloze-only"):
                        val = _cloze(val, ctx["_ord"], question)
                    elif f.startswith("tts") or f == "image-occlusion":
                        val = ""
                    elif f == "hint":                # no clicking on a slideshow: reveal on the answer
                        val = "" if question else val
                    elif f in _FILTERS:
                        val = _FILTERS[f](val)
                out.append(val)
        return "".join(out)

    return walk(_parse(tpl))


# collection

class AnkiFile:
    def __init__(self, path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.db.create_collation("unicase", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
        tables = {r[0] for r in self.db.execute("select name from sqlite_master where type='table'")}
        self.modern = "notetypes" in tables
        self.crt = self.db.execute("select crt from col").fetchone()[0]
        self._load_models()
        self._load_decks()

    # ---- metadata
    def _load_models(self):
        """self.models[ntid] = {'cloze': bool, 'fields': [names], 'tmpls': {ord: (q, a, name)}}"""
        self.models = {}
        if self.modern:
            for ntid, name, cfg in self.db.execute("select id, name, config from notetypes"):
                kind = pb_fields(cfg).get(1, [0])[0]
                self.models[ntid] = {"name": name, "cloze": kind == 1, "fields": [], "tmpls": {}}
            for ntid, ord_, name in self.db.execute("select ntid, ord, name from fields order by ntid, ord"):
                if ntid in self.models:
                    self.models[ntid]["fields"].append(name)
            for ntid, ord_, name, cfg in self.db.execute("select ntid, ord, name, config from templates"):
                if ntid in self.models:
                    f = pb_fields(cfg)
                    self.models[ntid]["tmpls"][ord_] = (_pb_str(f, 1), _pb_str(f, 2), name)
        else:
            raw = json.loads(self.db.execute("select models from col").fetchone()[0])
            for mid, m in raw.items():
                self.models[int(mid)] = {
                    "name": m["name"], "cloze": m.get("type") == 1,
                    "fields": [f["name"] for f in sorted(m["flds"], key=lambda f: f["ord"])],
                    "tmpls": {t["ord"]: (t["qfmt"], t["afmt"], t["name"]) for t in m["tmpls"]},
                }

    def _load_decks(self):
        if self.modern:
            rows = self.db.execute("select id, name from decks").fetchall()
            self.decks = {did: name.replace("\x1f", "::") for did, name in rows}
        else:
            raw = json.loads(self.db.execute("select decks from col").fetchone()[0])
            self.decks = {int(d): v["name"] for d, v in raw.items()}

    # ---- queries
    def deck_list(self):
        """[{name, count}] including subdeck cards, only decks that have cards."""
        own = dict(self.db.execute(
            "select did, count() from cards where queue >= 0 group by did"))
        totals = {}
        for did, name in self.decks.items():
            n = own.get(did, 0)
            if not n:
                continue
            parts = name.split("::")
            for i in range(1, len(parts) + 1):
                key = "::".join(parts[:i])
                totals[key] = totals.get(key, 0) + n
        return [{"name": k, "count": totals[k]} for k in sorted(totals, key=_deck_sort_key)]

    def _today(self):
        # Anki counts days from collection creation, rolling over at 4am local time
        return int((time.time() - self.crt - 4 * 3600) // 86400) if self.crt else 0

    def card_ids(self, deck_names, filt="all", order="shuffle"):
        wanted = set()
        for did, name in self.decks.items():
            if any(name == d or name.startswith(d + "::") for d in deck_names):
                wanted.add(did)
        if not wanted:
            return []
        where = [f"did in ({','.join(map(str, wanted))})", "queue >= 0"]   # skip suspended / buried
        if filt == "new":
            where.append("type = 0")
        elif filt == "learned":
            where.append("type != 0")
        elif filt == "due":
            where.append(f"((queue in (2, 3) and due <= {self._today()}) or "
                         f"(queue = 1 and due <= {int(time.time()) + 1200}))")
        sql = f"select id from cards where {' and '.join(where)} order by did, due, ord"
        return [r[0] for r in self.db.execute(sql)]

    def card(self, cid):
        row = self.db.execute(
            "select c.ord, c.did, n.mid, n.flds, n.tags from cards c join notes n on n.id = c.nid where c.id = ?",
            (cid,)).fetchone()
        if not row:
            return {"q": "", "a": "", "deck": ""}
        ord_, did, mid, flds, tags = row
        m = self.models.get(mid)
        if not m:
            return {"q": "", "a": "", "deck": self.decks.get(did, "")}
        values = flds.split("\x1f")
        fields = {name: values[i] if i < len(values) else "" for i, name in enumerate(m["fields"])}
        tmpl = m["tmpls"].get(0 if m["cloze"] else ord_) or next(iter(m["tmpls"].values()), ("", "", ""))
        deck = self.decks.get(did, "")
        ctx = {"Tags": tags.strip(), "Deck": deck, "Subdeck": deck.split("::")[-1],
               "Type": m["name"], "Card": tmpl[2], "CardID": str(cid), "_ord": ord_ + 1}
        q = render(tmpl[0], fields, ctx, question=True)
        a = render(tmpl[1], fields, dict(ctx, FrontSide=q), question=False)
        return {"q": q, "a": a, "deck": deck}

    def close(self):
        self.db.close()


def _deck_sort_key(name):
    return [p.lower() for p in name.split("::")]
