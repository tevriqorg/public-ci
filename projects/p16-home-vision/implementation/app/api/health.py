import os

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session
from app.domain.normalize_queue import NORMALIZATION_WORKER_PROTOCOL

router = APIRouter(tags=["health"])


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "database": "ok",
        "p16_release_revision": os.environ.get("P16_RELEASE_REVISION", ""),
        "normalize_worker_protocol": NORMALIZATION_WORKER_PROTOCOL,
    }
