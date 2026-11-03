"""
Bot monitorizare Pokemon TCG (RO) -> notificari Telegram / WhatsApp
Detecteaza: produse noi in stoc, revenire in stoc, PRECOMENZI (noi / deschise / lansate).

Comenzi:
  python pokemon_bot.py --test   # arata ce gaseste pe fiecare site (fara notificari)
  python pokemon_bot.py --once   # o singura verificare (pentru GitHub Actions)
  python pokemon_bot.py          # ruleaza non-stop (pe PC/server)
"""
import html, json, os, random, re, sys, time
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------
# SITE-URI. Optiuni per site:
#   "shopify": True       -> API-ul JSON Shopify (stoc exact)
#   "woo": True           -> API-ul WooCommerce (cu fallback pe HTML)
#   "extra": [url, ...]   -> pagini suplimentare din aceeasi lista (paginare)
#   "require": [...]      -> numele trebuie sa contina unul din cuvinte
#   "any_name": True      -> nu cere cuvantul "pokemon" in nume
#   "link_re": "regex"    -> iau doar linkurile care se potrivesc (ex: "/products/")
#   "exclude": [...]      -> cuvinte interzise in nume (doar pentru acest site)
#   "preorder_page": True -> tot ce e disponibil pe aceasta pagina e PRECOMANDA
#   "js": True            -> randare cu Playwright (doar daca da 0 produse)
# ---------------------------------------------------------------
SITES = [
    {"name": "Smyk", "url": "https://smyk.ro/personaj/pokemon", "require": ["tcg"]},
    {"name": "Bebe Tei", "url": "https://comenzi.bebetei.ro/brand/pokemon"},
    {"name": "Krit", "url": "https://krit.ro/en/categorie/pokemon"},
    {"name": "Secret Cards", "url": "https://secretcards.ro/en/categories/sealed-products",
     "any_name": True, "link_re": "/products/",
     "exclude": ["lorcana", "riftbound", "one piece", "kayou", "dragon ball", "yu-gi-oh",
                 "magic", "digimon", "disney", "sanrio", "union arena", "gundam"]},
    {"name": "LootShop", "url": "https://lootshop.ro/product-category/pokemon-tcg/", "woo": True},
    {"name": "Red Goblin", "url": "https://redgoblin.ro/collections/pokemon-1", "shopify": True},
    {"name": "TCG Arena", "url": "https://tcgarena.ro/collections/joc-de-carti-pokemon-tcg-romania", "shopify": True},
    {"name": "LexShop", "url": "https://www.lexshop.ro/pokemon-tcg",
     "extra": [f"https://www.lexshop.ro/pokemon-tcg?p={i}" for i in range(1, 6)]},
    {"name": "Pokemania", "url": "https://pokemania.ro/seturi-pokemon-tcg",
     "extra": [f"https://pokemania.ro/seturi-pokemon-tcg/p{i}" for i in range(2, 5)]},
    {"name": "Pokemania Precomenzi", "url": "https://pokemania.ro/precomenzi",
     "extra": ["https://pokemania.ro/precomenzi/p2"], "preorder_page": True},
    {"name": "TCG Shop", "url": "https://www.tcg-shop.ro/category/pokemon"},
]

KEYWORDS = ["pokemon", "pokémon"]
# accesorii / jucarii ignorate (dar NU daca numele contine un cuvant din KEEP)
EXCLUDE = ["plush", "plus ", "figurina", "figurină", "puzzle", "rucsac", "playmat",
           "portfolio", "sleeves", "deck box", "toploader", "album", "construit",
           "constructie", "construcție", "lego", "mega construx", "perna", "creioane"]
KEEP = ["booster", "elite trainer", "etb", "blister", "tin"]

OUT_OF_STOCK = ["stoc epuizat", "indisponibil", "out of stock", "epuizat", "sold out",
                "nu este in stoc", "nu este în stoc", "outofstock", "soldout",
                "alerta stoc", "alertă stoc", "notify me", "anunta-ma", "anunță-mă"]
PREORDER_WORDS = ["precomand", "pre-comand", "pre-order", "preorder", "pre order",
                  "backorder", "presale", "pre-sale"]
PRE_RE = re.compile("|".join(re.escape(w) for w in PREORDER_WORDS), re.I)

MAX_ALERTS_PER_SITE = 10
CHECK_EVERY, JITTER = 300, 60
STATE_FILE = "state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
}
ALERTED = set()   # evita alerte duble in aceeasi rulare


# ------------------------- Notificari -----------------------------
def send_whatsapp(text):
    """Trimite alerta pe TOATE canalele configurate (Telegram / WhatsApp)."""
    sent = False
    if os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        try:
            requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
                          timeout=20, data={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
                                            "text": text}).raise_for_status()
            sent = True
        except Exception as e:
            print(f"[!] Eroare Telegram: {e}")
    if os.getenv("GREENAPI_ID") and os.getenv("GREENAPI_TOKEN"):
        try:
            host = os.getenv("GREENAPI_HOST", "https://api.green-api.com").rstrip("/")
            requests.post(f"{host}/waInstance{os.environ['GREENAPI_ID']}/sendMessage/{os.environ['GREENAPI_TOKEN']}",
                          timeout=20, json={"chatId": os.environ["GREENAPI_CHAT"], "message": text}).raise_for_status()
            sent = True
        except Exception as e:
            print(f"[!] Eroare Green-API: {e}")
    if os.getenv("CALLMEBOT_APIKEY"):
        try:
            requests.get("https://api.callmebot.com/whatsapp.php", timeout=20, params={
                "phone": os.environ["CALLMEBOT_PHONE"], "text": text,
                "apikey": os.environ["CALLMEBOT_APIKEY"]}).raise_for_status()
            sent = True
        except Exception as e:
            print(f"[!] Eroare CallMeBot: {e}")
    if os.getenv("TWILIO_SID"):
        try:
            sid = os.environ["TWILIO_SID"]
            requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                          auth=(sid, os.environ["TWILIO_TOKEN"]), timeout=20,
                          data={"From": os.environ["TWILIO_FROM"], "To": os.environ["TWILIO_TO"],
                                "Body": text}).raise_for_status()
            sent = True
        except Exception as e:
            print(f"[!] Eroare Twilio: {e}")
    if not sent:
        print("[!] Nicio notificare configurata. Mesaj:\n" + text)


# ------------------------- Utilitare -----------------------------
def fetch_html(site, url=None):
    url = url or site["url"]
    if site.get("js"):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=45000)
            content = page.content()
            b.close()
            return content
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def wanted(name, site):
    n = name.lower()
    if not site.get("any_name") and not any(k in n for k in KEYWORDS):
        return False
    if any(x in n for x in site.get("exclude", [])):
        return False
    if any(x in n for x in EXCLUDE) and not any(k in n for k in KEEP):
        return False
    req = site.get("require")
    return not req or any(r in n for r in req)


def card_text(card):
    """Textul cardului + atribute (title/aria-label/value/alt) unde stau butoanele
    de tip 'Precomanda' / 'Alerta stoc'."""
    parts = [card.get_text(" ", strip=True)]
    for tag in card.find_all(True):
        for k in ("title", "aria-label", "value", "alt", "data-title", "data-label", "data-text"):
            v = tag.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v)
    return " ".join(parts).lower()


def classify(text):
    t = text.lower()
    return any(w in t for w in OUT_OF_STOCK), bool(PRE_RE.search(t))


# ------------------------- Extractie -----------------------------
def from_shopify(site):
    u = urlparse(site["url"])
    base = f"{u.scheme}://{u.netloc}"
    out = {}
    for page in range(1, 4):
        api = f"{base}{u.path.rstrip('/')}/products.json?limit=250&page={page}"
        r = requests.get(api, headers=HEADERS, timeout=25)
        r.raise_for_status()
        items = r.json().get("products", [])
        for p in items:
            variants = p.get("variants") or [{}]
            tags = p.get("tags", [])
            tags = ", ".join(tags) if isinstance(tags, list) else str(tags)
            blob = " ".join([p.get("title", ""), p.get("handle", ""), tags, p.get("product_type", "")])
            out[f"{base}/products/{p['handle']}"] = {
                "name": p["title"],
                "price": f"{variants[0].get('price', '?')} lei",
                "in_stock": any(v.get("available") for v in variants),
                "preorder": bool(PRE_RE.search(blob))}
        if len(items) < 250:
            break
    return out


def from_woo(site):
    u = urlparse(site["url"])
    api = (f"{u.scheme}://{u.netloc}/wp-json/wc/store/v1/products"
           f"?per_page=100&orderby=date&order=desc&search={site.get('search', 'pokemon')}")
    r = requests.get(api, headers=HEADERS, timeout=25)
    r.raise_for_status()
    out = {}
    for p in r.json():
        pr = p.get("prices") or {}
        minor = int(pr.get("currency_minor_unit", 2))
        price = int(pr.get("price") or 0) / (10 ** minor)
        blob = " ".join([
            p.get("name", ""), (p.get("add_to_cart") or {}).get("text", ""),
            " ".join(c.get("name", "") for c in p.get("categories", [])),
            " ".join(t.get("name", "") for t in p.get("tags", [])),
            re.sub("<[^>]+>", " ", p.get("short_description", ""))[:300]])
        backorder = bool(p.get("is_on_backorder"))
        pre = backorder or bool(PRE_RE.search(blob))
        out[p["permalink"]] = {
            "name": html.unescape(p["name"]), "price": f"{price:g} lei",
            "in_stock": bool(p.get("is_in_stock")) or backorder, "preorder": pre}
    return out


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def from_jsonld(soup, base):
    out = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for d in _walk(data):
            t = d.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                name, url = d.get("name"), d.get("url")
                if not name or not url:
                    continue
                offers = d.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                avail = str(offers.get("availability", "")).lower()
                pre = any(w in avail for w in ("preorder", "presale", "backorder"))
                oos = any(w in avail for w in ("outofstock", "soldout", "discontinued"))
                price = f'{offers.get("price", "?")} {offers.get("priceCurrency", "")}'.strip()
                out[urljoin(base, url)] = {"name": name, "price": price,
                                           "in_stock": pre or not oos, "preorder": pre}
    return out


def from_links(soup, base, site):
    """Linkuri de produs; starea (stoc / epuizat / precomanda) se citeste din cardul produsului.
    Urcam in arbore doar cat timp parintele contine UN SINGUR produs."""
    link_re = site.get("link_re")
    anchors = []
    for a in soup.find_all("a", href=True):
        name = a.get_text(" ", strip=True) or a.get("title", "")
        if len(name) < 8 or not wanted(name, site):
            continue
        if link_re and not re.search(link_re, a["href"]):
            continue
        anchors.append(a)
    hrefs = {urljoin(base, a["href"]) for a in anchors}
    cache = {}

    def distinct_in(node):
        key = id(node)
        if key not in cache:
            cache[key] = {urljoin(base, x["href"]) for x in node.find_all("a", href=True)} & hrefs
        return cache[key]

    out = {}
    for a in anchors:
        name = a.get_text(" ", strip=True) or a.get("title", "")
        card = a
        for _ in range(5):
            par = card.parent
            if par is None or len(distinct_in(par)) > 1:
                break
            card = par
        oos, pre = classify(card_text(card) + " " + name)
        url = urljoin(base, a["href"])
        prev = out.get(url)
        if prev and len(prev["name"]) > len(name):
            name = prev["name"]
        out[url] = {"name": name, "price": "?", "in_stock": not oos, "preorder": pre}
        if oos and pre:               # precomanda epuizata
            out[url]["in_stock"] = False
        elif pre:
            out[url]["in_stock"] = True
    return out


def scrape_html(site):
    urls = [site["url"]] + site.get("extra", [])
    found, jl = {}, {}
    for i, url in enumerate(urls):
        try:
            page = fetch_html(site, url)
        except Exception as e:
            if i == 0:
                raise
            continue                        # paginile extra pot sa nu existe
        soup = BeautifulSoup(page, "html.parser")
        if i == 0:
            site["_debug"] = (f"titlu pagina: {soup.title.string.strip() if soup.title and soup.title.string else None}"
                              f" | linkuri: {len(soup.find_all('a'))} | marime: {len(page)}")
        found.update(from_links(soup, url, site))
        jl.update(from_jsonld(soup, url))
        if i < len(urls) - 1:
            time.sleep(random.uniform(0.5, 1.5))
    for u, p in jl.items():                 # JSON-LD completeaza / corecteaza
        if u in found:
            f = found[u]
            pre = f["preorder"] or p["preorder"]
            f["preorder"] = pre
            f["in_stock"] = (f["in_stock"] and p["in_stock"]) or (pre and f["in_stock"])
            if f["price"] == "?":
                f["price"] = p["price"]
        elif not found:
            found[u] = p
    return found


def scrape(site):
    found = {}
    if site.get("shopify"):
        found = from_shopify(site)
    else:
        if site.get("woo"):
            try:
                found = {u: p for u, p in from_woo(site).items() if wanted(p["name"], site)}
            except Exception as e:
                print(f"[{site['name']}] API Woo indisponibil ({e}), incerc HTML")
        if not found:
            found = scrape_html(site)
    found = {u: p for u, p in found.items() if wanted(p["name"], site)}
    if site.get("preorder_page"):
        for p in found.values():
            p["preorder"] = True
    return found


# ------------------------- Logica -----------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def alert_kind(old, p):
    """Returneaza tipul alertei sau None."""
    if not p["in_stock"]:
        return None
    if old is None:
        return "new_pre" if p["preorder"] else "new"
    if not old.get("in_stock"):
        return "open_pre" if p["preorder"] else "restock"
    if old.get("preorder") and not p["preorder"]:
        return "released"
    return None


HEAD = {
    "new": "🆕 {s}: produs nou in stoc",
    "new_pre": "🕒 {s}: PRECOMANDA noua",
    "restock": "✅ {s}: REVENIT IN STOC",
    "open_pre": "🕒 {s}: PRECOMANDA DESCHISA (revenit)",
    "released": "📦 {s}: acum in stoc (nu mai e precomanda)",
}


def check_site(site, state):
    first = site["name"] not in state
    old = state.get(site["name"], {})
    try:
        new = scrape(site)
    except Exception as e:
        print(f"[{site['name']}] eroare: {e}")
        return
    if not new:
        print(f"[{site['name']}] 0 produse - verifica URL-ul / foloseste js:True")
        if site.get("_debug"):
            print(f"    diagnostic: {site['_debug']}")
        return
    sent = skipped = 0
    for url, p in new.items():
        kind = None if first else alert_kind(old.get(url), p)
        if not kind or url in ALERTED:
            continue
        ALERTED.add(url)
        if sent >= MAX_ALERTS_PER_SITE:
            skipped += 1
            continue
        msg = f"{HEAD[kind].format(s=site['name'])}\n{p['name']}\n💰 {p['price']}\n{url}"
        print(msg)
        send_whatsapp(msg)
        sent += 1
    if skipped:
        send_whatsapp(f"ℹ️ {site['name']}: inca {skipped} produse noi (verifica site-ul): {site['url']}")
    state[site["name"]] = new
    avail = sum(p["in_stock"] for p in new.values())
    pre = sum(p["in_stock"] and p["preorder"] for p in new.values())
    print(f"[{site['name']}] {len(new)} produse ({avail} disponibile, din care {pre} precomenzi)")


def test_mode():
    for s in SITES:
        print(f"\n=== {s['name']} ===")
        try:
            res = scrape(s)
        except Exception as e:
            print("  EROARE:", e)
            continue
        if not res:
            print("  0 produse gasite")
        for u, p in list(res.items())[:10]:
            icon = "🕒" if (p["in_stock"] and p["preorder"]) else ("✅" if p["in_stock"] else "❌")
            print(f"  {icon} {p['name'][:60]} | {p['price']}\n     {u}")


def run_once():
    state = load_state()
    for s in SITES:
        check_site(s, state)
        time.sleep(random.uniform(1, 3))
    save_state(state)


def main():
    state = load_state()
    send_whatsapp("🤖 Botul Pokemon TCG a pornit.")
    while True:
        ALERTED.clear()
        for s in SITES:
            check_site(s, state)
            time.sleep(random.uniform(2, 6))
        save_state(state)
        time.sleep(CHECK_EVERY + random.uniform(0, JITTER))


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_mode()
    elif "--once" in sys.argv:
        run_once()
    else:
        main()
