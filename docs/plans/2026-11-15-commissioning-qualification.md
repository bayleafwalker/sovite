# Change-bound commissioning qualification

Status: implemented source candidate; independent review and final verification
recorded in the PR. No release, deployment, signing or production evidence
conversion is established by this plan.

## Problem and decision

The legacy qualification issuer sets expiry relative to signing time. Re-signing
unchanged commissioning evidence therefore extends admission without a fresh
observation. `qualify` performs no deployment checks. Keep signatures and current
binding/authority checks, but make commissioning applicability explicit instead
of repeatedly resetting a calendar timer.

Sovite owns this behavior. Vuoro Cloud composes the released owner and must not
fork its verifier. The appservice health task owns deployment coordination. Live
served work checks on 2026-11-15 found no active Sovite items or reservations and
no overlapping open Sovite PR; Cloud's stale reservation on item2727 concerns
the operator-actions workflow, not qualification.

## Contract

- Add opt-in `--until-change`, mutually exclusive with `--valid-days`. Emit signed
  `sovite.qualification/v3`, `validity: until-change`, `expires_at: 0`. The
  unchanged numeric database schema stores zero as a V3-only sentinel. Old
  verifiers reject it. Preserve V1/V2 time bounds without automatic conversion.
- Require `sovite.commissioning/v1` deployment evidence, exact generation/profile
  and runtime binding, nonempty scoped limitations, and unique required passing
  role checks. Every check has `observed_at`, `evidence_ref`, `evidence_sha256`
  and explicit `valid_until`: an absolute timestamp or `null` for a fact whose
  applicability lasts until a bound configuration/runtime change. Reference and
  hash record provenance; they cannot prove truthful observation or deployment.
- Reject missing/invalid dates, future observations, expired deadlines and
  incomplete roles. Re-signing preserves every observation deadline. Time-bound
  evidence cannot be passed through the legacy signing option to refresh it.
  A null deadline is an explicit issuer applicability judgement, never a default
  for unknown freshness. Real time-sensitive facts need real deadlines and fresh
  observations, including recovery/model/provider/network facts where relevant.
- Retain signature validation, issuer validity/revocation, role/workspace scope,
  generation/runtime/envelope matching and current grants/inhibits. Hosted
  platform and tenant signatures remain independently mandatory. Import/signing
  never clears recovery inhibits or resolves uncertain execution effects.
- Select the record with the longest usable effective lifetime; a hosted pair
  reports its earlier role deadline. Bound inference permits, native execution
  profiles and helper projections by actual evidence/issuer deadlines and their
  existing independent lease/accounting/host-class limits. Never project zero
  as an epoch execution deadline or unlimited permission.
- Expose effective deadline in API/console and metrics. An untimed current V3
  record is labeled as change-bound. `sovite_qualification_time_bounded` gates
  approaching-expiry alerts; `sovite_qualified` remains admission status. Query
  only unexpired legacy/zero-sentinel candidates on the authorization hot path.

## Scope and acceptance

Owner files: qualification, CLI, API, console and Kubernetes signing helper;
existing permit/native-profile/helper-projection consumers; corresponding tests,
security and operations docs. No Cloud authority logic or appservice files.

Verification uses synthetic fixtures only: reproduce legacy signing-relative
expiry, preserve legacy import/read behavior, verify V3 online/offline CLI and
roundtrip, reject incomplete/malformed/expired/future evidence, preserve actual
deadlines on re-sign, and exercise revocation, issuer expiry, role/workspace and
binding changes. Check both hosted roles and earliest deadline, admission versus
execution expiry, model permit renewals, native profiles, helper projections,
recovery inhibits and metrics. Load the base00d29fa verifier to demonstrate it
rejects V3 while accepting V2 controls. Run focused tests, Helm structural
validation, console syntax/label checks and the repository CI suite; require an
independent findings-first review and repairs before landing.

No distributed protocol redesign is required: these are example-tested
qualification-time and authority boundaries, not a proof of live isolation,
production task competence or recoverability.

## Rollout and recovery

Publish a released Sovite artifact, then install matching API/worker/verifier and
signing-client versions. Update expiry alerts to require `time_bounded == 1`.
Re-evaluate retained commissioning evidence with actual observation/provenance
and freshness metadata, collect missing observations, and sign/import once
under existing operational authority. Observe the current binding, qualifications,
actual deadlines, workers and inhibits before retiring calendar renewal. An
expired observation requires a new observation; no periodic signer is added.

Old runtimes reject V3. During mixed-version deployment/rollback retain suitable
unexpired legacy evidence for that exact binding or hold admission. Source
acceptance does not publish an image or authorize release/deployment/signing.

Out of scope: live credentials or private appservice inputs, live qualification,
cluster mutation, Vuoro tenant upgrades, remote worker journal recovery and
promotion. No unresolved product decision remains; issuing owners decide
truthful evidence, appropriate scope and actual observation deadlines.
