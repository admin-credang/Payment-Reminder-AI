
import os
import binascii
from functools import lru_cache
from uuid import UUID, uuid4
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from dotenv import load_dotenv
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google.cloud import dialogflowcx_v3 as dialogflowcx
from google.auth import default as google_auth_default
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession
from requests.exceptions import RequestException
import logging
import hmac
import re
import base64
import hashlib
import json
import time
from datetime import date
from urllib.parse import urlencode, parse_qs, urlsplit, urlunsplit
from urllib.request import Request as URLRequest, urlopen
from urllib.error import HTTPError, URLError
from xml.sax.saxutils import escape
from fastapi.responses import Response

load_dotenv()

DIALOGFLOW_WEBHOOK_SECRET = os.getenv("DIALOGFLOW_WEBHOOK_SECRET")
CAMPAIGN_API_KEY = os.getenv("CAMPAIGN_API_KEY")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
DEMO_MODE = os.getenv("DEMO_MODE", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

if not DIALOGFLOW_WEBHOOK_SECRET:
    raise RuntimeError("Missing DIALOGFLOW_WEBHOOK_SECRET in .env")
logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
FRONTEND_BASE_URL = os.getenv(
    "FRONTEND_BASE_URL",
    "http://127.0.0.1:5500"
)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing Supabase credentials in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# Dialogflow CX Configuration
DIALOGFLOW_PROJECT_ID = "payment-reminder-ai-2026"
DIALOGFLOW_LOCATION = "asia-south1"
DIALOGFLOW_AGENT_ID = "12d88d77-8d49-4d31-8d41-818248d455bb"
DIALOGFLOW_THANK_YOU_PAGE = (
    f"projects/{DIALOGFLOW_PROJECT_ID}/locations/{DIALOGFLOW_LOCATION}/agents/"
    f"{DIALOGFLOW_AGENT_ID}/flows/00000000-0000-0000-0000-000000000000/"
    "pages/ee9e008b-a1f0-40d0-9271-c352fa90098b"
)
dialogflow_client = dialogflowcx.SessionsClient(
    client_options={
        "api_endpoint": f"{DIALOGFLOW_LOCATION}-dialogflow.googleapis.com"
    }
)

GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GOOGLE_TTS_API_ROOT = "https://texttospeech.googleapis.com/v1"
ARABIC_TTS_LANGUAGE_CODE = "ar-XA"


class GoogleTTSUnavailableError(Exception):
    def __init__(self, status_code: int | None, reason: str):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def create_google_tts_session() -> AuthorizedSession:
    try:
        credentials, _ = google_auth_default(
            scopes=[GOOGLE_CLOUD_PLATFORM_SCOPE]
        )
    except GoogleAuthError as error:
        raise GoogleTTSUnavailableError(
            None,
            "credentials_unavailable",
        ) from error
    return AuthorizedSession(credentials)


def google_tts_json_request(
    session: AuthorizedSession,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    payload: dict | None = None,
) -> dict:
    try:
        if method == "GET":
            response = session.get(url, params=params, timeout=10)
        else:
            response = session.post(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=30,
            )
    except GoogleAuthError as error:
        raise GoogleTTSUnavailableError(
            None,
            "credentials_unavailable",
        ) from error
    except RequestException as error:
        raise GoogleTTSUnavailableError(None, "network_error") from error

    try:
        response_data = response.json()
    except ValueError as error:
        raise GoogleTTSUnavailableError(
            response.status_code,
            "invalid_api_response",
        ) from error
    if not isinstance(response_data, dict):
        raise GoogleTTSUnavailableError(
            response.status_code,
            "invalid_api_response",
        )

    if not response.ok:
        error_data = response_data.get("error", {})
        allowed_reasons = {
            "SERVICE_DISABLED",
            "PERMISSION_DENIED",
            "RESOURCE_EXHAUSTED",
            "INVALID_ARGUMENT",
            "UNAUTHENTICATED",
            "FAILED_PRECONDITION",
        }
        details = error_data.get("details", [])
        if not isinstance(details, list):
            details = []
        reason = next(
            (
                detail.get("reason")
                for detail in details
                if detail.get("reason") in allowed_reasons
            ),
            error_data.get("status"),
        )
        if reason not in allowed_reasons:
            reason = "api_error"
        raise GoogleTTSUnavailableError(response.status_code, reason)
    return response_data


@lru_cache(maxsize=1)
def get_arabic_tts_voice_name() -> str:
    session = create_google_tts_session()
    response_data = google_tts_json_request(
        session,
        "GET",
        f"{GOOGLE_TTS_API_ROOT}/voices",
        params={"languageCode": ARABIC_TTS_LANGUAGE_CODE},
    )
    listed_voices = response_data.get("voices", [])
    if not isinstance(listed_voices, list):
        raise GoogleTTSUnavailableError(None, "invalid_voice_list")
    voices = [
        voice
        for voice in listed_voices
        if isinstance(voice, dict)
        and voice.get("name", "").startswith(f"{ARABIC_TTS_LANGUAGE_CODE}-")
        and ARABIC_TTS_LANGUAGE_CODE in voice.get("languageCodes", [])
    ]
    for family in ("chirp3-hd", "wavenet", "standard"):
        matching_names = sorted(
            voice["name"]
            for voice in voices
            if family in voice["name"].casefold()
        )
        if matching_names:
            return matching_names[0]
    raise GoogleTTSUnavailableError(None, "no_supported_arabic_voice")


def synthesize_arabic_tts(text: str) -> str:
    if not text or not text.strip():
        raise GoogleTTSUnavailableError(None, "empty_text")
    if len(text.encode("utf-8")) > 5000:
        raise GoogleTTSUnavailableError(None, "text_too_long")

    session = create_google_tts_session()
    voice_name = get_arabic_tts_voice_name()
    response_data = google_tts_json_request(
        session,
        "POST",
        f"{GOOGLE_TTS_API_ROOT}/text:synthesize",
        payload={
            "input": {"text": text},
            "voice": {
                "languageCode": ARABIC_TTS_LANGUAGE_CODE,
                "name": voice_name,
            },
            "audioConfig": {"audioEncoding": "MP3"},
        },
    )
    encoded_audio = response_data.get("audioContent")
    if not isinstance(encoded_audio, str) or not encoded_audio:
        raise GoogleTTSUnavailableError(None, "empty_audio")
    try:
        if not base64.b64decode(encoded_audio, validate=True):
            raise GoogleTTSUnavailableError(None, "empty_audio")
    except (binascii.Error, ValueError) as error:
        raise GoogleTTSUnavailableError(None, "invalid_audio") from error
    return encoded_audio


logger = logging.getLogger(__name__)
app = FastAPI(
    title="Payment Reminder API",
    description="Payment Reminder and BizOSOne Product Renewal API",
    version="2.0.0"
)

@app.middleware("http")
async def strip_api_prefix(request: Request, call_next):
    path = request.scope["path"]
    if path == "/api":
        request.scope["path"] = "/"
    elif path.startswith("/api/"):
        request.scope["path"] = path[4:]

    raw_path = request.scope.get("raw_path")
    if raw_path == b"/api":
        request.scope["raw_path"] = b"/"
    elif raw_path and raw_path.startswith(b"/api/"):
        request.scope["raw_path"] = raw_path[4:]

    return await call_next(request)




@app.get("/")
def home():
    return {"message": "Payment Reminder API is running"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        FRONTEND_BASE_URL,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "healthy", "demo_mode": DEMO_MODE}


def require_campaign_access(request: Request):
    if (
        DEMO_MODE
        and request.client
        and request.client.host in {"127.0.0.1", "::1"}
    ):
        return
    provided_key = request.headers.get("x-campaign-api-key", "")
    if not CAMPAIGN_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Campaign API is not configured"
        )
    if not hmac.compare_digest(provided_key, CAMPAIGN_API_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")


def twilio_configuration_required():
    if not all((
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
        TWILIO_FROM_NUMBER,
        PUBLIC_BASE_URL,
    )):
        raise HTTPException(
            status_code=503,
            detail="Outbound calling and SMS require Twilio credentials and PUBLIC_BASE_URL"
        )


def stripe_configuration_required():
    if not STRIPE_SECRET_KEY or not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Payment links require Stripe API and webhook credentials"
        )


def stripe_post(resource: str, values: dict):
    stripe_configuration_required()
    request = URLRequest(
        f"https://api.stripe.com/v1/{resource}",
        data=urlencode(values).encode(),
        headers={
            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError):
        logger.exception("Stripe request failed")
        raise HTTPException(status_code=502, detail="Payment provider request failed")


def send_invoice_payment_link(
    customer: dict,
    invoice: dict,
    campaign_id: str | None = None,
    call_sid: str | None = None,
    language_code: str = "en",
):
    stripe_configuration_required()
    twilio_configuration_required()
    existing = (
        supabase.table("call_logs")
        .select("id, call_status, call_notes")
        .eq("customer_id", customer["customer_id"])
        .eq("invoice_number", invoice["invoice_number"])
        .in_(
            "call_status",
            ["sms_accepted", "sms_queued", "sms_sending", "sms_sent", "sms_delivered"],
        )
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data
    if existing:
        try:
            notes = json.loads(existing[0].get("call_notes") or "{}")
            return {
                "payment_url": notes["payment_url"],
                "sms_status": existing[0]["call_status"].removeprefix("sms_"),
                "sms_sid": notes.get("sms_sid"),
            }
        except (KeyError, json.JSONDecodeError):
            raise HTTPException(
                status_code=409,
                detail="A payment link was recorded but could not be retrieved"
            )

    session_values = {
        "mode": "payment",
        "success_url": f"{FRONTEND_BASE_URL}/?payment=success",
        "cancel_url": f"{FRONTEND_BASE_URL}/?payment=cancelled",
        "customer_email": customer.get("email", ""),
        "line_items[0][price_data][currency]": "inr",
        "line_items[0][price_data][unit_amount]": str(round(float(invoice["amount"]) * 100)),
        "line_items[0][price_data][product_data][name]": f"Invoice {invoice['invoice_number']}",
        "line_items[0][quantity]": "1",
        "metadata[customer_id]": customer["customer_id"],
        "metadata[invoice_number]": invoice["invoice_number"],
    }
    session_values = {
        key: value for key, value in session_values.items() if value
    }
    session = stripe_post("checkout/sessions", session_values)
    payment_url = session.get("url")
    if not payment_url or not session.get("id"):
        raise HTTPException(status_code=502, detail="Payment provider returned no checkout link")

    text = f"BizOSOne payment link for invoice {invoice['invoice_number']}: {payment_url}"
    try:
        message = twilio_post(
            "Messages.json",
            {
                "To": customer["phone"],
                "From": TWILIO_FROM_NUMBER,
                "Body": text,
                "StatusCallback": f"{PUBLIC_BASE_URL}/sms/status",
            },
        )
        sms_sid = message.get("sid")
        sms_status = message.get("status", "queued") if sms_sid else "failed"
    except HTTPException:
        sms_sid = None
        sms_status = "failed"

    supabase.table("call_logs").insert({
        "campaign_id": campaign_id,
        "customer_id": customer["customer_id"],
        "invoice_number": invoice["invoice_number"],
        "call_status": f"sms_{sms_status}",
        "call_notes": json.dumps({
            "event": "payment_link",
            "checkout_session_id": session["id"],
            "payment_url": payment_url,
            "sms_sid": sms_sid,
            "sms_status": sms_status,
            "call_sid": call_sid,
            "selected_language": language_code,
        }),
    }).execute()
    return {
        "payment_url": payment_url,
        "sms_status": sms_status,
        "sms_sid": sms_sid,
    }


def mask_phone_number(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***"


def create_demo_payment_link(
    customer: dict,
    invoice: dict,
    campaign_id: str | None,
    call_sid: str | None,
    language_code: str,
):
    existing = (
        supabase.table("call_logs")
        .select("id, call_notes")
        .eq("customer_id", customer["customer_id"])
        .eq("invoice_number", invoice["invoice_number"])
        .eq("call_status", "completed")
        .like("call_notes", "%demo_payment_link%")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if existing:
        try:
            notes = json.loads(existing[0].get("call_notes") or "{}")
        except json.JSONDecodeError as error:
            raise HTTPException(
                status_code=500,
                detail="Stored demo payment record is invalid",
            ) from error
        if notes.get("payment_url"):
            return {
                "payment_url": notes["payment_url"],
                "sms_status": "simulated",
                "destination_masked": notes.get("destination_masked", "***"),
                "reused": True,
            }
        raise HTTPException(
            status_code=409,
            detail="A demo payment record exists but has no payment URL",
        )

    token = uuid4().hex
    payment_url = (
        f"{FRONTEND_BASE_URL.rstrip('/')}/?demo_payment={token}"
        f"&lang={language_code}"
    )
    destination_masked = mask_phone_number(customer.get("phone"))
    notes = {
        "event": "demo_payment_link",
        "mode": "demo",
        "demo_status": "demo_sms_simulated",
        "demo_token": token,
        "payment_url": payment_url,
        "amount": float(invoice["amount"]),
        "invoice_status": invoice["status"],
        "payment_link_status": "generated_demo",
        "sms_status": "simulated",
        "destination_masked": destination_masked,
        "payment_status": "pending",
        "call_sid": call_sid,
        "selected_language": language_code,
        "real_sms_sent": False,
        "real_payment_received": False,
    }
    created = (
        supabase.table("call_logs")
        .insert({
            "campaign_id": campaign_id,
            "customer_id": customer["customer_id"],
            "invoice_number": invoice["invoice_number"],
            "call_status": "completed",
            "call_notes": json.dumps(notes, ensure_ascii=False),
        })
        .execute()
    ).data
    if not created:
        raise HTTPException(status_code=500, detail="Unable to record demo payment link")
    return {
        "payment_url": payment_url,
        "sms_status": "simulated",
        "destination_masked": destination_masked,
        "reused": False,
    }


def record_demo_followup_action(
    customer_id: str,
    invoice_number: str,
    followup: dict,
    campaign_id: str | None,
    call_sid: str | None,
    language_code: str,
):
    existing = (
        supabase.table("call_logs")
        .select("id")
        .eq("customer_id", customer_id)
        .eq("invoice_number", invoice_number)
        .eq("call_status", "completed")
        .like("call_notes", f"%{followup['id']}%")
        .limit(1)
        .execute()
    ).data or []
    if existing:
        return
    notes = {
        "event": "demo_followup",
        "mode": "demo",
        "demo_status": "demo_followup_scheduled",
        "followup_id": followup["id"],
        "followup_status": followup["status"],
        "preferred_payment_date": followup["followup_date"],
        "call_outcome": "followup_scheduled",
        "payment_status": "pending",
        "call_sid": call_sid,
        "selected_language": language_code,
        "real_call_placed": False,
    }
    created = (
        supabase.table("call_logs")
        .insert({
            "campaign_id": campaign_id,
            "customer_id": customer_id,
            "invoice_number": invoice_number,
            "call_status": "completed",
            "call_notes": json.dumps(notes, ensure_ascii=False),
        })
        .execute()
    ).data
    if not created:
        raise HTTPException(status_code=500, detail="Unable to record demo follow-up")


def twilio_post(resource: str, values: dict):
    twilio_configuration_required()
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{TWILIO_ACCOUNT_SID}/{resource}"
    )
    credentials = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    request = URLRequest(
        url,
        data=urlencode(values, doseq=True).encode(),
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError):
        logger.exception("Twilio request failed")
        raise HTTPException(status_code=502, detail="Telephony provider request failed")


def verify_twilio_request(request: Request, fields: dict[str, str]):
    if not TWILIO_AUTH_TOKEN or not PUBLIC_BASE_URL:
        raise HTTPException(status_code=503, detail="Twilio webhook is not configured")
    signature = request.headers.get("x-twilio-signature", "")
    url = f"{PUBLIC_BASE_URL}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    signed = url + "".join(
        key + fields[key] for key in sorted(fields)
    )
    expected = base64.b64encode(
        hmac.new(
            TWILIO_AUTH_TOKEN.encode(),
            signed.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="Invalid telephony signature")


def twiml_message(text: str, language_code: str, action_url: str | None = None):
    locales = {"en": "en-IN", "hi": "hi-IN", "ar": "ar-SA"}
    voices = {
        "en": "Google.en-IN-Standard-A",
        "hi": "Google.hi-IN-Standard-A",
        "ar": "Google.ar-XA-Standard-A",
    }
    locale = locales.get(language_code, "en-IN")
    voice = voices.get(language_code, voices["en"])
    escaped_text = escape(text)
    if action_url:
        escaped_action_url = escape(action_url).replace('"', "&quot;")
        no_response = {
            "en": "No response received. Goodbye.",
            "hi": "कोई जवाब नहीं मिला। अलविदा।",
            "ar": "لم نتلق ردًا. إلى اللقاء.",
        }.get(language_code, "No response received. Goodbye.")
        gather = (
            f'<Gather input="speech" language="{locale}" timeout="7" '
            f'speechTimeout="auto" action="{escaped_action_url}" '
            f'method="POST"></Gather><Say language="{locale}" voice="{voice}">'
            f"{escape(no_response)}</Say><Hangup/>"
        )
    else:
        gather = ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say language="{locale}" voice="{voice}">{escaped_text}</Say>'
        f"{gather}</Response>"
    )


def fetch_due_invoices():
    today = date.today().isoformat()
    invoices = (
        supabase.table("invoices")
        .select("invoice_number, customer_id, amount, due_date, status")
        .in_("status", ["pending", "overdue"])
        .lte("due_date", today)
        .order("due_date")
        .limit(100)
        .execute()
    ).data or []
    customer_ids = list({row["customer_id"] for row in invoices})
    if not customer_ids:
        return []
    customers = (
        supabase.table("customers")
        .select("customer_id, full_name, phone, preferred_language")
        .in_("customer_id", customer_ids)
        .execute()
    ).data or []
    by_id = {row["customer_id"]: row for row in customers}
    return [
        {"invoice": invoice, "customer": by_id[invoice["customer_id"]]}
        for invoice in invoices
        if invoice["customer_id"] in by_id and by_id[invoice["customer_id"]].get("phone")
    ]


def parse_followup_date(value: str) -> date:
    normalized = value.strip().lower()
    today = date.today()
    relative_days = {
        "tomorrow": 1,
        "day after tomorrow": 2,
        "कल": 1,
        "परसों": 2,
        "غدا": 1,
        "غدًا": 1,
        "بعد غد": 2,
    }
    if normalized in relative_days:
        return today + timedelta(days=relative_days[normalized])
    match = re.search(r"\b(?:in|after)\s+(\d+)\s+days?\b", normalized)
    if match:
        return today + timedelta(days=int(match.group(1)))
    match = re.search(r"بعد\s+(\d+)\s+أيام?", normalized)
    if match:
        return today + timedelta(days=int(match.group(1)))

    for date_format in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(value.strip(), date_format).date()
        except ValueError:
            continue
    raise HTTPException(
        status_code=422,
        detail="Use a valid date, such as YYYY-MM-DD or 'tomorrow'"
    )


def save_payment_followup(customer_id: str, invoice_number: str, preferred_date: str):
    parsed_date = parse_followup_date(preferred_date)
    if parsed_date < date.today():
        raise HTTPException(status_code=422, detail="Follow-up date cannot be in the past")
    followup_date = parsed_date.isoformat()
    customer = (
        supabase.table("customers")
        .select("customer_id")
        .eq("customer_id", customer_id)
        .maybe_single()
        .execute()
    ).data
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    invoice = (
        supabase.table("invoices")
        .select("invoice_number, status")
        .eq("customer_id", customer_id)
        .eq("invoice_number", invoice_number)
        .maybe_single()
        .execute()
    ).data
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice["status"].lower() == "paid":
        raise HTTPException(status_code=409, detail="Invoice is already paid")

    existing = (
        supabase.table("payment_followups")
        .select("id, followup_date, status")
        .eq("customer_id", customer_id)
        .eq("invoice_number", invoice_number)
        .eq("followup_date", followup_date)
        .eq("status", "scheduled")
        .limit(1)
        .execute()
    ).data
    if existing:
        return existing[0], False
    created = (
        supabase.table("payment_followups")
        .insert({
            "customer_id": customer_id,
            "invoice_number": invoice_number,
            "followup_date": followup_date,
            "status": "scheduled",
        })
        .execute()
    ).data
    if not created:
        raise HTTPException(status_code=500, detail="Unable to record follow-up")
    return created[0], True


def normalize_dialogflow_date(value):
    if not isinstance(value, dict):
        return value
    for key in ("original", "resolved", "date_time"):
        candidate = value.get(key)
        if candidate:
            return normalize_dialogflow_date(candidate)
    if all(key in value for key in ("year", "month", "day")):
        try:
            return date(
                int(value["year"]),
                int(value["month"]),
                int(value["day"]),
            ).isoformat()
        except (TypeError, ValueError):
            return value
    return value


def update_campaign_call_log(call_sid: str | None, updates: dict):
    if not call_sid:
        return
    rows = (
        supabase.table("call_logs")
        .select("id, call_notes")
        .like("call_notes", f"%{call_sid}%")
        .limit(1)
        .execute()
    ).data
    if not rows:
        return
    raw_notes = rows[0].get("call_notes") or "{}"
    try:
        notes = json.loads(raw_notes)
    except json.JSONDecodeError as error:
        logger.error("Campaign call notes are invalid JSON for call %s", call_sid)
        raise HTTPException(
            status_code=500,
            detail="Unable to update campaign call outcome",
        ) from error
    notes.update({
        key: value
        for key, value in updates.items()
        if value is not None
    })
    supabase.table("call_logs").update({
        "call_notes": json.dumps(notes, ensure_ascii=False),
    }).eq("id", rows[0]["id"]).execute()


def validate_dialogflow_date(value, utterance: str) -> str:
    normalized = normalize_dialogflow_date(value)
    parsed = parse_followup_date(str(normalized))
    explicit_years = {
        int(year)
        for year in re.findall(r"(?<!\d)\d{4}(?!\d)", utterance)
    }
    if explicit_years and parsed.year not in explicit_years:
        raise HTTPException(
            status_code=422,
            detail="Dialogflow's resolved year does not match the date provided",
        )
    return parsed.isoformat()


def dialogflow_text(message: str, session_id: str, language_code: str, session_parameters=None):
    query_input = dialogflowcx.QueryInput(
        text=dialogflowcx.TextInput(text=message),
        language_code=language_code,
    )
    detect_request = dialogflowcx.DetectIntentRequest(
        session=dialogflow_client.session_path(
            DIALOGFLOW_PROJECT_ID,
            DIALOGFLOW_LOCATION,
            DIALOGFLOW_AGENT_ID,
            session_id,
        ),
        query_input=query_input,
    )
    if session_parameters:
        detect_request.query_params = dialogflowcx.QueryParameters(
            parameters=session_parameters
        )
    result = dialogflow_client.detect_intent(request=detect_request).query_result
    replies = [
        text
        for item in result.response_messages
        if item.text
        for text in item.text.text
    ]
    matched_intent = (
        result.match.intent.display_name
        if result.match and result.match.intent
        else None
    )
    has_invoice_details = any(
        marker in reply.lower()
        for reply in replies
        for marker in ("invoice number", "इनवॉइस नंबर", "رقم الفاتورة")
    )
    if result.current_page.display_name in (
        "Fetch Invoice Details",
        "Payment Decision",
    ) and not has_invoice_details and matched_intent not in ("Pay Now", "Pay Later"):
        customer_id = result.parameters.get("customer_id")
        if customer_id:
            logger.warning(
                "Dialogflow omitted invoice details before the payment decision; recovering from Supabase"
            )
            invoice_reply = invoice_fallback_reply(
                str(customer_id.get("original") or customer_id.get("string"))
                if isinstance(customer_id, dict)
                else str(customer_id),
                language_code,
                (session_parameters or {}).get("verified_phone"),
            )
            if replies:
                replies.insert(0, invoice_reply)
            else:
                replies.append(invoice_reply)
    return "\n".join(replies), result.current_page.display_name


async def twilio_form(request: Request):
    raw = await request.body()
    values = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: items[-1] for key, items in values.items()}


def same_phone(first: str, second: str):
    return re.sub(r"\D", "", first or "") == re.sub(r"\D", "", second or "")


def is_valid_e164_phone(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"\+[1-9]\d{7,14}", value.strip()))


def supabase_response_data(response, operation: str):
    if response is None:
        logger.error("Supabase returned no response for %s", operation)
        raise HTTPException(
            status_code=503,
            detail="Database did not return a response. Please retry.",
        )
    try:
        return response.data
    except AttributeError as error:
        logger.exception("Supabase returned an invalid response for %s", operation)
        raise HTTPException(
            status_code=503,
            detail="Database returned an invalid response. Please retry.",
        ) from error


class CampaignCreateRequest(BaseModel):
    campaign_name: str = Field(min_length=1, max_length=200)


@app.get("/campaigns")
def list_campaigns(request: Request):
    require_campaign_access(request)
    try:
        rows = (
            supabase.table("campaigns")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        ).data
        campaign_ids = [str(row["id"]) for row in rows or []]
        activity_by_campaign = {campaign_id: [] for campaign_id in campaign_ids}

        def summarize_activity(activity):
            try:
                notes = json.loads(activity.get("call_notes") or "{}")
            except json.JSONDecodeError:
                logger.error(
                    "Invalid campaign call notes for customer %s and invoice %s",
                    activity.get("customer_id"),
                    activity.get("invoice_number"),
                )
                notes = {}
            status = activity["call_status"]
            if notes.get("mode") == "demo":
                status = notes.get("demo_status", status)
            return {
                "customer_id": activity["customer_id"],
                "invoice_number": activity["invoice_number"],
                "status": status,
                "amount": notes.get("amount"),
                "payment_status": notes.get("payment_status", "pending"),
                "payment_link_status": notes.get("payment_link_status"),
                "sms_status": notes.get("sms_status"),
                "destination_masked": notes.get("destination_masked"),
                "preferred_payment_date": notes.get("preferred_payment_date"),
                "created_at": activity["created_at"],
            }

        if campaign_ids:
            activity_rows = (
                supabase.table("call_logs")
                .select(
                    "campaign_id, customer_id, invoice_number, call_status, "
                    "call_notes, created_at"
                )
                .in_("campaign_id", campaign_ids)
                .order("created_at", desc=True)
                .limit(500)
                .execute()
            ).data or []
            for activity in activity_rows:
                activity_by_campaign[str(activity["campaign_id"])].append(
                    summarize_activity(activity)
                )
        demo_rows = (
            supabase.table("call_logs")
            .select(
                "customer_id, invoice_number, call_status, call_notes, created_at"
            )
            .eq("call_status", "completed")
            .like("call_notes", '%"mode": "demo"%')
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        ).data or []
        campaigns = []
        for row in rows or []:
            campaign = dict(row)
            campaign["activity"] = activity_by_campaign[str(row["id"])]
            if any(item["status"].startswith("demo_") for item in campaign["activity"]):
                campaign["display_status"] = "demo_completed"
            campaigns.append(campaign)
        return {
            "success": True,
            "campaigns": campaigns,
            "demo_activity": [summarize_activity(row) for row in demo_rows],
        }
    except Exception:
        logger.exception("Campaign retrieval failed")
        raise HTTPException(status_code=500, detail="Unable to retrieve campaigns")


@app.get("/campaigns/eligible-invoices")
def get_eligible_campaign_invoices(request: Request):
    require_campaign_access(request)
    try:
        eligible = fetch_due_invoices()
        return {
            "success": True,
            "count": len(eligible),
            "invoices": eligible,
        }
    except Exception:
        logger.exception("Campaign invoice selection failed")
        raise HTTPException(status_code=500, detail="Unable to select campaign invoices")


@app.post("/campaigns")
def create_campaign(payload: CampaignCreateRequest, request: Request):
    require_campaign_access(request)
    try:
        created = (
            supabase.table("campaigns")
            .insert({
                "campaign_name": payload.campaign_name,
                "status": "draft",
            })
            .execute()
        ).data
        if not created:
            raise HTTPException(status_code=500, detail="Campaign was not created")
        return {"success": True, "campaign": created[0]}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Campaign creation failed")
        raise HTTPException(status_code=500, detail="Unable to create campaign")


@app.post("/campaigns/{campaign_id}/execute")
def execute_campaign(campaign_id: UUID, request: Request):
    require_campaign_access(request)
    if not DEMO_MODE:
        twilio_configuration_required()
    try:
        campaign_result = (
            supabase.table("campaigns")
            .select("id, status")
            .eq("id", str(campaign_id))
            .maybe_single()
            .execute()
        )
        campaign = campaign_result.data
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        if campaign["status"] in ("completed", "cancelled"):
            raise HTTPException(status_code=409, detail="Campaign is already closed")

        candidates = fetch_due_invoices()
        results = []
        for item in candidates:
            invoice = item["invoice"]
            customer = item["customer"]
            previous = (
                supabase.table("call_logs")
                .select("id, call_status")
                .eq("campaign_id", str(campaign_id))
                .eq("customer_id", customer["customer_id"])
                .eq("invoice_number", invoice["invoice_number"])
                .limit(1)
                .execute()
            ).data
            if previous:
                results.append({
                    "customer_id": customer["customer_id"],
                    "invoice_number": invoice["invoice_number"],
                    "status": "skipped_duplicate",
                })
                continue

            log_row = (
                supabase.table("call_logs")
                .insert({
                    "campaign_id": str(campaign_id),
                    "customer_id": customer["customer_id"],
                    "invoice_number": invoice["invoice_number"],
                    "call_status": "queued",
                    "call_notes": json.dumps({"source": "payment_campaign"}),
                })
                .execute()
            ).data
            if not log_row:
                raise HTTPException(status_code=500, detail="Unable to queue campaign call")

            lang = customer.get("preferred_language", "en")
            if lang not in ("en", "hi", "ar"):
                lang = "en"
            if DEMO_MODE:
                notes = {
                    "source": "payment_campaign_demo",
                    "mode": "demo",
                    "demo_status": "demo_call_initiated",
                    "call_outcome": "demo_call_initiated",
                    "selected_language": lang,
                    "destination_masked": mask_phone_number(customer.get("phone")),
                    "amount": float(invoice["amount"]),
                    "invoice_status": invoice["status"],
                    "payment_status": "pending",
                    "real_call_placed": False,
                }
                supabase.table("call_logs").update({
                    "call_status": "completed",
                    "call_notes": json.dumps(notes, ensure_ascii=False),
                }).eq("id", log_row[0]["id"]).execute()
                results.append({
                    "customer_id": customer["customer_id"],
                    "invoice_number": invoice["invoice_number"],
                    "status": "demo_call_initiated",
                    "simulated": True,
                })
                continue
            call_params = urlencode({
                "campaign_id": str(campaign_id),
                "customer_id": customer["customer_id"],
                "invoice_number": invoice["invoice_number"],
                "language_code": lang,
            })
            call_url = f"{PUBLIC_BASE_URL}/voice/start?{call_params}"
            callback_url = f"{PUBLIC_BASE_URL}/voice/status"
            try:
                call = twilio_post(
                    f"Calls.json",
                    {
                        "To": customer["phone"],
                        "From": TWILIO_FROM_NUMBER,
                        "Url": call_url,
                        "Method": "POST",
                        "StatusCallback": callback_url,
                        "StatusCallbackMethod": "POST",
                        "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
                    },
                )
                supabase.table("call_logs").update({
                    "call_notes": json.dumps({
                        "source": "payment_campaign",
                        "provider_call_sid": call.get("sid"),
                    }),
                }).eq("id", log_row[0]["id"]).execute()
                results.append({
                    "customer_id": customer["customer_id"],
                    "invoice_number": invoice["invoice_number"],
                    "status": "queued",
                })
            except HTTPException:
                supabase.table("call_logs").update({
                    "call_status": "failed",
                    "call_notes": json.dumps({"source": "payment_campaign", "provider_error": "Twilio call request failed"}),
                }).eq("id", log_row[0]["id"]).execute()
                results.append({
                    "customer_id": customer["customer_id"],
                    "invoice_number": invoice["invoice_number"],
                    "status": "failed",
                })

        if DEMO_MODE:
            final_status = "completed"
        elif any(result["status"] == "queued" for result in results):
            final_status = "in_progress"
        elif any(result["status"] == "failed" for result in results):
            final_status = "failed"
        else:
            final_status = "completed"
        supabase.table("campaigns").update({
            "status": final_status
        }).eq("id", str(campaign_id)).execute()
        return {
            "success": True,
            "simulated": DEMO_MODE,
            "real_calls_placed": 0 if DEMO_MODE else None,
            "results": results,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Campaign execution failed")
        raise HTTPException(status_code=500, detail="Unable to execute campaign")


@app.post("/payment-followups/dispatch")
def dispatch_due_payment_followups(request: Request):
    require_campaign_access(request)
    if not DEMO_MODE:
        twilio_configuration_required()
    try:
        due_rows = (
            supabase.table("payment_followups")
            .select("id, customer_id, invoice_number, followup_date")
            .eq("status", "scheduled")
            .lte("followup_date", date.today().isoformat())
            .limit(100)
            .execute()
        ).data or []
        if not due_rows:
            return {"success": True, "queued": 0, "skipped": 0, "failed": 0}

        campaign_name = f"Scheduled payment follow-ups {date.today().isoformat()}"
        existing_campaign = (
            supabase.table("campaigns")
            .select("id")
            .eq("campaign_name", campaign_name)
            .limit(1)
            .execute()
        ).data
        if existing_campaign:
            campaign_id = existing_campaign[0]["id"]
        else:
            campaign = (
                supabase.table("campaigns")
                .insert({
                    "campaign_name": campaign_name,
                    "status": "draft" if DEMO_MODE else "in_progress",
                })
                .execute()
            ).data
            if not campaign:
                raise HTTPException(status_code=500, detail="Unable to create follow-up campaign")
            campaign_id = campaign[0]["id"]

        queued = skipped = failed = 0
        for followup in due_rows:
            invoice = (
                supabase.table("invoices")
                .select("invoice_number, due_date, status")
                .eq("customer_id", followup["customer_id"])
                .eq("invoice_number", followup["invoice_number"])
                .maybe_single()
                .execute()
            ).data
            customer = (
                supabase.table("customers")
                .select("phone, preferred_language")
                .eq("customer_id", followup["customer_id"])
                .maybe_single()
                .execute()
            ).data
            if not invoice or invoice["status"].lower() == "paid" or not customer or not customer.get("phone"):
                supabase.table("payment_followups").update({
                    "status": "closed",
                }).eq("id", followup["id"]).execute()
                skipped += 1
                continue

            prior = (
                supabase.table("call_logs")
                .select("id, call_status")
                .eq("customer_id", followup["customer_id"])
                .eq("invoice_number", followup["invoice_number"])
                .in_("call_status", ["queued", "initiated", "ringing", "in_progress", "completed"])
                .like("call_notes", f"%{followup['id']}%")
                .limit(1)
                .execute()
            ).data
            if prior:
                skipped += 1
                continue

            call_log = (
                supabase.table("call_logs")
                .insert({
                    "campaign_id": str(campaign_id),
                    "customer_id": followup["customer_id"],
                    "invoice_number": followup["invoice_number"],
                    "call_status": "queued",
                    "call_notes": json.dumps({"followup_id": followup["id"]}),
                })
                .execute()
            ).data
            if not call_log:
                raise HTTPException(status_code=500, detail="Unable to queue follow-up call")
            lang = customer.get("preferred_language", "en")
            if lang not in ("en", "hi", "ar"):
                lang = "en"
            if DEMO_MODE:
                supabase.table("call_logs").update({
                    "call_status": "completed",
                    "call_notes": json.dumps({
                        "followup_id": followup["id"],
                        "mode": "demo",
                        "demo_status": "demo_call_initiated",
                        "call_outcome": "demo_call_initiated",
                        "destination_masked": mask_phone_number(customer.get("phone")),
                        "selected_language": lang,
                        "payment_status": "pending",
                        "real_call_placed": False,
                    }, ensure_ascii=False),
                }).eq("id", call_log[0]["id"]).execute()
                queued += 1
                continue
            query = urlencode({
                "campaign_id": str(campaign_id),
                "customer_id": followup["customer_id"],
                "invoice_number": followup["invoice_number"],
                "language_code": lang,
            })
            try:
                result = twilio_post("Calls.json", {
                    "To": customer["phone"],
                    "From": TWILIO_FROM_NUMBER,
                    "Url": f"{PUBLIC_BASE_URL}/voice/start?{query}",
                    "Method": "POST",
                    "StatusCallback": f"{PUBLIC_BASE_URL}/voice/status",
                    "StatusCallbackMethod": "POST",
                    "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
                })
                notes = {
                    "followup_id": followup["id"],
                    "provider_call_sid": result.get("sid"),
                }
                supabase.table("call_logs").update({
                    "call_notes": json.dumps(notes),
                }).eq("id", call_log[0]["id"]).execute()
                supabase.table("payment_followups").update({
                    "status": "dispatched",
                }).eq("id", followup["id"]).execute()
                queued += 1
            except HTTPException:
                supabase.table("call_logs").update({
                    "call_status": "failed",
                }).eq("id", call_log[0]["id"]).execute()
                failed += 1

        if DEMO_MODE and queued:
            supabase.table("campaigns").update({
                "status": "completed",
            }).eq("id", str(campaign_id)).execute()

        return {
            "success": True,
            "queued": queued,
            "skipped": skipped,
            "failed": failed,
            "simulated": DEMO_MODE,
            "real_calls_placed": 0 if DEMO_MODE else None,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Follow-up dispatch failed")
        raise HTTPException(status_code=500, detail="Unable to dispatch payment follow-ups")


@app.post("/voice/start")
async def start_outbound_voice(request: Request):
    twilio_configuration_required()
    fields = await twilio_form(request)
    verify_twilio_request(request, fields)
    customer_id = request.query_params.get("customer_id", "")
    invoice_number = request.query_params.get("invoice_number", "")
    language_code = request.query_params.get("language_code", "en")
    if language_code not in ("en", "hi", "ar"):
        language_code = "en"

    customer = (
        supabase.table("customers")
        .select("customer_id, phone")
        .eq("customer_id", customer_id)
        .maybe_single()
        .execute()
    ).data
    invoice = (
        supabase.table("invoices")
        .select("invoice_number")
        .eq("customer_id", customer_id)
        .eq("invoice_number", invoice_number)
        .maybe_single()
        .execute()
    ).data
    if (
        not customer
        or not invoice
        or not same_phone(customer["phone"], fields.get("To", ""))
    ):
        return Response(
            twiml_message(
                {
                    "en": "We could not verify this call. Goodbye.",
                    "hi": "हम इस कॉल की पुष्टि नहीं कर सके। अलविदा।",
                    "ar": "تعذر علينا التحقق من هذه المكالمة. إلى اللقاء.",
                }[language_code],
                language_code,
            ),
            media_type="application/xml",
        )

    call_sid = fields.get("CallSid", "")
    if not call_sid:
        raise HTTPException(status_code=400, detail="Missing telephony call identifier")
    reply, _ = dialogflow_text(
        "Hello",
        call_sid,
        language_code,
        {
            "verified_phone": fields["To"],
            "campaign_id": request.query_params.get("campaign_id"),
            "call_sid": call_sid,
        },
    )
    action_url = f"{PUBLIC_BASE_URL}{request.url.path}?{request.url.query}"
    action_url = action_url.replace("/voice/start?", "/voice/turn?")
    return Response(
        twiml_message(reply, language_code, action_url),
        media_type="application/xml",
    )


@app.post("/voice/turn")
async def continue_outbound_voice(request: Request):
    twilio_configuration_required()
    fields = await twilio_form(request)
    verify_twilio_request(request, fields)
    call_sid = fields.get("CallSid", "")
    language_code = request.query_params.get("language_code", "en")
    customer_id = request.query_params.get("customer_id", "")
    if language_code not in ("en", "hi", "ar"):
        language_code = "en"
    speech = fields.get("SpeechResult", "").strip()
    if not call_sid:
        raise HTTPException(status_code=400, detail="Missing telephony call identifier")
    if speech:
        reply, _ = dialogflow_text(
            speech,
            call_sid,
            language_code,
            {
                "verified_phone": fields.get("To", ""),
                "campaign_id": request.query_params.get("campaign_id"),
                "call_sid": call_sid,
            },
        )
    else:
        reply = {
            "en": "I could not hear that. Please try again.",
            "hi": "मैं आपकी बात सुन नहीं सका। कृपया फिर से बोलें।",
            "ar": "لم أتمكن من سماع ذلك. يرجى المحاولة مرة أخرى.",
        }[language_code]

    call_log = (
        supabase.table("call_logs")
        .select("id, call_notes")
        .like("call_notes", f"%{call_sid}%")
        .limit(1)
        .execute()
    ).data
    if call_log:
        try:
            notes = json.loads(call_log[0].get("call_notes") or "{}")
        except json.JSONDecodeError:
            notes = {}
        notes.setdefault("turns", []).append({
            "recognized_text": speech,
            "assistant_text": reply,
        })
        supabase.table("call_logs").update({
            "call_notes": json.dumps(notes, ensure_ascii=False),
        }).eq("id", call_log[0]["id"]).execute()

    action_url = f"{PUBLIC_BASE_URL}{request.url.path}?{request.url.query}"
    return Response(
        twiml_message(reply, language_code, action_url),
        media_type="application/xml",
    )


@app.post("/voice/status")
async def update_outbound_call_status(request: Request):
    fields = await twilio_form(request)
    verify_twilio_request(request, fields)
    call_sid = fields.get("CallSid", "")
    provider_status = fields.get("CallStatus", "")
    if not call_sid or not provider_status:
        raise HTTPException(status_code=400, detail="Missing call status fields")
    status_map = {
        "queued": "queued",
        "initiated": "initiated",
        "ringing": "ringing",
        "in-progress": "in_progress",
        "completed": "completed",
        "busy": "failed",
        "no-answer": "failed",
        "canceled": "failed",
        "failed": "failed",
    }
    status = status_map.get(provider_status, "failed")
    rows = (
        supabase.table("call_logs")
        .select("id, campaign_id, call_notes")
        .like("call_notes", f"%{call_sid}%")
        .limit(1)
        .execute()
    ).data
    if rows:
        supabase.table("call_logs").update({
            "call_status": status,
        }).eq("id", rows[0]["id"]).execute()
        try:
            notes = json.loads(rows[0].get("call_notes") or "{}")
        except json.JSONDecodeError:
            notes = {}
        followup_id = notes.get("followup_id")
        if followup_id:
            supabase.table("payment_followups").update({
                "status": "completed" if status == "completed" else "scheduled" if status == "failed" else "dispatched",
            }).eq("id", followup_id).execute()
        campaign_id = rows[0].get("campaign_id")
        if campaign_id:
            remaining = (
                supabase.table("call_logs")
                .select("id")
                .eq("campaign_id", campaign_id)
                .in_("call_status", ["queued", "initiated", "ringing", "in_progress"])
                .limit(1)
                .execute()
            ).data
            if not remaining:
                failures = (
                    supabase.table("call_logs")
                    .select("id")
                    .eq("campaign_id", campaign_id)
                    .eq("call_status", "failed")
                    .limit(1)
                    .execute()
                ).data
                supabase.table("campaigns").update({
                    "status": "completed_with_failures" if failures else "completed",
                }).eq("id", campaign_id).execute()
    return {"success": True}


@app.post("/sms/status")
async def update_sms_status(request: Request):
    fields = await twilio_form(request)
    verify_twilio_request(request, fields)
    message_sid = fields.get("MessageSid", "")
    provider_status = fields.get("MessageStatus", "")
    if not message_sid or not provider_status:
        raise HTTPException(status_code=400, detail="Missing SMS status fields")
    status = {
        "queued": "sms_queued",
        "accepted": "sms_queued",
        "sending": "sms_queued",
        "sent": "sms_sent",
        "delivered": "sms_delivered",
        "undelivered": "sms_failed",
        "failed": "sms_failed",
    }.get(provider_status, "sms_failed")
    logs = (
        supabase.table("call_logs")
        .select("id, call_notes")
        .like("call_notes", f"%{message_sid}%")
        .limit(1)
        .execute()
    ).data
    if logs:
        supabase.table("call_logs").update({
            "call_status": status,
        }).eq("id", logs[0]["id"]).execute()
    return {"success": True}


@app.post("/stripe/webhook")
async def stripe_payment_webhook(request: Request):
    stripe_configuration_required()
    signature_header = request.headers.get("stripe-signature", "")
    raw_body = await request.body()
    signature_parts = {}
    for item in signature_header.split(","):
        key, separator, value = item.partition("=")
        if separator:
            signature_parts.setdefault(key, []).append(value)
    timestamp = (signature_parts.get("t") or [""])[0]
    signatures = signature_parts.get("v1", [])
    try:
        valid_timestamp = abs(time.time() - int(timestamp)) <= 300
    except ValueError:
        valid_timestamp = False
    expected_signature = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode(),
        timestamp.encode() + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if (
        not valid_timestamp
        or not any(hmac.compare_digest(expected_signature, item) for item in signatures)
    ):
        raise HTTPException(status_code=400, detail="Invalid payment webhook signature")

    try:
        event = json.loads(raw_body)
        if event.get("type") not in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
        ):
            return {"success": True, "ignored": True}
        session = event["data"]["object"]
        if session.get("payment_status") != "paid":
            return {"success": True, "verified": False}
        metadata = session.get("metadata") or {}
        customer_id = metadata.get("customer_id")
        invoice_number = metadata.get("invoice_number")
        if not customer_id or not invoice_number:
            raise HTTPException(status_code=400, detail="Payment metadata is incomplete")

        invoice = (
            supabase.table("invoices")
            .select("amount, status")
            .eq("customer_id", customer_id)
            .eq("invoice_number", invoice_number)
            .maybe_single()
            .execute()
        ).data
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        paid_amount = session.get("amount_total")
        if (
            session.get("currency", "").lower() != "inr"
            or paid_amount != round(float(invoice["amount"]) * 100)
        ):
            raise HTTPException(status_code=400, detail="Payment amount does not match invoice")
        if invoice["status"] != "paid":
            supabase.table("invoices").update({
                "status": "paid",
            }).eq("customer_id", customer_id).eq(
                "invoice_number", invoice_number
            ).in_("status", ["pending", "overdue"]).execute()
        supabase.table("payment_followups").update({
            "status": "paid",
        }).eq("customer_id", customer_id).eq(
            "invoice_number", invoice_number
        ).eq("status", "scheduled").execute()
        return {"success": True, "verified": True}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Payment webhook processing failed")
        raise HTTPException(status_code=500, detail="Unable to process payment confirmation")


@app.get("/demo/payment/{token}")
def get_demo_payment_details(token: str):
    if not DEMO_MODE or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise HTTPException(status_code=404, detail="Demo payment page not found")
    rows = (
        supabase.table("call_logs")
        .select("customer_id, invoice_number, call_notes")
        .eq("call_status", "completed")
        .like("call_notes", f"%{token}%")
        .limit(5)
        .execute()
    ).data or []
    matching = None
    for row in rows:
        try:
            notes = json.loads(row.get("call_notes") or "{}")
        except json.JSONDecodeError as error:
            logger.error("Invalid notes in a demo payment record")
            raise HTTPException(
                status_code=500,
                detail="Unable to read demo payment details",
            ) from error
        if notes.get("demo_token") == token:
            matching = (row, notes)
            break
    if not matching:
        raise HTTPException(status_code=404, detail="Demo payment page not found")
    row, notes = matching
    invoice = (
        supabase.table("invoices")
        .select("invoice_number, amount, due_date, status")
        .eq("customer_id", row["customer_id"])
        .eq("invoice_number", row["invoice_number"])
        .maybe_single()
        .execute()
    ).data
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return {
        "demo": True,
        "label": "TESTING ONLY",
        "invoice_number": invoice["invoice_number"],
        "amount": invoice["amount"],
        "due_date": invoice["due_date"],
        "invoice_status": invoice["status"],
        "payment_status": "pending",
        "sms_status": "simulated",
        "destination_masked": notes.get("destination_masked", "***"),
        "language_code": notes.get("selected_language", "en"),
    }


# EXISTING INVOICE API - PRESERVED
@app.get("/customer/{customer_id}/invoice")
def get_customer_invoice(customer_id: str):
    try:
        customer_result = (
            supabase.table("customers")
            .select("customer_id, full_name, phone, email, preferred_language")
            .eq("customer_id", customer_id)
            .maybe_single()
            .execute()
        )

        customer = (
            customer_result.data
            if customer_result is not None
            else None
        )

        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        invoice_result = (
            supabase.table("invoices")
            .select("invoice_number, amount, due_date, status")
            .eq("customer_id", customer_id)
            .order("due_date", desc=True)
            .limit(1)
            .execute()
        )

        if not invoice_result.data:
            raise HTTPException(status_code=404, detail="No invoice found")

        return {
            "success": True,
            "customer": customer,
            "invoice": invoice_result.data[0]
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to retrieve invoice")


# 1. GET ALL ACTIVE PRODUCTS
@app.get("/products")
def get_products():
    try:
        result = (
            supabase.table("products")
            .select("*")
            .eq("is_active", True)
            .order("product_name")
            .execute()
        )

        return {"success": True, "products": result.data}

    except Exception:
        raise HTTPException(status_code=500, detail="Unable to retrieve products")


# 2. GET A SPECIFIC PRODUCT
@app.get("/products/{product_id}")
def get_product(product_id: str):
    try:
        result = (
            supabase.table("products")
            .select("*")
            .eq("product_id", product_id)
            .maybe_single()
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Product not found")

        return {"success": True, "product": result.data}

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to retrieve product")


# 3. GET CUSTOMER'S ASSIGNED PRODUCT AND RENEWAL
@app.get("/customer/{customer_id}/renewal")
def get_customer_renewal(customer_id: str):
    try:
        customer_result = (
            supabase.table("customers")
            .select("customer_id, full_name, phone, email")
            .eq("customer_id", customer_id)
            .maybe_single()
            .execute()
        )

        if not customer_result.data:
            raise HTTPException(status_code=404, detail="Customer not found")

        renewal_result = (
            supabase.table("customer_product_renewals")
            .select("*")
            .eq("customer_id", customer_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if not renewal_result.data:
            raise HTTPException(status_code=404, detail="No renewal found")

        renewal = renewal_result.data[0]

        product_result = (
            supabase.table("products")
            .select("*")
            .eq("product_id", renewal["product_id"])
            .maybe_single()
            .execute()
        )

        return {
            "success": True,
            "customer": customer_result.data,
            "renewal": renewal,
            "product": product_result.data
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to retrieve renewal")


# 4. CUSTOMER AGREES TO RENEW
@app.post("/renewal/{renewal_id}/agree")
def customer_agrees(renewal_id: UUID):
    try:
        result = (
            supabase.table("customer_product_renewals")
            .select("id, renewal_status")
            .eq("id", str(renewal_id))
            .maybe_single()
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Renewal not found")

        if result.data["renewal_status"] in ("confirmed", "declined", "expired"):
            raise HTTPException(status_code=409, detail="Renewal is already closed")

        updated = (
            supabase.table("customer_product_renewals")
            .update({
                "renewal_status": "customer_agreed",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", str(renewal_id))
            .execute()
        )

        return {"success": True, "renewal": updated.data[0]}

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to update renewal")


# 5. GENERATE A UNIQUE CONFIRMATION LINK
@app.post("/renewal/{renewal_id}/confirmation-link")
def generate_confirmation_link(renewal_id: UUID):
    try:
        result = (
            supabase.table("customer_product_renewals")
            .select("*")
            .eq("id", str(renewal_id))
            .maybe_single()
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Renewal not found")

        renewal = result.data

        if renewal["renewal_status"] == "confirmed":
            raise HTTPException(status_code=409, detail="Renewal already confirmed")

        if renewal["renewal_status"] == "declined":
            raise HTTPException(status_code=409, detail="Renewal was declined")

        # Reuse an existing, unexpired confirmation link
        if (
            renewal.get("confirmation_token")
            and renewal.get("token_expires_at")
            and datetime.fromisoformat(
                renewal["token_expires_at"].replace("Z", "+00:00")
            ) > datetime.now(timezone.utc)
        ):
            return {
                "success": True,
                "confirmation_link": renewal["confirmation_link"]
            }

        token = uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        link = f"{FRONTEND_BASE_URL}/?token={token}"

        updated = (
            supabase.table("customer_product_renewals")
            .update({
                "confirmation_token": str(token),
                "confirmation_link": link,
                "token_expires_at": expires_at.isoformat(),
                "renewal_status": "confirmation_link_generated",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", str(renewal_id))
            .execute()
        )

        return {
            "success": True,
            "confirmation_link": link,
            "expires_at": expires_at.isoformat()
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to generate link")


# 6. CHECK CONFIRMATION LINK DETAILS
@app.get("/confirmation/{token}")
def get_confirmation_details(token: UUID):
    try:
        result = (
            supabase.table("customer_product_renewals")
            .select("*")
            .eq("confirmation_token", str(token))
            .maybe_single()
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Invalid confirmation link")

        renewal = result.data

        if renewal["renewal_status"] == "confirmed":
            return {"success": True, "status": "confirmed"}

        expiry = renewal.get("token_expires_at")

        if not expiry or datetime.fromisoformat(
            expiry.replace("Z", "+00:00")
        ) <= datetime.now(timezone.utc):
            supabase.table("customer_product_renewals").update({
                "renewal_status": "expired",
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", renewal["id"]).execute()

            raise HTTPException(status_code=410, detail="Confirmation link expired")

        customer = (
            supabase.table("customers")
            .select("full_name")
            .eq("customer_id", renewal["customer_id"])
            .maybe_single()
            .execute()
        )

        product = (
            supabase.table("products")
            .select("product_name, description")
            .eq("product_id", renewal["product_id"])
            .maybe_single()
            .execute()
        )

        return {
            "success": True,
            "customer": customer.data,
            "product": product.data,
            "renewal": {
                "amount": renewal["renewal_amount"],
                "billing_cycle": renewal["billing_cycle"],
                "status": renewal["renewal_status"]
            }
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to validate link")


# 7. CONFIRM RENEWAL
@app.post("/confirmation/{token}/confirm")
def confirm_renewal(token: UUID):
    try:
        result = (
            supabase.table("customer_product_renewals")
            .select("*")
            .eq("confirmation_token", str(token))
            .maybe_single()
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Invalid confirmation link")

        renewal = result.data

        if renewal["renewal_status"] == "confirmed":
            return {"success": True, "status": "confirmed"}

        if renewal["renewal_status"] in ("declined", "expired"):
            raise HTTPException(status_code=409, detail="Renewal cannot be confirmed")

        expiry = renewal.get("token_expires_at")

        if not expiry or datetime.fromisoformat(
            expiry.replace("Z", "+00:00")
        ) <= datetime.now(timezone.utc):
            supabase.table("customer_product_renewals").update({
                "renewal_status": "expired",
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", renewal["id"]).execute()

            raise HTTPException(status_code=410, detail="Confirmation link expired")

        updated = (
            supabase.table("customer_product_renewals")
            .update({
                "renewal_status": "confirmed",
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            .eq("id", renewal["id"])
            .execute()
        )

        return {
            "success": True,
            "status": "confirmed",
            "message": "Renewal confirmed successfully"
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to confirm renewal")

# 8. DIALOGFLOW CHAT REQUEST MODEL

class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    language_code: str = Field(default="en", pattern="^(en|hi|ar)$")


class PaymentFollowupRequest(BaseModel):
    customer_id: str = Field(min_length=1, max_length=100)
    invoice_number: str = Field(min_length=1, max_length=100)
    followup_date: str = Field(min_length=1, max_length=100)


_CUSTOMER_ID_PARAMETER_KEY = re.compile(r"[^a-z0-9]")
_CUSTOMER_ID_TOKEN = re.compile(
    r"(?i)(?<![A-Z0-9])CUST[-_ ]?([0-9]+)(?![A-Z0-9])"
)
_CUSTOMER_ID_FULL_VALUE = re.compile(r"(?i)CUST[-_ ]?([0-9]+)")
_CUSTOMER_ID_LABEL = re.compile(
    r"(?i)\bcustomer(?:\s*(?:id|identifier|number|no\.?|code))?\b|"
    r"ग्राहक\s*(?:आईडी|आई\.?डी\.?|पहचान|संख्या|नंबर|कोड)|"
    r"(?:رقم|معرف|مُعرّف|معرّف|هوية)\s+(?:ال)?عميل"
)
_OTHER_IDENTIFIER_LABEL = re.compile(
    r"(?i)\binvoice\b|\bamount\b|\bdate\b|"
    r"इनवॉइस|चालान|राशि|तारीख|दिनांक|الفاتورة|فاتورة|المبلغ|تاريخ"
)


def _is_customer_id_parameter_name(name) -> bool:
    normalized_name = _CUSTOMER_ID_PARAMETER_KEY.sub(
        "",
        str(name).casefold(),
    )
    return normalized_name == "customerid"


def _customer_ids_from_parameter_value(value) -> set[str]:
    if isinstance(value, str):
        return {
            f"CUST{match.group(1)}"
            for match in _CUSTOMER_ID_TOKEN.finditer(value)
        }
    if isinstance(value, dict):
        candidates = set()
        for nested_value in value.values():
            candidates.update(_customer_ids_from_parameter_value(nested_value))
        return candidates
    if isinstance(value, (list, tuple)):
        candidates = set()
        for nested_value in value:
            candidates.update(_customer_ids_from_parameter_value(nested_value))
        return candidates
    return set()


def _collect_customer_id_parameters(value, candidates: set[str]) -> None:
    if isinstance(value, dict):
        for name, parameter_value in value.items():
            if _is_customer_id_parameter_name(name):
                candidates.update(
                    _customer_ids_from_parameter_value(parameter_value)
                )
            elif isinstance(parameter_value, (dict, list, tuple)):
                _collect_customer_id_parameters(parameter_value, candidates)
    elif isinstance(value, (list, tuple)):
        for nested_value in value:
            _collect_customer_id_parameters(nested_value, candidates)


def _customer_ids_from_text(text: str) -> set[str]:
    candidates = set()
    stripped_text = text.strip().strip(".,!?;:()[]{}<>\"'")
    standalone_match = _CUSTOMER_ID_FULL_VALUE.fullmatch(stripped_text)
    for match in _CUSTOMER_ID_TOKEN.finditer(text):
        if standalone_match:
            candidates.add(f"CUST{standalone_match.group(1)}")
            continue

        prefix = text[max(0, match.start() - 96):match.start()]
        customer_labels = list(_CUSTOMER_ID_LABEL.finditer(prefix))
        if not customer_labels:
            continue
        other_labels = list(_OTHER_IDENTIFIER_LABEL.finditer(prefix))
        if not other_labels or customer_labels[-1].start() > other_labels[-1].start():
            candidates.add(f"CUST{match.group(1)}")
    return candidates


def extract_dialogflow_customer_id(webhook_body: dict) -> str | None:
    candidates: set[str] = set()
    for section_name in (
        "sessionInfo",
        "intentInfo",
        "fulfillmentInfo",
    ):
        section = webhook_body.get(section_name)
        if isinstance(section, dict):
            _collect_customer_id_parameters(
                section.get("parameters"),
                candidates,
            )

    page_info = webhook_body.get("pageInfo")
    form_info = page_info.get("formInfo") if isinstance(page_info, dict) else None
    form_parameters = (
        form_info.get("parameterInfo")
        if isinstance(form_info, dict)
        else []
    )
    if isinstance(form_parameters, list):
        for form_parameter in form_parameters:
            if (
                isinstance(form_parameter, dict)
                and _is_customer_id_parameter_name(
                    form_parameter.get("displayName", "")
                )
            ):
                for value_key in ("value", "originalValue"):
                    candidates.update(
                        _customer_ids_from_parameter_value(
                            form_parameter.get(value_key)
                        )
                    )

    for text_key in ("text", "transcript"):
        text = webhook_body.get(text_key)
        if isinstance(text, str):
            candidates.update(_customer_ids_from_text(text))

    if len(candidates) != 1:
        return None
    return candidates.pop()


def invoice_fallback_reply(
    customer_id: str,
    language_code: str,
    verified_phone: str | None = None,
):
    customer_response = (
        supabase.table("customers")
        .select("full_name, phone")
        .eq("customer_id", customer_id)
        .maybe_single()
        .execute()
    )
    customer = (
        customer_response.data
        if customer_response is not None
        else None
    )
    if not customer:
        return {
            "en": "Sorry, I couldn't find your customer account. Please check your Customer ID.",
            "hi": "क्षमा करें, आपका ग्राहक खाता नहीं मिला। कृपया अपना ग्राहक आईडी जांचें।",
            "ar": "عذرًا، لم نتمكن من العثور على حساب العميل الخاص بك. يرجى التحقق من رقم العميل.",
        }[language_code]
    if verified_phone and not same_phone(customer["phone"], verified_phone):
        return {
            "en": "I couldn't verify your identity. Please contact us from your registered number.",
            "hi": "आपकी पहचान की पुष्टि नहीं हो सकी। कृपया अपने पंजीकृत नंबर से संपर्क करें।",
            "ar": "تعذر التحقق من هويتك. يرجى الاتصال من رقمك المسجل.",
        }[language_code]

    invoices_response = (
        supabase.table("invoices")
        .select("invoice_number, amount, due_date, status")
        .eq("customer_id", customer_id)
        .order("due_date", desc=True)
        .limit(1)
        .execute()
    )
    invoices = supabase_response_data(invoices_response, "invoice fallback invoice lookup")
    if not invoices:
        return {
            "en": f"Thank you, {customer['full_name']}. I couldn't find an invoice for your account.",
            "hi": f"धन्यवाद, {customer['full_name']}। आपके खाते के लिए कोई इनवॉइस नहीं मिला।",
            "ar": f"شكرًا لك، {customer['full_name']}. لم نعثر على فاتورة لحسابك.",
        }[language_code]

    invoice = invoices[0]
    amount = float(invoice["amount"])
    status = str(invoice["status"]).lower()
    due_date = invoice["due_date"]
    if language_code == "hi":
        status_text = {
            "pending": "लंबित",
            "paid": "भुगतान किया गया",
            "overdue": "बकाया",
        }.get(status, status)
        return (
            f"धन्यवाद, {customer['full_name']}। आपका इनवॉइस नंबर "
            f"{invoice['invoice_number']} है। बकाया राशि ₹{amount:,.2f} है। "
            f"भुगतान की अंतिम तारीख {due_date} है। स्थिति {status_text} है।"
        )
    if language_code == "ar":
        status_text = {
            "pending": "قيد الانتظار",
            "paid": "مدفوعة",
            "overdue": "متأخرة",
        }.get(status, status)
        return (
            f"شكرًا لك، {customer['full_name']}. رقم الفاتورة "
            f"{invoice['invoice_number']}. المبلغ المستحق {amount:,.2f} روبية. "
            f"تاريخ الاستحقاق {due_date}. الحالة الحالية {status_text}."
        )

    try:
        parsed_due_date = date.fromisoformat(due_date)
        formatted_due_date = (
            parsed_due_date.strftime("%B")
            + f" {parsed_due_date.day}, {parsed_due_date.year}"
        )
    except (TypeError, ValueError):
        formatted_due_date = due_date
    return (
        f"Thank you, {customer['full_name']}. Your invoice number is "
        f"{invoice['invoice_number']}. The outstanding amount is ₹{amount:,.2f}. "
        f"The due date is {formatted_due_date}. The current invoice status is {status}."
    )


@app.post("/payment-followup")
def create_payment_followup(payload: PaymentFollowupRequest):
    try:
        followup, created = save_payment_followup(
            payload.customer_id,
            payload.invoice_number,
            payload.followup_date,
        )
        return {
            "success": True,
            "followup": followup,
            "message": (
                "Payment follow-up recorded successfully"
                if created else "Payment follow-up already recorded"
            )
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Payment follow-up error")
        raise HTTPException(status_code=500, detail="Unable to record payment follow-up")

# 9. CHAT WITH DIALOGFLOW CX

@app.post("/chat")
def chat_with_dialogflow(request: ChatRequest):
    try:
        # Reuse the session ID to preserve conversation context
        session_id = request.session_id or str(uuid4())

        session_path = dialogflow_client.session_path(
            DIALOGFLOW_PROJECT_ID,
            DIALOGFLOW_LOCATION,
            DIALOGFLOW_AGENT_ID,
            session_id
        )

        query_input = dialogflowcx.QueryInput(
            text=dialogflowcx.TextInput(
                text=request.message
            ),
            language_code=request.language_code
        )

        detect_request = dialogflowcx.DetectIntentRequest(
            session=session_path,
            query_input=query_input
        )

        response = dialogflow_client.detect_intent(
            request=detect_request
        )

        query_result = response.query_result

        # Extract text responses from Dialogflow
        replies = []

        for message in query_result.response_messages:
            if message.text and message.text.text:
                replies.extend(message.text.text)

        page_name = query_result.current_page.display_name
        has_invoice_details = any(
            marker in reply.lower()
            for reply in replies
            for marker in ("invoice number", "इनवॉइस नंबर", "رقم الفاتورة")
        )
        matched_intent = (
            query_result.match.intent.display_name
            if query_result.match and query_result.match.intent
            else None
        )
        if (
            page_name in ("Fetch Invoice Details", "Payment Decision")
            and not has_invoice_details
            and matched_intent not in ("Pay Now", "Pay Later")
        ):
            customer_id = extract_dialogflow_customer_id({
                "sessionInfo": {
                    "parameters": dict(query_result.parameters or {})
                },
                "text": request.message,
            })
            if customer_id:
                logger.warning(
                    "Dialogflow returned no text on invoice page; recovering from Supabase"
                )
                invoice_reply = invoice_fallback_reply(
                    str(customer_id), request.language_code
                )
                if replies:
                    replies.insert(0, invoice_reply)
                else:
                    replies.append(invoice_reply)

        if matched_intent == "Pay Now" and any(
            "demo_payment=" in reply for reply in replies
        ):
            payment_url_match = re.search(
                r"https?://[^\s]+",
                "\n".join(replies),
            )
            if payment_url_match:
                payment_url = payment_url_match.group(0).rstrip(".,!?")
                parts = urlsplit(payment_url)
                query = parse_qs(parts.query, keep_blank_values=True)
                query["lang"] = [request.language_code]
                localized_url = urlunsplit(
                    (
                        parts.scheme,
                        parts.netloc,
                        parts.path,
                        urlencode(query, doseq=True),
                        parts.fragment,
                    )
                )
                replies = [
                    {
                        "en": (
                            f"A testing payment link was generated: {localized_url} "
                            "SMS was not sent, and payment has not been received. Thank you."
                        ),
                        "hi": (
                            f"परीक्षण के लिए भुगतान लिंक बनाया गया है: {localized_url} "
                            "SMS नहीं भेजा गया है और भुगतान प्राप्त नहीं हुआ है। धन्यवाद।"
                        ),
                        "ar": (
                            f"تم إنشاء رابط دفع للاختبار: {localized_url} "
                            "لم تُرسل رسالة نصية ولم يتم استلام أي دفعة. شكرًا لك."
                        ),
                    }[request.language_code]
                ]
            else:
                replies = [
                    {
                        "en": "I couldn't generate a testing payment link. SMS was not sent, and payment has not been received.",
                        "hi": "अभी परीक्षण भुगतान लिंक नहीं बनाया जा सका। SMS नहीं भेजा गया है और भुगतान प्राप्त नहीं हुआ है।",
                        "ar": "تعذر إنشاء رابط دفع للاختبار الآن. لم تُرسل رسالة نصية ولم يتم استلام أي دفعة.",
                    }[request.language_code]
                ]

        reply = "\n".join(replies)
        chat_response = {
            "success": True,
            "session_id": session_id,
            "reply": reply,
            "intent": (
                query_result.match.intent.display_name
                if query_result.match and query_result.match.intent
                else None
            ),
            "current_page": page_name
        }
        if request.language_code == "ar":
            try:
                chat_response["audio_base64"] = synthesize_arabic_tts(reply)
                chat_response["audio_error"] = None
            except GoogleTTSUnavailableError as error:
                logger.warning(
                    "Arabic Google Cloud TTS unavailable "
                    "(status=%s, reason=%s)",
                    error.status_code,
                    error.reason,
                )
                chat_response["audio_base64"] = None
                chat_response["audio_error"] = error.reason
        return chat_response

    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Dialogflow chat error")

        raise HTTPException(
            status_code=500,
            detail="Unable to communicate with Dialogflow CX"
        )

# 8. DIALOGFLOW CX WEBHOOK


# ==========================================
# DIALOGFLOW CX WEBHOOK
# ==========================================

@app.post("/dialogflow-webhook")
async def dialogflow_webhook(request: Request):
    try:
        received_secret = request.headers.get("x-webhook-secret", "")
        if not DIALOGFLOW_WEBHOOK_SECRET or not hmac.compare_digest(
            received_secret,
            DIALOGFLOW_WEBHOOK_SECRET
        ):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized webhook request"
            )
        body = await request.json()
        language_code = body.get("languageCode") or "en"
        if language_code not in ("en", "hi", "ar"):
            language_code = "en"

        fulfillment_tag = body.get("fulfillmentInfo", {}).get("tag", "")
        session_info = body.get("sessionInfo", {})
        parameters = dict(session_info.get("parameters", {}))
        form_parameters = (
            body.get("pageInfo", {})
            .get("formInfo", {})
            .get("parameterInfo", [])
        )
        for form_parameter in form_parameters:
            name = form_parameter.get("displayName")
            value = form_parameter.get("value")
            if (
                name in ("followup_date", "expected_payment_date")
                and form_parameter.get("state") == "FILLED"
                and value is not None
            ):
                parameters.setdefault(name, value)
        customer_id = extract_dialogflow_customer_id(body)
        verified_phone = parameters.get("verified_phone")
        campaign_id = parameters.get("campaign_id")
        call_sid = parameters.get("call_sid")

        text = body.get("text", body.get("transcript", ""))
        conversation_complete = False

        logger.info(
            "Dialogflow customer ID diagnostic: language=%s, "
            "customer_id_present=%s, empty_after_trimming=%s, "
            "parameter_value_length=%d, "
            "expected_format_match=%s",
            language_code,
            customer_id is not None,
            not bool(customer_id),
            len(customer_id or ""),
            bool(re.fullmatch(r"(?i)CUST\d+", customer_id or "")),
        )

        if not customer_id:
            if language_code == "hi":
                reply = "मुझे आपका ग्राहक आईडी नहीं मिला। कृपया अपना ग्राहक आईडी दोबारा बताएं।"
            elif language_code == "ar":
                reply = "لم أتمكن من تحديد رقم العميل الخاص بك. يرجى إدخال رقم العميل مرة أخرى."
            else:
                reply = "I couldn't identify your Customer ID. Could you please provide it again?"
        else:
            try:
                customer_result = (
                    supabase.table("customers")
                    .select("customer_id, full_name, phone")
                    .eq("customer_id", customer_id)
                    .maybe_single()
                    .execute()
                )
            except Exception as error:
                error_message = str(getattr(error, "message", None) or error)
                for sensitive_value in (SUPABASE_URL, SUPABASE_KEY, customer_id):
                    if sensitive_value:
                        error_message = error_message.replace(
                            sensitive_value,
                            "[REDACTED]",
                        )
                error_message = " ".join(error_message.split())[:500]
                logger.error(
                    "Supabase Dialogflow customer lookup failed "
                    "(error_type=%s, code=%s, status=%s): %s",
                    type(error).__name__,
                    getattr(error, "code", None),
                    getattr(error, "status_code", None),
                    error_message or "[empty error message]",
                )
                raise HTTPException(
                    status_code=503,
                    detail="Unable to look up the customer. Please retry.",
                ) from None

            customer_record_found = bool(
                customer_result is not None
                and getattr(customer_result, "data", None)
            )
            logger.info(
                "Dialogflow customer lookup result: language=%s, record_found=%s, "
                "no_matching_record=%s",
                language_code,
                customer_record_found,
                not customer_record_found,
            )

            customer = (
                supabase_response_data(
                    customer_result,
                    "Dialogflow customer lookup",
                )
                if customer_result is not None
                else None
            )
            if not customer:
                logger.info(
                    "Supabase Dialogflow customer lookup completed with no matching row"
                )
                if language_code == "hi":
                    reply = "क्षमा करें, आपका ग्राहक खाता नहीं मिला। कृपया अपना ग्राहक आईडी जांचें।"
                elif language_code == "ar":
                    reply = "عذرًا، لم نتمكن من العثور على حساب العميل الخاص بك. يرجى التحقق من رقم العميل."
                else:
                    reply = "Sorry, I couldn't find your customer account. Please check your Customer ID."
            else:
                call_metadata = {
                    "campaign_id": str(campaign_id) if campaign_id else None,
                    "customer_id": customer_id,
                    "selected_language": language_code,
                    "call_id": call_sid,
                }
                if fulfillment_tag != "pay_later":
                    call_metadata["customer_verified"] = bool(
                        verified_phone
                        and same_phone(customer["phone"], str(verified_phone))
                    )
                update_campaign_call_log(call_sid, call_metadata)
            if (
                customer
                and fulfillment_tag != "pay_later"
                and verified_phone
                and not same_phone(
                customer["phone"],
                str(verified_phone),
                )
            ):
                if language_code == "hi":
                    reply = "आपकी पहचान की पुष्टि नहीं हो सकी। कृपया अपने पंजीकृत नंबर से संपर्क करें।"
                elif language_code == "ar":
                    reply = "تعذر التحقق من هويتك. يرجى الاتصال من رقمك المسجل."
                else:
                    reply = "I couldn't verify your identity. Please contact us from your registered number."
            elif customer and (not verified_phone or same_phone(customer["phone"], str(verified_phone))):
                invoice_result = (
                    supabase.table("invoices")
                    .select("invoice_number, amount, due_date, status")
                    .eq("customer_id", customer_id)
                    .order("due_date", desc=True)
                    .limit(1)
                    .execute()
                )
                invoices = supabase_response_data(
                    invoice_result,
                    "Dialogflow invoice lookup",
                )
                if not invoices:
                    if language_code == "hi":
                        reply = (
                            f"धन्यवाद, {customer['full_name']}। "
                            "हालांकि, आपके खाते के लिए कोई इनवॉइस नहीं मिला।"
                        )
                    elif language_code == "ar":
                        reply = (
                            f"شكرًا لك، {customer['full_name']}. "
                            "لم نتمكن من العثور على فاتورة لحسابك."
                        )
                    else:
                        reply = (
                            f"Thank you, {customer['full_name']}. "
                            "However, I couldn't find an invoice for your account."
                        )
                else:
                    invoice = invoices[0]
                    amount = float(invoice["amount"])
                    due_date = invoice["due_date"]
                    invoice_number = invoice["invoice_number"]
                    status = invoice["status"]
                    update_campaign_call_log(call_sid, {
                        "invoice_number": invoice_number,
                        "outstanding_amount": amount,
                        "invoice_status": status,
                    })
                    if fulfillment_tag in ("pay_now", "send_payment_link"):
                        registered_phone = str(customer.get("phone") or "").strip()
                        if not is_valid_e164_phone(registered_phone):
                            update_campaign_call_log(call_sid, {
                                "payment_decision": "pay_now",
                                "payment_link_status": "not_created",
                                "sms_status": "not_sent",
                                "call_outcome": "registered_phone_missing_or_invalid",
                            })
                            reply = {
                                "en": "I don't have a valid registered mobile number on file, so I couldn't send a payment link.",
                                "hi": "मेरे पास फ़ाइल में वैध पंजीकृत मोबाइल नंबर नहीं है, इसलिए मैं भुगतान लिंक नहीं भेज सका।",
                                "ar": "لا يوجد لدي رقم هاتف محمول مسجل صالح، لذلك لم أتمكن من إرسال رابط الدفع.",
                            }[language_code]
                        elif status.lower() == "paid":
                            conversation_complete = True
                            update_campaign_call_log(call_sid, {
                                "payment_decision": "pay_now",
                                "payment_status": "paid",
                                "call_outcome": "invoice_already_paid",
                            })
                            reply = {
                                "en": "Our records show that this invoice is already paid.",
                                "hi": "हमारे रिकॉर्ड में यह इनवॉइस पहले से भुगतान किया गया दिख रहा है।",
                                "ar": "تُظهر سجلاتنا أن هذه الفاتورة مدفوعة بالفعل.",
                            }[language_code]
                        elif DEMO_MODE:
                            payment = create_demo_payment_link(
                                customer,
                                invoice,
                                str(campaign_id) if campaign_id else None,
                                str(call_sid) if call_sid else None,
                                language_code,
                            )
                            update_campaign_call_log(call_sid, {
                                "payment_decision": "pay_now",
                                "payment_link_status": "generated_demo",
                                "sms_status": "simulated",
                                "destination_masked": payment["destination_masked"],
                                "payment_status": "pending",
                                "call_outcome": "demo_payment_link_generated",
                            })
                            conversation_complete = True
                            reply = {
                                "en": (
                                    f"A testing payment link was generated for invoice "
                                    f"{invoice_number}, amount ₹{amount:,.2f}:\n"
                                    f"{payment['payment_url']}\nSMS was not sent, and payment has not been received."
                                ),
                                "hi": (
                                    f"इनवॉइस {invoice_number}, राशि ₹{amount:,.2f} के लिए "
                                    f"परीक्षण हेतु भुगतान लिंक बनाया गया है:\n"
                                    f"{payment['payment_url']}\nSMS नहीं भेजा गया है और भुगतान प्राप्त नहीं हुआ है।"
                                ),
                                "ar": (
                                    f"تم إنشاء رابط دفع للاختبار للفاتورة {invoice_number} "
                                    f"بمبلغ ₹{amount:,.2f}:\n"
                                    f"{payment['payment_url']}\nلم تُرسل رسالة نصية ولم يتم استلام أي دفعة."
                                ),
                            }[language_code]
                        else:
                            try:
                                payment = send_invoice_payment_link(
                                    {**customer, "phone": registered_phone},
                                    invoice,
                                    str(campaign_id) if campaign_id else None,
                                    str(call_sid) if call_sid else None,
                                    language_code,
                                )
                                update_campaign_call_log(call_sid, {
                                    "payment_decision": "pay_now",
                                    "payment_link_status": "created",
                                    "sms_status": payment["sms_status"],
                                    "sms_sid": payment.get("sms_sid"),
                                })
                                if payment["sms_status"] not in (
                                    "accepted",
                                    "queued",
                                    "sending",
                                    "sent",
                                    "delivered",
                                ):
                                    update_campaign_call_log(call_sid, {
                                        "call_outcome": "sms_not_accepted",
                                    })
                                    reply = {
                                        "en": "A payment link was created, but the SMS provider did not accept the message. Please contact support.",
                                        "hi": "भुगतान लिंक बनाया गया, लेकिन SMS प्रदाता ने संदेश स्वीकार नहीं किया। कृपया सहायता से संपर्क करें।",
                                        "ar": "تم إنشاء رابط الدفع، لكن مزود الرسائل لم يقبل الرسالة. يرجى التواصل مع الدعم.",
                                    }[language_code]
                                else:
                                    conversation_complete = True
                                    update_campaign_call_log(call_sid, {
                                        "call_outcome": "payment_link_sms_accepted",
                                    })
                                    reply = {
                                        "en": "Certainly. The secure payment link has been accepted for SMS delivery to your registered mobile number. Please complete your payment using that link. Thank you!",
                                        "hi": "ज़रूर। सुरक्षित भुगतान लिंक आपके पंजीकृत मोबाइल नंबर पर SMS से भेजने के लिए स्वीकार कर लिया गया है। कृपया उस लिंक से भुगतान पूरा करें। धन्यवाद!",
                                        "ar": "بالتأكيد. تم قبول رابط الدفع الآمن لإرساله عبر رسالة نصية إلى رقم هاتفك المسجل. يرجى إتمام الدفع باستخدام الرابط. شكرًا لك!",
                                    }[language_code]
                            except HTTPException:
                                logger.warning("Payment link or SMS provider is not configured or unavailable")
                                update_campaign_call_log(call_sid, {
                                    "payment_decision": "pay_now",
                                    "payment_link_status": "not_created",
                                    "sms_status": "not_sent",
                                    "call_outcome": "payment_link_unavailable",
                                })
                                reply = {
                                    "en": "I couldn't send a payment link right now. Please try again later.",
                                    "hi": "अभी भुगतान लिंक नहीं भेजा जा सका। कृपया बाद में फिर प्रयास करें।",
                                    "ar": "تعذر إرسال رابط الدفع الآن. يرجى المحاولة لاحقًا.",
                                }[language_code]
                    elif fulfillment_tag in ("pay_later", "schedule_followup"):
                        if status.lower() == "paid":
                            conversation_complete = True
                            update_campaign_call_log(call_sid, {
                                "payment_decision": "pay_later",
                                "payment_status": "paid",
                                "call_outcome": "invoice_already_paid",
                            })
                            reply = {
                                "en": "Our records show that this invoice is already paid.",
                                "hi": "हमारे रिकॉर्ड में यह इनवॉइस पहले से भुगतान किया गया दिख रहा है।",
                                "ar": "تُظهر سجلاتنا أن هذه الفاتورة مدفوعة بالفعل.",
                            }[language_code]
                        else:
                            requested_date = (
                                parameters.get("followup_date")
                                or parameters.get("expected_payment_date")
                            )
                            requested_date = normalize_dialogflow_date(requested_date)
                            if not requested_date:
                                update_campaign_call_log(call_sid, {
                                    "payment_decision": "pay_later",
                                    "followup_status": "awaiting_date",
                                })
                                reply = {
                                    "en": "On which date would you prefer to make the payment?",
                                    "hi": "आप किस तारीख को भुगतान करना चाहेंगे?",
                                    "ar": "في أي تاريخ تفضل سداد المبلغ؟",
                                }[language_code]
                            else:
                                try:
                                    validated_date = validate_dialogflow_date(
                                        requested_date,
                                        str(text or ""),
                                    )
                                    followup, _ = save_payment_followup(
                                        customer_id,
                                        invoice_number,
                                        validated_date,
                                    )
                                    if DEMO_MODE:
                                        record_demo_followup_action(
                                            customer_id,
                                            invoice_number,
                                            followup,
                                            str(campaign_id) if campaign_id else None,
                                            str(call_sid) if call_sid else None,
                                            language_code,
                                        )
                                    update_campaign_call_log(call_sid, {
                                        "payment_decision": "pay_later",
                                        "preferred_payment_date": followup["followup_date"],
                                        "followup_status": followup["status"],
                                        "call_outcome": "followup_scheduled",
                                    })
                                    conversation_complete = True
                                    reply = {
                                        "en": f"Your payment follow-up is set for {followup['followup_date']}. Thank you.",
                                        "hi": f"आपका भुगतान फ़ॉलो-अप {followup['followup_date']} के लिए दर्ज है। धन्यवाद।",
                                        "ar": f"تم تسجيل متابعة الدفع بتاريخ {followup['followup_date']}. شكرًا لك.",
                                    }[language_code]
                                except HTTPException:
                                    update_campaign_call_log(call_sid, {
                                        "payment_decision": "pay_later",
                                        "followup_status": "invalid_date",
                                    })
                                    reply = {
                                        "en": "I couldn't record that date. Please give a valid future date.",
                                        "hi": "यह तारीख दर्ज नहीं हो सकी। कृपया भविष्य की सही तारीख बताएं।",
                                        "ar": "تعذر تسجيل هذا التاريخ. يرجى إدخال تاريخ صحيح في المستقبل.",
                                    }[language_code]
                    elif language_code == "hi":
                        translated_status = {
                            "pending": "लंबित",
                            "paid": "भुगतान किया गया",
                            "overdue": "भुगतान की समय सीमा समाप्त"
                        }.get(status.lower(), status)
                        reply = (
                            f"धन्यवाद, {customer['full_name']}। "
                            f"आपका इनवॉइस नंबर {invoice_number} है। "
                            f"बकाया राशि {amount:,.2f} रुपये है। "
                            f"भुगतान की अंतिम तारीख {due_date} है। "
                            f"इनवॉइस की वर्तमान स्थिति {translated_status} है।"
                        )
                    elif language_code == "ar":
                        translated_status = {
                            "pending": "قيد الانتظار",
                            "paid": "مدفوعة",
                            "overdue": "متأخرة"
                        }.get(status.lower(), status)
                        reply = (
                            f"شكرًا لك، {customer['full_name']}. "
                            f"رقم الفاتورة هو {invoice_number}. "
                            f"المبلغ المستحق هو {amount:,.2f} روبية. "
                            f"تاريخ استحقاق الدفع هو {due_date}. "
                            f"حالة الفاتورة الحالية هي {translated_status}."
                        )
                    else:
                        reply = (
                            f"Thank you, {customer['full_name']}. "
                            f"Your invoice number is {invoice_number}. "
                            f"The outstanding amount is rupees {amount:,.2f}. "
                            f"The due date is {due_date}. "
                            f"The current invoice status is {status}."
                        )
        response = {
            "fulfillmentResponse": {
                "messages": [
                    {
                        "text": {
                            "text": [reply]
                        }
                    }
                ]
            },
            "sessionInfo": {
                "parameters": {
                    **parameters,
                    "customer_id": customer_id,
                    "webhook_language": language_code,
                    "verified_phone": verified_phone,
                    "campaign_id": campaign_id,
                    "call_sid": call_sid,
                }
            },
        }
        if customer_id and conversation_complete:
            response["targetPage"] = DIALOGFLOW_THANK_YOU_PAGE
        return response
    except HTTPException:
        raise
    except Exception:
        logger.exception("Dialogflow webhook error")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve customer invoice"
        )
