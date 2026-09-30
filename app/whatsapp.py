"""WhatsApp tap-to-send: each message's wording, and the link that opens WhatsApp with it
written. The receptionist presses send in their own WhatsApp, so messages go from the
clinic's own number. Nothing here talks to WhatsApp; the automatic version comes later."""
import re
import secrets
import string
import uuid
from datetime import timedelta
from urllib.parse import quote

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Clinic, MessageTemplate, ShareLink
from app.security import hash_token, now

LANGUAGES = ("en", "hi")

# A shared prescription, invoice or estimate opens for this long.
SHARE_LINK_DAYS = 30

# What each message may mention. Messages that carry a document must keep its {link}.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "visit_reminder": ("name", "clinic", "date", "reason"),
    "call_reminder": ("name", "clinic", "date", "reason"),
    "general": ("name", "clinic"),
    "prescription": ("name", "clinic", "doctor", "date", "link"),
    "invoice": ("name", "clinic", "number", "amount", "link"),
    "estimate": ("name", "clinic", "number", "valid_until", "link"),
    "review_request": ("name", "clinic", "treatment", "review_link"),
    "lab_ready": ("name", "clinic", "treatment", "date"),
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "prescription": ("link",),
    "invoice": ("link",),
    "estimate": ("link",),
    "review_request": ("review_link",),
}
KINDS = tuple(PLACEHOLDERS)

DEFAULTS: dict[tuple[str, str], str] = {
    ("visit_reminder", "en"): (
        "Hello {name}, this is {clinic}. A reminder of your dental visit on {date}. "
        "If you need another day, please reply to this message."
    ),
    ("visit_reminder", "hi"): (
        "नमस्ते {name}, {clinic} से संदेश। आपकी डेंटल विज़िट {date} को है। "
        "अगर दिन बदलना हो तो कृपया इस संदेश का जवाब दें।"
    ),
    ("call_reminder", "en"): (
        "Hello {name}, this is {clinic}. We wanted to check how you are doing after your treatment. "
        "Please reply here or call us if anything troubles you."
    ),
    ("call_reminder", "hi"): (
        "नमस्ते {name}, {clinic} से संदेश। इलाज के बाद आप कैसा महसूस कर रहे हैं? "
        "कोई परेशानी हो तो यहाँ जवाब दें या हमें कॉल करें।"
    ),
    ("general", "en"): "Hello {name}, this is {clinic}.",
    ("general", "hi"): "नमस्ते {name}, {clinic} से संदेश।",
    ("prescription", "en"): (
        "Hello {name}, here is your prescription from {doctor} at {clinic}: {link}\nThe link works for 30 days."
    ),
    ("prescription", "hi"): "नमस्ते {name}, {clinic} में {doctor} का पर्चा यहाँ है: {link}\nयह लिंक 30 दिन तक चलेगा।",
    ("invoice", "en"): "Hello {name}, here is your invoice {number} from {clinic} for {amount}: {link}\nThank you.",
    ("invoice", "hi"): "नमस्ते {name}, {clinic} का बिल {number} ({amount}) यहाँ है: {link}\nधन्यवाद।",
    ("estimate", "en"): (
        "Hello {name}, here is your treatment estimate {number} from {clinic}: {link}\n"
        "It is valid until {valid_until}. Please ask us if you have any questions."
    ),
    ("estimate", "hi"): (
        "नमस्ते {name}, {clinic} से आपके इलाज का अनुमान {number} यहाँ है: {link}\n"
        "यह {valid_until} तक मान्य है। कोई सवाल हो तो हमसे पूछें।"
    ),
    ("review_request", "en"): (
        "Hello {name}, thank you for choosing {clinic}. We hope your {treatment} went well. "
        "If you have a minute, we would be grateful for a review: {review_link}"
    ),
    ("review_request", "hi"): (
        "नमस्ते {name}, {clinic} चुनने के लिए धन्यवाद। आशा है आपका {treatment} अच्छा रहा। "
        "अगर एक मिनट हो तो कृपया अपनी राय यहाँ लिखें: {review_link}"
    ),
    ("lab_ready", "en"): "Hello {name}, this is {clinic}. Your {treatment} is ready. Please visit us on {date} for the fitting.",
    ("lab_ready", "hi"): "नमस्ते {name}, {clinic} से संदेश। आपका {treatment} तैयार है। कृपया {date} को फिटिंग के लिए आएँ।",
}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def check_wording(kind: str, body: str) -> str:
    """The wording as saved, or a clear message about what is wrong with it."""
    body = body.strip()
    allowed = PLACEHOLDERS[kind]
    unknown = [p for p in _PLACEHOLDER.findall(body) if p not in allowed]
    if unknown:
        choices = ", ".join("{" + p + "}" for p in allowed)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{{{unknown[0]}}} cannot be used in this message. You can use {choices}.",
        )
    missing = [p for p in REQUIRED.get(kind, ()) if "{" + p + "}" not in body]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"This message must keep {{{missing[0]}}}, or the patient cannot open it.",
        )
    return body


def saved_wording(db: Session, clinic: Clinic) -> dict[tuple[str, str], MessageTemplate]:
    return {(t.kind, t.language): t for t in db.scalars(select(MessageTemplate))}


def wording(db: Session, clinic: Clinic, kind: str, language: str) -> str:
    """The clinic's own wording for this message, or Denti's default."""
    saved = db.get(MessageTemplate, {"clinic_id": clinic.id, "kind": kind, "language": language})
    return saved.body if saved else DEFAULTS[(kind, language)]


def render(body: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), body)


def whatsapp_number(phone: str) -> str | None:
    """The number as WhatsApp links want it: country code and digits only. A 10 digit number
    is taken as Indian."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        return "91" + digits
    if 11 <= len(digits) <= 15 and not phone.strip().startswith("0"):
        return digits
    return None


def chat_url(number: str, text: str) -> str:
    """Opens WhatsApp on the phone or computer, with the message written and ready to send."""
    return f"https://wa.me/{number}?text={quote(text, safe='')}"


def first_name(name: str) -> str:
    parts = name.split()
    return parts[0] if parts else name


_TOKEN_LETTERS = string.ascii_letters + string.digits


def _link_token() -> str:
    """43 letters and digits (about 256 random bits). No dashes or underscores, so the link
    reads cleanly in a message and is never cut short by WhatsApp."""
    return "".join(secrets.choice(_TOKEN_LETTERS) for _ in range(43))


def new_share_link(db: Session, clinic: Clinic, *, patient_id: uuid.UUID, kind: str, created_by: uuid.UUID, **target) -> tuple[ShareLink, str]:
    """A fresh link to one document, and its web address. Only the token's hash is stored."""
    token = _link_token()
    link = ShareLink(
        clinic_id=clinic.id,
        patient_id=patient_id,
        kind=kind,
        token_hash=hash_token(token),
        created_by=created_by,
        expires_at=now() + timedelta(days=SHARE_LINK_DAYS),
        **target,
    )
    db.add(link)
    db.flush()
    return link, f"{settings.app_base_url.rstrip('/')}/{clinic.slug}/d/{token}"


def find_share_link(db: Session, token: str) -> ShareLink | None:
    return db.scalar(select(ShareLink).where(ShareLink.token_hash == hash_token(token)))
