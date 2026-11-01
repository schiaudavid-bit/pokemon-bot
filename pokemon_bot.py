"""
Bot monitorizare Pokemon TCG (RO) -> notificari Telegram / WhatsApp

Comenzi:
  python pokemon_bot.py --test   # arata ce produse gaseste pe fiecare site (fara notificari)
  python pokemon_bot.py --once   # o singura verificare (pentru GitHub Actions)
  python pokemon_bot.py          # ruleaza non-stop (pe PC/server)
"""
import json, os, random, sys, time
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------
# SITE-URI. Optiuni per site:
#   "shopify": True  -> foloseste API-ul JSON al Shopify (cel mai sigur)
#   "require": [...] -> produsul trebuie sa contina unul din cuvinte (ex: "tcg")
#   "js": True       -> randare cu Playwright (doar daca --test da 0 produse)
# ---------------------------------------------------------------
SITES = [
    {"name": "Smyk",         "url": "https://smyk.ro/personaj/pokemon", "require": ["tcg"]},
    {"name": "Bebe Tei",     "url": "https://comenzi.bebetei.ro/brand/pokemon"},
    {"name": "Krit",         "url": "https://krit.ro/en/categorie/pokemon"},
    {"name": "Secret Cards", "url": "https://secretcards.ro/en"},  # DE VERIFICAT: pune aici pagina "Sealed Products"
    {"name": "LootShop",     "url": "https://lootshop.ro/product-category/pokemon-tcg/"},
    {"name": "Red Goblin",   "url": "https://redgoblin.ro/collections/pokemon-1", "shopify": True},
    {"name": "TCG Arena",    "url": "https://tcgarena.ro/collections/joc-de-carti-pokemon-tcg-romania", "shopify": True},
    {"name": "LexShop",      "url": "https://www.lexshop.ro/pokemon-tcg"},
    {"name": "Pokemania",    "url": "https://pokemania.ro/booster-packs"},
    {"name": "TCG Shop",     "url": "https://www.tcg-shop.ro/category/pokemon"},
]

KEYWORDS = ["pokemon", "pokémon"]
# accesorii / jucarii ignorate
EXCLUDE = ["plush", "plus ", "figurina", "figurină", "puzzle", "rucsac", "playmat",
           "portfolio", "sleeves", "deck box", "toploader", "album", "construit",
           "constructie", "construcție", "lego", "mega construx", "perna", "creioane"]
OUT_OF_STOCK = ["stoc epuizat", "indisponibil", "out of stock", "epuizat", "sold out",
                "nu este in stoc", "nu este în stoc", "outofstock", "soldout"]

CHECK_EVERY, JITTER = 300, 60
STATE_FILE = "state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
}


# ------------------------- Notificari -----------------------------
def send_whatsapp(text):
    """Trimite alerta pe TOATE canalele configurate (Telegram / WhatsApp)."""
    sent = False
    # --- Telegram (cel mai simplu si stabil) ---
    if os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        try:
            requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
                          timeout=20, data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
                                            "disable_web_page_preview": "false"}).raise_for_status()
            sent = True
        except Exception as e:
            print(f"[!] Eroare Telegram: {e}")
    # --- WhatsApp prin Green-API (cont gratuit, legi WhatsApp-ul tau prin QR) ---
    if os.getenv("GREENAPI_ID") and os.getenv("GREENAPI_TOKEN"):
        try:
            host = os.getenv("GREENAPI_HOST", "https://api.green-api.com").rstrip("/")
            requests.post(f"{host}/waInstance{os.environ['GREENAPI_ID']}/sendMessage/{os.environ['GREENAPI_TOKEN']}",
                          timeout=20, json={"chatId": os.environ["GREENAPI_CHAT"], "message": text}).raise_for_status()
            sent = True
        except Exception as e:
            print(f"[!] Eroare Green-API: {e}")
    # --- WhatsApp prin CallMeBot ---
    if os.getenv("CALLMEBOT_APIKEY"):
        try:
            requests.get("https://api.callmebot.com/whatsapp.php", timeout=20, params={
                "phone": os.environ["CALLMEBOT_PHONE"], "text": text,
                "apikey": os.environ["CALLMEBOT_APIKEY"]}).raise_for_status()
            sent = True
        except Exception as e:
            print(f"[!] Eroare CallMeBot: {e}")
    # --- WhatsApp prin Twilio ---
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


# ------------------------- Fetch -----------------------------
def fetch_html(site):
    if site.get("js"):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(site["url"], wait_until="networkidle", timeout=45000)
            html = page.content()
            b.close()
            return html
    r = requests.get(site["url"], headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def wanted(name, site):
    n = name.lower()
    if not any(k in n for k in KEYWORDS):
        return False
    if any(x in n for x in EXCLUDE):
        return False
    req = site.get("require")
    return not req or any(r in n for r in req)


# ------------------------- Extractie -----------------------------
def from_shopify(site):
    u = urlparse(site["url"])
    api = f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}/products.json?limit=250"
    r = requests.get(api, headers=HEADERS, timeout=25)
    r.raise_for_status()
    out = {}
    for p in r.json().get("products", []):
        variants = p.get("variants") or [{}]
        out[f"{u.scheme}://{u.netloc}/products/{p['handle']}"] = {
            "name": p["title"],
            "price": f"{variants[0].get('price', '?')} lei",
            "in_stock": any(v.get("available") for v in variants)}
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
                price = f'{offers.get("price", "?")} {offers.get("priceCurrency", "")}'.strip()
                out[urljoin(base, url)] = {"name": name, "price": price,
                                           "in_stock": ("instock" in avail) if avail else True}
    return out


def from_links(soup, base, site):
    """Plan B: linkuri cu 'pokemon' in text; stocul se deduce din cardul produsului.
    Urcam in arbore doar cat timp parintele contine UN SINGUR produs, ca sa nu
    amestecam textul 'stoc epuizat' de la produse vecine."""
    anchors = [a for a in soup.find_all("a", href=True)
               if len(a.get_text(" ", strip=True) or a.get("title", "")) >= 8
               and wanted(a.get_text(" ", strip=True) or a.get("title", ""), site)]
    out = {}
    for a in anchors:
        name = a.get_text(" ", strip=True) or a.get("title", "")
        card = a
        for _ in range(5):
            par = card.parent
            if par is None:
                break
            distinct = {x["href"] for x in anchors if x in par.descendants}
            if len(distinct) > 1:
                break
            card = par
        txt = card.get_text(" ", strip=True).lower()
        out[urljoin(base, a["href"])] = {
            "name": name, "price": "?",
            "in_stock": not any(w in txt for w in OUT_OF_STOCK)}
    return out


def scrape(site):
    if site.get("shopify"):
        found = from_shopify(site)
    else:
        soup = BeautifulSoup(fetch_html(site), "html.parser")
        found = from_links(soup, site["url"], site)
        jl = from_jsonld(soup, site["url"])
        # JSON-LD are stoc mai exact; il folosim peste link-uri cand exista
        for u, p in jl.items():
            if u in found:
                found[u].update(p)
            elif not found:
                found[u] = p
    return {u: p for u, p in found.items() if wanted(p["name"], site)}


# ------------------------- Logica -----------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


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
        return
    for url, p in new.items():
        if not p["in_stock"]:
            continue
        b = old.get(url)
        if b is None and not first:
            msg = f"🆕 {site['name']}: produs nou\n{p['name']}\n💰 {p['price']}\n{url}"
        elif b and not b["in_stock"]:
            msg = f"✅ {site['name']}: REVENIT IN STOC\n{p['name']}\n💰 {p['price']}\n{url}"
        else:
            continue
        print(msg)
        send_whatsapp(msg)
    state[site["name"]] = new
    print(f"[{site['name']}] {len(new)} produse ({sum(p['in_stock'] for p in new.values())} in stoc)")


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
        for u, p in list(res.items())[:8]:
            print(f"  {'✅' if p['in_stock'] else '❌'} {p['name'][:60]} | {p['price']}\n     {u}")


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
