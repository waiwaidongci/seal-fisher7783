Schema v1 is defined by the SQLAlchemy models and created at startup via Base.metadata.create_all. For this local small application no destructive migration is run. Back up named db-data before modifying schemas.

## Rewind feature tables (schema v1 extension)

The dial-rewind restoration feature adds four tables, all created by the same
`Base.metadata.create_all` startup path (no manual DDL needed):

- `rewind_shifts` — one shift entry: name, whole-shift actual total/missed,
  monotonically increasing `version` (incremented on every create/amend),
  status `draft` / `adopted` / `superseded`, and audit timestamps.
- `rewind_intervals` — per-interval process (A/B), total/missed dial moduli,
  start/end readings of both dials, and the interval output upper bound.
- `rewind_restorations` — every solver run with the `version` it was computed
  against, status (`ok` / `no_solution` / `ambiguous` / `timeout` / `invalid`),
  the unique A/B counts when ok, the per-interval formula basis, samples of
  other feasible interval solutions, elapsed time and author.
- `rewind_adoptions` — at most one adoption per restoration (unique constraint
  on `restoration_id`); records the version adopted, counts written and the
  `observations` source keys produced in the original inspection.

Concurrency control: adoption uses `SELECT ... FOR UPDATE` on the shift row plus
an optimistic check that the client-supplied `expected_version` and the
restoration's stored version both equal the current shift version. Any concurrent
amend bumps the version, so stale adoptions are rejected with 409 and retries
can never double-write. On MySQL 8 the row lock serializes competing
transactions; the unique constraint is the final idempotency guarantee.
