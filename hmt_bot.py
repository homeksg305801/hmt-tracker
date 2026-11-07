"""
HMT Watches stock tracker: single-run version for GitHub Actions.

Each run: (1) loads state.json, (2) scrapes hmtwatches.in, (3) answers any
Telegram commands sent since the last run, (4) alerts you if a tracked watch
stopped showing "Coming Soon", (5) saves state.json.

Telegram commands:
    /products          list watches currently on the site (numbered)
    /track 3 7 12      track those numbers from the latest /products list
    /track all         track everything listed
    /list              show tracked watches + last known status
    /untrack 2         stop tracking number 2 from /list (or "all")
"""
import json
import os
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"

BASE = "https://www.hmtwatches.in"
SOURCES = [u.strip() for u in os.getenv("SOURCE_URLS", BASE + "/").split(",") if u.strip()]
STATE_FILE = "state.json"
SOLD_OUT_MARKERS = ("coming soon", "out of stock", "sold out")
ICON = {True: "🟢", False: "🔴", None: "⚪"}

http = requests.Session()
http.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("tracked", {})
    s.setdefault("listing", [])
    s.setdefault("problem_notified", False)
    return s


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ---------- telegram ----------
def send(text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        try:
            requests.post(f"{API}/sendMessage", json={"chat_id": CHAT_ID, "text": c.strip()},
                          timeout=15)
        except requests.RequestException as e:
            print("Telegram send failed:", e)


def send_photo(photo_url, caption):
    try:
        r = requests.post(f"{API}/sendPhoto",
                          json={"chat_id": CHAT_ID, "photo": photo_url, "caption": caption},
                          timeout=20)
        if r.ok:
            return
    except requests.RequestException:
        pass
    send(caption)


def pending_commands():
    """Return (texts, next_offset). Telegram keeps unread messages for us."""
    r = requests.get(f"{API}/getUpdates", params={"timeout": 0}, timeout=20)
    r.raise_for_status()
    updates = r.json().get("result", [])
    texts = []
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == CHAT_ID and "text" in msg:
            texts.append(msg["text"])
    next_offset = updates[-1]["update_id"] + 1 if updates else None
    return texts, next_offset


def confirm_updates(next_offset):
    if next_offset:
        try:
            requests.get(f"{API}/getUpdates",
                         params={"offset": next_offset, "limit": 1, "timeout": 0}, timeout=20)
        except requests.RequestException as e:
            print("confirm failed:", e)


# ---------- scraping ----------
def scrape():
    """Return {image_key: product}. The image filename is the stable identity of a
    watch, because the product links carry encrypted ids that change on every load."""
    found = {}
    for url in SOURCES:
        r = http.get(url, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select('a[href*="product_overview"]'):
            img = a.find("img")
            src = img and (img.get("data-src") or img.get("src"))
            if not src:
                continue
            key = src.rsplit("/", 1)[-1]

            card = a
            while card.parent is not None and len(
                    {x["href"] for x in card.parent.select('a[href*="product_overview"]')}) <= 1:
                card = card.parent
            text = card.get_text(" ", strip=True)

            m = re.match(r"(.*?)\s*RS\.?\s*([\d,]+)", text, re.I)
            name = (m.group(1) if m else text[:40]).strip() or key
            price = m.group(2) if m else "?"
            available = not any(s in text.lower() for s in SOLD_OUT_MARKERS)

            if key in found:  # same watch in several tabs: any "in stock" card wins
                found[key]["available"] = found[key]["available"] or available
                continue
            found[key] = {"key": key, "name": name, "price": price,
                          "image": urljoin(url, src), "link": urljoin(url, a["href"]),
                          "available": available}
    return found


# ---------- commands ----------
def cmd_products(state, found):
    if not found:
        send("Couldn't read the site right now (blocked or layout changed). Try again later.")
        return
    items = sorted(found.values(), key=lambda p: (p["name"], p["price"]))
    state["listing"] = [{k: p[k] for k in ("key", "name", "price", "image")} for p in items]
    lines = [f"{i}. {ICON[p['available']]} {'📌 ' if p['key'] in state['tracked'] else ''}"
             f"{p['name']} – ₹{p['price']}" for i, p in enumerate(items, 1)]
    send("\n".join(lines) + "\n\nUse /track <numbers> or /track all  (📌 = already tracked)")


def cmd_track(state, found, args):
    listing = state["listing"]
    if not listing:
        send("Send /products first, then /track <numbers>.")
        return
    if args and args[0].lower() == "all":
        picks = listing
    else:
        picks = [listing[int(a) - 1] for a in args if a.isdigit() and 1 <= int(a) <= len(listing)]
    if not picks:
        send("Usage: /track 3 7 12   or   /track all   (numbers come from /products)")
        return
    added = []
    for p in picks:
        if p["key"] in state["tracked"]:
            continue
        cur = (found or {}).get(p["key"])
        avail = cur["available"] if cur else None
        state["tracked"][p["key"]] = {"name": p["name"], "price": p["price"],
                                      "image": p["image"], "available": avail}
        added.append(f"{p['name']} – ₹{p['price']}" + ("  (already available!)" if avail else ""))
    send("✅ Now tracking:\n" + "\n".join(added) if added else "Nothing new to track.")


def cmd_list(state):
    if not state["tracked"]:
        send("Nothing tracked yet. Use /products then /track.")
        return
    send("\n".join(f"{i}. {ICON[t.get('available')]} {t['name']} – ₹{t['price']}"
                   for i, t in enumerate(state["tracked"].values(), 1)))


def cmd_untrack(state, args):
    keys = list(state["tracked"])
    if args and args[0].lower() == "all":
        state["tracked"].clear()
    else:
        for a in args:
            if a.isdigit() and 1 <= int(a) <= len(keys):
                state["tracked"].pop(keys[int(a) - 1], None)
    send("🗑 Done. Send /list to see what's left.")


def handle(text, state, found):
    parts = text.strip().split()
    if not parts:
        return
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd == "/products":
        cmd_products(state, found)
    elif cmd == "/track":
        cmd_track(state, found, args)
    elif cmd == "/list":
        cmd_list(state)
    elif cmd == "/untrack":
        cmd_untrack(state, args)
    else:
        send("Commands:\n/products\n/track <numbers|all>\n/list\n/untrack <number|all>")


# ---------- main ----------
def main():
    state = load_state()
    try:
        found = scrape()
    except Exception as e:
        print("scrape error:", e)
        found = None

    texts, next_offset = pending_commands()
    for t in texts:
        handle(t, state, found)

    if not found:
        if not state["problem_notified"]:
            send("⚠️ I couldn't read any products from the site (it may be blocking GitHub's "
                 "servers, or its layout changed). I'll keep trying quietly.")
            state["problem_notified"] = True
    else:
        state["problem_notified"] = False
        for key, t in state["tracked"].items():
            p = found.get(key)
            if p is None:
                continue  # not on the page right now: unknown, don't alert
            if p["available"] and t.get("available") is not True:
                send_photo(p["image"],
                           f"🟢 NO LONGER 'COMING SOON'!\n{p['name']} – ₹{p['price']}\n{p['link']}")
            t["available"] = p["available"]

    save_state(state)
    confirm_updates(next_offset)
    print(f"done: {len(found or {})} products seen, {len(state['tracked'])} tracked")


if __name__ == "__main__":
    main()
