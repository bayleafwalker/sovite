# Bounded patch draft v1

This is a model-output contract for one answer-only call. It contains full text
replacements for files supplied in a small source context, each bound to the
SHA-256 of its exact base contents. It contains no shell commands, patches,
renames, deletions, binary file support, test commands, or merge authority.

The owner admits only unique paths present in the context and within its
resolver-issued allowed areas. It rejects `.git` and owner-declared immutable
or oracle paths, symlinked or special files, changed base preconditions, and
oversized context/output. It applies replacements with no-follow file access,
then uses fixed local Git operations to create one direct child commit and
records the existing immutable candidate receipt. The model cannot choose a
repository, path root, command, remote, credential, or branch.

The profile stays disabled unless the composed adapter supports a provider-side
hard cost ceiling and returns a trusted actual-usage receipt. The current
`AdapterRunner` is answer-only but does not supply either guarantee. If a call
can have started and its usage is missing or ambiguous, the owner records the
usage as unknown, retains the reservation, and blocks the attempt. A local
reservation alone does not cap a provider charge.
