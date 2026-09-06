"""Manual face-tag correction (Premise 22): mitigates face-recognition false
negatives (sunglasses, angle, occlusion) by letting the reviewer add a missed
tag. The tag is used for the cover / cloud place-ID candidate and shown on the
photo; it never changes which photos are included (the user curates that)."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException

from review_app.db import get_db

router = APIRouter()


def _enrolled_labels(conn) -> set[str]:
    return {
        row["person_label"]
        for row in conn.execute("SELECT DISTINCT person_label FROM enrolled_faces").fetchall()
    }


@router.post("/items/{item_id}/faces")
def correct_face_tag(item_id: int, label: str = Form(...), action: str = Form(...), conn=Depends(get_db)):
    valid_labels = _enrolled_labels(conn)
    if label not in valid_labels:
        raise HTTPException(
            status_code=400,
            detail=f"label must be one of the enrolled people: {sorted(valid_labels)}",
        )
    if action not in {"add", "remove"}:
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")

    row = conn.execute(
        "SELECT face_tags FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")

    tags = set(json.loads(row["face_tags"]))
    if action == "add":
        tags.add(label)
    else:
        tags.discard(label)
    conn.execute(
        "UPDATE items SET face_tags = ? WHERE id = ?",
        (json.dumps(sorted(tags)), item_id),
    )
    conn.commit()

    return {"ok": True, "face_tags": sorted(tags)}
