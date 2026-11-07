"""
HMT Watches stock tracker: single-run version for GitHub Actions (uses a headless
browser, because the category pages build their product grid with JavaScript).

Each run: load state.json -> render the pages -> answer Telegram commands sent
since the last run -> alert on tracked watches that are no longer "Coming Soon"
-> save state.json.

Telegram commands:
    /products          list watches found on all tracked pages (numbered)
    /track 3 7 12      track those numbers from the latest /products list
    /track all         track everything listed
    /list              show tracked watches + last known status
    /untrack 2         stop tracking number 2 from /list (or "all")
    /why 12            show the tile text the bot read for watch 12
    /status            what the bot saw on its latest check
"""
import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"

DEFAULT_SOURCES = "https://www.hmtwatches.in/,https://hmtwatches.in/mens"
SOURCES = [u.strip() for u in (os.getenv("SOURCE_URLS") or DEFAULT_SOURCES).split(",") if u.strip()]
DEBUG = (os.getenv("DEBUG") or "").lower() == "true"
STATE_FILE = "state.json"
SOLD_OUT_MARKERS = ("coming soon", "out of stock", "sold out", "currently unavailable")
ICON = {True: "🟢", False: "🔴", None: "⚪"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CARD = 'a[href*="product_overview"]'
MORE = re.compile(r"load more|show more|view more|see more", re.I)


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("tracked", {})
    s.setdefault("listing", [])
    if not isinstance(s.get("problem_notified"), str):
        s["problem_notified"] = ""
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
    r = requests.get(f"{API}/getUpdates", params={"timeout": 0}, timeout=20)
    r.raise_for_status()
    updates = r.json().get("result", [])
    texts = []
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) == CHAT_ID and "text" in msg:
            texts.append(msg["text"])
    return texts, (updates[-1]["update_id"] + 1 if updates else None)


def confirm_updates(next_offset):
    if next_offset:
        try:
            requests.get(f"{API}/getUpdates",
                         params={"offset": next_offset, "limit": 1, "timeout": 0}, timeout=20)
        except requests.RequestException as e:
            print("confirm failed:", e)


# ---------- parsing ----------
PRICE = re.compile(r"(?:RS\.?|₹)\s*([\d,]+)", re.I)


def combine(a, b):
    """A 'coming soon'-type marker seen anywhere wins (False); unknown (None) loses."""
    if a is False or b is False:
        return False
    if a is True or b is True:
        return True
    return None


def merge(found, items):
    for k, p in items.items():
        if k in found:
            found[k]["available"] = combine(found[k]["available"], p["available"])
        else:
            found[k] = p
    return set(items)


def image_key(a):
    img = a.find("img")
    src = img and (img.get("data-src") or img.get("src"))
    return (src.rsplit("/", 1)[-1], src) if src else (None, None)


def keys_in(node):
    return {image_key(x)[0] for x in node.select(CARD)} - {None}


def parse(html, base_url):
    """Return {image_key: product}. The image filename is the stable identity of a
    watch (the product links carry encrypted ids that differ even within one tile)."""
    found = {}
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select(CARD):
        key, src = image_key(a)
        if not key:
            continue

        # Find the whole tile by its images: the biggest ancestor that still holds
        # only this one watch. (Links can't be used: one tile can have several links
        # with different encrypted ids.)
        card = a
        while (card.parent is not None and card.parent.name not in ("body", "html")
               and len(keys_in(card.parent)) <= 1):
            card = card.parent
        text = card.get_text(" ", strip=True)

        m = PRICE.search(text)
        name = (text[:m.start()] if m else text[:40])
        for x in SOLD_OUT_MARKERS:  # drop a label that sits in front of the name
            name = re.sub(re.escape(x), "", name, flags=re.I)
        name = " ".join(name.split())[:90] or key
        img = a.find("img")
        full = ((img.get("alt") if img else "") or a.get("title") or "").strip()
        if full and name.endswith("..") and full.lower().startswith(name.rstrip(".").lower()):
            name = full

        low = text.lower()
        if any(x in low for x in SOLD_OUT_MARKERS):
            available = False
        elif m:
            available = True      # price shown and no "coming soon"-type label
        else:
            available = None      # can't tell (not a real product tile?): never alert
        merge(found, {key: {"key": key, "name": name, "price": m.group(1) if m else "?",
                            "image": urljoin(base_url, src), "link": urljoin(base_url, a["href"]),
                            "available": available, "snippet": text[:160]}})
    return found


# ---------- rendering ----------
def label_for(url):
    return urlparse(url).path.strip("/") or "home"


def image_set(page):
    srcs = page.eval_on_selector_all(
        CARD + " img", "els => els.map(e => e.getAttribute('data-src') || e.getAttribute('src'))")
    return set(srcs)


def load_everything(page):
    """Scroll / press 'load more' until the number of products stops growing."""
    last, stable = -1, 0
    for _ in range(40):
        n = page.locator(CARD).count()
        stable = stable + 1 if n == last else 0
        if stable >= 2:
            break
        last = n
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            btn = page.get_by_text(MORE)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(1500)


def render_source(page, url):
    """Return a list of HTML snapshots (page, extra pagination pages, each tab)."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector(CARD, state="attached", timeout=20000)
    except PWTimeout:
        pass
    load_everything(page)
    htmls = [page.content()]

    # classic pagination ("Next" links)
    for _ in range(15):
        nxt = page.locator('a[rel="next"], .pagination a:has-text("Next"), '
                           '.pagination a:has-text("»")')
        try:
            if not nxt.count() or not nxt.first.is_visible():
                break
            disabled = nxt.first.evaluate(
                "e => /disabled/.test(e.className) || e.getAttribute('aria-disabled') === 'true'"
                " || (e.parentElement && /disabled/.test(e.parentElement.className))")
            if disabled:
                break
            before = image_set(page)
            nxt.first.click(timeout=5000)
            page.wait_for_timeout(2500)
            load_everything(page)
            if image_set(page) == before:
                break
            htmls.append(page.content())
        except Exception as e:
            print("pagination stopped:", e)
            break

    # tabs such as "Newly Listed" / "Fast Moving" may load their own products
    tabs = page.locator('a[href^="#menu"]')
    for i in range(tabs.count()):
        try:
            tabs.nth(i).click(timeout=3000, force=True)
            page.wait_for_timeout(1500)
            load_everything(page)
            htmls.append(page.content())
        except Exception as e:
            print(f"tab {i} skipped:", e)
    return htmls


def dump(label, htmls):
    os.makedirs("debug", exist_ok=True)
    for i, h in enumerate(htmls):
        with open(f"debug/{label.replace('/', '_')}_{i}.html", "w", encoding="utf-8") as f:
            f.write(h)


def scrape():
    """Return (found, counts). counts = {source label: number of watches seen}."""
    found = {}
    counts = {label_for(u): 0 for u in SOURCES}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(user_agent=UA, locale="en-IN",
                                   viewport={"width": 1366, "height": 900}).new_page()
        for url in SOURCES:
            label, keys, htmls = label_for(url), set(), []
            try:
                htmls = render_source(page, url)
                for h in htmls:
                    keys |= merge(found, parse(h, url))
            except Exception as e:
                print(f"[{label}] failed:", e)
                try:
                    htmls = htmls or [page.content()]
                except Exception:
                    pass
            counts[label] = len(keys)
            print(f"[{label}] {len(keys)} watches, {len(htmls)} snapshots")
            if htmls and (DEBUG or not keys):
                dump(label, htmls)
        browser.close()
    return found, counts


# ---------- commands ----------
def counts_line(counts):
    return "Seen on: " + " · ".join(f"{k} {v}" for k, v in counts.items())


def cmd_products(state, found, counts):
    if not found:
        send("Couldn't read the site right now (blocked or layout changed). Try again later.")
        return
    items = sorted(found.values(), key=lambda p: (p["name"], p["price"]))
    state["listing"] = [{k: p[k] for k in ("key", "name", "price", "image")} for p in items]
    lines = [f"{i}. {ICON[p['available']]} {'📌 ' if p['key'] in state['tracked'] else ''}"
             f"{p['name']} – ₹{p['price']} [{p['key'].split('.')[0][-4:]}]"
             for i, p in enumerate(items, 1)]
    send("\n".join(lines) + f"\n\n{counts_line(counts)}\n"
         "🔴 coming soon / out of stock · 🟢 no such label found · ⚪ couldn't tell\n"
         "/track <numbers|all> to track · /why <number> to see what I read · 📌 = tracked")


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
        cur = found.get(p["key"])
        avail = cur["available"] if cur else None
        state["tracked"][p["key"]] = {"name": p["name"], "price": p["price"],
                                      "image": p["image"], "available": avail}
        added.append(f"{p['name']} – ₹{p['price']}" + ("  🟢 looks available right now" if avail else ""))
    send("✅ Now tracking:\n" + "\n".join(added) if added else "Nothing new to track.")


def cmd_why(state, found, args):
    listing = state["listing"]
    if not (args and args[0].isdigit() and 1 <= int(args[0]) <= len(listing)):
        send("Usage: /why 12   (number from /products)")
        return
    item = listing[int(args[0]) - 1]
    p = found.get(item["key"])
    if not p:
        send(f"{item['name']} isn't visible on the site right now.")
        return
    send(f"{ICON[p['available']]} {p['name']} – ₹{p['price']}\nimage file: {p['key']}\n"
         f"Text I read on its tile:\n\"{p['snippet']}\"")


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


def handle(text, state, found, counts):
    parts = text.strip().split()
    if not parts:
        return
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd == "/products":
        cmd_products(state, found, counts)
    elif cmd == "/track":
        cmd_track(state, found, args)
    elif cmd == "/why":
        cmd_why(state, found, args)
    elif cmd == "/list":
        cmd_list(state)
    elif cmd == "/untrack":
        cmd_untrack(state, args)
    elif cmd == "/status":
        send(f"✅ Checked just now.\n{counts_line(counts)}\nTracking {len(state['tracked'])} watches.")
    else:
        send("Commands:\n/products\n/track <numbers|all>\n/list\n/untrack <number|all>\n/why <number>\n/status")


# ---------- main ----------
def main():
    state = load_state()
    try:
        found, counts = scrape()
    except Exception as e:
        print("scrape error:", e)
        found, counts = {}, {label_for(u): 0 for u in SOURCES}

    texts, next_offset = pending_commands()
    for t in texts:
        handle(t, state, found, counts)

    failed = [k for k, v in counts.items() if v == 0]
    sig = ",".join(failed)
    if sig and sig != state["problem_notified"]:
        send(f"⚠️ I couldn't read any products from: {sig}. The site may be blocking GitHub's "
             "servers or its layout changed. I'll keep trying quietly.")
    state["problem_notified"] = sig

    for key, t in state["tracked"].items():
        p = found.get(key)
        if p is None:
            continue  # not visible right now: unknown, don't alert
        if p["available"] is True and t.get("available") is not True:
            send_photo(p["image"],
                       f"🟢 NO LONGER 'COMING SOON'!\n{p['name']} – ₹{p['price']}\n{p['link']}")
        if p["available"] is not None:
            t["available"] = p["available"]

    save_state(state)
    confirm_updates(next_offset)
    print(f"done: {len(found)} watches seen, {len(state['tracked'])} tracked")


if __name__ == "__main__":
    main()
