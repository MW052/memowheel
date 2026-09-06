# Adding languages: Chinese / Japanese / Korean + Russian

**Status:** plan (not yet implemented)
**Target languages:** `zh` (Chinese), `ja` (Japanese), `ko` (Korean), `ru` (Russian)
**Author note:** all four are **left-to-right** and need **no glyph shaping**
(unlike Arabic/Indic), so the hard part is *font coverage*, not text layout.

---

## 1. Two separate problems

"Adding a language" splits into two tracks that share almost nothing:

| | UI (the browser app) | Video (the rendered MP4) |
|---|---|---|
| Where text comes from | keyed strings in `review_app/i18n.py` (793 keys) | **data the user typed** (place names, itinerary) + a little fixed text |
| What it needs | translated strings + a language picker | **fonts that cover the script** |
| For these 4 languages | straightforward | new bundled fonts (the real work) |

The video is already **data-driven**: a Russian user's film shows the Russian
place names they entered — *if the caption font can draw Cyrillic*. Right now it
can't (verified: the bundled caption fonts cover only Latin + Hebrew), so today
those glyphs render as tofu boxes. **This is true independent of UI translation.**

---

## 2. UI track (review_app)

The i18n layer is already built for this. Adding a language is documented in
`review_app/i18n.py` as "add its code to `LANGS` and a table to `TRANSLATIONS`,"
with per-key English fallback so partial translations never blank the UI.

### 2.1 String externalization (recommended refactor)
Today all strings live in one growing `i18n.py`. With 793 keys × 5 languages
that becomes unwieldy and hard to hand to translators.

- Move each language to `review_app/i18n/<code>.json` (`en.json` is the source of
  truth / fallback).
- `i18n.py` loads the JSONs at startup into `TRANSLATIONS`; `_EN` becomes
  `en.json`. `translator()`, `merged_table()`, `i18n_context()` are unchanged.
- Benefit: a translator (or a machine-translation pass) receives one clean JSON;
  diffs are readable; coverage is measurable.
- **Restart required** — i18n loads at startup, no hot reload (see CLAUDE.md).

### 2.2 Language picker (the one real UI code change)
`review_app/templates/base.html:67` hardcodes a **binary** EN↔HE toggle
(`'he' if lang == 'en' else 'en'`, labels `EN·עב`). With 5 languages this must
become a small **dropdown** listing `LANGS` (each item links to `/lang/{code}`).
The route `review_app/routes/lang.py` already accepts any code — no backend
change. Keep it a plain `<a>`/`<form>` list so it still works without JS.

### 2.3 UI fonts for CJK/Cyrillic
- **Cyrillic:** most system UI fonts cover it; acceptable to rely on the browser
  fallback, or add a Cyrillic-covering webfont for visual consistency.
- **CJK:** the app's bundled webfonts (`Assistant-VF`, etc.) don't cover CJK; the
  browser will fall back to a system CJK font. That's fine for readability;
  bundling CJK **web**fonts is optional and heavy (see §3).

### 2.4 Translation integrity (the actual risk — not volume)
Many strings carry `{placeholders}` and inline HTML (`<strong>`, `<br>`). A naive
machine-translation pass will reorder or drop these and break the UI.
- Workflow: machine-translate → **human review**, preserving every `{name}` token
  and HTML tag verbatim.
- Add a **lint/coverage script** (`tools/i18n_check.py`): for each language,
  report (a) missing keys vs `en.json`, (b) keys whose `{placeholders}` set
  differs from English, (c) mismatched HTML tags. Cheap, prevents silent breakage.

---

## 3. Video track (generate/assemble.py) — fonts are the whole job

Verified coverage of the current caption fonts:

```
Assistant-SemiBold.ttf   -> Latin, Hebrew only
FrankRuhlLibre-Bold.ttf  -> Latin, Hebrew only
```

To draw Cyrillic and CJK place names, bundle fonts that cover those scripts:

- **Cyrillic:** one extra font family with Cyrillic + Latin (e.g. a Noto Serif /
  Noto Sans weight, OFL). Small (~hundreds of KB).
- **CJK:** Noto Sans/Serif **CJK** (OFL). Large — **~15–45 MB per weight**
  depending on how many of SC/TC/JP/KR are covered. Subsetting is unsafe here
  because place names are arbitrary user input.

Implementation notes (all in `generate/assemble.py`):
- Add a **font-selection step**: pick the caption font per caption based on the
  script actually present in the string (Hebrew → Frank Ruhl, CJK → Noto CJK,
  Cyrillic/Latin → existing/Noto). A simple Unicode-range check per string is
  enough; a caption is usually single-script.
- `_rtl()` / `python-bidi` already pass LTR text (CJK, Cyrillic) through
  unchanged — **no shaping, no libraqm needed** for these four languages. This is
  the big simplification vs. Arabic.
- Line-height / box sizing (`TITLE_BOX_HEIGHT`, `PLACE_TAG_BOX_HEIGHT`): CJK
  glyphs are taller/denser; verify the caption boxes don't clip. Likely fine, but
  QA at 1080p.
- **DESIGN.md governs fonts.** Direction A is "a warm keepsake, made by hand."
  New families must be chosen to match and **need explicit approval** before use
  (CLAUDE.md). Noto is neutral/legible but not "warm" — a serif (Noto Serif CJK /
  a Cyrillic serif) will sit better with Frank Ruhl than a grotesque sans.

---

## 4. Packaging impact
- New font files are loose data in the bundle (`packaging/trip_video.spec` already
  ships `generate/fonts` + `review_app/static/fonts`). Add the new ones there.
- **Bundle size:** CJK fonts add tens of MB to `dist/`. Acceptable for a desktop
  app, but note it. If size matters, ship CJK fonts as an **optional add-on** the
  app downloads on first use of a CJK caption (an optional first-run download
  pattern; note the face models are now bundled, so nothing else downloads).
- Rebuild via `packaging\build.ps1` after any of this.

## 5. Bilingual mandate
CLAUDE.md requires Hebrew + Latin keep working in every UI/video change. The
font-selection step in §3 must keep routing Hebrew to Frank Ruhl and preserve the
existing bilingual caption behavior.

---

## 6. Suggested phasing
1. **Refactor** strings to per-language JSON + add the picker + the i18n lint
   script. (No new language yet; pure infrastructure. Ship + verify HE still
   works.)
2. **Russian UI** end-to-end as the template language (LTR, cheap fonts) — proves
   the whole path including the picker with 3 languages.
3. **Russian video** — add a Cyrillic caption font + the per-script font
   selector; QA a Cyrillic place name in a render.
4. **CJK UI** (zh/ja/ko) — translation + system-font fallback.
5. **CJK video** — bundle Noto CJK (or the optional-download path); QA clipping.

## 7. Open decisions
- **Which CJK exactly?** zh (Simplified? Traditional?), ja, ko — each affects font
  choice/size. One Noto CJK weight can cover all four regions but is largest.
- **Bundle CJK fonts vs. first-run download?** (size vs. offline-first.)
- **Font families** for CJK/Cyrillic captions — needs a DESIGN.md-aligned pick and
  your approval.
- **Translation source:** machine-translate-then-review, or human translators?
