# Spec: Occasion Video — a people-and-time maker on the shared engine

## Context

Trip Video turns **one folder of a trip's media** into a keepsake film: ingest →
cluster into "moments" by **place + day** → review/curate → render an MP4 with
title cards, place captions, and music. The render engine (`generate/assemble.py`
+ `generate/pacing.py`) is mature: title slides, Ken-Burns/fade motion, a
continuous music bed that ducks under a clip's native audio, draft/final,
parallel rendering, and bilingual (Hebrew RTL + Latin) captions.

**Occasion Video** (`features.py` key `occasion`, today `status:"soon"`) is the
second maker: a video for a **birthday, anniversary, or memorial**, built from
photos of **one honoree (or a couple/group)** gathered from the user's *whole*
library via the shared `face_index` engine — not from a single trip folder. Its
narrative axis is the subject's **life over time** (eras/years), not place+day.
This is the consumer `face_index` was explicitly built to serve
(`docs/specs/face-gallery-search.md`, "Occasion Video — source selection").

The headline finding that shapes this whole spec:

> **The render engine is the crown jewel and is almost entirely reusable. What is
> "trip-shaped" is the *organizing axis* (place + day) and the *source* (one
> folder). Occasion Video keeps the engine, swaps the axis to *people +
> life-chronology*, and drops everything about *place*.**

## Goal & scope

**In scope (v1):**
- A new maker that produces an occasion film from **the honoree's photos/clips
  across the library**, selected via `face_index`.
- **Occasion framing:** type (birthday / anniversary / memorial), honoree
  name(s), a headline ("Happy 60th, Dad"), opening dedication, closing message.
- **Chronological organization** into **eras/years**, with a title card per era
  instead of a day card.
- Full reuse of the render engine, per-item editing, People & Groups, project
  store, and the app shell.

**In scope (v1) — also:**
- **Multi-contributor media.** An "Add media" import: pull in existing photos/
  clips others contributed (a folder they sent), run through the same face
  gather/triage as the library source. This is *adding existing media*, not a
  capture feature.

**Explicitly deferred (hooks called out, not built):**
- Prominence-aware auto-selection beyond a simple heuristic.
- Pets (the `face_index` backend seam already covers this if ever pursued).

## Principles

- **Keep the engine, swap the axis.** Occasion is not a fork of Trip Video; both
  are thin planners feeding one shared renderer. The only real engineering is the
  seam that lets them.
- **People + time, never place.** The organizing data is *who* (shared
  `people.py` / `face_index`) and *when* (`media_capture_date`, already EXIF-only
  and fast). GPS plays no part.
- **Fewer moving parts than Trip.** Occasion *drops* whole subsystems (place
  resolution, the cloud LLM, its consent gate, itinerary). It should ship simpler,
  not just different — and with **no network dependency at all**.
- **Respect the occasion.** A memorial is not a birthday. Birthday and
  anniversary share the warm keepsake system; **memorial gets its own distinct
  design direction** (its own palette, typography emphasis, music, and copy
  register), authored in `DESIGN.md` as a sanctioned second direction — not a
  muted recolor of the default.

## The one real architectural change — a maker-agnostic timeline plan

Today `generate/assemble._plan(conn)` reads **trip columns directly**:
`clusters.trip_day` (ordering + day boundaries), `day_captions.caption_text`
(day title cards), `clusters.place_name` (the per-moment place tag), and
`trip_intro` (intro/outro). Everything *downstream* of `_plan` — `_build_item_clip`,
the audio bed (`_build_audio_track`), keyframing, chunking, the parallel render
(`_render_final_parallel`) — is already **generic**: it consumes an ordered list
of specs (`{"kind":"title",…}` | `{"kind":"item", row, show_place_tag}`) plus an
audio-segment list. Nothing below `_plan` knows what a "day" is.

**Change:** extract a small **timeline-plan** abstraction that `_plan` *consumes*
instead of building from trip tables:

```
TimelinePlan:
    intro_text:  str | ""              # opening title slide
    sections: [ Section ]              # ordered
    outro_text:  str | ""              # closing slide
Section:
    title:   str | ""                  # the section's title card (day header | era label)
    items:   [ item row ]              # ordered; already chronological within the section
    item_tag(row) -> str | None        # optional per-item caption shown on the first item
                                        #   (trip: place_name; occasion: a milestone/date, or None)
```

- **Trip planner** (`generate/plan_trip.py`, lifted from today's `_plan`): builds
  the plan from clusters → days → places. Behaviour identical — the per-item tag
  is the existing place tag; section titles are today's `_format_day_header`.
- **Occasion planner** (`generate/plan_occasion.py`): builds the plan from
  eras → the honoree's items, section titles = era labels, item tags = optional
  milestone/date captions.

`generate_video(conn, …)` picks the planner by project type (see below) and feeds
its plan into the unchanged render path. **Net effect:** one clean seam unlocks the
entire engine for Occasion and every future maker. Phase 1 does this refactor with
**zero behaviour change** to Trip Video, verified against current output.

## Reuse as-is (the bulk of the app)

| Subsystem | Why it transfers unchanged |
|---|---|
| **Render engine** `generate/assemble.py`, `pacing.py` | Consumes the timeline plan; title cards, motion, music-bed+duck, parallel render, draft/final, RTL captions all generic. |
| **Face stack** `ingest/faces.py`, `people.py`, `face_index.py` | Enrollment, People & Groups, and library search **are** Occasion's source. |
| **Project store** `project_store.py` | Each occasion = a project; people shared across all projects already. |
| **Per-item editing** (`review.py` + lightbox) | EXIF dates, rotation, crop, clip trim, quality scoring — all per-item, place-independent. |
| **App shell** | Home hub, review UI shell, i18n plumbing, folder picker, `/generate` trigger/status/`/output` serving. |

## What is NEW (does not exist in Trip Video)

### 1. Source = people, not a folder
Occasion's "import" is a **people-driven gather**, replacing the trip folder scan:
1. Pick the honoree — an enrolled person or a **group** (e.g. the couple) via the
   People & Groups type-ahead.
2. Run `face_index.find_person` / `find_group` across the indexed library
   (sensitivity `tier` from the calibrated cutoffs in `face_index.TIERS`).
3. **Triage** the ranked *confident* / *maybe* results (reuse the
   `gallery_results.html` grid: lightbox, right-click "not them"/exclude).
4. Confirmed hits **populate the project's `items`** (path, `item_type`,
   `timestamp` via `media_capture_date`, clip `t_ms` → a suggested trim).

This is where the **tier calibration** tool (`tools/calibrate_gallery.py`) earns
out: import quality rides directly on those cutoffs.

### 2. Organizing axis = life chronology, by calendar year
New grouping, replacing place+day gap clustering: cluster the gathered items by
**calendar year** of `media_capture_date`. Each year becomes a section with a
title card ("2015", user-renamable). Undated items go to an "Undated" section the
user can place. (Age-based or named-era grouping can layer on later; v1 is the
year.)

### 3. Occasion framing content
Replaces `trip_intro`'s trip framing:
- **Occasion type** — birthday / anniversary / memorial. Sets default tone, music
  mood, and title-copy templates.
- **Honoree name(s)** and a **headline** ("Happy 60th, Dad" / "Dana & Noam, 25
  years" / "Remembering Sarah").
- **Opening dedication** and **closing message** (the intro/outro slide text).

### 4. Prominence-aware selection (light heuristic)
For a birthday, prefer photos where the honoree is **prominent**. `face_index`
`subjects` already store `bbox` and `det_score`, so the signal exists: rank/flag by
the matched face's relative area and centrality. v1 = a sort option + a gentle
default; no hard auto-pruning (consistent with Trip's "include all, user curates").

### 5. Milestone / date captions
Optional per-section or per-item captions tied to **time or event** ("1998 — the
wedding"), surfaced as the plan's `item_tag`. Replaces the place caption.

## What is ABANDONED (dropped for Occasion)

| Dropped | Module(s) / columns | Why |
|---|---|---|
| **All GPS / place resolution** | `ingest/geo.py`, `cloud/resolve_places`, `items.place_name/place_source`, `clusters.place_name`, the render place tag | An occasion is about people and time, not where. |
| **Cloud LLM place-ID + its consent gate** | `cloud/llm`, `routes/consent.py`, the API-key setup | Removes the app's **only network dependency** and the entire consent flow. |
| **Place+day gap "moment" clustering** | `ingest/clustering.py` | Replaced by era grouping. |
| **Itinerary import** | `ingest/itinerary.py` | A trip's day-by-day plan; meaningless for an occasion. |
| **"Find my trip by place" import mode** | the place-scan import path in `setup.py` | Source is person-driven now. |
| **Representative-for-place selection** | `ingest/selection.py` (cloud-ID candidate) | No place ID to pick a candidate for. |
| **Per-moment "Find place" in review** | review place endpoints | No place axis. |

That drop list is also what makes Occasion **simpler** than Trip in several places.

## Data model

Reuse the existing catalog schema; add a per-project **occasion facet** and give
the shared `clusters`/`day_captions`/intro tables an occasion reading. Two
columns are new; nothing is destructive (additive `IF NOT EXISTS` /
`_apply_migrations`, consistent with `db/schema.py`).

- **Project type.** The project registry (`projects.json`) gains a
  `kind: "trip" | "occasion"` (default `"trip"` for existing projects).
  `generate_video` and the review UI branch on it.
- **Occasion facet** — a single-row table like `trip_intro`:
  ```sql
  CREATE TABLE IF NOT EXISTS occasion (
      id            INTEGER PRIMARY KEY CHECK (id = 1),
      occasion_type TEXT NOT NULL DEFAULT 'birthday',  -- birthday|anniversary|memorial
      honoree       TEXT NOT NULL DEFAULT '',          -- person label(s) / group name
      headline      TEXT NOT NULL DEFAULT '',
      dedication    TEXT NOT NULL DEFAULT '',           -- opening slide
      closing       TEXT NOT NULL DEFAULT '',           -- closing slide
      theme         TEXT NOT NULL DEFAULT ''            -- memorial theme key (VIDEO_STYLES); '' = default
  );
  ```
- **Sections reuse `clusters` + `day_captions`.** An era is stored as a cluster
  whose `trip_day` holds the era's **sort key** (e.g. the era's first date) and
  whose label lives in `day_captions` keyed by that value. `place_name`/
  `place_source` stay NULL. This keeps the renderer's existing ordering (by
  section then timestamp) with no schema churn; the *planner*, not the schema,
  carries the occasion meaning. (Revisit only if the semantic overload proves
  confusing; the timeline-plan seam makes a later split cheap.)

## UI surfaces (v1)

- **Home hub:** flip `features.occasion` to `status:"available"`, `url:"/occasion"`.
- **New-occasion wizard** (`routes/occasion.py` + templates, mirroring the People
  pages' style): occasion type → honoree pick (People & Groups type-ahead) →
  sensitivity → **gather** (progress, reusing the search path) → **triage grid**
  (confirm/exclude) → framing (headline/dedication/closing) → review.
- **Review:** the existing review grid with occasion labels — sections are eras,
  no place controls; everything else (include/drop, reorder, lightbox edit, clip
  trim, music) as today.
- **Generate:** unchanged trigger/status/`/output`; `generate_video` selects the
  occasion planner.
- **i18n:** EN + HE keys for the wizard, occasion types, era labels, framing;
  keep the tables balanced; HE renders RTL.

## Decisions (settled)

1. **Grouping = calendar year** of capture date, section labels user-renamable.
   Age/named-era grouping is a later layer.
2. **Multi-contributor media is in scope** as an "Add media" import (existing
   photos/clips others contributed), run through the same face gather/triage.
3. **Memorial = three user-selectable themes.** When `occasion_type ==
   'memorial'` the wizard offers three distinct memorial looks (Slate dusk /
   Evergreen / Candlelight, defined in `DESIGN.md`) and the person picks which the
   video uses. Stored as `occasion.theme`; applied via `VIDEO_STYLES`.
   Birthday/anniversary keep Direction A.
4. **Include-all + curate**, same as Trip. Prominence is a *sort*, not an
   auto-prune.
5. **Reuse `clusters`/`day_captions` for sections** rather than new tables, given
   the timeline-plan seam.
6. **Festive decorations on the intro & outro cards**, chosen by the style/mood:
   balloons (Warm), fireworks (Festive), confetti (Bright); hearts (anniversary
   Classic/Romance), confetti (Golden); memorial undecorated. Procedural and
   asset-free (`generate/overlays.py`, numpy + PIL), drawn behind the title so it
   stays legible, and only on intro/outro — not the year section cards.

## Phasing

1. **Render seam (committable groundwork, no behaviour change).** Extract the
   `TimelinePlan`; move today's `_plan` logic into `plan_trip.py`; `generate_video`
   consumes a plan. Verify byte-comparable output on an existing trip.
2. **Occasion skeleton.** Project `kind`; `occasion` facet table; wizard shell;
   `features.occasion` → available; framing → intro/outro slides.
3. **People-driven gather + triage.** Honoree pick → `face_index` search →
   triage grid → populate `items`.
4. **Era grouping + section cards.** Chronological clustering + `plan_occasion.py`.
5. **Occasion polish.** Prominence sort, occasion music defaults, memorial tone
   per `DESIGN.md`.

## Verification approach

- **Phase 1 is the critical gate:** render the *same* trip project before and
  after the seam refactor; confirm identical timeline (section/title order,
  durations, place tags, audio segments) — ideally a byte-identical MP4 or an
  asserted spec/audio-segment dump.
- Occasion unit tests on a throwaway data dir: gather for a person from a seeded
  `face_index`, confirm items populate with capture dates, era grouping buckets
  correctly (incl. undated), the occasion planner emits the expected sections +
  framing slides.
- Browser pass on an isolated data dir: run the wizard end-to-end for a birthday,
  curate, render a draft; repeat for a memorial to check tone.
- Bilingual: flip to Hebrew — wizard, era labels, and framing slides render RTL;
  i18n tables stay balanced.
- Restart `manage.py serve` after any `.py` edit (no hot reload).
