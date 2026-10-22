# Data engineering procedure

This procedure specializes delivery requirements for data artifacts. Its
content reference pins these instructions; selecting the procedure neither
opens a data source nor authorizes a query or deployment.

## Establish meaning before implementation

Identify the output grain, source population, keys, measures, units, null
semantics, exclusions and consumer expectations. State the relevant effective
time, processing cutoff and correction behavior. Distinguish an observed source
pattern from an approved business rule: repeated identifiers do not by
themselves establish whether to retain history or select the latest revision.

Record material semantic decisions against the requirements release. If a
decision is missing, pause the affected artifact and continue only work that
does not depend on that decision.

## Execute in the solution sandbox

Use the authorized source snapshot or extraction basis and a provisioned output
namespace. Record the code revision, input identities, reference-data versions,
parameters, execution environment and artifact identities. The permitted model
route must be able to receive the actual input and tool-result classifications.
A missing sandbox capability is a blocker, not permission to use production.

Run the transformation on the agreed representative inputs. Exercise correction
and replay behavior where they are part of the requirement. Record unexpected
duplicates, invalid keys, missing mappings and rejected records explicitly.
If a source cannot be frozen, state the weaker reproduction basis and obtain
the required owner decision before treating the evidence as sufficient.

## Reconcile independently

Derive expected populations and totals from the requirements and independent
controls. Compare counts, totals and relevant distributions at meaningful
boundaries: selected source population, accepted transformations and output
artifact. Explain exclusions and tolerances. Do not derive the expected result
by calling the same transformation logic under review.

An inline reviewer examines the assigned artifact's semantics and executed
evidence. A holistic reviewer examines how the assembled artifacts interact:
join multiplicity, overlapping populations, inconsistent cutoffs, schema
compatibility, consumer behavior and aggregate control totals. Passing each
source contribution separately does not prove that their union is correct.

## Capture lineage and operational handoff

Capture execution lineage from exact input and code versions to each output.
Capture semantic lineage for affected fields: source attributes, transformations,
joins, exclusions, calculation definitions and the decisions that explain them.
Keep the scope proportionate to the change while preserving enough detail to
explain a reported value.

Supply deployment order, rollback or correction behavior and post-deployment
checks when deployment is in scope. Distinguish a reviewable code candidate,
an authorized deployment and an accepted data outcome. The synthetic example
ends at a reconciled sandbox artifact and reviewable candidate.

## Transfer only the permitted context

Keep classified source data and findings within their admitted boundary. For a
public review or external publication, use an installed output contract to
prepare a complete permitted context, including the needed requirements and
candidate evidence. An authorized approver must release its exact bytes with
the required evidence. Merely changing an `input_class` field or replacing data
with a pointer does not establish that the receiving agent may read it.
