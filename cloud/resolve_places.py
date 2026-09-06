"""Cluster place resolution: GPS reverse-geocode branch (Premise 1a, 21) and
the cloud place-ID / consent / manual-fallback branch (Premise 1b-c, 12, 13).

Both branches resolve one place per CLUSTER (moment), not per item - Premise
8 unifies "moment" and "location cluster" into the same unit regardless of
whether GPS is present.
"""

import json
from pathlib import Path

import cv2
import reverse_geocoder

from ingest.selection import ClusterItem, select_cloud_id_candidate
from ingest.image_utils import load_corrected_bgr
from ingest.faces import blur_faces
from cloud.place_id import identify_place, PlaceIdApiError


def resolve_gps_clusters(conn) -> None:
    """GPS-tagged clusters (future trips, Premise 1a) need no consent gate -
    coordinates alone aren't biometric data (Premise 21)."""
    clusters = conn.execute(
        "SELECT id FROM clusters WHERE place_name IS NULL AND consent_status = 'pending'"
    ).fetchall()
    for cluster in clusters:
        item = conn.execute(
            "SELECT gps_lat, gps_lon FROM items WHERE cluster_id = ? AND gps_lat IS NOT NULL LIMIT 1",
            (cluster["id"],),
        ).fetchone()
        if item is None:
            continue  # no GPS in this cluster, leave for the cloud/manual branch
        result = reverse_geocoder.get((item["gps_lat"], item["gps_lon"]))
        place_name = f"{result['name']}, {result['admin1']}"
        conn.execute(
            "UPDATE clusters SET place_name = ?, place_source = 'gps', "
            "consent_status = 'not_needed' WHERE id = ?",
            (place_name, cluster["id"]),
        )
    conn.commit()


def _gps_place_label(result) -> str:
    """A reverse-geocoder hit -> the "City, Region" caption. One place to keep the
    GPS label format consistent (matches find_location_for_item)."""
    return f"{result['name']}, {result['admin1']}"


def resolve_gps_items(conn) -> None:
    """Auto-set each GPS-tagged item's OWN place from its coordinates, at import.

    Runs offline (the `reverse_geocoder` city/town database - nothing leaves the
    machine, no API key) and only fills items that have coordinates but no place
    yet, so it never clobbers a place the user typed or fetched from the AI. The
    per-photo "Find location"/send-to-API path is unchanged: it still overrides any
    specific photo with a finer landmark name (find_location_for_item).

    Per-ITEM (not per-cluster) on purpose - the caption then shows under every
    geotagged photo, the same field the AI button sets (items.place_name), which
    generate renders directly (assemble.py: item_place wins over cluster_place).

    mode=1 keeps reverse_geocoder single-threaded: import already runs inside the
    server process (a background thread for the tray/windowless build), and the
    default multiprocessing mode would spawn extra worker processes there."""
    rows = conn.execute(
        "SELECT id, gps_lat, gps_lon FROM items "
        "WHERE gps_lat IS NOT NULL AND gps_lon IS NOT NULL AND place_name IS NULL"
    ).fetchall()
    if not rows:
        return  # no geotagged-but-unlabeled items - skip loading the geo dataset

    coords = [(row["gps_lat"], row["gps_lon"]) for row in rows]
    results = reverse_geocoder.search(coords, mode=1, verbose=False)
    conn.executemany(
        "UPDATE items SET place_name = ?, place_source = 'gps' WHERE id = ?",
        [(_gps_place_label(res), row["id"]) for row, res in zip(rows, results)],
    )
    conn.commit()


def select_representatives(conn) -> None:
    """For clusters with no place yet and no GPS, pick the cloud-ID
    candidate (best-quality, face-free photo). Clusters with no such
    candidate route straight to manual tagging (Premise 1c, 12)."""
    clusters = conn.execute(
        "SELECT id FROM clusters WHERE place_name IS NULL AND consent_status = 'pending'"
    ).fetchall()
    for cluster in clusters:
        rows = conn.execute(
            "SELECT id, item_type, quality_score, face_tags FROM items WHERE cluster_id = ?",
            (cluster["id"],),
        ).fetchall()
        items = [
            ClusterItem(
                id=r["id"], item_type=r["item_type"],
                quality_score=r["quality_score"] or 0.0,
                face_tags=json.loads(r["face_tags"]),
            )
            for r in rows
        ]
        candidate = select_cloud_id_candidate(items)
        if candidate is None:
            conn.execute(
                "UPDATE clusters SET consent_status = 'manual' WHERE id = ?",
                (cluster["id"],),
            )
        else:
            conn.execute(
                "UPDATE items SET is_representative = 1 WHERE id = ?", (candidate.id,)
            )
    conn.commit()


def pending_consent_clusters(conn) -> list[dict]:
    """Clusters ready for the consent checklist: has a representative photo,
    still pending. Returned for the review UI to render."""
    rows = conn.execute(
        """SELECT c.id AS cluster_id, i.id AS representative_item_id, i.path AS representative_path
           FROM clusters c JOIN items i ON i.cluster_id = c.id
           WHERE c.place_name IS NULL AND c.consent_status = 'pending'
             AND i.is_representative = 1"""
    ).fetchall()
    return [dict(row) for row in rows]


def apply_consent(conn, approved_cluster_ids: list[int], excluded_cluster_ids: list[int]) -> None:
    """Called after the user submits the consent checklist (Premise 12).
    Excluded clusters route to manual tagging. Approved clusters get one
    API call each; failures also route to manual (Premise 13), no retry."""
    for cluster_id in excluded_cluster_ids:
        conn.execute(
            "UPDATE clusters SET consent_status = 'excluded' WHERE id = ?", (cluster_id,)
        )

    for cluster_id in approved_cluster_ids:
        row = conn.execute(
            "SELECT i.path FROM items i WHERE i.cluster_id = ? AND i.is_representative = 1",
            (cluster_id,),
        ).fetchone()
        if row is None:
            continue
        try:
            place_name = identify_place(row["path"])
            conn.execute(
                "UPDATE clusters SET place_name = ?, place_source = 'cloud_api', "
                "consent_status = 'approved' WHERE id = ?",
                (place_name, cluster_id),
            )
        except PlaceIdApiError as e:
            print(f"[place-id] cluster {cluster_id} failed: {e}")
            conn.execute(
                "UPDATE clusters SET consent_status = 'failed' WHERE id = ?", (cluster_id,)
            )
    conn.commit()


def blur_and_send(conn, cluster_id: int) -> None:
    """Manual fallback for clusters with no face-free photo at all: blur
    every detected face on the cluster's best-quality photo, then send
    THAT anonymized copy to the cloud API. Deliberate, user-triggered
    exception to Premise 12's default - the original, unmodified photo
    still never leaves the machine."""
    row = conn.execute(
        "SELECT id, path FROM items WHERE cluster_id = ? AND item_type = 'photo' "
        "ORDER BY quality_score DESC LIMIT 1",
        (cluster_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"cluster {cluster_id} has no photos to blur")

    image = load_corrected_bgr(row["path"])
    blurred = blur_faces(image)

    # Kept (not deleted) so you can visually confirm the blur actually
    # worked before trusting it - review these in data/blurred/.
    blurred_dir = Path("data") / "blurred"
    blurred_dir.mkdir(parents=True, exist_ok=True)
    blurred_path = blurred_dir / f"cluster_{cluster_id}_item_{row['id']}.jpg"
    cv2.imwrite(str(blurred_path), blurred)

    try:
        place_name = identify_place(str(blurred_path))
        conn.execute(
            "UPDATE clusters SET place_name = ?, place_source = 'cloud_api_blurred', "
            "consent_status = 'approved' WHERE id = ?",
            (place_name, cluster_id),
        )
        conn.commit()
    except PlaceIdApiError as e:
        print(f"[place-id-blurred] cluster {cluster_id} failed: {e}")
        raise


def send_specific_item_for_id(conn, item_id: int) -> str:
    """Send a user-CHOSEN photo (not the auto-picked "best quality" one)
    for cloud place-ID - useful when the automatic candidate is a bad
    angle but another shot in the same cluster clearly shows the
    landmark. Blurs faces first if the chosen photo has any (same
    Premise 12 exception as blur_and_send); sends as-is if already
    face-free. Clips can't be sent (not an image format the API accepts)."""
    row = conn.execute(
        "SELECT id, path, item_type, cluster_id, face_tags FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"item {item_id} not found")
    if row["item_type"] != "photo":
        raise ValueError("only photos can be sent for place-ID, not video clips")
    if row["cluster_id"] is None:
        raise ValueError(f"item {item_id} has no cluster")

    face_tags = json.loads(row["face_tags"])
    if face_tags:
        image = load_corrected_bgr(row["path"])
        blurred = blur_faces(image)
        blurred_dir = Path("data") / "blurred"
        blurred_dir.mkdir(parents=True, exist_ok=True)
        send_path = blurred_dir / f"item_{item_id}_manual_pick.jpg"
        cv2.imwrite(str(send_path), blurred)
        place_source = "cloud_api_blurred"
    else:
        send_path = Path(row["path"])
        place_source = "cloud_api"

    try:
        place_name = identify_place(str(send_path))
        conn.execute(
            "UPDATE clusters SET place_name = ?, place_source = ?, "
            "consent_status = 'approved' WHERE id = ?",
            (place_name, place_source, row["cluster_id"]),
        )
        conn.commit()
        return place_name
    except PlaceIdApiError as e:
        print(f"[place-id-manual-pick] item {item_id} failed: {e}")
        raise


def find_location_for_item(conn, item_id: int) -> str:
    """On-demand location for a user-chosen INCLUDED photo in review. Sets a place
    on THIS PHOTO (items.place_name) - not the moment/cluster - so the caption
    shows on this specific photo at generate, the same way the first photo of a
    group shows its place. GPS first (free, local, no photo leaves the machine);
    otherwise the vision AI (needs an API key + trip country). Raises ValueError
    for a user-fixable problem, PlaceIdApiError for an AI-call failure."""
    row = conn.execute(
        "SELECT id, path, item_type, gps_lat, gps_lon, face_tags FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"item {item_id} not found")

    if row["gps_lat"] is not None and row["gps_lon"] is not None:
        result = reverse_geocoder.get((row["gps_lat"], row["gps_lon"]))
        place_name = f"{result['name']}, {result['admin1']}"
        place_source = "gps"
    else:
        if row["item_type"] != "photo":
            raise ValueError("only photos can be identified by sight, not video clips")
        from secret_store import has_api_key
        if not has_api_key():
            raise ValueError(
                "This photo has no GPS. Add an API key and trip country in "
                "Setup → Settings to identify it by sight, or type the place manually."
            )
        # Blur faces before sending (Premise 12) if the photo has any.
        if json.loads(row["face_tags"]):
            blurred_dir = Path("data") / "blurred"
            blurred_dir.mkdir(parents=True, exist_ok=True)
            send_path = blurred_dir / f"item_{item_id}_manual_pick.jpg"
            cv2.imwrite(str(send_path), blur_faces(load_corrected_bgr(row["path"])))
            place_source = "cloud_api_blurred"
        else:
            send_path = Path(row["path"])
            place_source = "cloud_api"
        place_name = identify_place(str(send_path))  # raises PlaceIdApiError on failure

    conn.execute(
        "UPDATE items SET place_name = ?, place_source = ? WHERE id = ?",
        (place_name, place_source, item_id),
    )
    conn.commit()
    return place_name
