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
    import boto3
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
    )


def _build_s3_key(folder: str, filename: str, user_id: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    return f"{folder}/{user_id}/{unique_name}"


def get_presigned_upload_url(
    folder: str,
    filename: str,
    content_type: str,
    user_id: str,
    max_size: Optional[int] = None,
) -> Tuple[str, str, dict]:
    """
    Returns (s3_key, presigned_post_url, fields) for direct browser → S3 upload.
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

    s3 = _get_s3_client()
    conditions = [
        ["content-length-range", 1, max_size],
        {"Content-Type": content_type},
    ]
    response = s3.generate_presigned_post(
        Bucket=settings.S3_BUCKET_NAME,
        Key=s3_key,
        Fields={"Content-Type": content_type},
        Conditions=conditions,
        ExpiresIn=900,  # 15 minutes to complete upload
    )
    return s3_key, response["url"], response["fields"]


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
