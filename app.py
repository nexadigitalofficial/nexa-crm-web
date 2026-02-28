import os
import time
import requests
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_file, request as flask_request
from bs4 import BeautifulSoup
from flask_cors import CORS

# ── Firebase Admin SDK ──────────────────────────────────────────
import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore
from google.cloud.firestore_v1.base_query import FieldFilter

app = Flask(__name__)
CORS(app)

# ================================================================
# AYARLAR
# ================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8462430471:AAEM_AjKYLKKVFpBsxGDkNmN91H77XHS81g")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "6183709337")
SERVICE_ACCOUNT    = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "service-account.json")

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


@app.route("/api/listings", methods=["GET"])
def get_listings():
    data = fetch_real_estate_data()
    return jsonify({"success": True, "data": data})


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
    init_firebase_admin()
    start_scheduler()
    app.run(host="0.0.0.0", port=port, debug=False)
