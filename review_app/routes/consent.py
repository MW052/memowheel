"""Cloud place-ID consent checklist (Premise 12): per-cluster opt-out, not
all-or-nothing. Checkboxes default checked; unchecking one before
submitting excludes just that cluster, routing it to manual tagging."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from review_app.db import get_db
from cloud.resolve_places import pending_consent_clusters, apply_consent
from review_app.templating import templates

router = APIRouter()


@router.get("/consent", response_class=HTMLResponse)
def consent_page(request: Request, conn=Depends(get_db)):
    pending = pending_consent_clusters(conn)
    return templates.TemplateResponse(request, "consent.html", {"pending": pending})


@router.post("/consent")
def submit_consent(
    approved_cluster_ids: list[int] = Form(default=[]),
    all_pending_cluster_ids: list[int] = Form(...),
    conn=Depends(get_db),
):
    approved = set(approved_cluster_ids)
    all_pending = set(all_pending_cluster_ids)
    excluded = all_pending - approved
    apply_consent(conn, list(approved), list(excluded))
    return {"ok": True, "approved": len(approved), "excluded": len(excluded)}
