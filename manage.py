"""Command-line entry points: enroll faces, run ingest, serve the review app.

    python manage.py enroll user "C:\\path\\to\\reference_photos_folder"
    python manage.py enroll wife "C:\\path\\to\\reference_photos_folder"
    python manage.py itinerary "C:\\path\\to\\itinerary.txt"
    python manage.py ingest "C:\\path\\to\\trip\\photos" [--exclude folder1,folder2] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
    python manage.py ingest "C:\\path\\to\\camera_roll" --list-places      (list places found in the folder)
    python manage.py ingest "C:\\path\\to\\camera_roll" --places "Lisbon, Lisbon;Sintra, Lisbon"
    python manage.py reset-ingest
    python manage.py retry-unresolved
    python manage.py serve
    python manage.py tray   (windowless: system-tray icon, no console)

'enroll' also accepts individual file paths instead of a folder:
    python manage.py enroll user photo1.jpg photo2.jpg ...

'ingest' skips any subfolder whose name matches --exclude, e.g. a "test"
folder nested inside the real trip folder:
    python manage.py ingest "C:\\...\\japan 2026" --exclude test

'reset-ingest' clears items/clusters/skipped_files (test-run leftovers)
but keeps enrolled_faces, day_captions, and trip_intro - the real project
data you don't want to redo.

'retry-unresolved' resets 'failed' clusters back to 'pending' so they can
be re-approved at /consent - useful after a selection-logic bug fix.
"""

import sys
from pathlib import Path

from db.schema import get_connection, init_db, DEFAULT_DB_PATH
from ingest.faces import assess_enrollment, save_enrollment, load_enrolled
from ingest.itinerary import parse_itinerary
from ingest.pipeline import run_ingest, PHOTO_EXTENSIONS
from cloud.resolve_places import select_representatives


def _resolve_reference_photos(paths: list[str]) -> list[str]:
    """A single directory argument expands to every photo file in it
    (non-recursive). Otherwise, each argument is used as-is."""
    if len(paths) == 1 and Path(paths[0]).is_dir():
        folder = Path(paths[0])
        return [
            str(p) for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in PHOTO_EXTENSIONS
        ]
    return paths


def cmd_enroll(person_label: str, raw_paths: list[str]) -> None:
    if not person_label.strip():
        print("person label must be a non-empty name (e.g. a person's name)")
        sys.exit(1)
    reference_photo_paths = _resolve_reference_photos(raw_paths)
    if not reference_photo_paths:
        print(f"No photo files found in {raw_paths}")
        sys.exit(1)
    init_db()
    try:
        quality = assess_enrollment(reference_photo_paths)
    except ValueError:
        print("Couldn't find a face in any of those photos. Use clear, "
              "front-facing photos of just this person.")
        sys.exit(1)

    pct = "n/a" if quality.median is None else f"{round(quality.median * 100)}%"
    print(f"Face found in {quality.found} of {quality.total} photo(s). "
          f"Match quality: {pct}")
    if quality.undetected:
        print(f"  No face found in: {', '.join(quality.undetected)}")
    if quality.weak:
        print(f"  Look like a different person (remove/replace): {', '.join(quality.weak)}")

    if not quality.accepted:
        print(f"NOT enrolled '{person_label}': the photos don't look like the same "
              f"person (quality {pct}). Use 3-4 clear, front-facing photos of only "
              f"this person; avoid group shots, sunglasses, side angles, and blur.")
        sys.exit(1)

    conn = get_connection(DEFAULT_DB_PATH)
    try:
        save_enrollment(conn, person_label, quality.embedding, reference_photo_paths)
    finally:
        conn.close()
    print(f"Enrolled '{person_label}' from {len(reference_photo_paths)} reference photo(s).")


def cmd_itinerary(path: str) -> None:
    """Seeds day_captions with a default caption per day (the joined place
    list) - editable afterward in the review UI. Uses INSERT OR IGNORE so
    re-running this doesn't clobber captions you've already edited there."""
    itinerary = parse_itinerary(path)
    init_db()
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        for trip_day, day in itinerary.items():
            conn.execute(
                "INSERT OR IGNORE INTO day_captions (trip_day, caption_text) VALUES (?, ?)",
                (trip_day, day.short_info),
            )
        conn.commit()
    finally:
        conn.close()
    print(f"Seeded captions for {len(itinerary)} day(s). Edit them in the review UI before generating.")


def cmd_ingest(folder: str, exclude_dirs: set[str] | None = None,
               date_start=None, date_end=None, places=None) -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        enrolled = load_enrolled(conn)
    finally:
        conn.close()
    if not enrolled:
        print("At least one person must be enrolled first (run 'enroll'). Currently enrolled: none")
        sys.exit(1)
    counts = run_ingest(folder, enrolled, exclude_dirs=exclude_dirs,
                        date_start=date_start, date_end=date_end, places=places)
    if counts and (date_start or date_end):
        print(f"Skipped {counts['skipped_out_of_range']} file(s) outside the date range; "
              f"included {counts['included_undated']} undated file(s).")
    if counts and places:
        print(f"Skipped {counts['skipped_out_of_place']} file(s) outside the selected place(s).")
    print("Ingest complete. Run 'python manage.py serve' to review.")


def cmd_list_places(folder: str, exclude_dirs: set[str] | None = None) -> None:
    """Print the places found in a folder (from photo GPS tags) so a CLI user can
    copy exact labels into `ingest --places`. Read-only."""
    from pathlib import Path
    from ingest.geo import scan_places
    result = scan_places(Path(folder), exclude_dirs)
    for p in result["places"]:
        print(f"{p['count']:>6}  {p['label']}")
    if result["no_location"]:
        print(f"{result['no_location']:>6}  (no location)")
    if not result["places"] and not result["no_location"]:
        print("No media found.")


def cmd_reset_ingest() -> None:
    """Clears test-run leftovers (items/clusters/skipped_files) before a
    real ingest, without touching enrolled_faces, day_captions, or
    trip_intro - those are real project data, not test artifacts."""
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM clusters")
        conn.execute("DELETE FROM skipped_files")
        conn.commit()
    finally:
        conn.close()
    print("Cleared items/clusters/skipped_files. enrolled_faces, day_captions, and trip_intro were left untouched.")


def cmd_retry_unresolved() -> None:
    """Resets 'failed' clusters back to 'pending' and clears their
    representative flag so select_representatives() re-picks fresh
    (using whatever selection-logic fixes have landed since they last
    failed - e.g. the clip-as-candidate bug). Re-approve them at /consent
    afterward; this does not re-run the API call itself."""
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        failed_cluster_ids = [
            row["id"] for row in
            conn.execute("SELECT id FROM clusters WHERE consent_status = 'failed'").fetchall()
        ]
        if not failed_cluster_ids:
            print("No failed clusters to retry.")
            return
        conn.executemany(
            "UPDATE items SET is_representative = 0 WHERE cluster_id = ?",
            [(cid,) for cid in failed_cluster_ids],
        )
        conn.executemany(
            "UPDATE clusters SET consent_status = 'pending' WHERE id = ?",
            [(cid,) for cid in failed_cluster_ids],
        )
        conn.commit()
        select_representatives(conn)
    finally:
        conn.close()
    print(f"Reset {len(failed_cluster_ids)} failed cluster(s) to pending. Re-check /consent.")


def cmd_serve() -> None:
    import os
    import uvicorn

    from review_app.main import build_log_config

    # Open the setup page shortly after the server comes up, so a non-technical
    # user who double-clicks the launcher lands in the UI without touching a
    # browser bar. Set TRIPVIDEO_NO_BROWSER=1 to suppress (tests, headless).
    if not os.environ.get("TRIPVIDEO_NO_BROWSER"):
        import threading
        import webbrowser
        threading.Timer(2.0, lambda: webbrowser.open("http://127.0.0.1:8000/")).start()

    # timeout_graceful_shutdown bounds how long Ctrl+C's graceful shutdown
    # waits for in-flight requests before forcing an exit - without this,
    # a slow/stuck request could make shutdown hang indefinitely.
    # log_config adds timestamps to uvicorn's request/access log lines.
    uvicorn.run(
        "review_app.main:app", host="127.0.0.1", port=8000,
        timeout_graceful_shutdown=5, log_config=build_log_config(),
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    if command == "enroll":
        cmd_enroll(sys.argv[2], sys.argv[3:])
    elif command == "itinerary":
        cmd_itinerary(sys.argv[2])
    elif command == "ingest":
        exclude_dirs = None
        if "--exclude" in sys.argv:
            idx = sys.argv.index("--exclude")
            exclude_dirs = {name.strip() for name in sys.argv[idx + 1].split(",") if name.strip()}

        def _date_flag(flag):
            from datetime import date
            if flag in sys.argv:
                raw = sys.argv[sys.argv.index(flag) + 1]
                try:
                    return date.fromisoformat(raw)
                except ValueError:
                    print(f"{flag} must be YYYY-MM-DD, got {raw!r}")
                    sys.exit(1)
            return None

        date_start, date_end = _date_flag("--from"), _date_flag("--to")
        if date_start and date_end and date_start > date_end:
            print("--from is after --to")
            sys.exit(1)
        if "--list-places" in sys.argv:
            cmd_list_places(sys.argv[2], exclude_dirs)
        else:
            # Labels contain commas ("Lisbon, Lisbon"), so places are ';'-separated.
            # Run `ingest <folder> --list-places` first to see the exact labels.
            places = None
            if "--places" in sys.argv:
                raw = sys.argv[sys.argv.index("--places") + 1]
                places = [s.strip() for s in raw.split(";") if s.strip()] or None
            cmd_ingest(sys.argv[2], exclude_dirs, date_start, date_end, places)
    elif command == "reset-ingest":
        cmd_reset_ingest()
    elif command == "retry-unresolved":
        cmd_retry_unresolved()
    elif command == "serve":
        cmd_serve()
    elif command == "tray":
        import tray_app
        tray_app.main()
    else:
        print(__doc__)
        sys.exit(1)
