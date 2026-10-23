# Delivery workflows

Sovite can validate a delivery workflow, bind it to an installed set of grants,
and admit individual role jobs against that exact binding. Workflow definitions
are separate from `PlatformBundle`: editing a requirement, procedure or review
policy creates a new content revision without activating platform configuration.

The current implementation supplies definitions, immutable bindings, classified
role admission and approved output transfers. A workflow consumer still owns
the delivery loop: decomposing work, starting sessions, collecting evidence,
counting review failures, maintaining blockers and selecting a candidate. Sovite
does not yet run an unattended workflow scheduler or automatically create or
merge pull requests. A declared `completion.route: merge` records an intended
handoff; it grants no repository authority and causes no merge.

## Boundary decision (ADR decision 7)

The revised decision makes the effective required input class a property of each
binding, defaulting to the installation's most restrictive class. The owner bind
step and subsequent admission both enforce the selected routes. Lowering class
or crossing a private-to-public boundary requires an authorized release of exact
output content, including repository candidates and Vuoro findings. Review model
diversity is sought within the permitted boundary. Class-aware result contracts
provide available content to cleared callers and a scoped reader path when the
caller lacks content clearance; the reader still needs separate authorization.
Legacy receipt-only `reference` contracts remain a compatibility option, rather
than defining the general MCP result behavior.

## Start with a complete example

The examples use synthetic C0 requirements and the installed grants in
[`config/platform.delivery.yaml`](../config/platform.delivery.yaml).

| Definition | Outcome and inputs |
| --- | --- |
| [`software-delivery.yaml`](../config/workflows/software-delivery.yaml) | Finite planning, requirements-driven implementation, independent inline and holistic review, and human review handoff. The fixture specifies label normalization. |
| [`data-engineering.yaml`](../config/workflows/data-engineering.yaml) | The same delivery structure with pinned data procedures and evidence for semantics, sandbox execution, reconciliation and lineage. |
| [`one-shot.yaml`](../config/workflows/one-shot.yaml) | One implementer session returning a bounded documentation correction. No planner, decomposition or reviewer sessions. |

Validate a definition without selecting any installed capability:

```sh
sovite workflow validate config/workflows/software-delivery.yaml
```

Compile a complete offline binding:

```sh
sovite workflow compile config/workflows/software-delivery.yaml \
  --bundle config/platform.delivery.yaml \
  --installation-id example-delivery-installation \
  --requirements-ref config/workflows/inputs/software-requirements.ref.json \
  --session-policy-ref config/workflows/inputs/session-policy.ref.json \
  --input-class C0 \
  --output /tmp/software-delivery.binding.json
```

For the data example, select `data-engineering.yaml` and
`inputs/data-requirements.ref.json`. For the single-session example, select
`one-shot.yaml` and `inputs/one-shot-requirements.ref.json`. Both use the same
session-policy reference. The fixtures demonstrate authoring and binding;
actual execution also needs a suitable project workspace, provider credentials
and any required sandbox access.

`--input-class C0` applies only to the supplied synthetic context. Omitting the
class selects the highest-ranked class in the entire installed bundle. A set of
public-only C0 grants therefore cannot silently turn an unclassified task into
a public task. The installation identifier distinguishes deployments even when
they use identical bundle bytes.

Offline compilation creates a preview and performs no network calls, activation
or dispatch. It cannot by itself be submitted as an authorized workflow. The
author bind API must durably record the exact binding under the installation's
current generation before a role job can use it. In a
hosted installation, `SOVITE_HOSTING_ENVELOPE` also participates in bundle
compilation and the installation identifier must agree with the envelope. Use
the running installation's bind API for an authoritative active-generation
snapshot.

## Define the delivery contract

The schema is
[`delivery-workflow.schema.json`](../config/delivery-workflow.schema.json).
Unknown fields, duplicate YAML keys, unresolved roles and omitted coordinated
transitions fail validation.

The external planner has a named role, required input names, a finite handoff
and the `requirements-release` output contract. In the examples it receives
`intent`, `constraints` and `decision_policy`, and may use the discovery role.
The planner and discovery grants are invoked through ordinary Sovite jobs before
delivery binding. Their jobs still need classification and normal admission.
The workflow role-job endpoint excludes these authoring roles. A consumer stops
the planning session when an accepted requirements release and applicable
session policy exist, then binds their owner references.

Delivery roles are conventional and explicit:

| Role | Installed grant in the example | Declared responsibility |
| --- | --- | --- |
| `orchestrator` | `delivery-coordinate` | Divide requirements into coherent sections with explicit dependencies; arrange sessions and candidates. |
| `implementer` | `delivery-implement` | Fulfil the requirements using implementation choices within the session policy. |
| `inline_reviewer` | `delivery-inline-review` | Derive acceptance criteria before inspecting the candidate and assess outcomes independently. |
| `holistic_reviewer` | `delivery-holistic-review` | Assess the assembled candidate's intention, integration and general quality. |

Coordinated definitions include implementation, inline review, reimplementation,
inline outcome, holistic review and holistic outcome transfers. They also state
that an unresolved product decision pauses affected work while unrelated work
continues. `product_decisions.target` chooses interactive resolution or decision
records; `escalation.target` chooses the block queue or interactive orchestrator.
The consumer implements these queues and session transitions.

The example escalates after two reviews with substantive failures, counting the
initial review when it fails. Nits become nonblocking from review pass two.
These are distinct policies: a failed acceptance requirement cannot become a
nit because a retry budget is exhausted. The compiler validates the policy;
the consumer must persist the review counts and enforce it.

The implementer and both reviewers require separate Agent definitions. Actual
review sessions must have independent contexts and instructions. Optional
`review.model_diversity: required` also requires each reviewer to use a different
configured model selection from the implementer. Both reviewers may share a
second model: two admitted private models can provide this arrangement.
Equal immutable-weight digests count as the same model across adapters. When
either route lacks a digest, equal explicit model names also count as the same
selection across adapters; differing subscription model names do not attest
different immutable weights.
`preferred` permits the same model and never changes a route or relaxes a data
boundary. Model diversity alone does not establish independent review.

## Keep provider routing in the installed bundle

Workflow roles select grants. The bundle resolves each grant to its Agent,
Harness, Model, Compute and destination. A workflow does not choose a provider
endpoint, inject credentials or create permission to use a model.

The delivery bundle demonstrates native Codex and Claude subscription routes and
an OpenCode Go route. These are maintained native CLI sessions, separate from
the `openai-compatible` direct API adapter. The same workflow structure can bind
private model grants. Provider login, available model identifiers, workspace
capabilities and actual execution readiness belong to the deployment; passing
workflow validation cannot establish them.

The concrete selections are `gpt-6.1-sol`, `claude-sonnet-5` and
`deepseek-v4.1-flash`. Codex and Claude document the first two selections in their
[Codex model guide](https://developers.openai.com/codex/models) and
[Claude Code model release notes](https://code.claude.com/docs/en/whats-new/2026-w27).
Availability still depends on the authenticated account and installed CLI.
No provider account smoke test is implied by these examples.

The supplied subscription launchers are answer-only: native tools and implicit
integrations are disabled. They can return candidate implementation text and
assess supplied evidence. Applying changes, executing checks or running a data
platform sandbox needs a separately authorized executor or tool integration.
The example implementer is instructed to report those absent capabilities as
blockers; it cannot satisfy executed-evidence obligations by producing text.

This development bundle declares six fixed native services at loopback ports
8091 through 8096. Each needs its own `SOVITE_HARNESS_AGENT_ID` matching the
declared Agent, its selected bundle, service bearer token and durable ledger.
Credential references resolve in that service: `SOVITE_CODEX_AUTH_FILE`,
`SOVITE_CLAUDE_OAUTH_TOKEN`, or `SOVITE_OPENCODE_GO_KEY`. Service transport tokens
are separate from these account credentials. The bundle does not deploy the
services, provision credentials or supply verified harness image digests for
enforced mode.

Public versus private is an installed destination property, independent of the
provider name. `boundary` can be explicit; otherwise a `cloud` destination is
public and a `local` or `private` destination is private. At or above the bundle's
`restricted_class` threshold, model context must stay within a private boundary.
If no threshold is specified, the highest class is restricted. Declared output
and tool context also participate in model admission.

## Bind requirements, policy and evidence

Each immutable owner reference contains `owner`, `resource_id`, `revision` and
`content_digest`. The example `.ref.json` files hash the exact neighboring UTF-8
content bytes. Their owner is `example-workspace`; they are fixtures, not evidence
of an actual owner's approval. Replace them with accepted owner references for
real work. Sovite validates reference structure and pins it without fetching or
approving the underlying content.

A `DeliveryWorkflowBinding` contains the full validated definition, its
`workflow_revision`, installation identity, bundle generation, run input class,
resolved roles, compiled transfers, and the requirements and session-policy
references. Its `dispatch_authorized` field is always `false`. The binding digest
covers the complete envelope except the digest field itself, using UTF-8 JSON
with sorted keys, compact separators, preserved Unicode and finite JSON values.
The workflow revision uses that same encoding for the definition alone.

Domain procedures are content-pinned references. Evidence obligations identify
the stage and the required observation; they do not manufacture observations or
execute checks during compilation. Changing a procedure reference changes the
workflow revision. Changing a requirements or session-policy reference changes
the binding while preserving the unchanged definition's revision.

## Cross boundaries with explicit output releases

Every role, including authoring roles, defaults to the run input class. A
planner that works separately on public inputs before restricted delivery may
explicitly select `input_class: C0`; this does not permit it to read the later
restricted requirements, and it remains excluded from delivery dispatch.
A downstream delivery role may specify a lower
`input_class` only when its incoming transfers have suitable installed output
contracts. The entry orchestrator, or the direct implementer, must accept the
original run class. Private-to-public transfers require an output contract even
when their class labels match. The compiler checks source ceilings, target
classes and allowed destinations, then pins each installed contract revision.

The examples declare two external edges: the orchestrator's repository candidate
uses `publish-candidate` and the holistic reviewer's Vuoro finding uses
`publish-finding`. Both point to the installed public C0 destinations. The pinned
[`output-release` procedure](../config/workflows/procedures/output-release.md)
requires assessment of the complete proposed content, including acceptance
context, evidence and references. These edges describe permitted publication
routes; a repository or Vuoro publishing adapter is still a consumer concern.

A declaration does not itself declassify anything. An identity explicitly listed
in the output contract's `approvers` records approval of exact output content,
with the completed source job, source digest, active generation, contract
revision and immutable verification evidence. Operators also need an explicit
approver entry. Approval is idempotent for the exact source, contract, content,
evidence and actor; changing those inputs creates a different release identity.

A release-gated role must use a grant with `workflow_only: true`; binding fails
otherwise. Raw job submission cannot use such a grant to bypass the graph.
The delivery example marks all four delivery grants workflow-only. Ordinary
planner/discovery jobs remain available, and their authenticated caller is
responsible for truthful explicit input labels within installed authority.

To invoke a release-gated role, submit the binding, role, matching `transfer_id`
and `source_release`, with an empty `task`. Sovite supplies exactly the approved
content. It does not append the original requirements, policy references, full
binding or another free-form prompt to the released context. Include all
permitted acceptance context in the approved content itself. Source authority
and the exact transfer are checked again at claim, renewal and finish; revoking
a source after enqueue can block downstream execution or disclosure.

## API and runtime responsibilities

Set `SOVITE_INSTALLATION_ID` for a standalone installation. Hosted installations
take their identity from the owner envelope; a conflicting setting is rejected.

| Endpoint | Request and authority |
| --- | --- |
| `POST /api/workflows/validate` | `{workflow}`; workflow author access, or a standalone operator. Returns definition validity and revision. |
| `POST /api/workflows/bind` | `{workflow, requirements_ref, session_policy_ref, expected_generation, input_class?}`; same author access. Resolves the active installation, durably records the exact binding under its digest, and returns it. |
| `POST /api/workflows/jobs` | `{binding, role, task, idempotency_key, source_release?, transfer_id?}`; operator or admitted caller. Requires the exact owner-recorded binding, recompiles against active configuration, and applies current grant authority, class, qualification and inhibits. |
| `POST /api/output-releases` | `{source_job_id, source_digest, contract_id, expected_contract_revision, expected_generation, content, evidence_refs}`; an explicitly authorized contract approver. |
| `GET /api/output-releases/{release_id}` | Authorized contract approver or operator; verifies the current source, contract and stored content before returning the release. |

Use a stable idempotency key for one intended role invocation. A successful job
means its execution completed under Sovite's controls; acceptance still depends
on the requirements and review evidence. A consumer must resolve uncertain job
state before starting another invocation or repeating an external effect.

An offline binding with a valid digest is still rejected as `workflow_not_bound`
until an authorized author records it. A caller therefore cannot bypass a
release-gated graph by constructing a different direct C0 workflow around the
same public grant. Recording a binding is an owner admission step, distinct from
the caller's per-job execution authority and any required output release.

Return contracts also remain installed policy. Legacy `reference` grants return
receipt status. With `class-aware` grants, a caller with sufficient configured
clearance can receive verified content; otherwise it receives a receipt and a
scoped reader description. `read_result` requires explicit `result_readers`
membership for that grant. A receipt or reader description does not grant
access. Current authority and artifact integrity are checked at disclosure.

Activating a different PlatformBundle generation supersedes old bindings and
output releases. Rebind against the current generation before further role
dispatch; a valid content hash does not make an old installation snapshot
current. Sovite already handles job cancellation and release checks on bundle
supersession. There is no separate workflow activation that starts a scheduler.

## Data engineering outcome

The data fixture's grain is account and month, with a September 2026 effective
period and an inclusive processing cutoff of `2026-10-03T00:00:00Z`. Latest
visible revisions, cancellations, exact duplicates and late arrivals have
explicit semantics. Expected sandbox output is A / 2026-09 / 12.00 EUR and
B / 2026-09 / 3.00 EUR, with a 15.00 EUR control total.

The pinned data procedure asks the implementation session to produce identified
artifacts and executed sandbox evidence. Inline review checks the assigned
artifact's semantics and independent reconciliation. Holistic review checks
assembled populations, join multiplicity, cutoffs, compatibility and aggregate
controls. Execution lineage records input and code identities; semantic lineage
explains fields, joins, exclusions and approved business decisions.

The fixture ends at a reconciled sandbox outcome and reviewable candidate.
Merging code, deploying an artifact, running a production backfill and accepting
a business data outcome have separate owners and evidence. A missing sandbox
capability or semantic decision is a blocker for affected work, rather than an
implied permission to substitute production execution.
