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

---

# Direction M (Memorial) — PROPOSED, pending approval

> A **second, sanctioned direction** used ONLY by Occasion Video when
> `occasion_type == 'memorial'` (docs/specs/occasion-video.md). Birthday and
> anniversary keep Direction A (warm keepsake). This is **not a recolor** of A —
> it changes color, type emphasis, spacing, motion, music and copy register to a
> reverent one. It stays recognizably the same product: same bilingual font
> system, same tactile restraint, same photos-carry-the-emotion principle.
>
> **Direction M is not one look but a small set of three memorial themes the
> person picks for their video** (Slate dusk / Evergreen / Candlelight — see "The
> three memorial themes" below). They share every memorial fundamental on this
> page (type, spacing, motion, copy); they differ only in color/card treatment so
> a family can choose the register that fits — dusk, daylight, or candlelight.
> The three are implemented as `generate/assemble.VIDEO_STYLES` entries selected
> per occasion.

## Intent
Where A is *warm, personal, unhurried*, M is **quiet, reverent, still**. The job
is remembrance, not celebration. The design recedes further than A so the person
and the photographs hold the room; emptiness and silence are used on purpose.

## Aesthetic Direction
- **Direction:** memorial-editorial — a candlelit evening to A's warm afternoon.
  Still, spacious, dignified; never somber-clichéd (no black borders, no heavy
  mourning ornament).
- **Decoration level:** less than A. A single hairline rule and generous
  negative space replace A's stamped-date motif. At most one restrained glyph.
- **Mood:** calm, tender, respectful. Longer holds; more air; dimmer.

## Typography (same fonts, different emphasis)
Same bundled families as A (Frank Ruhl Libre + Assistant + Cutive Mono) — keeps
Hebrew+Latin coverage and product continuity. Shift the emphasis:
- **The person's name is the hero** — Frank Ruhl Libre, large (48–64), generous
  leading, centered, lots of surrounding space. In A the *trip* is the hero; in M
  the *person* is.
- **Kicker:** a quiet "In loving memory" / "לזכר" above the name — letterspaced
  Assistant small-caps, muted.
- **Life dates:** a born–died range (`1942 – 2026`) in Cutive Mono / small-caps,
  slate-indigo — the dignified analogue of A's date-stamp.
- **Year/section headers:** Frank Ruhl Libre, airier and quieter than A's.
- More serif, more leading, fewer bold weights than A.

## Color (cooler, deeper, quieter — and chosen per video)
A's terracotta is retired here — too lively. All three memorial themes share a
cool-neutral base and the espresso/cream caption mechanism; they differ in the
title-card treatment and the single accent (used only for a hairline under the
name and the life-date rule). Shared neutrals: paper `#EFEDE8`, card `#F8F6F1`,
line `#DED8CE`, muted text `#6F6A64`, ink `#241F1B` (espresso, shared with A).
Captions on photos stay cream `#F7F1E6` on the espresso scrim in every theme, for
legibility over bright images. The three themes (see below) set card background,
title text, and accent.

## Spacing
One step more generous than A everywhere (emptiness = reverence). Same 8px base;
lean on 24 / 32 / 48 / 64 / 96. Title cards especially: wide margins, the name
alone with air around it.

## Motion (slower, stiller)
- **Video:** near-still — a very slow drift or a held frame rather than A's Ken
  Burns push; **longer cross-dissolves** (~0.8–1.2s) that dip through black
  between chapters; title-card holds +1–2s longer than A. Stillness is allowed.
- **App:** fades only; **no hover-lift** (A's ~1px card lift feels too lively
  here). Durations at the long end of A's scale.

## Music & audio (defaults/guidance)
Gentle, sparse, low-tempo instrumental by default; duck further under clips than
A; **silence between chapters is acceptable**, even wanted. (Tracks are still
user-provided — this guides defaults and the mood we suggest.)

## Copy register
Restrained and present-tense; **no celebratory words** ("Happy", "keepsake",
exclamation marks). Patterns:
- Kicker: "In loving memory" · Hebrew "לזכר".
- Headline: the person's name (the honoree field).
- Closing: a quiet line — e.g. "Always remembered".
- **Hebrew needs care:** the traditional memorial phrases are gendered
  (ברוך/ברוכה); default to a neutral, gentle line and let the user set the exact
  wording. Flag this for review rather than hard-coding a gendered phrase.

## Where this applies (implementation sketch — for after approval)
- **App:** a `data-theme="memorial"` on `<body>` for occasion projects whose
  `occasion_type == 'memorial'`, swapping the accent/paper tokens above. Birthday
  / anniversary render Direction A unchanged. The review chrome stays calm; this
  mostly tints accents and softens motion.
- **Video (`generate/assemble.py`):** a memorial style path keyed on
  `occasion_type` — espresso default card, slower motion + longer holds/dissolves,
  the music guidance above. The planner (`_plan_occasion`) already carries the
  framing; only the *style* constants branch.
- Keep it token-driven so A and M share one stylesheet with a theme switch, not a
  forked UI.

## The three memorial themes (the person picks one per video)
Offered in the Occasion wizard when `occasion_type == 'memorial'`; stored on the
occasion and applied to the rendered video (card background, title text, the
accent hairline under the name + the life-date rule). All share the fundamentals
above.

| Theme | Card | Title text | Accent (hairline / date) | Reads as |
|-------|------|-----------|--------------------------|----------|
| **Slate dusk** (default) | espresso `#241F1B` | cream `#F7F1E6` | slate-indigo `#3E5C76` | dusk, dignified, a hint of candle |
| **Evergreen** | linen `#EFEDE8` | ink `#241F1B` | muted evergreen `#55685C` | daylight, peace, a garden of remembrance |
| **Candlelight** | espresso `#241F1B` | cream `#F7F1E6` | candle-brass `#B59A62` (+ a small centered mark) | warmest, most traditional — a yahrzeit candle |

- **Title card:** the person's name (serif hero) with a short accent **hairline**
  under it in the theme's accent; the kicker "In loving memory / לזכר" above, and
  the born–died **life-date rule** in the accent color.
- **Captions over photos:** cream on the espresso scrim in all three.
- **One open copy choice:** the Hebrew closing line — traditional memorial phrases
  are gendered (ברוך/ברוכה), so we default to a neutral, gentle line and let the
  user set exact wording (surfaced as the occasion's Closing field).

## Occasion decorations (intro & outro cards)
Birthday and anniversary films get a gentle, asset-free motion motif on the
**opening and closing cards only** (not the year section cards), chosen by the
style/mood: balloons (Warm), fireworks (Festive), confetti (Bright) for birthdays;
hearts (Classic/Romance) and confetti (Golden) for anniversaries. **Memorial has
none** — stillness is the point. Motifs are drawn procedurally
(`generate/overlays.py`, numpy + PIL, deterministic) and composited **behind** the
title so the words stay fully legible. Keep them restrained — a quiet rise/float,
never a screen-filling spectacle.

---

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-24 | Initial design system created (Direction A: warm keepsake) | /design-consultation; product reframed as a keepsake maker for friends/family. Chose editorial-keepsake over calm-minimal (B) and cinematic (C) to match the emotional job. |
| 2026-07-24 | Frank Ruhl Libre + Assistant as core fonts | Only warm/editorial pairing that natively covers Hebrew + Latin, which the bilingual captions require and most nostalgic display fonts lack. |
| 2026-07-24 | Single terracotta accent on warm paper | Postal/stamp language; memorable without shouting; keeps focus on the photos. |
| 2026-07-24 | AI mockups skipped | OpenAI org verification required on the user's account; used a live HTML font/color preview instead (more faithful for a bilingual serif system). |
| 2026-10-24 | Direction M (Memorial) = **three user-selectable themes** | Occasion Video memorials need a reverent register distinct from A. Rather than fix one look, the app offers three memorial themes (Slate dusk / Evergreen / Candlelight) and the person picks which the video uses — implemented as VIDEO_STYLES entries chosen per occasion. |
