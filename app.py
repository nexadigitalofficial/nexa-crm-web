import os
import time
import requests
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_file, request as flask_request
from bs4 import BeautifulSoup
from flask_cors import CORS
from wa_cloud import send_whatsapp, send_whatsapp_template, wa_status, verify_webhook_token

# ── Firebase Admin SDK ──────────────────────────────────────────
import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore, auth as fb_auth
from google.cloud.firestore_v1.base_query import FieldFilter

app = Flask(__name__)
CORS(app)

# ================================================================
# AYARLAR
# ================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8462430471:AAEM_AjKYLKKVFpBsxGDkNmN91H77XHS81g")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "6183709337")
SERVICE_ACCOUNT    = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "service-account.json")

# WhatsApp Cloud API — Meta
# WA_PHONE_NUMBER_ID : Meta Business 2192 WhatsApp 2192 Phone Number ID
# WA_ACCESS_TOKEN    : System User permanent token
# WA_ADVISOR_PHONE   : Danışmanın WA numarası (bildirim alacak)
WA_ADVISOR_PHONE   = os.environ.get("WA_ADVISOR_PHONE", "905324514008")


# İlan hedef URL
TARGET_URL = "https://www.cb.com.tr/ilanlar?officeid=372&officeuserid=18631"

# Ankara koordinatları (fallback)
ANKARA_LAT = 39.9334
ANKARA_LNG = 32.8597
DIKMEN_LAT = 39.8854
DIKMEN_LNG = 32.8514

ANKARA_SEMTLER = [
    "Dikmen", "Çukurambar", "Birlik Mahallesi", "Çayyolu",
    "Oran", "Angora Evleri", "Beysukent",
    "Kızılay", "Tunalı", "Ayrancı", "Gaziosmanpaşa", "GOP",
    "Kavaklidere", "Kavaklıdere", "Çankaya",
    "Balgat", "Emek", "Bahçelievler", "Öveçler",
    "Güvenevler", "Yıldız", "Çetin Emeç", "Mustafa Kemal",
    "Aziziye", "Naci Çakır",
    "Keçiören", "Mamak", "Altındağ", "Sincan",
    "Etimesgut", "Gölbaşı", "Pursaklar", "Yenimahalle",
    "Bağlıca", "Batıkent", "Eryaman",
]

# ================================================================
# FİREBASE ADMIN — başlatma
# ================================================================
_fb_initialized = False
db_admin = None

def init_firebase_admin():
    global _fb_initialized, db_admin
    if _fb_initialized:
        return
    try:
        if os.path.exists(SERVICE_ACCOUNT):
            cred = credentials.Certificate(SERVICE_ACCOUNT)
            firebase_admin.initialize_app(cred)
            db_admin = admin_firestore.client()
            _fb_initialized = True
            print("✅ Firebase Admin bağlandı")
        else:
            print(f"⚠️  {SERVICE_ACCOUNT} bulunamadı — Telegram hatırlatmaları devre dışı")
    except Exception as e:
        print(f"❌ Firebase Admin hatası: {e}")


# ================================================================
# TELEGRAM
# ================================================================
def send_telegram(text: str) -> bool:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        return resp.ok
    except Exception as e:
        print(f"Telegram gönderim hatası: {e}")
        return False


# ================================================================
# SAYFA ROUTE'LARI
# ================================================================

@app.route("/")
def home():
    """Web sitesi — site.html"""
    try:
        return send_file("site.html")
    except Exception as e:
        return f"site.html bulunamadı: {e}", 404


@app.route("/crm")
def crm():
    """CRM paneli — crm.html"""
    try:
        return send_file("crm.html")
    except Exception as e:
        return f"crm.html bulunamadı: {e}", 404


# ================================================================
# API — İLAN SCRAPER
# ================================================================

_coord_cache: dict = {}
_last_nominatim_call: float = 0.0
_TR_MAP = str.maketrans("çğışöüÇĞİŞÖÜ", "cgisouCGISOu")


def _normalize(text: str) -> str:
    return text.translate(_TR_MAP).upper()


def geocode_query(query: str):
    global _last_nominatim_call
    if query in _coord_cache:
        return _coord_cache[query]
    elapsed = time.time() - _last_nominatim_call
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1,
                    "countrycodes": "tr",
                    "viewbox": "32.5,40.1,33.2,39.6", "bounded": 1},
            headers={"User-Agent": "DikmenEliteGayrimenkul/1.0 (erdogan@cb.com.tr)"},
            timeout=8,
        )
        _last_nominatim_call = time.time()
        data = resp.json()
        if data:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
            _coord_cache[query] = (lat, lon)
            return lat, lon
    except Exception as e:
        print(f"Geocode hatası: {e}")
    _coord_cache[query] = None
    return None


def extract_location_from_title(title: str):
    title_norm = _normalize(title)
    matches = [s for s in ANKARA_SEMTLER if _normalize(s) in title_norm]
    if not matches:
        return None
    return f"{max(matches, key=len)}, Ankara, Türkiye"


def get_listing_coords(title: str, loc: str):
    query = extract_location_from_title(title)
    if query:
        coords = geocode_query(query)
        if coords:
            return coords
    if loc and loc != "Ankara":
        coords = geocode_query(f"{loc}, Ankara, Türkiye")
        if coords:
            return coords
    coords = geocode_query("Dikmen, Çankaya, Ankara, Türkiye")
    return coords or (DIKMEN_LAT, DIKMEN_LNG)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}


def clean_text(element) -> str:
    return element.get_text(strip=True) if element else ""


def fetch_real_estate_data() -> list:
    print(f"📡 İstek gönderiliyor: {TARGET_URL}")
    try:
        response = requests.get(TARGET_URL, headers=HEADERS, timeout=20)
        if response.status_code != 200:
            print(f"❌ Bağlantı Hatası: {response.status_code}")
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        listings = []
        cards = soup.select(".cb-list-item")
        print(f"🔎 Bulunan İlan Sayısı: {len(cards)}")

        for card in cards:
            try:
                title_el = card.select_one(".cb-list-item-info h2")
                title = clean_text(title_el)
                if not title:
                    continue

                price_el = card.select_one(".feature-item .text-primary")
                price = clean_text(price_el)

                link_el = card.select_one(".cb-list-img-container a")
                link = link_el["href"] if link_el else "#"
                if link and not link.startswith("http"):
                    link = "https://www.cb.com.tr" + link

                img_el = card.select_one(".cb-list-img-container img")
                img_url = "https://via.placeholder.com/400x300"
                if img_el:
                    img_url = img_el.get("src") or img_el.get("data-src") or img_url

                region_el = card.select_one('span[itemprop="addressRegion"]')
                street_el = card.select_one('span[itemprop="streetAddress"]')
                region = clean_text(region_el)
                street = clean_text(street_el)
                loc = f"{region}, {street}" if region and street else "Ankara"

                rooms = area = ""
                for feat in card.select(".feature-item"):
                    text = clean_text(feat)
                    if "m2" in text or "m²" in text:
                        area = text
                    elif "+" in text:
                        rooms = text

                lat, lng = get_listing_coords(title, loc)
                listings.append({
                    "title": title, "price": price, "loc": loc,
                    "img": img_url, "link": link, "rooms": rooms, "area": area,
                    "type": "Kiralık" if "Kiralık" in title else "Satılık",
                    "lat": lat, "lng": lng,
                })
            except Exception as e:
                print(f"⚠️ İlan parse hatası: {e}")
                continue

        print(f"✅ Toplam işlenen: {len(listings)} ilan")
        return listings
    except Exception as e:
        print(f"❌ Kritik Hata: {e}")
        return []


@app.route("/admin")
def admin():
    """Admin paneli — admin.html"""
    try:
        return send_file("admin.html")
    except Exception as e:
        return f"admin.html bulunamadı: {e}", 404


# ================================================================
# ================================================================
# ADMIN AUTH  — Firebase ID Token doğrulaması
# ================================================================

def _require_admin():
    """
    Firebase JS SDK'dan gelen idToken'ı doğrular.
    Başarılıysa (decoded_token, None), başarısızsa (None, hata_mesajı) döner.
    """
    if not _fb_initialized:
        print("⚠️  _require_admin: Firebase başlatılmamış")
        return None, "Firebase bağlı değil"
    auth_header = flask_request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        print(f"⚠️  _require_admin: Token başlığı eksik — {flask_request.path}")
        return None, "Token eksik"
    id_token = auth_header[7:]
    if not id_token or len(id_token) < 20:
        return None, "Token geçersiz (çok kısa)"
    try:
        decoded = fb_auth.verify_id_token(id_token)
        print(f"✅ Admin doğrulandı: {decoded.get('email','?')} — {flask_request.path}")
        return decoded, None
    except fb_auth.ExpiredIdTokenError:
        print("⚠️  _require_admin: Token süresi dolmuş")
        return None, "Oturum süresi doldu"
    except fb_auth.InvalidIdTokenError as e:
        print(f"⚠️  _require_admin: Geçersiz token — {e}")
        return None, "Geçersiz token"
    except Exception as e:
        print(f"❌ _require_admin beklenmedik hata: {type(e).__name__}: {e}")
        return None, f"Doğrulama hatası: {type(e).__name__}"


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    # Client tarafında token silindiği için backend'de yapılacak bir şey yok
    return jsonify({"ok": True})


# ================================================================
# WHATSAPP CLOUD API ROUTES
# ================================================================

@app.route("/api/wa/status", methods=["GET"])
def whatsapp_status():
    """Meta Graph API üzerinden WA phone number durumunu kontrol eder."""
    return jsonify(wa_status())


@app.route("/api/wa/webhook", methods=["GET"])
def whatsapp_webhook_verify():
    """
    Meta webhook doğrulaması (GET).
    Meta Business → WhatsApp → Configuration → Webhook URL olarak kaydedin.
    Verify Token: WA_VERIFY_TOKEN env variable ile eşleşmeli.
    """
    mode      = flask_request.args.get("hub.mode")
    token     = flask_request.args.get("hub.verify_token")
    challenge = flask_request.args.get("hub.challenge")

    if mode == "subscribe" and verify_webhook_token(token):
        print("✅ WhatsApp webhook doğrulandı")
        return challenge, 200

    print(f"❌ Webhook doğrulama başarısız. Token: {token}")
    return "Forbidden", 403


@app.route("/api/wa/webhook", methods=["POST"])
def whatsapp_webhook_receive():
    """
    Meta'dan gelen mesaj/durum bildirimlerini alır (POST).
    Gelen mesajları Firestore wa_inbound koleksiyonuna kaydeder.
    """
    data = flask_request.get_json(silent=True) or {}

    try:
        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Gelen mesajlar
                for msg in value.get("messages", []):
                    from_phone = msg.get("from", "")
                    msg_type   = msg.get("type", "")
                    body       = msg.get("text", {}).get("body", "") if msg_type == "text" else f"[{msg_type}]"
                    timestamp  = msg.get("timestamp", "")
                    print(f"📥 WA gelen mesaj: {from_phone} → {body[:80]}")

                    if _fb_initialized:
                        db_admin.collection("wa_inbound").add({
                            "from":      from_phone,
                            "type":      msg_type,
                            "body":      body,
                            "timestamp": timestamp,
                            "raw":       msg,
                            "receivedAt": datetime.now(timezone.utc).isoformat(),
                        })

                # Mesaj durum güncellemeleri (sent/delivered/read/failed)
                for status in value.get("statuses", []):
                    msg_id     = status.get("id", "")
                    wa_status_ = status.get("status", "")
                    recipient  = status.get("recipient_id", "")
                    print(f"📊 WA durum: {msg_id} → {wa_status_} ({recipient})")

                    if _fb_initialized and msg_id:
                        # wa_message_log'daki kaydı güncelle
                        docs = (db_admin.collection("wa_message_log")
                                .where("messageId", "==", msg_id).limit(1).stream())
                        for doc in docs:
                            doc.reference.update({
                                "deliveryStatus": wa_status_,
                                "statusUpdatedAt": datetime.now(timezone.utc).isoformat(),
                            })

    except Exception as e:
        print(f"Webhook işleme hatası: {e}")

    # Meta her zaman 200 bekler
    return jsonify({"status": "ok"}), 200


@app.route("/api/wa/send", methods=["POST"])
def whatsapp_send():
    """
    Admin panelinden manuel WA mesajı göndermek için.
    Body: { phone: "905324514008", message: "..." }
    Korumalı endpoint — Firebase ID token gerektirir.
    """
    token, err = _require_admin()
    if err:
        return jsonify({"ok": False, "error": err}), 401

    body    = flask_request.get_json(silent=True) or {}
    phone   = body.get("phone", "")
    message = body.get("message", "")

    if not phone or not message:
        return jsonify({"ok": False, "error": "phone ve message zorunlu"}), 400

    result = send_whatsapp(phone, message)

    if result["ok"] and _fb_initialized:
        db_admin.collection("wa_message_log").add({
            "phone":     phone,
            "message":   message[:200],
            "messageId": result.get("message_id", ""),
            "source":    "admin_manual",
            "status":    "sent",
            "sentAt":    datetime.now(timezone.utc).isoformat(),
        })

    return jsonify(result)


# ================================================================
# BLOG API
# ================================================================

def _serialize_post(doc):
    """Firestore dokümanını JSON-safe dict'e çevirir."""
    d = doc.to_dict()
    d["id"] = doc.id
    for field in ["createdAt", "updatedAt"]:
        val = d.get(field)
        if val is None:
            d[field] = ""
        elif hasattr(val, "isoformat"):
            try:
                d[field] = val.isoformat()
            except Exception:
                d[field] = str(val)
        else:
            d[field] = str(val)
    return d


@app.route("/api/blog/posts", methods=["GET"])
def get_blog_posts():
    """Herkese açık — site.html buradan çeker."""
    if not _fb_initialized:
        return jsonify({"ok": False, "data": []}), 503
    try:
        query = (db_admin.collection("blogs")
                 .where(filter=FieldFilter("published", "==", True))
                 .limit(24))
        posts = [_serialize_post(doc) for doc in query.stream()]
        posts.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return jsonify({"ok": True, "data": posts})
    except Exception as e:
        print(f"get_blog_posts hatası: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blog/all", methods=["GET"])
def get_all_blog_posts():
    """Admin paneli için — tüm yazılar."""
    token, err = _require_admin()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    if not _fb_initialized:
        return jsonify({"ok": False, "data": []}), 503
    try:
        posts = [_serialize_post(doc) for doc in db_admin.collection("blogs").stream()]
        posts.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return jsonify({"ok": True, "data": posts})
    except Exception as e:
        print(f"get_all_blog_posts hatası: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blog/posts", methods=["POST"])
def create_blog_post():
    token, err = _require_admin()
    if err:
        print(f"❌ Auth hatası: {err}")
        return jsonify({"ok": False, "error": err}), 401
    if not _fb_initialized:
        return jsonify({"ok": False, "error": "Firebase bağlı değil"}), 503

    data = flask_request.json or {}
    print(f"📝 Blog oluşturma isteği: {data.get('title', '(başlıksız)')}")

    now  = datetime.now(timezone.utc)
    post = {
        "title":     data.get("title", "").strip(),
        "summary":   data.get("summary", "").strip(),
        "content":   data.get("content", "").strip(),
        "image":     data.get("image", "").strip(),
        "category":  data.get("category", "Genel").strip(),
        "readTime":  data.get("readTime", "3 dk").strip(),
        "published": bool(data.get("published", True)),
        "createdAt": now,
        "updatedAt": now,
    }
    if not post["title"]:
        return jsonify({"ok": False, "error": "Başlık zorunlu"}), 400

    try:
        result = db_admin.collection("blogs").add(post)
        # result → (DatetimeWithNanoseconds, DocumentReference)
        doc_ref = result[1] if isinstance(result, tuple) else result
        doc_id  = doc_ref.id
        print(f"✅ Blog oluşturuldu: {doc_id}")
        return jsonify({"ok": True, "id": doc_id})
    except Exception as e:
        import traceback
        print(f"❌ create_blog_post hatası: {e}")
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blog/posts/<post_id>", methods=["PUT"])
def update_blog_post(post_id):
    token, err = _require_admin()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    if not _fb_initialized:
        return jsonify({"ok": False, "error": "Firebase bağlı değil"}), 503
    data    = flask_request.json or {}
    allowed = ["title", "summary", "content", "image", "category", "readTime", "published"]
    update  = {k: data[k] for k in allowed if k in data}
    update["updatedAt"] = datetime.now(timezone.utc)
    try:
        db_admin.collection("blogs").document(post_id).update(update)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/blog/posts/<post_id>", methods=["DELETE"])
def delete_blog_post(post_id):
    token, err = _require_admin()
    if err:
        return jsonify({"ok": False, "error": err}), 401
    if not _fb_initialized:
        return jsonify({"ok": False, "error": "Firebase bağlı değil"}), 503
    try:
        db_admin.collection("blogs").document(post_id).delete()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ================================================================
# API — İLAN SCRAPER
# ================================================================

@app.route("/api/listings", methods=["GET"])
def get_listings():
    data = fetch_real_estate_data()
    return jsonify({"success": True, "data": data})


# ── CB İlan Detay Önizleme ────────────────────────────────────────────────────
@app.route("/api/listing/preview", methods=["GET"])
def listing_preview():
    """
    a.py / scrape_detail() mantığıyla CB ilan detay sayfasını scrape eder.
    Query : ?url=https://www.cb.com.tr/...
    Return: {ok, title, price, location, rooms, sqm, type, status,
             cb_url, images:[str,...], features:[{label,value},...],
             description, agent:{name,img,office}}
    """
    import re as _re
    from urllib.parse import urlparse

    BASE = "https://www.cb.com.tr"

    cb_url = flask_request.args.get("url", "").strip()
    parsed = urlparse(cb_url)
    if parsed.scheme not in ("http", "https") or \
       parsed.netloc not in ("www.cb.com.tr", "cb.com.tr"):
        return jsonify({"ok": False, "error": "Sadece cb.com.tr URL desteklenir"}), 400

    try:
        resp = requests.get(cb_url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return jsonify({"ok": False, "error": f"HTTP {resp.status_code}"}), 502

        soup = BeautifulSoup(resp.content, "lxml" if __import__("importlib").util.find_spec("lxml") else "html.parser")

        # ── Başlık ──────────────────────────────────────────────────────────
        title = clean_text(soup.select_one("h1") or soup.select_one("h2")) or "İlan Detayı"

        # ── Fiyat ───────────────────────────────────────────────────────────
        price = ""
        for sel in [".feature-item .text-primary",
                    ".price-box .price",
                    "[class*='price']",
                    ".cb-detail-header .price"]:
            el = soup.select_one(sel)
            if el:
                price = _re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
                if price:
                    break
        if not price:
            for row in soup.select("table tr"):
                cells = row.find_all("td")
                if len(cells) >= 2 and "Fiyat" in cells[0].get_text():
                    price = clean_text(cells[1])
                    break

        # ── Lokasyon ────────────────────────────────────────────────────────
        location = ""
        hdr = soup.select_one(".cb-detail-header")
        if hdr:
            parts = [clean_text(s) for s in hdr.select("p .text-secondary") if clean_text(s)]
            location = " / ".join(parts)
        if not location:
            r_el = soup.select_one('[itemprop="addressRegion"]')
            s_el = soup.select_one('[itemprop="streetAddress"]')
            location = " / ".join(clean_text(e) for e in [r_el, s_el] if e and clean_text(e))

        # ── İlan tipi / durumu ──────────────────────────────────────────────
        url_l = cb_url.lower()
        status = "Kiralık" if "kiralik" in url_l else "Satılık"
        path_parts = cb_url.rstrip("/").split("/")
        prop_type = path_parts[-2].replace("-", " ").title() if len(path_parts) >= 2 else "—"
        badge = soup.select_one(".price-box .badge")
        if badge:
            status = clean_text(badge)

        # ── Görseller — a.py scrape_detail() mantığı ────────────────────────
        images = []
        seen_srcs = set()

        def _add_img(src):
            src = src.strip()
            if not src or "placeholder" in src or "icon" in src.lower():
                return
            if src.startswith("/"):
                src = BASE + src
            # Thumbnail URL'lerini yüksek çözünürlüklü versiyona yükselt
            # CB formatı: _410X261.jpg → _1000X664.jpg
            import re as _rx
            src_hires = _rx.sub(r'_\d+X\d+(\.[a-z]+)$', r'_1000X664\1', src, flags=_rx.IGNORECASE)
            # Görsel zaten listede mi? Dosya adını karşılaştır
            fname = src_hires.split("/")[-1].split("_")[0]
            if fname in seen_srcs:
                return
            seen_srcs.add(fname)
            images.append(src_hires)

        # 1) Bilinen slider seçicileri (öncelik sırasıyla)
        for sel in [
            "#cb-item-gallery .carousel-item img",
            "div.swiper-slide img",
            "div.slick-slide img",
            ".detail-slider img",
            ".stock-slider img",
            ".cb-detail-slider img",
            "figure img",
        ]:
            found = soup.select(sel)
            if found:
                for img in found:
                    src = (img.get("src") or img.get("data-src") or
                           img.get("data-lazy") or "").strip()
                    if src:
                        _add_img(src)
                if images:
                    break

        # 2) Slider bulunamazsa: media.cb / StockMedia img'leri
        if not images:
            for img in soup.find_all("img"):
                src = (img.get("src") or img.get("data-src") or "").strip()
                if "media.cb" in src or "StockMedia" in src:
                    _add_img(src)

        # 3) og:image fallback
        if not images:
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                images.append(og["content"])

        # ── Özellik tablosu — a.py gibi çoklu yöntem ────────────────────────
        feats = []
        seen = set()
        SKIP = {"portföy no", "portföy kategorisi"}

        # a) Tablo satırları
        for row in soup.select("table tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                k = clean_text(cells[0]).rstrip(":").strip()
                v = clean_text(cells[1]).strip()
                if k and v and len(k) < 50 and k.lower() not in seen and k.lower() not in SKIP:
                    feats.append({"label": k, "value": v})
                    seen.add(k.lower())

        # b) dt / dd çiftleri
        for dt, dd in zip(soup.find_all("dt"), soup.find_all("dd")):
            k, v = clean_text(dt), clean_text(dd)
            if k and v and k.lower() not in seen:
                feats.append({"label": k, "value": v})
                seen.add(k.lower())

        # c) cb-checkbox-list özellik kartları (İç / Dış özellikler)
        for card in soup.select(".card.no-radius"):
            sec_el = card.select_one(".card-header h3")
            sec = clean_text(sec_el) if sec_el else "Özellik"
            for li in card.select(".cb-checkbox-list .property"):
                b_el = li.select_one("b")
                k = clean_text(b_el).rstrip(":") if b_el else ""
                if b_el:
                    b_el.extract()
                v = li.get_text(strip=True)
                combined = (k + " " + v).strip().rstrip(":") if k else v
                if combined and combined.lower() not in seen:
                    feats.append({"label": k if k else sec, "value": v if k else combined})
                    seen.add(combined.lower())

        # d) li içinde ":" olan feature satırları
        for li in soup.select("ul.features li, .property-features li, .cb-features li"):
            txt = clean_text(li)
            if ":" in txt and len(txt) < 80:
                parts = txt.split(":", 1)
                k, v = parts[0].strip(), parts[1].strip()
                if k and v and k.lower() not in seen:
                    feats.append({"label": k, "value": v})
                    seen.add(k.lower())

        feats = feats[:20]

        # ── Oda / m² ────────────────────────────────────────────────────────
        rooms = sqm = ""
        for f in feats:
            lbl = f["label"].lower()
            if not rooms and ("oda" in lbl or "room" in lbl):
                rooms = f["value"]
            if not sqm and ("m²" in lbl or "m2" in lbl or "alan" in lbl
                            or "brüt" in lbl or "metre" in lbl):
                sqm = f["value"]

        # .feature-item (header'daki hızlı bilgiler)
        for fi in soup.select(".cb-detail-header .features .feature-item, .feature-item"):
            txt = clean_text(fi)
            if not rooms and "+" in txt:
                rooms = txt
            if not sqm and "m" in txt.lower() and any(c.isdigit() for c in txt):
                sqm = txt

        # Regex fallback
        page_text = soup.get_text(" ", strip=True)
        if not rooms:
            m = _re.search(r"(\d+\+\d+|\d+\+0)", page_text)
            if m:
                rooms = m.group(1)
        if not sqm:
            m = _re.search(r"(\d+)\s*m[²2]", page_text)
            if m:
                sqm = m.group(1) + " m²"

        # ── Açıklama ────────────────────────────────────────────────────────
        description = ""
        for sel in [".description", ".ilan-aciklama", ".detail-description",
                    "#aciklama", "[itemprop='description']", ".cb-detail-content p"]:
            el = soup.select_one(sel)
            if el:
                description = el.get_text(" ", strip=True)[:600]
                break

        # ── Danışman ────────────────────────────────────────────────────────
        agent = {
            "name":   "Erdoğan Işık",
            "img":    "https://media.cb.com.tr/OfficeUserImages/3830/ERDOgAN-IsIK_HTKB8N5P81_75X75.jpg",
            "office": "CB Çizgi",
        }

        a_link = soup.select_one("a[href*='/danismanlar/']")
        if a_link:
            agent["name"] = clean_text(a_link)

        pro = soup.select_one(".cb-professional")
        if pro:
            n_el = pro.select_one("h4") or pro.select_one(".name")
            if n_el:
                agent["name"] = clean_text(n_el)

        img_el = (soup.select_one("img[src*='OfficeUser']") or
                  (pro.select_one("img") if pro else None))
        if img_el:
            src = img_el.get("src", "")
            agent["img"] = BASE + src if src.startswith("/") else src

        off_link = soup.select_one("a[href*='/ofisler/']")
        if off_link:
            agent["office"] = clean_text(off_link)

        return jsonify({
            "ok":          True,
            "title":       title,
            "price":       price,
            "location":    location,
            "rooms":       rooms,
            "sqm":         sqm,
            "type":        prop_type,
            "status":      status,
            "cb_url":      cb_url,
            "images":      images,
            "features":    feats,
            "description": description,
            "agent":       agent,
        })

    except Exception as e:
        print(f"❌ listing/preview hatası: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ================================================================
# API — CRM / TELEGRAM / FOLLOWUP
# ================================================================

@app.route("/api/telegram/notify", methods=["POST"])
def telegram_notify():
    """Lead kaydedilince anında Telegram bildirimi."""
    data = flask_request.json or {}
    name     = data.get("name", "İsimsiz")
    phone    = data.get("phone", "-")
    email    = data.get("email", "-")
    source   = data.get("source", "CRM")
    msg_     = data.get("message", "")
    stage    = data.get("stage", "")
    category = data.get("category", "")

    text = (
        f"🔔 <b>Yeni Lead!</b>\n\n"
        f"👤 <b>{name}</b>\n"
        f"📞 {phone}\n"
        f"📧 {email}\n"
        f"🌐 Kaynak: {source}\n"
        + (f"📂 Kategori: {category}\n" if category else "")
        + (f"📊 Aşama: {stage}\n" if stage else "")
        + (f"💬 {msg_}\n" if msg_ else "")
        + f"\n⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    ok = send_telegram(text)
    return jsonify({"ok": ok})



# ================================================================
# LEAD STATE MACHINE
# ================================================================

LEAD_STAGES = [
    "new_lead",        # Form gönderildi, henüz işlem yok
    "report_sent",     # Otomatik rapor gönderildi
    "contacted",       # Danışman ilk teması kurdu
    "appointment",     # Randevu alındı
    "closed_won",      # Anlaşma yapıldı
    "closed_lost",     # Lead kaybedildi
]


def _log_lead_event(lead_id: str, event_type: str, payload: dict):
    """Lead event timeline'a kayıt yazar."""
    if not _fb_initialized:
        return
    try:
        db_admin.collection("leads").document(lead_id).collection("events").add({
            "type":      event_type,
            "payload":   payload,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        print(f"_log_lead_event hatası: {e}")


def _write_notification_log(lead_id: str, channel: str, status: str, detail: str = ""):
    """Bildirim gönderim logunu notifications koleksiyonuna yazar."""
    if not _fb_initialized:
        return
    try:
        db_admin.collection("notifications").add({
            "leadId":    lead_id,
            "channel":   channel,
            "status":    status,
            "detail":    detail,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        print(f"_write_notification_log hatası: {e}")


def _send_with_retry(fn, *args, retries=3, delay=2, **kwargs):
    """Fonksiyonu retries kez dener. (True, None) veya (False, hata) döner."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            result = fn(*args, **kwargs)
            if result:
                return True, None
        except Exception as e:
            last_err = e
        if attempt < retries:
            time.sleep(delay)
    return False, str(last_err or "Bilinmeyen hata")


@app.route("/api/lead/state", methods=["POST"])
def update_lead_state():
    """Lead aşamasını günceller ve event log'a yazar."""
    if not _fb_initialized:
        return jsonify({"ok": False, "error": "Firebase bağlı değil"}), 503

    data      = flask_request.json or {}
    lead_id   = data.get("leadId")
    new_stage = data.get("newStage")

    if not lead_id or not new_stage:
        return jsonify({"ok": False, "error": "leadId ve newStage zorunlu"}), 400
    if new_stage not in LEAD_STAGES:
        return jsonify({"ok": False, "error": f"Geçersiz stage. Geçerliler: {LEAD_STAGES}"}), 400

    try:
        ref = db_admin.collection("leads").document(lead_id)
        doc = ref.get()
        if not doc.exists:
            return jsonify({"ok": False, "error": "Lead bulunamadı"}), 404

        old_stage = doc.to_dict().get("status", "")
        now_iso   = datetime.now(timezone.utc).isoformat()

        ref.update({
            "status":         new_stage,
            "stageChangedAt": now_iso,
            "updatedAt":      now_iso,
        })
        _log_lead_event(lead_id, "stage_change", {
            "from":  old_stage,
            "to":    new_stage,
            "actor": data.get("actorEmail", "system"),
            "note":  data.get("note", ""),
        })
        print(f"✅ Lead aşaması güncellendi: {lead_id} → {new_stage}")
        return jsonify({"ok": True, "leadId": lead_id, "newStage": new_stage})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lead/report", methods=["POST"])
def send_lead_report():
    """
    Form gönderiminden hemen sonra tetiklenir.
    Telegram üzerinden danışmana otomatik rapor gönderir, retry mekanizması içerir.
    Body: { leadId, name, phone, email?, neighborhood?, property_type?, notes? }
    """
    data    = flask_request.json or {}
    lead_id = data.get("leadId", "")
    name    = data.get("name", "İsimsiz")
    phone   = data.get("phone", "-")
    email   = data.get("email", "")
    neigh   = data.get("neighborhood", "")
    ptype   = data.get("property_type", "")
    notes   = data.get("notes", "")

    result = {"ok": True, "channels": {}}

    advisor_msg = (
        f"📋 <b>Yeni Değerleme Talebi!</b>\n\n"
        f"👤 <b>{name}</b>\n"
        f"📞 {phone}\n"
        + (f"📧 {email}\n" if email else "")
        + (f"📍 Mahalle: {neigh}\n" if neigh else "")
        + (f"🏠 Mülk Tipi: {ptype}\n" if ptype else "")
        + (f"💬 Not: {notes}\n" if notes else "")
        + f"\n⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
        f"🔗 Lead ID: <code>{lead_id}</code>"
    )

    ok_tg, err_tg = _send_with_retry(send_telegram, advisor_msg)
    result["channels"]["telegram"] = "sent" if ok_tg else f"failed: {err_tg}"

    # ── WhatsApp Cloud API bildirimi ─────────────────────────────
    wa_msg = (
        f"📋 *Yeni Değerleme Talebi!*\n\n"
        f"👤 *{name}*\n"
        f"📞 {phone}\n"
        + (f"📧 {email}\n" if email else "")
        + (f"📍 Mahalle: {neigh}\n" if neigh else "")
        + (f"🏠 Mülk Tipi: {ptype}\n" if ptype else "")
        + (f"💬 Not: {notes}\n" if notes else "")
        + f"\n⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
        f"🔗 Lead: {lead_id}"
    )
    wa_result = send_whatsapp(WA_ADVISOR_PHONE, wa_msg)
    result["channels"]["whatsapp"] = "sent" if wa_result["ok"] else f"skipped: {wa_result.get('error','')}"

    if _fb_initialized and lead_id:
        _write_notification_log(lead_id, "telegram",  "sent" if ok_tg    else "failed",  err_tg or "")
        _write_notification_log(lead_id, "whatsapp",  "sent" if wa_result["ok"] else "skipped", wa_result.get("error",""))
        if ok_tg or wa_result["ok"]:
            try:
                now_iso = datetime.now(timezone.utc).isoformat()
                db_admin.collection("leads").document(lead_id).update({
                    "status":         "report_sent",
                    "reportSentAt":   now_iso,
                    "stageChangedAt": now_iso,
                    "updatedAt":      now_iso,
                })
                _log_lead_event(lead_id, "stage_change", {
                    "from":  "new_lead",
                    "to":    "report_sent",
                    "actor": "system",
                    "note":  "Otomatik rapor gönderildi (Telegram + WhatsApp Cloud API)",
                })
            except Exception as e:
                print(f"Lead güncelleme hatası: {e}")

    if not ok_tg and not wa_result["ok"]:
        print(f"❌ Rapor hiçbir kanaldan gönderilemedi! Lead: {lead_id}")
        result["ok"] = False

    return jsonify(result)


@app.route("/api/lead/events/<lead_id>", methods=["GET"])
def get_lead_events(lead_id):
    """Lead'e ait tüm event timeline'ını döner."""
    if not _fb_initialized:
        return jsonify({"ok": False, "data": []}), 503
    try:
        events = []
        for doc in (db_admin.collection("leads").document(lead_id)
                    .collection("events").order_by("createdAt").stream()):
            d = doc.to_dict()
            d["id"] = doc.id
            events.append(d)
        return jsonify({"ok": True, "data": events})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/lead/stages", methods=["GET"])
def get_lead_stages():
    """Geçerli stage listesini döner (frontend için)."""
    return jsonify({"ok": True, "stages": LEAD_STAGES})

@app.route("/api/followup/schedule", methods=["POST"])
def schedule_followup():
    if not _fb_initialized:
        return jsonify({"ok": False, "error": "Firebase bağlı değil"}), 503

    data = flask_request.json or {}
    uid = data.get("uid")
    if not uid:
        return jsonify({"ok": False, "error": "uid gerekli"}), 400

    now = datetime.now(timezone.utc)
    followup_data = {
        "contactId":    data.get("contactId", ""),
        "contactName":  data.get("contactName", ""),
        "contactPhone": data.get("contactPhone", ""),
        "contactEmail": data.get("contactEmail", ""),
        "notes": {
            "week1": data.get("notes", {}).get("week1", "1. hafta takip görüşmesi"),
            "week2": data.get("notes", {}).get("week2", "2. hafta durum değerlendirmesi"),
            "week3": data.get("notes", {}).get("week3", "3. hafta kapanış fırsatı"),
        },
        "startDate":  now.isoformat(),
        "week1Date":  (now + timedelta(days=7)).isoformat(),
        "week2Date":  (now + timedelta(days=14)).isoformat(),
        "week3Date":  (now + timedelta(days=21)).isoformat(),
        "sent":  {"week1": False, "week2": False, "week3": False},
        "done":      False,
        "createdAt": now.isoformat()
    }

    try:
        ref = (db_admin.collection("users").document(uid)
               .collection("followups").add(followup_data))
        doc_id = ref[1].id

        name = followup_data["contactName"]
        text = (
            f"🚀 <b>Takip Planı Başlatıldı!</b>\n\n"
            f"👤 <b>{name}</b>\n"
            f"📞 {followup_data['contactPhone']}\n\n"
            f"📅 <b>Takvim:</b>\n"
            f"  • 1. Hafta: {(now + timedelta(days=7)).strftime('%d.%m.%Y')} → {followup_data['notes']['week1']}\n"
            f"  • 2. Hafta: {(now + timedelta(days=14)).strftime('%d.%m.%Y')} → {followup_data['notes']['week2']}\n"
            f"  • 3. Hafta: {(now + timedelta(days=21)).strftime('%d.%m.%Y')} → {followup_data['notes']['week3']}\n"
            f"\n⏰ {now.strftime('%d.%m.%Y %H:%M')}"
        )
        send_telegram(text)
        return jsonify({"ok": True, "id": doc_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/followup/update", methods=["POST"])
def update_followup():
    if not _fb_initialized:
        return jsonify({"ok": False, "error": "Firebase bağlı değil"}), 503

    data = flask_request.json or {}
    uid         = data.get("uid")
    followup_id = data.get("followupId")
    notes       = data.get("notes", {})

    if not uid or not followup_id:
        return jsonify({"ok": False, "error": "uid ve followupId gerekli"}), 400

    try:
        ref = (db_admin.collection("users").document(uid)
               .collection("followups").document(followup_id))
        update_data = {}
        for week in ["week1", "week2", "week3"]:
            if week in notes:
                update_data[f"notes.{week}"] = notes[week]
        update_data["updatedAt"] = datetime.now(timezone.utc).isoformat()
        ref.update(update_data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/followup/cancel", methods=["POST"])
def cancel_followup():
    if not _fb_initialized:
        return jsonify({"ok": False, "error": "Firebase bağlı değil"}), 503

    data        = flask_request.json or {}
    uid         = data.get("uid")
    followup_id = data.get("followupId")

    if not uid or not followup_id:
        return jsonify({"ok": False, "error": "uid ve followupId gerekli"}), 400

    try:
        ref = (db_admin.collection("users").document(uid)
               .collection("followups").document(followup_id))
        ref.update({"done": True, "cancelledAt": datetime.now(timezone.utc).isoformat()})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/followup/list", methods=["POST"])
def list_followups():
    if not _fb_initialized:
        return jsonify({"ok": False, "data": []}), 503

    data       = flask_request.json or {}
    uid        = data.get("uid")
    contact_id = data.get("contactId")

    if not uid:
        return jsonify({"ok": False, "error": "uid gerekli"}), 400

    try:
        query = (db_admin.collection("users").document(uid)
                 .collection("followups").where(filter=FieldFilter("done", "==", False)))
        if contact_id:
            query = query.where(filter=FieldFilter("contactId", "==", contact_id))

        result = []
        for doc in query.stream():
            d = doc.to_dict()
            d["id"] = doc.id
            result.append(d)

        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ================================================================
# SCHEDULER — Hatırlatma & Haftalık Takip
# ================================================================

def check_reminders():
    if not _fb_initialized or db_admin is None:
        return
    try:
        for user_doc in db_admin.collection("users").stream():
            uid = user_doc.id
            for rem in (db_admin.collection("users").document(uid)
                        .collection("reminders")
                        .where(filter=FieldFilter("done", "==", False))
                        .where(filter=FieldFilter("telegramSent", "==", False))
                        .stream()):
                r = rem.to_dict()
                due = r.get("dueDate", "")
                if not due:
                    continue
                try:
                    due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                except Exception:
                    try:
                        due_dt = datetime.strptime(due[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except Exception:
                        continue

                if due_dt <= datetime.now(timezone.utc):
                    name   = r.get("contactName", "Müşteri")
                    text_  = r.get("text", "Hatırlatma")
                    phone_ = r.get("contactPhone", "")
                    msg = (
                        f"⏰ <b>Hatırlatma!</b>\n\n"
                        f"👤 <b>{name}</b>" + (f" — {phone_}" if phone_ else "") + "\n"
                        f"📝 {text_}\n\n"
                        f"📅 {due_dt.strftime('%d.%m.%Y')}"
                    )
                    if send_telegram(msg):
                        rem.reference.update({"telegramSent": True})
                        print(f"📨 Hatırlatma gönderildi: {name}")
    except Exception as e:
        print(f"check_reminders hatası: {e}")


def check_followups():
    if not _fb_initialized or db_admin is None:
        return
    try:
        now = datetime.now(timezone.utc)
        for user_doc in db_admin.collection("users").stream():
            uid = user_doc.id
            for f_doc in (db_admin.collection("users").document(uid)
                          .collection("followups")
                          .where(filter=FieldFilter("done", "==", False))
                          .stream()):
                f = f_doc.to_dict()
                name  = f.get("contactName", "Müşteri")
                phone = f.get("contactPhone", "")
                notes = f.get("notes", {})
                sent  = f.get("sent", {})
                updates = {}

                for week_key, date_key in [
                    ("week1", "week1Date"),
                    ("week2", "week2Date"),
                    ("week3", "week3Date"),
                ]:
                    if sent.get(week_key):
                        continue
                    due_str = f.get(date_key, "")
                    if not due_str:
                        continue
                    try:
                        due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                    except Exception:
                        continue

                    if due_dt <= now:
                        week_num = week_key.replace("week", "")
                        note_text = notes.get(week_key, f"{week_num}. hafta takip")
                        msg = (
                            f"📆 <b>{week_num}. Hafta Takip Bildirimi</b>\n\n"
                            f"👤 <b>{name}</b>"
                            + (f"\n📞 {phone}" if phone else "") + "\n\n"
                            f"📝 <i>{note_text}</i>\n\n"
                            f"⏰ {now.strftime('%d.%m.%Y %H:%M')}"
                        )
                        if send_telegram(msg):
                            updates[f"sent.{week_key}"] = True
                            print(f"📨 {week_num}. hafta takip gönderildi: {name}")

                if updates:
                    new_sent = {**sent, **{k.split(".")[1]: v for k, v in updates.items()}}
                    if all(new_sent.get(w, False) for w in ["week1", "week2", "week3"]):
                        updates["done"] = True
                        updates["completedAt"] = now.isoformat()
                        send_telegram(
                            f"✅ <b>Takip Tamamlandı!</b>\n\n"
                            f"👤 <b>{name}</b> için 3 haftalık takip süreci tamamlandı.\n"
                            f"⏰ {now.strftime('%d.%m.%Y %H:%M')}"
                        )
                    f_doc.reference.update(updates)
    except Exception as e:
        print(f"check_followups hatası: {e}")


def start_scheduler():
    def loop():
        while True:
            try:
                check_reminders()
                check_followups()
            except Exception as e:
                print(f"Scheduler hatası: {e}")
            time.sleep(60)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    print("⏱️  Scheduler başladı (60s) — Hatırlatmalar + Haftalık Takipler")


# ================================================================
# BAŞLAT
# ================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Unified Sunucu Başlatıldı: http://0.0.0.0:{port}")
    print(f"   🌐 Web Sitesi : http://0.0.0.0:{port}/")
    print(f"   📊 CRM Paneli : http://0.0.0.0:{port}/crm")
    print(f"   🔧 Admin Panel: http://0.0.0.0:{port}/admin")
    init_firebase_admin()
    start_scheduler()
    app.run(host="0.0.0.0", port=port, debug=False)
