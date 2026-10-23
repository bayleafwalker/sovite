# Example requirements: label normalization

This is a synthetic C0 authoring fixture. It is not approval to modify a live
application. Its reference identifies these exact bytes so that the workflow
compiler can demonstrate a requirements binding without a remote content owner.

## Outcome

Provide a pure `normalize_label(value)` operation in the target example project.
Consumers receive a predictable display label without introducing persistence,
network access or configuration changes.

## Requirements

- R1: Accept a string and return a string. Reject other input types with the
  target language's normal invalid-type error.
- R2: Remove leading and trailing ASCII spaces (U+0020), and replace each run of
  internal ASCII spaces with one ASCII space. Preserve all other characters.
- R3: Reject an empty result with an explicit invalid-value error.
- R4: Normalizing an already normalized label returns the same value.
- R5: Follow the project's existing public API and error conventions. If those
  conventions contradict R1 or R3, request a product decision before changing
  either requirement.
- R6: Supply the implementation, focused acceptance checks, and a concise
  description of the resulting behavior. Produce a reviewable candidate.

## Acceptance examples

| Input | Expected result |
| --- | --- |
| `  alpha   beta  ` | `alpha beta` |
| `alpha` | `alpha` |
| An empty string | Invalid-value error |
| A string containing only ASCII spaces | Invalid-value error |
| A number | Invalid-type error |
| A tab between `alpha` and `beta` | Preserve the tab |

The implementation agent chooses its internal code structure. These requirements
do not prescribe task-by-task edits or authorize publishing a release.
