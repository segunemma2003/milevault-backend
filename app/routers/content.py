"""Blog posts and job openings — public read endpoints + admin CRUD."""
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from xml.sax.saxutils import escape
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_admin
from app.models.content import BlogPost, JobOpening
from app.models.user import User

router = APIRouter(tags=["Content"])


# ─── Schemas ──────────────────────────────────────────────────────────────────

class BlogPostIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    slug: Optional[str] = Field(None, max_length=200)
    excerpt: Optional[str] = None
    content: str = ""
    category: Optional[str] = Field(None, max_length=80)
    cover_image_url: Optional[str] = Field(None, max_length=1024)
    author_name: Optional[str] = Field(None, max_length=120)
    is_published: bool = False


class JobOpeningIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    department: Optional[str] = Field(None, max_length=120)
    location: Optional[str] = Field(None, max_length=255)
    employment_type: Optional[str] = Field(None, max_length=60)
    description: str = ""
    apply_url: Optional[str] = Field(None, max_length=1024)
    is_open: bool = True


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:180] or "post"


def _unique_slug(db: Session, base: str, exclude_id: Optional[str] = None) -> str:
    slug, n = base, 2
    while True:
        q = db.query(BlogPost).filter(BlogPost.slug == slug)
        if exclude_id:
            q = q.filter(BlogPost.id != exclude_id)
        if not q.first():
            return slug
        slug = f"{base}-{n}"
        n += 1


def _post_out(p: BlogPost, full: bool = True) -> dict:
    out = {
        "id": p.id,
        "slug": p.slug,
        "title": p.title,
        "excerpt": p.excerpt,
        "category": p.category,
        "cover_image_url": p.cover_image_url,
        "author_name": p.author_name,
        "is_published": p.is_published,
        "published_at": p.published_at,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }
    if full:
        out["content"] = p.content
    return out


def _job_out(j: JobOpening) -> dict:
    return {
        "id": j.id,
        "title": j.title,
        "department": j.department,
        "location": j.location,
        "employment_type": j.employment_type,
        "description": j.description,
        "apply_url": j.apply_url,
        "is_open": j.is_open,
        "created_at": j.created_at,
        "updated_at": j.updated_at,
    }


# ─── Public ───────────────────────────────────────────────────────────────────

@router.get("/blog", summary="Published blog posts")
def list_published_posts(db: Session = Depends(get_db)):
    posts = (
        db.query(BlogPost)
        .filter(BlogPost.is_published.is_(True))
        .order_by(BlogPost.published_at.desc())
        .all()
    )
    return [_post_out(p, full=False) for p in posts]


@router.get("/blog/{slug}", summary="A published blog post")
def get_published_post(slug: str, db: Session = Depends(get_db)):
    p = db.query(BlogPost).filter(BlogPost.slug == slug, BlogPost.is_published.is_(True)).first()
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    return _post_out(p)


@router.get("/careers", summary="Open job positions")
def list_open_jobs(db: Session = Depends(get_db)):
    jobs = (
        db.query(JobOpening)
        .filter(JobOpening.is_open.is_(True))
        .order_by(JobOpening.created_at.desc())
        .all()
    )
    return [_job_out(j) for j in jobs]


SITE_URL = "https://bondrova.com"
STATIC_PAGES = [
    ("/", "weekly", "1.0"),
    ("/how-it-works", "monthly", "0.9"),
    ("/security", "monthly", "0.7"),
    ("/faq", "monthly", "0.8"),
    ("/about", "monthly", "0.6"),
    ("/contact", "yearly", "0.6"),
    ("/blog", "weekly", "0.7"),
    ("/careers", "weekly", "0.5"),
    ("/register", "yearly", "0.6"),
    ("/privacy", "yearly", "0.3"),
    ("/terms", "yearly", "0.3"),
    ("/cookies", "yearly", "0.2"),
]


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(db: Session = Depends(get_db)):
    """XML sitemap: static marketing pages + every published blog post."""
    urls = [
        f"<url><loc>{SITE_URL}{path}</loc><changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for path, freq, prio in STATIC_PAGES
    ]
    for p in db.query(BlogPost).filter(BlogPost.is_published.is_(True)).all():
        lastmod = (p.updated_at or p.published_at or datetime.utcnow()).strftime("%Y-%m-%d")
        urls.append(
            f"<url><loc>{SITE_URL}/blog/{escape(p.slug)}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>monthly</changefreq><priority>0.6</priority></url>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(urls)
        + "</urlset>"
    )
    return Response(content=xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


# ─── Admin: blog ──────────────────────────────────────────────────────────────

@router.get("/admin/blog", summary="All blog posts (admin)")
def admin_list_posts(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    return [_post_out(p, full=False) for p in db.query(BlogPost).order_by(BlogPost.created_at.desc()).all()]


@router.get("/admin/blog/{post_id}", summary="Get blog post (admin)")
def admin_get_post(post_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    p = db.get(BlogPost, post_id)
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    return _post_out(p)


def _apply_post(db: Session, p: BlogPost, data: BlogPostIn) -> None:
    p.title = data.title.strip()
    p.slug = _unique_slug(db, _slugify(data.slug or data.title), exclude_id=p.id)
    p.excerpt = data.excerpt
    p.content = data.content
    p.category = data.category
    p.cover_image_url = data.cover_image_url
    p.author_name = data.author_name
    if data.is_published and not p.is_published:
        p.published_at = datetime.utcnow()
    p.is_published = data.is_published


@router.post("/admin/blog", status_code=status.HTTP_201_CREATED, summary="Create blog post (admin)")
def admin_create_post(data: BlogPostIn, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    p = BlogPost(is_published=False)
    _apply_post(db, p, data)
    db.add(p)
    db.commit()
    db.refresh(p)
    return _post_out(p)


@router.put("/admin/blog/{post_id}", summary="Update blog post (admin)")
def admin_update_post(post_id: str, data: BlogPostIn, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    p = db.get(BlogPost, post_id)
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    _apply_post(db, p, data)
    db.commit()
    db.refresh(p)
    return _post_out(p)


@router.delete("/admin/blog/{post_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete blog post (admin)")
def admin_delete_post(post_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    p = db.get(BlogPost, post_id)
    if p:
        db.delete(p)
        db.commit()


# ─── Admin: careers ───────────────────────────────────────────────────────────

@router.get("/admin/careers", summary="All job openings (admin)")
def admin_list_jobs(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    return [_job_out(j) for j in db.query(JobOpening).order_by(JobOpening.created_at.desc()).all()]


def _apply_job(j: JobOpening, data: JobOpeningIn) -> None:
    for field, value in data.model_dump().items() if hasattr(data, "model_dump") else data.dict().items():
        setattr(j, field, value.strip() if isinstance(value, str) else value)


@router.post("/admin/careers", status_code=status.HTTP_201_CREATED, summary="Create job opening (admin)")
def admin_create_job(data: JobOpeningIn, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    j = JobOpening()
    _apply_job(j, data)
    db.add(j)
    db.commit()
    db.refresh(j)
    return _job_out(j)


@router.put("/admin/careers/{job_id}", summary="Update job opening (admin)")
def admin_update_job(job_id: str, data: JobOpeningIn, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    j = db.get(JobOpening, job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    _apply_job(j, data)
    db.commit()
    db.refresh(j)
    return _job_out(j)


@router.delete("/admin/careers/{job_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete job opening (admin)")
def admin_delete_job(job_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    j = db.get(JobOpening, job_id)
    if j:
        db.delete(j)
        db.commit()
