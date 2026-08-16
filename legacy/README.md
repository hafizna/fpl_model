# Legacy prototype

The project began as exploratory notebooks and a single `fpl_tools.py` module while learning data/AI coding workflows.

The prototype established several useful product ideas:

- live FPL API ingestion
- player recommendation tables
- expected-points exploration
- fixture analysis
- squad optimization

It also used intentionally rough heuristics that should **not** be copied into the production model, including fixed weighted scores, a simplified clean-sheet probability, assumed appearance points, random train/test splitting for season-level features, and greedy squad selection.

The new codebase keeps the original product intent but rebuilds the modelling pipeline around deadline-safe data, component-level xPts, temporal validation, and auditable context features.

Original notebooks can be kept locally or added later under a clearly marked archival subfolder if useful for provenance. They are not runtime dependencies.
