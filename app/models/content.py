"""Admin-managed site content: blog posts and job openings."""
import uuid
from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, String, Text
from app.database import Base


class BlogPost(Base):
    __tablename__ = "blog_posts"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    slug = Column(String(200), unique=True, index=True, nullable=False)
    title = Column(String(255), nullable=False)
    excerpt = Column(Text, nullable=True)
    content = Column(Text, nullable=False, default="")  # HTML, sanitised on the client
    category = Column(String(80), nullable=True)
    cover_image_url = Column(String(1024), nullable=True)
    author_name = Column(String(120), nullable=True)
    is_published = Column(Boolean, default=False, nullable=False)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class JobOpening(Base):
    __tablename__ = "job_openings"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(255), nullable=False)
    department = Column(String(120), nullable=True)
    location = Column(String(255), nullable=True)
    employment_type = Column(String(60), nullable=True)  # Full-time | Part-time | Contract | Internship
    description = Column(Text, nullable=False, default="")
    apply_url = Column(String(1024), nullable=True)  # link or mailto:
    is_open = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
