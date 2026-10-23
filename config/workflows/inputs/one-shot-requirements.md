# Example requirements: a bounded documentation correction

This is a synthetic C0 one-session fixture, not an instruction to edit Sovite's
own documentation during workflow validation.

In a supplied example project's README, replace the single outdated heading
`Instalation` with `Installation`. Preserve every other byte of the document.
Return the candidate diff and confirm that exactly one occurrence changed.

The work needs no discovery, decomposition or separate review session. If the
heading occurs zero times or more than once, report the discrepancy rather than
guessing which occurrence the requirement means. Publication is outside this
outcome.
