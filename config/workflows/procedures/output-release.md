# Output release procedure

An output contract declares an allowed transformation and destination. It does
not certify any model-generated text or automatically lower its classification.
Apply this procedure before an authorized approver releases exact output bytes.

## Bind the source and purpose

Identify the completed source job, its configuration generation and actual
output class. Identify the selected output contract and its pinned revision,
the intended destination and the exact content proposed for transfer. Verify
that the approver is authorized by the contract and that the current source
access and release conditions still hold.

## Assess the complete permitted context

For a repository candidate, inspect the diff, filenames, comments, examples and
supporting text. Confirm that the candidate contains only information allowed at
the destination and that the included acceptance context preserves the meaning
needed to review it. Remove or transform restricted source records, credentials,
internal identifiers and incidental implementation transcript content as
required by the contract.

For a review request, assess the requirements and candidate together. A sanitized
diff accompanied by the original restricted requirements is not a sanitized
review request. The recipient must receive only the approved context; it must
not automatically dereference a protected binding or private source reference.

For a Vuoro finding, assess the finding text, evidence descriptions and lineage
references. The released finding must distinguish observed results, unresolved
questions and owner decisions. References must not provide unintended access
to restricted artifacts or encode private data in their identifiers.

## Record and release exact content

Record the transformation performed and the evidence supporting the selected
output class. The authorized approver releases the exact content digest under
the selected contract, not an editable draft or an unrestricted future stream.
A changed source, contract, destination or output body requires a new review
under the applicable authority.

The receiving role consumes the released bytes as its complete task context.
Do not append original requirements, a full workflow binding, an implementation
transcript or an extra free-form prompt to a release meant to constrain context.

An output release is separate from accepting the work, approving a pull
request, publishing a repository candidate, or accepting a data outcome.
Those actions retain their own owner and execution controls.
