"""Telegram Quotex signal logger -> data/signals.csv + data/raw_messages.csv
GitHub Actions-ல் ஓடும். Secrets: API_ID, API_HASH, SESSION_STRING
"""
import os, re, csv, json, asyncio
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
SESSION = os.environ.get("SESSION_STRING", "")
TZ_OFFSET = float(os.environ.get("SIGNAL_TZ_OFFSET", "6"))  # இயல்பு நேர மண்டலம் (மணி)
FIRST_RUN_LIMIT = int(os.environ.get("FIRST_RUN_LIMIT", "300"))
GROUP = os.environ.get("GROUP", "").strip()                 # எ.கா. quotex / pocket
DATA_DIR = os.path.join("data", GROUP) if GROUP else "data"
CONFIG = os.path.join("data", "channels.json")              # {"quotex": ["-1001234:6", ...], "pocket": [...]}

STATE = os.path.join(DATA_DIR, "state.json")
SIGNALS = os.path.join(DATA_DIR, "signals.csv")
RAW = os.path.join(DATA_DIR, "raw_messages.csv")


def _channel_list():
    """GROUP இருந்தால் data/channels.json-ல் இருந்து; இல்லையென்றால் CHANNELS env.
    உருப்படி வடிவம்: "id_அல்லது_பெயர்"  அல்லது  "id_அல்லது_பெயர்:நேர_மண்டலம்" """
    if GROUP:
        if not os.path.exists(CONFIG):
            return []
        items = json.load(open(CONFIG, encoding="utf-8")).get(GROUP, [])
    else:
        raw = os.environ.get("CHANNELS") or os.environ.get("CHANNEL") or "Secret_Quotex_Bot"
        items = raw.split(",")
    out = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        key, off = item, TZ_OFFSET
        if ":" in item:
            k, o = item.rsplit(":", 1)
            try:
                key, off = k.strip(), float(o)
            except ValueError:
                pass
        out.append((key.lstrip("@"), off))
    return out


CHANNEL_LIST = _channel_list()
CHANNEL = CHANNEL_LIST[0][0] if CHANNEL_LIST else ""   # trader_demo பழைய பெயருக்காக

FIELDS = ["channel", "msg_id", "msg_utc", "fmt", "pair", "entry_local", "entry_utc",
          "lead_sec", "expiry", "direction", "price", "channel_rate_pct",
          "result_raw", "result_class", "result_utc"]
RAW_FIELDS = ["channel", "msg_id", "msg_utc", "text"]

# result_class: DW = முதல் ட்ரேடிலேயே வெற்றி, MW = முதல் தோல்வி, MTG-ல் வெற்றி,
#               L = MTG-க்குப் பிறகும் தோல்வி, D = doji/draw


def find(pattern, text):
    m = re.search(pattern, text, re.I)
    return m.group(1) if m else None


OUTCOME_RE = r"(DIRECT WIN|WIN \d MTG|LOSS \d MTG|MTG LOSS|MTG WIN|DOJI)"


def outcome_class(o):
    o = o.upper()
    if o == "DIRECT WIN":
        return "DW"
    if o == "DOJI":
        return "D"
    if "WIN" in o:
        return "MW"
    return "L"


def norm_pair(p):
    return p.upper().replace("-OTCQ", "-OTC")


# ---------- பொது (generic) parser: A/B தவிர மற்ற சேனல்களுக்கு (fmt = "G") ----------
CUR = ("USD|EUR|GBP|JPY|AUD|CAD|CHF|NZD|BRL|INR|PKR|BDT|ARS|COP|EGP|IDR|MXN|NGN|PHP|TRY|ZAR|DZD|KES|LBP|"
       "SGD|HKD|CNH|SEK|NOK|DKK|RUB|THB|VND|BTC|ETH|XAU|XAG|SOL|LTC|BNB|XRP|ADA|DOGE|TND|MAD|SAR|AED|QAR|UAH")
PAIR_RE = re.compile(rf"\b(?:{CUR})\s?/?\s?(?:{CUR})(?:[-_ ]?otc[q]?)?\b", re.I)
DIR_RE = re.compile(r"\b(PUT|CALL|BUY|SELL|UP|DOWN)\b", re.I)
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
RES_RE = re.compile(r"\b(WIN|WON|LOSS|LOST|LOSE|DOJI|REFUND)\b", re.I)
MTG_RE = re.compile(r"\b(MTG|MARTINGALE|GALE|G1|G2|LEVEL\s?\d|STEP\s?\d|PROTECTION)\b", re.I)


_RATE_RE = re.compile(r"\b(?:win|loss|lose|success)\s*rate\b[^\n]*", re.I)


def _clean_pair(p):
    p = re.sub(r"[\s/]", "", p.upper())
    return re.sub(r"[-_]?OTCQ?$", "-OTC", p)


def _generic_signal(text):
    text = _RATE_RE.sub("", text)
    if RES_RE.search(text):
        return None
    p, d, t = PAIR_RE.search(text), DIR_RE.search(text), TIME_RE.search(text)
    if not (p and d and t):
        return None
    direction = "PUT" if d.group(1).upper() in ("PUT", "SELL", "DOWN") else "CALL"
    exp = re.search(r"\b([MH]\d{1,2})\b", text)
    return _clean_pair(p.group(0)), "%s:%s" % (t.group(1), t.group(2)), direction, (exp.group(1) if exp else "")


def _generic_result(text):
    text = _RATE_RE.sub("", text)
    ms = RES_RE.findall(text)
    if not ms:
        return None
    words = {w.upper() for w in ms}
    win = bool(words & {"WIN", "WON"})
    loss = bool(words & {"LOSS", "LOST", "LOSE"})
    p, t = PAIR_RE.search(text), TIME_RE.search(text)
    if not (p and t) or (win and loss):
        return None
    if words & {"DOJI", "REFUND"} and not (win or loss):
        cls = "D"
    elif win:
        cls = "MW" if MTG_RE.search(text) else "DW"
    else:
        cls = "L"
    return _clean_pair(p.group(0)), "%s:%s" % (t.group(1), t.group(2)), sorted(words)[0], cls



def _entry(local_msg, hhmm, future):
    h, mi = map(int, hhmm.split(":"))
    e = local_msg.replace(hour=h, minute=mi, second=0, microsecond=0)
    if future:      # சிக்னல்: entry நேரம் மெசேஜுக்குப் பின்
        if e < local_msg - timedelta(hours=12):
            e += timedelta(days=1)
        elif e > local_msg + timedelta(hours=12):
            e -= timedelta(days=1)
    else:           # முடிவு: entry நேரம் மெசேஜுக்கு முன்
        if e > local_msg + timedelta(minutes=2):
            e -= timedelta(days=1)
    return e


def parse(text, msg_dt, msg_id, tz_offset=None):
    """சிக்னல் (வடிவம் A அல்லது B) என்றால் dict, இல்லையென்றால் None"""
    fmt = pair = tm = direction = expiry = price = rate = None
    a = find(r"PAIR\W+([A-Za-z0-9/_\-]+)", text)
    b = find(r"TIME\W+(\d{1,2}:\d{2})", text)
    d = find(r"DIRECTION\W+(PUT|CALL)", text)
    if a and b and d:                                   # வடிவம் A (PAIR/TIME/...)
        fmt, pair, tm, direction = "A", a, b, d
        expiry = find(r"EXPIRY\W+([A-Za-z]\d+)", text)
        price = find(r"PRICE\W+(\d+(?:\.\d+)?)", text)
        rate = find(r"Success Rate\W+([\d.]+)\s*%", text)
    elif "\U0001F4CA" in text and "\U0001F553" in text and not re.search(OUTCOME_RE, text, re.I):
        p = find(r"\U0001F4CA\s*([A-Za-z0-9/_\-]+)", text)           # வடிவம் B (NonSTOP)
        t = find(r"\U0001F553\s*(\d{1,2}:\d{2})", text)
        dd = find(r"\b(PUT|CALL)\b", text)
        ex = find(r"\u23F3\s*([A-Za-z]\d+)", text)
        if p and t and dd and ex:
            fmt, pair, tm, direction, expiry = "B", p, t, dd, ex
    if not fmt:
        g = _generic_signal(text)
        if not g:
            return None
        fmt = "G"
        pair, tm, direction, expiry = g
    tz = timezone(timedelta(hours=TZ_OFFSET if tz_offset is None else tz_offset))
    local_msg = msg_dt.astimezone(tz)
    entry = _entry(local_msg, tm, future=True)
    return {
        "msg_id": msg_id,
        "msg_utc": msg_dt.astimezone(timezone.utc),
        "fmt": fmt,
        "pair": pair.upper(),
        "entry_local": entry.strftime("%Y-%m-%d %H:%M"),
        "entry_utc": entry.astimezone(timezone.utc),
        "lead_sec": round((entry - local_msg).total_seconds(), 1),
        "expiry": (expiry or "").upper(),
        "direction": direction.upper(),
        "price": price,
        "channel_rate_pct": rate,
    }


def parse_result(text, msg_dt, tz_offset=None):
    """சேனல் வெளியிடும் முடிவு மெசேஜ் -> dict அல்லது None"""
    o = find(OUTCOME_RE, text)
    if o:
        m = re.search(r"\U0001F432\s*([A-Za-z0-9/_\-]+)\s*\u2503\s*\U0001F553\s*(\d{1,2}:\d{2})", text)  # வடிவம் A
        if m:
            pair, tm = m.group(1), m.group(2)
        else:                                                                                   # வடிவம் B
            pair = find(r"\U0001F4CA\s*([A-Za-z0-9/_\-]+)", text)
            tm = find(r"\U0001F553\s*(\d{1,2}:\d{2})", text)
        raw, cls = o.upper(), outcome_class(o)
    else:
        g = _generic_result(text)
        if not g:
            return None
        pair, tm, raw, cls = g
    if not (pair and tm):
        return None
    tz = timezone(timedelta(hours=TZ_OFFSET if tz_offset is None else tz_offset))
    entry = _entry(msg_dt.astimezone(tz), tm, future=False)
    return {"pair": norm_pair(pair), "entry_utc": entry.astimezone(timezone.utc),
            "result_raw": raw, "result_class": cls,
            "result_utc": msg_dt.astimezone(timezone.utc)}


def append_csv(path, fields, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: (v.isoformat() if isinstance(v, datetime) else v)
                        for k, v in r.items()})


_DMAP = None


async def _dialog_map(client):
    global _DMAP
    if _DMAP is None:
        _DMAP = {}
        async for d in client.iter_dialogs():
            _DMAP[str(d.id)] = d.entity
            _DMAP.setdefault("name:" + (d.name or "").lower(), d.entity)
    return _DMAP


async def resolve(client, key):
    """சேனலை id / username / பெயர் மூலம் கண்டுபிடி (id சிறந்தது)"""
    key = str(key).strip()
    dm = await _dialog_map(client)
    if key in dm:
        return dm[key]
    if not key.lstrip("-").isdigit():
        try:
            return await client.get_entity(key)
        except Exception:
            pass
        k = key.lower().lstrip("@")
        for name, ent in dm.items():
            if name.startswith("name:") and k in name[5:]:
                return ent
    raise LookupError(f"சேனல் '{key}' கிடைக்கவில்லை. அதில் Join ஆகியுள்ளீர்களா? list_channels.py அல்லது discover ஓட்டி id-ஐச் சரிபாருங்கள்.")


def rebuild():
    """raw_messages.csv-ல் இருந்து signals.csv-ஐ (முடிவுகளுடன்) முழுவதும் மீண்டும் உருவாக்கு.
    Parser மேம்பட்டால் பழைய தரவுக்கும் தானாகப் பொருந்தும்."""
    if not os.path.exists(RAW):
        return 0, 0
    offs = {k: o for k, o in CHANNEL_LIST}
    sigs, results = [], {}
    with open(RAW, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ch, text = r["channel"], r["text"] or ""
            dt = datetime.fromisoformat(r["msg_utc"])
            off = offs.get(ch, TZ_OFFSET)
            row = parse(text, dt, int(r["msg_id"]), off)
            if row:
                row["channel"] = ch
                sigs.append(row)
                continue
            res = parse_result(text, dt, off)
            if res:
                key = (ch, res["pair"], res["entry_utc"])
                results.setdefault(key, res)          # முதல் முடிவு மட்டும்
    matched = 0
    for row in sigs:
        res = results.get((row["channel"], norm_pair(row["pair"]), row["entry_utc"]))
        if res:
            row["result_raw"], row["result_class"], row["result_utc"] = \
                res["result_raw"], res["result_class"], res["result_utc"]
            matched += 1
    with open(SIGNALS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for row in sigs:
            w.writerow({k: (v.isoformat() if isinstance(v, datetime) else v)
                        for k, v in row.items()})
    return len(sigs), matched


def load_state(first_key):
    if not os.path.exists(STATE):
        return {}
    d = json.load(open(STATE))
    if "channels" in d:
        return d["channels"]
    if "last_id" in d:                      # பழைய வடிவம் -> முதல் சேனலுக்கு
        return {first_key: d["last_id"]}
    return {}


def migrate(path, fields, default):
    """பழைய CSV-ல் 'channel' நெடுவரிசை இல்லையென்றால் சேர்"""
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if "channel" in (r.fieldnames or []):
            return
        rows = list(r)
    for row in rows:
        row["channel"] = default
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


async def main():
    if not (API_ID and API_HASH and SESSION):
        raise SystemExit("API_ID / API_HASH / SESSION_STRING secrets இல்லை")
    if not CHANNEL_LIST:
        print(f"[{GROUP or 'default'}] சேனல் பட்டியல் காலி (data/channels.json-ல் சேர்க்கவும்). தவிர்க்கப்பட்டது.")
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    first_key = CHANNEL_LIST[0][0]
    migrate(RAW, RAW_FIELDS, first_key)
    state = load_state(first_key)

    fetched = {}
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Session string செல்லாது. gen_session.py மூலம் புதியது உருவாக்கி secret-ஐ மாற்றுங்கள்.")
        for idx, (key, off) in enumerate(CHANNEL_LIST, 1):
            last_id = state.get(key, 0)
            msgs = None
            for attempt in (1, 2):
                try:
                    ent = await resolve(client, key)
                    msgs = [m async for m in client.iter_messages(
                        ent, min_id=last_id, limit=None if last_id else FIRST_RUN_LIMIT)]
                    break
                except FloodWaitError as e:
                    if e.seconds > 120 or attempt == 2:
                        print(f"[{idx}] Telegram வேகக் கட்டுப்பாடு ({e.seconds}s), தவிர்க்கப்பட்டது")
                        break
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    print(f"[{idx}] தவிர்க்கப்பட்டது: {e}")
                    break
            if msgs is not None:
                msgs.reverse()  # பழையது முதலில்
                fetched[key] = (off, msgs)
            await asyncio.sleep(1)
    finally:
        await client.disconnect()

    for idx, (key, (off, msgs)) in enumerate(fetched.items(), 1):
        raw_rows = [{"channel": key, "msg_id": m.id, "msg_utc": m.date.isoformat(), "text": m.message}
                    for m in msgs if m.message]
        if raw_rows:
            append_csv(RAW, RAW_FIELDS, raw_rows)
        if msgs:
            state[key] = max(m.id for m in msgs)
        print(f"[{idx}] புதிய மெசேஜ்கள்={len(raw_rows)}")
    json.dump({"channels": state}, open(STATE, "w"))
    n, matched = rebuild()
    print(f"[{GROUP or 'default'}] signals.csv: மொத்த சிக்னல்கள்={n}, முடிவுடன் பொருந்தியவை={matched}")


if __name__ == "__main__":
    asyncio.run(main())
