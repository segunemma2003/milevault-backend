"""Shared Bondrova branding for transactional emails."""
from app.config import settings

BRAND_NAME = "Bondrova"
BRAND_ORANGE = "#FE7D03"
BRAND_DARK = "#111111"


def sender() -> str:
    return f"{BRAND_NAME} <{settings.DEFAULT_FROM_EMAIL}>"


def button(url: str, label: str) -> str:
    return (
        f"<a href='{url}' style='display:inline-block;padding:12px 20px;background:{BRAND_ORANGE};"
        f"color:{BRAND_DARK};font-weight:600;text-decoration:none;border-radius:6px;'>{label}</a>"
    )


def render_email(body_html: str) -> str:
    """Wrap an email body in the branded layout (logo header + footer)."""
    if "data-bondrova-layout" in body_html:
        return body_html
    base = settings.FRONTEND_URL.rstrip("/")
    logo_url = f"{base}/bondrova-logo.png"
    return f"""<!DOCTYPE html>
<html><body data-bondrova-layout style="margin:0;padding:0;background:#f4f4f5;font-family:Arial,Helvetica,sans-serif;color:#1f2937;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;">
<tr><td align="center" style="background:{BRAND_DARK};padding:20px;">
<a href="{base}"><img src="{logo_url}" alt="{BRAND_NAME}" width="200" style="display:block;width:200px;max-width:60%;height:auto;border:0;"></a>
</td></tr>
<tr><td style="height:4px;background:{BRAND_ORANGE};line-height:4px;font-size:0;">&nbsp;</td></tr>
<tr><td style="padding:28px 32px;font-size:15px;line-height:1.6;">
{body_html}
</td></tr>
<tr><td style="padding:16px 32px;background:#fafafa;border-top:1px solid #eeeeee;font-size:12px;color:#6b7280;text-align:center;">
&copy; {BRAND_NAME} &middot; Secure escrow for buyers and sellers
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""
