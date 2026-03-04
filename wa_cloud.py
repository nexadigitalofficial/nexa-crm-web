"""
================================================================
wa_cloud.py — Meta WhatsApp Cloud API entegrasyonu
================================================================

Kullanım:
  from wa_cloud import send_whatsapp, send_whatsapp_template, wa_status

Gerekli env variable'ları:
  WA_PHONE_NUMBER_ID   → Meta Business → WhatsApp → Phone Number ID
  WA_ACCESS_TOKEN      → Permanent System User Token (ya da geçici test token)
  WA_VERIFY_TOKEN      → Webhook doğrulama için kendiniz belirleyeceğiniz şifre

Ücretsiz tier: 1 000 business-initiated konuşma/ay
Müşteri yazdıktan sonra 24 saat içinde freeform mesaj atılabilir.
24 saat dışında sadece onaylı Template mesajları gönderilebilir.
================================================================
"""

import os
import requests
from datetime import datetime

# ── Konfigürasyon ────────────────────────────────────────────────
WA_API_VERSION    = "v19.0"
WA_BASE_URL       = f"https://graph.facebook.com/{WA_API_VERSION}"
WA_PHONE_ID       = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_TOKEN          = os.environ.get("WA_ACCESS_TOKEN",    "")
WA_VERIFY_TOKEN   = os.environ.get("WA_VERIFY_TOKEN",    "nexa_webhook_secret")
WA_TIMEOUT        = 10   # saniye


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type":  "application/json",
    }


def _is_configured() -> bool:
    return bool(WA_PHONE_ID and WA_TOKEN)


# ── Telefon Normalize ─────────────────────────────────────────────
def normalize_phone(raw: str) -> str | None:
    """
    Türkiye numaralarını uluslararası formata çevirir.
    Örnekler:
      "05324514008"  → "905324514008"
      "+90 532 451 40 08" → "905324514008"
      "5324514008"   → "905324514008"
    """
    if not raw:
        return None
    digits = "".join(filter(str.isdigit, raw))

    if digits.startswith("0") and len(digits) == 11:
        digits = "9" + digits           # 0XXXXXXXXXX → 90XXXXXXXXXX

    if digits.startswith("90") and len(digits) == 12:
        return digits

    if len(digits) == 10 and digits[0] in ("4", "5"):
        return "90" + digits            # 5XXXXXXXXX → 905XXXXXXXXX

    return digits if len(digits) >= 10 else None


# ── Freeform Metin Mesajı ─────────────────────────────────────────
def send_whatsapp(phone: str, message: str) -> dict:
    """
    Freeform metin mesajı gönderir.
    SADECE müşteri son 24 saat içinde yazdıysa ya da
    kendi bot numaranıza (danışman numarasına) gönderirken kullanın.

    Returns:
        {"ok": True,  "message_id": "wamid.xxx"}
        {"ok": False, "error": "...", "code": 400}
    """
    if not _is_configured():
        return {"ok": False, "error": "WA_PHONE_NUMBER_ID veya WA_ACCESS_TOKEN eksik"}

    phone_norm = normalize_phone(phone)
    if not phone_norm:
        return {"ok": False, "error": f"Geçersiz telefon numarası: {phone}"}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                phone_norm,
        "type":              "text",
        "text":              {"preview_url": False, "body": message},
    }

    try:
        resp = requests.post(
            f"{WA_BASE_URL}/{WA_PHONE_ID}/messages",
            headers=_headers(),
            json=payload,
            timeout=WA_TIMEOUT,
        )
        data = resp.json()

        if resp.ok and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            print(f"✅ WA gönderildi → {phone_norm} | id: {msg_id}")
            return {"ok": True, "message_id": msg_id, "phone": phone_norm}

        # API hata detayı
        err = data.get("error", {})
        print(f"❌ WA API hatası: {err.get('message', str(data))}")
        return {
            "ok":    False,
            "error": err.get("message", str(data)),
            "code":  err.get("code", resp.status_code),
        }

    except requests.exceptions.Timeout:
        print("❌ WA API timeout")
        return {"ok": False, "error": "API timeout"}
    except Exception as e:
        print(f"❌ WA beklenmedik hata: {e}")
        return {"ok": False, "error": str(e)}


# ── Template Mesajı ───────────────────────────────────────────────
def send_whatsapp_template(
    phone: str,
    template_name: str,
    language_code: str = "tr",
    components: list | None = None,
) -> dict:
    """
    Onaylı template mesajı gönderir (24 saat penceresi dışı için zorunlu).

    Meta Business Manager → WhatsApp → Message Templates'ten
    template oluşturup onaylatmanız gerekir.

    Örnek: send_whatsapp_template("905324514008", "lead_received", "tr", [
        {"type": "body", "parameters": [{"type": "text", "text": "Ahmet Yılmaz"}]}
    ])
    """
    if not _is_configured():
        return {"ok": False, "error": "WA_PHONE_NUMBER_ID veya WA_ACCESS_TOKEN eksik"}

    phone_norm = normalize_phone(phone)
    if not phone_norm:
        return {"ok": False, "error": f"Geçersiz telefon numarası: {phone}"}

    template_payload: dict = {
        "name":     template_name,
        "language": {"code": language_code},
    }
    if components:
        template_payload["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "to":                phone_norm,
        "type":              "template",
        "template":          template_payload,
    }

    try:
        resp = requests.post(
            f"{WA_BASE_URL}/{WA_PHONE_ID}/messages",
            headers=_headers(),
            json=payload,
            timeout=WA_TIMEOUT,
        )
        data = resp.json()

        if resp.ok and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            print(f"✅ WA template gönderildi → {phone_norm} | template: {template_name}")
            return {"ok": True, "message_id": msg_id, "phone": phone_norm}

        err = data.get("error", {})
        return {
            "ok":    False,
            "error": err.get("message", str(data)),
            "code":  err.get("code", resp.status_code),
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── API Durumu ────────────────────────────────────────────────────
def wa_status() -> dict:
    """
    Phone Number ID'nin durumunu Meta Graph API'den kontrol eder.
    Token geçerli mi, numara aktif mi öğrenilir.
    """
    if not _is_configured():
        return {
            "ok":          False,
            "configured":  False,
            "error":       "WA_PHONE_NUMBER_ID veya WA_ACCESS_TOKEN tanımlanmamış",
        }
    try:
        resp = requests.get(
            f"{WA_BASE_URL}/{WA_PHONE_ID}",
            headers=_headers(),
            params={"fields": "display_phone_number,verified_name,quality_rating,platform_type"},
            timeout=WA_TIMEOUT,
        )
        data = resp.json()

        if resp.ok:
            return {
                "ok":                  True,
                "configured":          True,
                "display_phone":       data.get("display_phone_number", ""),
                "verified_name":       data.get("verified_name", ""),
                "quality_rating":      data.get("quality_rating", ""),
                "platform_type":       data.get("platform_type", ""),
                "phone_number_id":     WA_PHONE_ID,
            }

        err = data.get("error", {})
        return {
            "ok":         False,
            "configured": True,
            "error":      err.get("message", str(data)),
            "code":       err.get("code", resp.status_code),
        }

    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}


# ── Webhook Doğrulama Yardımcısı ─────────────────────────────────
def verify_webhook_token(token: str) -> bool:
    """Meta'nın webhook doğrulaması için gelen token'ı kontrol eder."""
    return token == WA_VERIFY_TOKEN
wa
