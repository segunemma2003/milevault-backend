import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.kyc import KycDocument
from app.models.user import User
from app.dependencies import get_current_user
from app.services.notification_service import create_notification
from app.config import settings

router = APIRouter(prefix="/kyc", tags=["kyc"])


async def _read_upload_request(request: Request):
    """Accept either JSON {"s3_key": ...} (file already PUT to the bucket) or a multipart file."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        body = await request.json()
        return "key", body
    form = await request.form()
    return "file", form

REQUIRED_DOCS = {"id_card", "passport", "drivers_license"}
ADDRESS_DOC = "address_proof"
SELFIE_DOC = "selfie"
LIVENESS_DOC = "liveness"

# ─── Live face check (no third party) ─────────────────────────────────────────
# The server picks random actions + a code to read aloud and signs them; the browser records a
# short live video of the person doing exactly that; a KYC reviewer watches it against the
# challenge and the ID photo. A pre-recorded video can't match a challenge it has never seen.
LIVENESS_ACTIONS = ["Turn your head to the left", "Turn your head to the right", "Look up", "Look down",
                    "Smile", "Blink twice", "Raise your eyebrows", "Move closer to the camera"]
LIVENESS_TTL_SECONDS = 15 * 60


def _sign(payload: str) -> str:
    import hashlib, hmac
    return hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_liveness_challenge(user_id: str) -> dict:
    import base64, json, secrets, time
    rng = secrets.SystemRandom()
    actions = rng.sample(LIVENESS_ACTIONS, 3)
    code = "".join(rng.choice("123456789") for _ in range(4))
    body = base64.urlsafe_b64encode(json.dumps({"u": user_id, "a": actions, "c": code, "t": int(time.time())},
                                               separators=(",", ":")).encode()).decode()
    return {"token": f"{body}.{_sign(body)}", "actions": actions, "code": code,
            "expires_in": LIVENESS_TTL_SECONDS}


def read_liveness_challenge(token: str, user_id: str) -> dict:
    """The signed challenge, if it's genuine, this user's, and fresh. Raises 400 otherwise."""
    import base64, hmac, json, time
    try:
        body, sig = (token or "").rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            raise ValueError("bad signature")
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid face-check challenge. Please start the check again.")
    if data.get("u") != user_id:
        raise HTTPException(status_code=400, detail="This face-check challenge belongs to another account.")
    if time.time() - int(data.get("t", 0)) > LIVENESS_TTL_SECONDS:
        raise HTTPException(status_code=400, detail="The face-check challenge expired. Please start again.")
    return data


@router.get("/liveness/challenge", summary="Get a random live face-check challenge")
def liveness_challenge(current_user: User = Depends(get_current_user)):
    return issue_liveness_challenge(str(current_user.id))


@router.get("/status")
def get_kyc_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    docs = db.query(KycDocument).filter(KycDocument.user_id == current_user.id).all()

    doc_types_submitted = {d.type for d in docs}
    doc_list = [
        {
            "id": d.id,
            "user_id": d.user_id,
            "type": d.type,
            "file_url": d.file_url,
            "file_name": d.file_name,
            "status": d.status,
            "rejection_reason": d.rejection_reason,
            "created_at": d.created_at,
        }
        for d in docs
    ]

    has_identity = bool(doc_types_submitted & REQUIRED_DOCS)
    has_address = ADDRESS_DOC in doc_types_submitted
    has_liveness = LIVENESS_DOC in doc_types_submitted

    if not has_identity and not has_address:
        overall_status = "incomplete"
    elif all(d.status == "verified" for d in docs) and has_identity and has_address and (has_liveness or current_user.is_kyc_verified):
        overall_status = "verified"
    elif any(d.status == "rejected" for d in docs):
        overall_status = "rejected"
    elif docs:
        overall_status = "pending"
    else:
        overall_status = "incomplete"

    return {
        "is_kyc_verified": current_user.is_kyc_verified,
        "documents": doc_list,
        "overall_status": overall_status,
    }


@router.post("/upload", status_code=201)
async def upload_kyc_document(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Accepts JSON {doc_type, s3_key, filename} (file already in the bucket) or multipart doc_type + file."""
    from app.services.s3_service import owns_key, put_bytes, _build_s3_key

    valid_types = {"id_card", "passport", "drivers_license", "address_proof", "selfie", LIVENESS_DOC}
    allowed_content_types = {"image/jpeg", "image/png", "application/pdf"}
    kind, data = await _read_upload_request(request)
    doc_type = str(data.get("doc_type") or "")
    if doc_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid document type. Must be one of: {', '.join(valid_types)}")

    if kind == "key":
        s3_key = str(data.get("s3_key") or "")
        if not owns_key(s3_key, "kyc_documents", str(current_user.id)):
            raise HTTPException(status_code=400, detail="Invalid upload key")
        original_name = str(data.get("filename") or s3_key.rsplit("/", 1)[-1])[:255]
    elif doc_type == LIVENESS_DOC:
        raise HTTPException(status_code=400, detail="The live face check must be recorded in the app.")
    else:
        file = data.get("file")
        if file is None or not hasattr(file, "read"):
            raise HTTPException(status_code=400, detail="No file provided")
        if file.content_type not in allowed_content_types:
            raise HTTPException(status_code=400, detail="Only JPG, PNG, and PDF files are allowed")
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large (max 10MB)")
        original_name = file.filename or "document"
        s3_key = _build_s3_key("kyc_documents", original_name, str(current_user.id))
        put_bytes(s3_key, content, file.content_type)

    notes = None
    if doc_type == LIVENESS_DOC:
        ext = s3_key.rsplit(".", 1)[-1].lower()
        if ext not in ("webm", "mp4", "mov"):
            raise HTTPException(status_code=400, detail="The live face check must be a video recording.")
        ch = read_liveness_challenge(str(data.get("challenge_token") or ""), str(current_user.id))
        notes = f"Asked to: {' → '.join(ch['a'])} · then read aloud the code {' '.join(ch['c'])}"

    # Remove existing doc of same type if any
    existing = db.query(KycDocument).filter(
        KycDocument.user_id == current_user.id, KycDocument.type == doc_type
    ).first()
    if existing:
        db.delete(existing)
        db.flush()

    doc = KycDocument(
        user_id=current_user.id,
        type=doc_type,
        file_url=f"s3:{s3_key}",
        file_name=original_name,
        status="pending",
        notes=notes,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    create_notification(
        db, current_user.id,
        "KYC Document Submitted",
        f"Your {doc_type.replace('_', ' ')} has been submitted for review.",
        "kyc",
        related_item_id=doc.id,
        related_item_type="kyc",
    )

    return {
        "id": doc.id,
        "user_id": doc.user_id,
        "type": doc.type,
        "file_url": doc.file_url,
        "file_name": doc.file_name,
        "status": doc.status,
        "created_at": doc.created_at,
    }
