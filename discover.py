"""எல்லா சேனல்களையும் படித்து சுருக்க அறிக்கை (படிக்க மட்டும், எதையும் அனுப்பாது).
Telegram folder (எ.கா. "Trading Bots") அல்லது பெயர் வடிகட்டி மூலம் சேனல்களைத் தேர்ந்து,
ஒவ்வொன்றிலும் கடைசி LIMIT மெசேஜ்களை ஆய்வு செய்து data/discovery/ -ல் எழுதும்:
  discovery_report.md      (ஒவ்வொரு சேனலின் புள்ளிவிவரம் + மெசேஜ் மாதிரிகள்)
  channels_suggested.json  (data/channels.json ஆகப் பயன்படுத்தலாம்)
Env: API_ID, API_HASH, SESSION_STRING, FOLDER (இயல்பு "Trading Bots"), LIMIT (இயல்பு 200)
"""
import os, re, json, asyncio, statistics, collections
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
SESSION = os.environ.get("SESSION_STRING", "")
FOLDER = os.environ.get("FOLDER", "Trading Bots")
LIMIT = int(os.environ.get("LIMIT", "200") or 200)
OUT = os.environ.get("OUT", os.path.join("data", "discovery"))
KEYWORDS = re.compile(r"quotex|pocket|binary|signal|trad|bot|option|forex|iq", re.I)

CUR = ("USD|EUR|GBP|JPY|AUD|CAD|CHF|NZD|BRL|INR|PKR|BDT|ARS|COP|EGP|IDR|MXN|NGN|PHP|TRY|ZAR|DZD|KES|LBP|"
       "SGD|HKD|CNH|SEK|NOK|DKK|RUB|THB|VND|BTC|ETH|XAU|XAG|SOL|LTC|BNB|XRP|ADA|DOGE|TND|MAD|SAR|AED|QAR|UAH")
PAIR_RE = re.compile(rf"\b(?:{CUR})\s?/?\s?(?:{CUR})(?:[-_ ]?otc[q]?)?\b", re.I)
DIR_RE = re.compile(r"\b(PUT|CALL|BUY|SELL|UP|DOWN)\b", re.I)
TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
RES_RE = re.compile(r"\b(WIN|WON|LOSS|LOST|LOSE|DOJI|REFUND)\b", re.I)
WIN_RE = re.compile(r"\b(WIN|WON)\b", re.I)
LOSS_RE = re.compile(r"\b(LOSS|LOST|LOSE)\b", re.I)
MTG_RE = re.compile(r"\b(MTG|MARTINGALE|GALE|G1|G2|LEVEL\s?\d|STEP\s?\d|PROTECTION)\b", re.I)
AD_RE = re.compile(r"t\.me/|https?://|\bVIP\b|\bJOIN\b|REGISTER|SIGN\s?UP|DEPOSIT|BONUS|PROMO", re.I)


RATE_RE = re.compile(r"\b(?:win|loss|lose|success)\s*rate\b[^\n]*", re.I)


def strip_rates(t):
    return RATE_RE.sub("", t)


def template(t):
    t = PAIR_RE.sub("PAIR", t)
    t = TIME_RE.sub("HH:MM", t)
    t = re.sub(r"https?://\S+", "URL", t)
    t = re.sub(r"\d+(?:\.\d+)?", "0", t)
    return re.sub(r"\s+", " ", t).strip()[:160]


def estimate_tz(items):
    """items: [(msg_utc_datetime, hour, minute)] சிக்னல் மெசேஜ்கள் -> நேர மண்டலம் (மணி) அல்லது None"""
    diffs = []
    for d, h, m in items:
        diff = (h * 60 + m) - (d.hour * 60 + d.minute)
        diffs.append((diff + 720) % 1440 - 720)
    if len(diffs) < 5:
        return None
    return round((statistics.median(diffs) - 1) / 30) / 2


def analyze(name, msgs, now):
    """msgs: [(id, date_utc, text, has_media)]"""
    texts = [t for _, _, t, _ in msgs if t]
    st = {"name": name, "n": len(msgs), "no_text": sum(1 for _, _, t, _ in msgs if not t)}
    if not msgs:
        return st | {"sig": 0, "res": 0, "age_days": 9999, "span_days": 0, "tz": None, "templates": [], "platform": "other"}
    dates = [d for _, d, _, _ in msgs]
    st["first"], st["last"] = min(dates), max(dates)
    st["age_days"] = (now - st["last"]).total_seconds() / 86400
    st["span_days"] = max((st["last"] - st["first"]).total_seconds() / 86400, 0.04)
    sig, res, timed, mtg, ads, win, loss = 0, 0, [], 0, 0, 0, 0
    for _, d, t, _ in msgs:
        if not t:
            continue
        t = strip_rates(t)
        is_res = bool(RES_RE.search(t))
        has_pair, has_dir, tm = PAIR_RE.search(t), DIR_RE.search(t), TIME_RE.search(t)
        if is_res and has_pair:
            res += 1
            win += bool(WIN_RE.search(t))
            loss += bool(LOSS_RE.search(t))
        elif has_pair and has_dir:
            sig += 1
            if tm:
                timed.append((d, int(tm.group(1)), int(tm.group(2))))
        mtg += bool(MTG_RE.search(t))
        ads += bool(AD_RE.search(t))
    st.update(sig=sig, res=res, mtg=mtg, ads=ads, win=win, loss=loss,
              sig_per_day=sig / st["span_days"], tz=estimate_tz(timed))
    cnt = collections.Counter()
    ex = {}
    for _, _, t, _ in msgs:
        if t:
            k = template(t)
            cnt[k] += 1
            ex.setdefault(k, t)
    st["templates"] = [(c, ex[k]) for k, c in cnt.most_common(4)]
    blob = (name + " " + " ".join(texts[:100])).lower()
    q, p = blob.count("quotex"), blob.count("pocket")
    st["platform"] = "quotex" if q > p else ("pocket" if p > 0 else "other")
    return st


def verdict(st):
    if st["n"] == 0:
        return "வெற்று"
    if st["age_days"] > 7:
        return "செயலற்றது (>7 நாள்)"
    if st["sig"] >= 10 and st["res"] >= 10:
        return "சிக்னல்+முடிவு ✔ (பகுப்பாய்வுக்கு ஏற்றது)"
    if st["sig"] >= 10:
        return "சிக்னல் மட்டும் (முடிவு இல்லை)"
    return "விளம்பரம்/பிற"


async def folder_ids(client, name):
    try:
        from telethon.tl.functions.messages import GetDialogFiltersRequest
        r = await client(GetDialogFiltersRequest())
        for f in getattr(r, "filters", r):
            t = getattr(f, "title", None)
            t = getattr(t, "text", t)
            if t and str(t).strip().lower() == name.strip().lower():
                peers = list(getattr(f, "include_peers", []) or []) + list(getattr(f, "pinned_peers", []) or [])
                return {utils.get_peer_id(p) for p in peers}
    except Exception as e:
        print("Folder வாசிப்பு தோல்வி:", type(e).__name__)
    return None


async def main():
    if not (API_ID and API_HASH and SESSION):
        raise SystemExit("API_ID / API_HASH / SESSION_STRING இல்லை")
    os.makedirs(OUT, exist_ok=True)
    now = datetime.now(timezone.utc)
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.connect()
    results = []
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Session செல்லாது")
        ids = await folder_ids(client, FOLDER)
        dialogs = []
        async for d in client.iter_dialogs():
            ent = d.entity
            is_bot = bool(getattr(ent, "bot", False))
            if d.is_user and not is_bot:
                continue
            if ids is not None:
                if d.id in ids:
                    dialogs.append(d)
            elif KEYWORDS.search(d.name or ""):
                dialogs.append(d)
        print(f"folder={'கிடைத்தது' if ids is not None else 'கிடைக்கவில்லை (பெயர் வடிகட்டி)'} | சேனல்கள்={len(dialogs)}")
        for i, d in enumerate(dialogs, 1):
            msgs = None
            for attempt in (1, 2):
                try:
                    msgs = [(m.id, m.date, m.message or "", bool(m.media))
                            async for m in client.iter_messages(d.entity, limit=LIMIT)]
                    break
                except FloodWaitError as e:
                    if e.seconds > 120 or attempt == 2:
                        break
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    print(f"[{i}] பிழை: {type(e).__name__}")
                    break
            print(f"[{i}/{len(dialogs)}] {'ok' if msgs is not None else 'தவிர்க்கப்பட்டது'}")
            if msgs is not None:
                st = analyze(d.name or str(d.id), msgs, now)
                st["id"] = d.id
                st["username"] = getattr(d.entity, "username", None)
                results.append(st)
            await asyncio.sleep(1.5)
    finally:
        await client.disconnect()
    write_report(results, now)


def write_report(results, now):
    results.sort(key=lambda s: (s["platform"], -s.get("sig", 0)))
    suggested = {"quotex": [], "pocket": [], "other": []}
    L = [f"# Discovery report ({now:%Y-%m-%d %H:%M} UTC)", "",
         f"கோப்பகம்: {FOLDER} | சேனல்கள்: {len(results)} | ஒவ்வொன்றிலும் கடைசி {LIMIT} மெசேஜ்கள்", "",
         "| # | பெயர் | தளம் | மெசேஜ் | நாட்கள் | சிக்னல்/நாள் | சிக்னல் | முடிவு | MTG | விளம்பரம் | tz | தீர்ப்பு |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(results, 1):
        tz = s.get("tz")
        L.append("| {} | {} | {} | {} | {:.1f} | {:.1f} | {} | {} | {} | {} | {} | {} |".format(
            i, s["name"].replace("|", "/")[:28], s["platform"], s["n"], s.get("span_days", 0),
            s.get("sig_per_day", 0), s.get("sig", 0), s.get("res", 0), s.get("mtg", 0), s.get("ads", 0),
            "-" if tz is None else f"{tz:g}", verdict(s)))
        key = str(s["id"]) + ("" if tz is None else f":{tz:g}")
        if s.get("sig", 0) >= 10 and s["age_days"] <= 7:
            suggested[s["platform"]].append(key)
    L += ["", "---", ""]
    for i, s in enumerate(results, 1):
        L += [f"## {i}. {s['name']}  (id={s['id']}, username={s.get('username')})",
              f"தளம்={s['platform']} | {verdict(s)} | மெசேஜ்={s['n']} (உரை இல்லாதவை {s['no_text']})"]
        if s["n"]:
            L.append(f"முதல்={s['first']:%Y-%m-%d %H:%M} கடைசி={s['last']:%Y-%m-%d %H:%M} UTC | win-சொற்கள்={s['win']} loss-சொற்கள்={s['loss']}")
        for c, ex in s["templates"]:
            L += ["", f"**மாதிரி x{c}:**", "```", ex[:400], "```"]
        L.append("")
    open(os.path.join(OUT, "discovery_report.md"), "w", encoding="utf-8").write("\n".join(L))
    json.dump(suggested, open(os.path.join(OUT, "channels_suggested.json"), "w"), indent=2)
    print("அறிக்கை எழுதப்பட்டது:", OUT)


if __name__ == "__main__":
    asyncio.run(main())
