# Pacing / Music-Sync Design (Next Steps step 3)

Decided before the SQLite schema (step 4) per the outside-voice resequencing in the
plan-eng-review — this determines what fields the schema needs.

## Decision

- **Photos**: fixed base duration, default **4.0 seconds**, rendered with a Ken Burns
  pan/zoom effect. Overridable per-item via `duration_override` in the review UI.
- **Video clips**: played at their natural trimmed length, capped at a default max of
  **8.0 seconds** — never time-stretched or compressed to fit a slot.
- **No beat-sync.** Background music plays under the whole sequence: fades in over the
  first ~2s, fades out over the last ~2s, and is looped (if shorter than the final video)
  or trimmed (if longer) to match total length. No audio-beat-driven cut timing — that
  would need audio analysis (e.g. librosa) as a new dependency for a feature this project
  doesn't need (Premise 6: "good enough beats elegant").
- **No quality-based duration weighting for v1.** Every included photo gets the same
  base duration regardless of quality score. Simpler default; can be revisited later if
  the output feels monotonous.
- **Total duration is a soft target (15 minutes), not a hard constraint.** It's achieved
  by how many items you choose to include at review time, not by force-fitting item
  durations to hit an exact total. The generate step renders whatever's included at each
  item's default/override duration and the total comes out close to the target.

## Schema implication

This decision means the schema (step 4) needs, beyond what was already listed in the
design doc:
- `item_type` (`photo` | `clip`) — duration behavior differs by type.
- `duration_override` (nullable float) — lets the review UI override an item's default
  duration without needing a separate weighting subsystem.

No beat-timestamp field, no per-item weight field — both were considered and explicitly
not needed given the decisions above.
