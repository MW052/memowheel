# Design System — Trip Video

> Direction A: **a warm keepsake, made by hand.**
> Read this before making any visual or UI decision, in the app *or* in the
> generated video. The app and the video share one visual language on purpose.

## Product Context
- **What this is:** A locally-run tool that turns a trip's photos and clips into
  a keepsake video — ingest, cluster by place/day, curate in a review UI, then
  generate an MP4 with title cards, place captions, and music.
- **Who it's for:** Anyone producing their *own* videos from their *own* albums.
  Mixed technical skill; design for the least-technical user and allow power
  shortcuts. No accounts — each person runs their own copy.
- **Space/industry:** Personal travel-memory / photo-keepsake tools (adjacent:
  Polarsteps, Journi, Day One, Apple/Google Photos memories).
- **Project type:** Local web app (setup → review/curate → generate) **plus** the
  on-screen style of the generated video.
- **The one memorable thing:** it should feel like a warm keepsake someone made
  by hand — personal and a little nostalgic, not like software.
- **Bilingual, always:** captions and place names appear in Hebrew (RTL, via
  `python-bidi`) and Latin. Every type and layout choice must hold up in both.

## Aesthetic Direction
- **Direction:** Editorial keepsake (warm, tactile, postcard/scrapbook, but
  disciplined — not craft-fair busy).
- **Decoration level:** intentional — subtle paper warmth, hairline rules, a
  stamped-date motif. Nothing more.
- **Mood:** Warm, personal, unhurried. The design gets out of the way so the
  photos carry the emotion.
- **Reference points:** travel-journal apps' day-by-day spine; 2026 "new-nostalgic"
  editorial warmth (postcards, ticket stubs, heritage type).

## Typography
Both workhorse fonts cover Hebrew + Latin. All three are free / OFL and must be
**bundled in the repo** (e.g. `generate/fonts/`) so the video renderer can load
them from disk — replaces the current `C:/Windows/Fonts/arial.ttf`.

- **Display / Hero / Titles:** **Frank Ruhl Libre** — classic Hebrew-native
  editorial serif; carries the keepsake warmth. Trip title, day headers, video
  title cards.
- **Body / UI / Labels / Buttons:** **Assistant** — warm humanist Hebrew+Latin
  sans; highly legible for non-technical users.
- **Metadata / dates / stamps:** letterspaced small-caps Assistant, or **Cutive
  Mono** for a typewriter date-stamp feel (dates are numeric/Latin).
- **Data/Tables:** Assistant with tabular figures (`font-variant-numeric:
  tabular-nums`). No dense tables in this product.
- **Code:** n/a (not a developer surface).
- **Loading (web):** Google Fonts / Bunny Fonts `<link>`; self-host the same TTFs
  for the video renderer.
- **Scale (px):** 12, 14, 16 (body), 20, 26 (day header), 38 (video title),
  44–64 (hero). Serif for ≥20 headings; sans for ≤16 UI text.

## Color
Restrained-warm: warm neutrals + a single terracotta accent. Color is rare and
meaningful; the photos are the color.

- **Approach:** restrained (one accent + warm neutrals).
- **Primary accent:** `#C25B4A` terracotta — primary buttons, active states, the
  stamp motif. Use sparingly. Hover/darker: `#A8452F`.
- **Secondary:** `#3E5C76` ink-indigo — dates, links, quiet secondary info.
- **Neutrals (light → dark):** paper `#FBF7F0`, card `#FEFCF8`, `#EDE6DA`,
  `#D8CEC0`, `#A89C8D`, muted text `#7A716A`, ink text `#2B2724`.
- **Semantic (muted to fit):** success `#5E7D5A`, warning `#C89A3C`,
  error `#B24A3A`, info `#3E5C76`.
- **Video surfaces:** cream title card `#F7F1E6` (default) and espresso title
  card `#241F1B` (alternate, for balance between bright photos). Place captions
  render in cream `#F7F1E6` on a translucent espresso band (`#2B2724` at ~55%
  opacity) — this is the caption-scrim already shipped in `generate/assemble.py`,
  recolored warm instead of pure black.
- **Dark mode (app):** not a priority — this is a light-first keepsake. If added
  later, warm the darks (espresso, not neutral black) and reduce accent
  saturation ~15%.

## Spacing
- **Base unit:** 8px.
- **Density:** comfortable-to-spacious — generous margins give the scrapbook-page
  calm and keep it approachable.
- **Scale:** 2 / 4 / 8 / 16 / 24 / 32 / 48 / 64.

## Layout
- **Approach:** hybrid — grid-disciplined for the review photo grid (predictable,
  easy), creative-editorial for day headers and video title cards (asymmetry,
  big margins, a stamped date).
- **Spine:** day-by-day chronological, mirroring the pipeline's clusters/days.
- **Grid:** review photo grid 4-across desktop / 2-across mobile; content column
  max-width ~1120px.
- **Border radius:** sm 6px, md 12px, lg 18px. Soft, not bubble-round.
- **Elevation:** soft warm shadows, e.g.
  `0 1px 2px rgba(43,39,36,.05), 0 10px 30px rgba(43,39,36,.07)`.

## Motion
- **Approach:** intentional but gentle — nothing bouncy.
- **App:** subtle card hover-lift (~1px, 120ms), soft fades on state changes.
- **Video:** slow Ken Burns (already implemented), cross-dissolves between
  segments, unhurried title-card holds.
- **Easing:** enter `ease-out`, exit `ease-in`, move `ease-in-out`.
- **Duration:** micro 50–100ms, short 150–250ms, medium 250–400ms, long 400–700ms.

## Where this lives in the code
- **App UI:** `review_app/templates/*.html` (currently a single dark
  `review.html` — this system replaces the dark, bare-bones look).
- **Video output:** `generate/assemble.py` — title cards (`_title_slide`),
  place captions (`_text_overlay` / scrim), fonts (`CAPTION_FONT`), colors.
- **New surface to design/build:** setup & onboarding (enroll faces, start an
  album, import photos/clips) — today it's CLI (`manage.py`), needs a GUI for the
  non-technical audience.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-24 | Initial design system created (Direction A: warm keepsake) | /design-consultation; product reframed as a keepsake maker for friends/family. Chose editorial-keepsake over calm-minimal (B) and cinematic (C) to match the emotional job. |
| 2026-07-24 | Frank Ruhl Libre + Assistant as core fonts | Only warm/editorial pairing that natively covers Hebrew + Latin, which the bilingual captions require and most nostalgic display fonts lack. |
| 2026-07-24 | Single terracotta accent on warm paper | Postal/stamp language; memorable without shouting; keeps focus on the photos. |
| 2026-07-24 | AI mockups skipped | OpenAI org verification required on the user's account; used a live HTML font/color preview instead (more faithful for a bilingual serif system). |
