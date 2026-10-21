"""People + groups + search - the shared people domain.

This app is a hub of "makers" (see features.py); every maker works with the same
enrolled people, which are shared across all projects (project_store snapshots and
restores the people tables on each project switch). This module is the
web-independent core for that domain so any current or future maker can import it
without pulling in the web layer:

- a *person* is a `person_label` string - the same string enrolled in
  `enrolled_faces` and stored as a tag inside `items.face_tags` (a JSON array);
- a *group* is a named set of people (`person_groups` / `person_group_members`);
- *search* resolves a free-text query to the set of people it names (by person
  name OR group name) and finds the photos/clips they appear in - optionally
  across every project, not just the live one.

All functions take an open sqlite3 connection (`db.schema.get_connection`), except
the top-level `search()` which spans projects and opens catalogs itself.
"""

import json
import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
def list_people(conn) -> list[dict]:
    """Enrolled people with their reference-photo count and the groups each
    belongs to: [{"label", "photo_count", "groups": [name, ...]}]."""
    rows = conn.execute(
        "SELECT person_label AS label, COUNT(*) AS photo_count "
        "FROM enrolled_faces GROUP BY person_label ORDER BY person_label"
    ).fetchall()
    members = _members_by_label(conn)
    return [
        {"label": r["label"], "photo_count": r["photo_count"],
         "groups": members.get(r["label"], [])}
        for r in rows
    ]


def person_labels(conn) -> set[str]:
    """The distinct enrolled person labels."""
    return {
        r["person_label"]
        for r in conn.execute("SELECT DISTINCT person_label FROM enrolled_faces")
    }


def search_suggestions(conn) -> list[str]:
    """Every name a search can match in this catalog - enrolled person labels,
    group names, and labels actually on photos (which may differ from the
    enrolled names after a rename) - sorted and de-duplicated, for the search
    box's type-ahead list."""
    names = set(person_labels(conn))
    names.update(r["name"] for r in conn.execute("SELECT name FROM person_groups"))
    names.update(labels_on_photos(conn))
    return sorted(names, key=str.casefold)


def _members_by_label(conn) -> dict[str, list[str]]:
    """{person_label: [group name, ...]} for every membership, sorted by group."""
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT m.person_label AS label, g.name AS name "
        "FROM person_group_members m JOIN person_groups g ON g.id = m.group_id "
        "ORDER BY g.name COLLATE NOCASE"
    ):
        out.setdefault(r["label"], []).append(r["name"])
    return out


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
def list_groups(conn) -> list[dict]:
    """All groups with their members: [{"id", "name", "members": [label, ...],
    "count"}], ordered by name."""
    groups = conn.execute(
        "SELECT id, name FROM person_groups ORDER BY name COLLATE NOCASE"
    ).fetchall()
    by_group: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT group_id, person_label FROM person_group_members "
        "ORDER BY person_label COLLATE NOCASE"
    ):
        by_group.setdefault(r["group_id"], []).append(r["person_label"])
    return [
        {"id": g["id"], "name": g["name"],
         "members": by_group.get(g["id"], []),
         "count": len(by_group.get(g["id"], []))}
        for g in groups
    ]


def create_group(conn, name: str) -> int:
    """Create a group and return its id. Raises ValueError on a blank or
    duplicate name (names are unique, case-insensitive)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A group name is required.")
    try:
        cur = conn.execute("INSERT INTO person_groups (name) VALUES (?)", (name,))
    except sqlite3.IntegrityError:
        raise ValueError(f"A group called '{name}' already exists.")
    conn.commit()
    return cur.lastrowid


def rename_group(conn, group_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("A group name is required.")
    if conn.execute("SELECT 1 FROM person_groups WHERE id = ?", (group_id,)).fetchone() is None:
        raise ValueError("Group not found.")
    try:
        conn.execute("UPDATE person_groups SET name = ? WHERE id = ?", (name, group_id))
    except sqlite3.IntegrityError:
        raise ValueError(f"A group called '{name}' already exists.")
    conn.commit()


def delete_group(conn, group_id: int) -> None:
    """Delete a group; its memberships go with it (ON DELETE CASCADE)."""
    conn.execute("DELETE FROM person_groups WHERE id = ?", (group_id,))
    conn.commit()


def add_member(conn, group_id: int, label: str) -> None:
    """Put a person in a group (idempotent). Validates both exist."""
    if conn.execute("SELECT 1 FROM person_groups WHERE id = ?", (group_id,)).fetchone() is None:
        raise ValueError("Group not found.")
    if label not in person_labels(conn):
        raise ValueError(f"'{label}' is not an enrolled person.")
    conn.execute(
        "INSERT OR IGNORE INTO person_group_members (group_id, person_label) VALUES (?, ?)",
        (group_id, label),
    )
    conn.commit()


def remove_member(conn, group_id: int, label: str) -> None:
    conn.execute(
        "DELETE FROM person_group_members WHERE group_id = ? AND person_label = ?",
        (group_id, label),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Keep membership consistent with the people it points at
# ---------------------------------------------------------------------------
def on_person_renamed(conn, old_label: str, new_label: str) -> None:
    """Follow a person rename through group membership (the label IS the link).
    Collapses onto any existing row so the (group, person) key stays unique."""
    if old_label == new_label:
        return
    for r in conn.execute(
        "SELECT group_id FROM person_group_members WHERE person_label = ?", (old_label,)
    ).fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO person_group_members (group_id, person_label) VALUES (?, ?)",
            (r["group_id"], new_label),
        )
    conn.execute("DELETE FROM person_group_members WHERE person_label = ?", (old_label,))
    conn.commit()


def on_person_deleted(conn, label: str) -> None:
    """Drop a deleted person from every group."""
    conn.execute("DELETE FROM person_group_members WHERE person_label = ?", (label,))
    conn.commit()


# ---------------------------------------------------------------------------
# Re-syncing photo tags to current names (across every trip)
#
# face_tags are per-project; people are shared. A rename done while one project
# was loaded leaves every OTHER project's photos tagged with the old label. These
# helpers rewrite tags across every catalog so a rename reaches all trips, and let
# the UI tidy up labels that drifted before renames propagated.
# ---------------------------------------------------------------------------
def relabel_in_catalog(conn, mapping: dict) -> int:
    """Rewrite face_tags in the catalog `conn` points at, replacing each label in
    `mapping` with its new value (merging/de-duplicating). Returns rows changed.

    Reads tags via json.loads, so it matches regardless of json.dumps' ASCII
    escaping - the LIKE-based rewrite this replaces silently missed Hebrew
    labels, which is how the drift went unnoticed."""
    if not mapping:
        return 0
    changed = 0
    rows = conn.execute(
        "SELECT id, face_tags FROM items "
        "WHERE face_tags IS NOT NULL AND face_tags != '[]'"
    ).fetchall()
    for r in rows:
        try:
            tags = json.loads(r["face_tags"])
        except (json.JSONDecodeError, TypeError):
            continue
        new = sorted({mapping.get(t, t) for t in tags})
        if new != sorted(set(tags)):
            conn.execute("UPDATE items SET face_tags = ? WHERE id = ?",
                         (json.dumps(new), r["id"]))
            changed += 1
    if changed:
        conn.commit()
    return changed


def _other_project_dbs() -> list:
    """Every registered project's catalog FILE except the live catalog (which a
    caller relabels through its own request connection)."""
    import project_store
    from db.schema import DEFAULT_DB_PATH

    live = Path(DEFAULT_DB_PATH).resolve()
    seen = {live}
    out = []
    for pr in project_store.list_projects():
        dbp = project_store._project_db(pr["id"])
        rp = dbp.resolve()
        if rp in seen or not dbp.is_file():
            continue
        seen.add(rp)
        out.append(dbp)
    return out


def relabel_other_projects(mapping: dict) -> int:
    """Apply `mapping` to every project catalog other than the live one. Total
    rows changed. Opens a short-lived connection per project file."""
    if not mapping:
        return 0
    from db.schema import get_connection

    total = 0
    for dbp in _other_project_dbs():
        c = get_connection(dbp)
        try:
            total += relabel_in_catalog(c, mapping)
        finally:
            c.close()
    return total


def orphan_labels_all(live_conn) -> list[str]:
    """Labels present on photos - in the live catalog or any project - that are
    not a current enrolled person. These are the drifted tags the re-sync UI
    offers to remap (e.g. 'user'/'wife' after the people were renamed)."""
    from db.schema import get_connection

    enrolled = {l.casefold() for l in person_labels(live_conn)}
    found = {l for l in labels_on_photos(live_conn) if l.casefold() not in enrolled}
    for dbp in _other_project_dbs():
        c = get_connection(dbp)
        try:
            found.update(l for l in labels_on_photos(c) if l.casefold() not in enrolled)
        finally:
            c.close()
    return sorted(found, key=str.casefold)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def labels_on_photos(conn) -> set[str]:
    """Every distinct label actually present on photos in this catalog.

    This can differ from the enrolled names: a person renamed while another
    project was loaded leaves that project's photos tagged with the OLD label
    (people are shared across projects, face_tags are per-project). Searching has
    to work against what's really on the photos, so we read the tags directly."""
    present: set[str] = set()
    for r in conn.execute(
        "SELECT DISTINCT face_tags FROM items "
        "WHERE face_tags IS NOT NULL AND face_tags != '[]'"
    ):
        try:
            present.update(json.loads(r["face_tags"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return present


def match_on_photos(conn, q: str) -> set[str]:
    """On-photo labels in this catalog that match `q` (case-insensitive
    substring). Touches only the items table, so it is safe on older project
    catalogs that predate the groups tables."""
    ql = (q or "").strip().casefold()
    if not ql:
        return set()
    return {l for l in labels_on_photos(conn) if ql in l.casefold()}


def resolve_query(conn, q: str) -> dict:
    """Resolve a free-text query to the people it names and the labels those imply.

    Matches `q` (case-insensitive substring) against three things:
      - enrolled person names,
      - group names (a match contributes all the group's members), and
      - labels actually present on this catalog's photos (so a name that drifted
        from its enrollment - e.g. photos still tagged 'wife' after the person was
        renamed - is still findable).

    Returns {"labels": set[str], "people": [label, ...], "groups": [name, ...]},
    where `labels` is the union used to find photos and `people`/`groups` say what
    matched. `people` includes any matching on-photo label, enrolled or not.
    """
    q = (q or "").strip()
    if not q:
        return {"labels": set(), "people": [], "groups": []}
    needle = f"%{q}%"
    ql = q.casefold()

    enrolled = [
        r["person_label"]
        for r in conn.execute(
            "SELECT DISTINCT person_label FROM enrolled_faces "
            "WHERE person_label LIKE ? COLLATE NOCASE ORDER BY person_label",
            (needle,),
        )
    ]
    group_rows = conn.execute(
        "SELECT id, name FROM person_groups WHERE name LIKE ? COLLATE NOCASE "
        "ORDER BY name COLLATE NOCASE",
        (needle,),
    ).fetchall()
    groups = [r["name"] for r in group_rows]
    on_photos = sorted(l for l in labels_on_photos(conn) if ql in l.casefold())

    labels: set[str] = set(enrolled) | set(on_photos)
    for g in group_rows:
        for m in conn.execute(
            "SELECT person_label FROM person_group_members WHERE group_id = ?", (g["id"],)
        ):
            labels.add(m["person_label"])
    # `people` = everyone the user might recognise the query by: enrolled matches
    # plus any on-photo label, de-duplicated and stable-sorted.
    people = sorted(set(enrolled) | set(on_photos), key=str.casefold)
    return {"labels": labels, "people": people, "groups": groups}


def item_has_any(face_tags_json: str, labels: set[str]) -> bool:
    """True if a stored face_tags JSON array shares any label with `labels`."""
    if not labels:
        return False
    try:
        tags = json.loads(face_tags_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    return any(t in labels for t in tags)


def search_items(conn, labels: set[str]) -> list[dict]:
    """Photos/clips in the catalog `conn` points at whose face_tags include any
    of `labels`. Each row carries enough to render a result tile:
    {id, path, item_type, face_tags: [..], trip_day, place_name}.

    Membership is tested in Python (face_tags is a JSON array, not queryable
    columns). No SQL LIKE pre-filter: tags are stored with json.dumps' default
    ASCII escaping, so a Hebrew label is on disk as \\uXXXX and a raw-text LIKE
    would silently miss it. json.loads decodes the escapes, so item_has_any
    matches regardless. Catalogs are per-trip (modest size), so the full scan is
    cheap."""
    if not labels:
        return []
    rows = conn.execute(
        """SELECT i.id, i.path, i.item_type, i.face_tags,
                  c.trip_day AS trip_day, i.place_name AS place_name
           FROM items i LEFT JOIN clusters c ON c.id = i.cluster_id
           WHERE i.face_tags IS NOT NULL AND i.face_tags != '[]'
           ORDER BY i.timestamp IS NULL, i.timestamp, i.id"""
    ).fetchall()
    out = []
    for r in rows:
        if not item_has_any(r["face_tags"], labels):
            continue
        out.append({
            "id": r["id"],
            "path": r["path"],
            "item_type": r["item_type"],
            "face_tags": sorted(json.loads(r["face_tags"] or "[]")),
            "trip_day": r["trip_day"],
            "place_name": r["place_name"],
        })
    return out


def search(scope: str, q: str) -> dict:
    """Cross-project search entry point.

    scope "current": search only the live catalog.
    scope "all":     search every registered project (live catalog for the
                     current one, each other project's catalog file read-only).

    Enrolled people and groups are identical in every catalog (the people domain
    is shared), but the labels on photos can differ per project (rename drift).
    Enrolled people and groups are therefore resolved ONCE against the live
    catalog (older project files may predate the groups tables, so we never query
    them there); each project then also matches its own on-photo labels. Returns:
      {"people": [...], "groups": [...], "labels": [...],
       "projects": [{"id", "name", "current": bool, "items": [item, ...]}, ...],
       "total": int}
    where each item adds "project_id" to the search_items row. Projects with no
    match are omitted; `people`/`groups`/`labels` are unioned across the catalogs
    searched.
    """
    import project_store
    from db.schema import DEFAULT_DB_PATH, get_connection

    live = get_connection(DEFAULT_DB_PATH)
    try:
        # Shared part (enrolled people + groups) resolved once on the live catalog.
        base = resolve_query(live, q)
        shared_labels = set(base["labels"])
        current_id = project_store.current_id()
        projects_out: list[dict] = []
        total = 0
        all_people: set[str] = set(base["people"])
        all_groups: set[str] = set(base["groups"])
        all_labels: set[str] = set()

        if scope == "all":
            targets = [
                (p["id"], p.get("name") or "Untitled trip", p["id"] == current_id)
                for p in project_store.list_projects()
            ]
            # A live catalog that was never registered as a project (edge case):
            # still search it so the user's current photos are never invisible.
            if current_id is None:
                targets.append((None, "Current trip", True))
        else:
            name = (project_store.current() or {}).get("name") or "Current trip"
            targets = [(current_id, name, True)]

        for pid, name, is_current in targets:
            if is_current:
                conn = live
            else:
                dbp = project_store._project_db(pid)
                if not dbp.is_file():
                    continue
                conn = get_connection(dbp)
            try:
                # Shared labels + this project's own on-photo labels (drifted tags).
                on_photos = match_on_photos(conn, q)
                labels = shared_labels | on_photos
                items = search_items(conn, labels)
            finally:
                if not is_current:
                    conn.close()
            all_people.update(on_photos)
            if not items:
                continue
            for it in items:
                it["project_id"] = pid
            projects_out.append(
                {"id": pid, "name": name, "current": is_current, "items": items}
            )
            total += len(items)
            all_labels.update(labels)

        return {
            "people": sorted(all_people, key=str.casefold),
            "groups": sorted(all_groups, key=str.casefold),
            "labels": sorted(all_labels),
            "projects": projects_out,
            "total": total,
        }
    finally:
        live.close()
