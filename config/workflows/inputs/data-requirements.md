# Example requirements: account-period charge aggregate

This is a synthetic C0 data fixture. The amounts and identifiers below are
invented. No customer data, live source access or regulatory interpretation is
needed to satisfy it.

## Outcome and grain

Produce a sandbox data artifact with one row per account and calendar month that
has at least one included active event. The measure is `charge_amount_eur`, an
exact decimal with two fractional digits. All supplied amounts are already EUR;
currency conversion is outside this outcome.

Use effective dates in September 2026 and a processing cutoff of
`2026-10-03T00:00:00Z`, inclusive. An event recorded after the cutoff does not
contribute even when its effective date is in September.

## Source fixture

| Event | Revision | Account | Effective date | Recorded at (UTC) | Amount EUR | State |
| --- | --- | --- | --- | --- | --- | --- |
| e1 | 1 | A | 2026-09-12 | 2026-09-12T10:00:00Z | 10.00 | active |
| e1 | 2 | A | 2026-09-12 | 2026-10-02T10:00:00Z | 12.00 | active |
| e2 | 1 | A | 2026-09-13 | 2026-09-13T10:00:00Z | 5.00 | active |
| e2 | 2 | A | 2026-09-13 | 2026-10-01T10:00:00Z | 0.00 | cancelled |
| e3 | 1 | B | 2026-09-14 | 2026-09-14T10:00:00Z | 3.00 | active |
| e3 | 1 | B | 2026-09-14 | 2026-09-14T10:00:00Z | 3.00 | active |
| e4 | 1 | A | 2026-09-15 | 2026-10-04T10:00:00Z | 7.00 | active |

## Requirements

- D1: Select the highest revision visible by the cutoff for each event ID.
  Cancelled latest revisions contribute no amount.
- D2: Collapse byte-equivalent duplicate source records. Conflicting records
  with the same event ID and revision must fail validation; do not select one
  arbitrarily or publish a partial result.
- D3: Include only the target effective period. Produce A / 2026-09 / 12.00 and
  B / 2026-09 / 3.00. The aggregate control total is 15.00 EUR.
- D4: Replaying the same source snapshot and configuration must produce the same
  rows and total without adding duplicates to the sandbox artifact.
- D5: Retain lineage from each result row to contributing event IDs and revisions.
  Document why superseded, cancelled, duplicated and late records were excluded.
- D6: Record the code revision, input fixture digest, run configuration, target
  artifact identity and observed reconciliation result.
- D7: Execute only in the provisioned solution sandbox or against this local
  synthetic fixture. Production deployment, backfill and consumer cutover are
  separate work requiring their own authority.

## Acceptance boundary

The outcome includes an executed sandbox result and reconciliation evidence.
Passing static SQL or schema checks alone is insufficient. An independent
reviewer derives expected results from this contract and the fixture, not by
reusing the transformation being evaluated as its own control calculation.
