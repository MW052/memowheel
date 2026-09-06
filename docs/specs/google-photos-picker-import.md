# Spec: Import from Google Photos (Picker API)

Status: proposed · Author: Michael Weiss · 2026-08-19

## 1. Problem

Many people keep everything in **Google Photos**, which — unlike Google Drive —
**cannot be mounted as a folder** (Drive for Desktop doesn't include Photos;
Google split them in 2019). So the drive-based picker can't reach it, and the
file-based importer (`_iter_media_files` → PIL/ffmpeg on paths) has nothing to
open.

The zero-code answer is **Google Takeout** (export → unzip → import that folder),
now documented in the README and user guide. This spec covers the optional
*in-app* path: let the user pick photos from Google Photos inside the app and
import them directly.

## 2. The hard constraint: no more "list my library"

As of **March 31, 2025** Google **removed the broad Library API read scopes**
(`photoslibrary.readonly`, list/search of a user's whole library) for
general third-party apps. An app can now only:
- access media **it uploaded itself** (`…readonly.appcreateddata`), or
- use the **Google Photos Picker API**: the *user* selects items in Google's own
  picker UI, and the app gets temporary access to **just those** items.

So "browse all your Google Photos in our app" is **not possible** anymore. The
only sanctioned in-app route is the **Picker API** — the user hand-picks (photos
or whole albums) in Google's UI, and we download the selection.

## 3. Approach

Same shape as the connected-device spec (`import-from-connected-device.md`):
authenticate → user picks → **download selected items to a temp folder → run the
existing import on that folder**. Everything device/cloud-specific sits *in front
of* the pipeline; nothing downstream changes.

Picker API flow:
1. **OAuth 2.0**, installed-app flow with **PKCE**, scope
   `https://www.googleapis.com/auth/photospicker.mediaitems.readonly`.
2. **Create a session** — `POST https://photospicker.googleapis.com/v1/sessions`
   → returns `id`, `pickerUri`, `pollingConfig`.
3. Open `pickerUri` in the user's browser (the app already opens a browser).
4. **Poll** `GET /v1/sessions/{id}` until `mediaItemsSet == true` (honor
   `pollingConfig` interval/timeout).
5. **List** picked items — `GET /v1/mediaItems?sessionId={id}` → each has
   `baseUrl`, `filename`, and `mediaMetadata` (`creationTime`, dimensions,
   photo/video info).
6. **Download** bytes with the OAuth token in the `Authorization` header:
   photos `"{baseUrl}=d"`, videos `"{baseUrl}=dv"`.
7. **Delete** the session when done.

## 4. Design

- Isolated module `cloud/google_photos.py`, mirroring the device interface:
  ```
  start_session() -> {session_id, picker_uri}
  poll_session(session_id) -> {"ready": bool}
  download_selection(session_id, dest_dir, progress_cb) -> counts
  ```
- Routes (Setup, behind an Advanced "Import from Google Photos" action):
  - `POST /setup/gphotos/start` → OAuth if needed, create session, return
    `picker_uri`; the wizard opens it in a new tab.
  - `GET /setup/gphotos/status?session_id=…` → `{ready}` (polled by the UI).
  - `POST /setup/gphotos/import` → `download_selection(...)` into
    `data/_gphotos_import/<ts>/`, then the **existing**
    `run_ingest(temp_dir, …)` (date filter reused; picking already narrows), then
    delete the temp dir.
- **Token storage:** the OAuth **refresh token** goes in the existing
  `secret_store` (OS keychain), same place the LLM key lives — never plaintext.
- **Credentials:** a Google Cloud project + OAuth **client ID** (Desktop type).
  For a distributed desktop app the client id ships in the binary; the installed-
  app + PKCE flow is designed for that (the "secret" isn't truly secret), but see
  Risks.

## 5. Consequences to flag

- **GPS is stripped on download.** Google removes location EXIF from Picker/API
  downloads, so the app's *free offline place labels* (`resolve_gps_items`) won't
  fire for Photos-sourced items — place-ID falls back to the vision AI or manual
  entry. Capture **time** is preserved (in EXIF and `mediaMetadata.creationTime`),
  so clustering and the date filter still work.
- **Privacy-model shift.** This introduces a **Google account, OAuth, tokens, and
  network calls** — a real departure from the app's "local, no accounts, nothing
  leaves the machine" premise. It's read-only *from* Google (we download, never
  upload), but it's still the first feature that ties the app to an external
  account. Deliberate decision required.
- **App verification.** The Picker scope is user-data; an unverified app shows
  Google's "unverified app" warning and is capped (~100 users) until Google
  verifies it — a process with branding/policy requirements. Fine for
  friends-and-family; a blocker for wide distribution without verification.
- **HEIC.** iPhone libraries are often HEIC, not in `PHOTO_EXTENSIONS`; needs
  `pillow-heif` (shared follow-up with the device spec).

## 6. Dependencies & packaging

- HTTP + OAuth: `google-auth` + `google-auth-oauthlib` + `requests` (or hand-
  rolled OAuth over `requests`/`httpx` to stay lean). Adds bundle size and needs
  PyInstaller hooks; verify on a clean box.
- Network access at import time (obviously); handle offline/timeout gracefully.

## 7. Phasing

- **Phase 1:** OAuth (installed-app + PKCE) + session + poll + download-to-temp +
  reuse import. Manual pick is the filter. Ships the core "import from Google
  Photos" value.
- **Phase 2:** persist/refresh token for silent re-auth; album-first UX; real
  download progress + video (`=dv`) handling; retry/resume.
- **Phase 3:** HEIC decode; surface the GPS-stripped caveat in-UI.

## 8. Risks

- **Google API churn** — they already reshaped these APIs in 2025; an
  integration here carries ongoing maintenance risk that Takeout does not.
- **OAuth verification / consent UX** for a distributed app.
- **Shipping client credentials** in a desktop binary (mitigated by PKCE, but a
  consideration).
- **Network + account dependency** vs. the app's offline-local ethos.
- **GPS loss** weakens the automatic place-labelling that's a selling point.

## 9. Recommendation

**Default to Takeout** (already documented) — zero code, no accounts, no API
risk, and it works with the date filter. Build the Picker integration **only if
in-app Google Photos import is a real priority**, and if so, do **Phase 1** first
behind the isolated `google_photos` module, sharing the **download-to-temp →
reuse pipeline** bridge with the connected-device feature (`_device_import` /
`_gphotos_import` can share the temp-import plumbing). Weigh the OAuth +
verification + maintenance cost against how many users actually need in-app
import over exporting a folder once.

## 10. Open questions

1. Is in-app Google Photos import worth introducing **OAuth/accounts** into a
   deliberately account-free local app — or is documenting Takeout enough?
2. Is the **GPS-stripped** trade-off (no free auto place labels for Photos
   imports) acceptable?
3. Google **app verification** — pursue it (wide distribution) or stay under the
   unverified-app cap (friends-and-family only)?
4. Share the temp-import + copy/download plumbing with the connected-device spec
   as one "external import" subsystem?
