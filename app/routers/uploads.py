"""
Upload router — S3 presigned URL generation + local fallback.
Flow for images/documents:
  1. Client calls POST /uploads/presign  →  gets {url, fields, s3_key}
  2. Client uploads DIRECTLY to S3 using the presigned POST  (no server involved)
  3. After S3 confirms, client sends s3_key back to relevant endpoint.

For videos: same flow + Celery watermark task is queued post-upload.
Local fallback (when S3 not configured): client POSTs to /uploads/local/{path}.
"""
import os
import mimetypes
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Path
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.s3_service import (
    get_presigned_upload_url,
    get_presigned_download_url,
    save_local_upload,
    ALLOWED_IMAGE_TYPES,
    ALLOWED_VIDEO_TYPES,
    ALLOWED_DOCUMENT_TYPES,
)
from app.config import settings

router = APIRouter(prefix="/uploads", tags=["Uploads"])

FOLDER_MAP = {
    "avatar": "avatars",
    "kyc": "kyc_documents",
    "dispute": "dispute_evidence",
    "delivery": "milestone_delivery",
    "transaction": "transaction_docs",
    "agent_evidence": "agent_evidence",
    "general": "general",
}


@router.post("/presign", summary="Get a presigned S3 URL for direct browser upload")
def get_presign(
    filename: str,
    content_type: str,
    folder: str = "general",
    current_user: User = Depends(get_current_user),
):
    """
    Returns a presigned POST URL so the client can upload directly to S3.
    This keeps large files off the API server and ensures uploads < 3s for the API call.

    Response:
      - upload_url: POST to this URL (S3 or local fallback)
      - fields: FormData fields to include in the S3 POST
      - s3_key: The final object key — pass this back to API endpoints that need it
      - method: 's3' or 'local'
    """
    mapped_folder = FOLDER_MAP.get(folder, "general")

    if content_type not in (ALLOWED_IMAGE_TYPES | ALLOWED_VIDEO_TYPES | ALLOWED_DOCUMENT_TYPES):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "UNSUPPORTED_FILE_TYPE",
                "message": (
                    f"'{content_type}' is not allowed. "
                    f"Supported types: images (JPEG/PNG/WebP/GIF), "
                    f"videos (MP4/MOV/WebM/AVI), documents (PDF/DOC/DOCX)."
                ),
            },
        )

    try:
        s3_key, upload_url, fields = get_presigned_upload_url(
            folder=mapped_folder,
            filename=filename,
            content_type=content_type,
            user_id=str(current_user.id),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": "PRESIGN_ERROR", "message": str(e)})

    is_video = content_type in ALLOWED_VIDEO_TYPES
    is_image = content_type in ALLOWED_IMAGE_TYPES

    return {
        "s3_key": s3_key,
        "upload_url": upload_url,
        "fields": fields,
        "method": "s3" if settings.s3_enabled else "local",
        "watermark_queued": is_video or is_image,
        "note": (
            "After uploading, call POST /uploads/confirm with s3_key to trigger watermarking."
            if (is_video or is_image) else None
        ),
    }


@router.post("/confirm", summary="Confirm upload complete — triggers watermarking for images/videos")
def confirm_upload(
    s3_key: str,
    content_type: str,
    current_user: User = Depends(get_current_user),
):
    """
    Called by the client after a successful S3 direct upload.
    Queues the Celery watermark task for images and videos.
    """
    if content_type in ALLOWED_IMAGE_TYPES:
        from app.services.tasks import watermark_image
        task = watermark_image.delay(s3_key, str(current_user.id))
        return {"message": "Image upload confirmed. Watermarking queued.", "task_id": task.id, "s3_key": s3_key}

    if content_type in ALLOWED_VIDEO_TYPES:
        from app.services.tasks import watermark_video
        task = watermark_video.delay(s3_key, str(current_user.id))
        return {"message": "Video upload confirmed. Watermarking queued (may take a few minutes).", "task_id": task.id, "s3_key": s3_key}

    return {"message": "Upload confirmed.", "s3_key": s3_key}


@router.get("/download", summary="Get a presigned download URL for an S3 object")
def get_download_url(
    s3_key: str,
    filename: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """
    Returns a time-limited presigned GET URL to download a stored file.
    Clients should NEVER use S3 keys directly — always go through this endpoint.
    """
    if not s3_key or ".." in s3_key or s3_key.startswith("/"):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_KEY", "message": "Invalid S3 key. Do not attempt path traversal."},
        )

    url = get_presigned_download_url(s3_key, filename or "download")
    return {"url": url, "expires_in_seconds": settings.S3_PRESIGNED_EXPIRY}


# ─── Local fallback endpoints (used when S3 is not configured) ───────────────

@router.post("/local/{folder}/{user_id}/{filename:path}", include_in_schema=False)
async def local_upload(
    folder: str,
    user_id: str,
    filename: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Receives file when S3 is not configured (dev/local only)."""
    if settings.is_production:
        raise HTTPException(
            status_code=403,
            detail={"error": "LOCAL_UPLOADS_DISABLED", "message": "Local uploads are not allowed in production. Configure AWS S3."},
        )

    s3_key = f"{folder}/{user_id}/{filename}"
    content = await file.read()

    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail={"error": "FILE_TOO_LARGE", "message": "File exceeds the 50 MB limit for local uploads."},
        )

    save_local_upload(s3_key, content)

    content_type = file.content_type or "application/octet-stream"
    if content_type in ALLOWED_IMAGE_TYPES:
        try:
            from app.services.s3_service import add_watermark_to_image
            watermarked = add_watermark_to_image(content)
            save_local_upload(s3_key, watermarked)
        except Exception:
            pass  # Non-critical; save original if watermark fails

    return {"message": "File saved locally.", "s3_key": s3_key}


@router.get("/local/{path:path}", include_in_schema=False)
def serve_local_file(
    path: str,
    current_user: User = Depends(get_current_user),
):
    """Serves local files (dev only)."""
    if settings.is_production:
        raise HTTPException(status_code=403, detail="Local file serving disabled in production.")

    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=403, detail={"error": "FORBIDDEN", "message": "Path traversal not allowed."})

    local_path = os.path.join(settings.UPLOAD_DIR, path)
    if not os.path.exists(local_path):
        raise HTTPException(
            status_code=404,
            detail={"error": "FILE_NOT_FOUND", "message": "File not found. It may have been moved or deleted."},
        )

    media_type, _ = mimetypes.guess_type(local_path)
    return FileResponse(local_path, media_type=media_type or "application/octet-stream")
