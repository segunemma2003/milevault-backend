"""
S3 service — presigned upload/download URLs, watermarking trigger.
Falls back to local filesystem when AWS credentials are not configured.
"""
import os
import uuid
import logging
from typing import Optional, Tuple
from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-msvideo"}
ALLOWED_DOCUMENT_TYPES = {"application/pdf", "application/msword",
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
ALL_ALLOWED_TYPES = ALLOWED_IMAGE_TYPES | ALLOWED_VIDEO_TYPES | ALLOWED_DOCUMENT_TYPES

MAX_IMAGE_SIZE = 10 * 1024 * 1024     # 10 MB
MAX_VIDEO_SIZE = 500 * 1024 * 1024    # 500 MB
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024  # 20 MB


def _get_s3_client():
    """S3 client for AWS or any S3-compatible provider (Backblaze B2, R2, MinIO)."""
    import boto3
    from botocore.config import Config

    kwargs = dict(
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    if settings.AWS_S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = settings.AWS_S3_ENDPOINT_URL
    return boto3.client("s3", **kwargs)


def key_prefix() -> str:
    p = settings.S3_KEY_PREFIX.strip().strip("/")
    return f"{p}/" if p else ""


def _build_s3_key(folder: str, filename: str, user_id: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    ext = "".join(c for c in ext if c.isalnum())[:10] or "bin"
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    return f"{key_prefix()}{folder}/{user_id}/{unique_name}"


def owns_key(s3_key: str, folder: str, user_id: str) -> bool:
    """True when the key was issued to this user for this folder by get_presigned_upload_url."""
    return (
        bool(s3_key)
        and ".." not in s3_key
        and s3_key.startswith(f"{key_prefix()}{folder}/{user_id}/")
    )


def put_bytes(s3_key: str, data: bytes, content_type: str) -> None:
    """Server-side upload (used when the API receives the file itself)."""
    if not settings.s3_enabled:
        save_local_upload(s3_key, data)
        return
    _get_s3_client().put_object(
        Bucket=settings.S3_BUCKET_NAME, Key=s3_key, Body=data, ContentType=content_type
    )


def get_presigned_view_url(s3_key: str, expires: Optional[int] = None) -> str:
    """Presigned GET suitable for <img src> (inline, no download disposition)."""
    if not settings.s3_enabled:
        return f"/api/v1/uploads/local/{s3_key}"
    if settings.CDN_BASE_URL:
        return f"{settings.CDN_BASE_URL.rstrip('/')}/{s3_key}"
    return _get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=expires or settings.S3_PRESIGNED_EXPIRY,
    )


def get_presigned_upload_url(
    folder: str,
    filename: str,
    content_type: str,
    user_id: str,
    max_size: Optional[int] = None,
    size: Optional[int] = None,
) -> Tuple[str, str, dict]:
    """
    Returns (s3_key, presigned_put_url, headers) for direct browser → bucket upload.
    The browser must PUT the file with exactly these headers.
    Presigned PUT is used (not POST) because Backblaze B2 does not support
    browser POST policies. When `size` is given it is signed into the URL,
    so the bucket rejects any body of a different length.
    Falls back to a local upload endpoint URL when S3 is not configured.
    """
    if content_type not in ALL_ALLOWED_TYPES:
        raise ValueError(f"File type '{content_type}' is not allowed.")

    if max_size is None:
        if content_type in ALLOWED_IMAGE_TYPES:
            max_size = MAX_IMAGE_SIZE
        elif content_type in ALLOWED_VIDEO_TYPES:
            max_size = MAX_VIDEO_SIZE
        else:
            max_size = MAX_DOCUMENT_SIZE

    s3_key = _build_s3_key(folder, filename, user_id)

    if not settings.s3_enabled:
        # Fallback: return a local endpoint
        return s3_key, f"/api/v1/uploads/local/{s3_key}", {}

    if size is not None and (size < 1 or size > max_size):
        raise ValueError(f"File is too large. Maximum size is {max_size // (1024 * 1024)} MB.")

    if not settings.AWS_S3_ENDPOINT_URL:
        # Native AWS: keep the presigned POST policy (enforces size server-side).
        response = _get_s3_client().generate_presigned_post(
            Bucket=settings.S3_BUCKET_NAME,
            Key=s3_key,
            Fields={"Content-Type": content_type},
            Conditions=[["content-length-range", 1, max_size], {"Content-Type": content_type}],
            ExpiresIn=900,
        )
        return s3_key, response["url"], response["fields"]

    params = {"Bucket": settings.S3_BUCKET_NAME, "Key": s3_key, "ContentType": content_type}
    if size is not None:
        params["ContentLength"] = size
    url = _get_s3_client().generate_presigned_url(
        "put_object", Params=params, ExpiresIn=900,  # 15 minutes to complete upload
    )
    return s3_key, url, {"Content-Type": content_type}


def get_presigned_download_url(s3_key: str, filename: str = "download") -> str:
    """Returns a presigned GET URL for a stored object."""
    if not settings.s3_enabled:
        return f"/api/v1/uploads/local/{s3_key}"

    if settings.CDN_BASE_URL:
        return f"{settings.CDN_BASE_URL.rstrip('/')}/{s3_key}"

    s3 = _get_s3_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.S3_BUCKET_NAME,
            "Key": s3_key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=settings.S3_PRESIGNED_EXPIRY,
    )


def delete_s3_object(s3_key: str) -> bool:
    """Soft-deletes: moves to a deleted/ prefix rather than hard deleting."""
    if not settings.s3_enabled:
        # Remove local file
        local_path = os.path.join(settings.UPLOAD_DIR, s3_key)
        try:
            os.remove(local_path)
        except FileNotFoundError:
            pass
        return True
    try:
        s3 = _get_s3_client()
        new_key = f"deleted/{s3_key}"
        s3.copy_object(
            Bucket=settings.S3_BUCKET_NAME,
            CopySource={"Bucket": settings.S3_BUCKET_NAME, "Key": s3_key},
            Key=new_key,
        )
        s3.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=s3_key)
        return True
    except Exception as e:
        logger.error(f"Failed to delete S3 object {s3_key}: {e}")
        return False


def save_local_upload(s3_key: str, file_bytes: bytes) -> str:
    """Saves file locally when S3 is not configured. Returns local path."""
    local_path = os.path.join(settings.UPLOAD_DIR, s3_key)
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    with open(local_path, "wb") as f:
        f.write(file_bytes)
    return local_path


def add_watermark_to_image(image_bytes: bytes, text: str = None) -> bytes:
    """Adds a text watermark to an image. Returns watermarked bytes."""
    from PIL import Image, ImageDraw, ImageFont
    import io

    watermark_text = text or settings.WATERMARK_TEXT
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    width, height = img.size

    txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    # Use default font scaled to image size
    font_size = max(20, width // 20)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    # Diagonal watermark, semi-transparent
    bbox = draw.textbbox((0, 0), watermark_text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (width - tw) // 2
    y = (height - th) // 2
    draw.text((x, y), watermark_text, fill=(255, 255, 255, 80), font=font)

    watermarked = Image.alpha_composite(img, txt_layer).convert("RGB")
    output = io.BytesIO()
    watermarked.save(output, format="JPEG", quality=90)
    return output.getvalue()
