# Pricing Workflow State Machine

## Responsibility Split

Linux performs pricing, validation, orchestration, durable state changes, leases, and audit traces. Windows performs GSP API actions because it has network access and a local GSP login. A Windows process never decides the next workflow state by itself; it reports evidence and the Linux state machine applies the transition.

```text
pricing -> validating -> submitting -> verifying -> under_approval
                                  \                 -> approved
                                   \                -> rejected
                                    -> manual_review
```

`verifying` is not a restart from the beginning. It is a persisted state with a different claimable action: `verify_gsp_submission`. An expired `submit_gsp` lease is forcibly changed to `verifying`, so another worker cannot immediately repeat the write.

## Backend API

```text
POST /api/agent/pricing-task
GET  /api/agent/pricing-workflows
POST /api/agent/pricing-workflows/claim
POST /api/agent/pricing-workflows/{task_id}/report
POST /api/agent/pricing-workflows/{task_id}/retry
GET  /api/agent/tasks/{task_id}
```

Create requests should supply a stable `idempotency_key`. The server derives one `task_id` and one `effect_key` from it. Retrying the same request with the same key returns the original task without rerunning pricing.

An unauthenticated create request is pricing-only: after validation it stops in `manual_review`. Entering `submitting` requires `submission_authorized=true` plus the Agent token, or a token-protected manual retry. Public list/task responses replace `gsp_payload` with a presence flag; only an authenticated Windows claim receives the templates.

Each claimed action has a `lease_id`, worker owner, and expiry. Reports also have a `report_id`; replaying a report returns the original response. Only one worker can hold a task lease at a time.

## Unknown Submit Result

```text
submit_gsp
  -> confirmed PLA/workflow started: under_approval
  -> timeout/connection loss: verifying

verifying
  -> matching submitted application found: under_approval
  -> confirmed absent: bounded safe retry through submitting
  -> matching incomplete application + local stage ledger: resume remaining stages only
  -> inconclusive or unsafe to resume: manual_review
```

The correlation marker is `DPA:{task_id}`. The Windows worker appends it to the application description and comment before the first create call. Verification scans GSP list/detail records for that exact marker.

## Windows Stage Ledger

`pricing_workflow_ledger.local.json` is ignored by Git and contains no GSP credentials. It records only the effect key, PLA/workflow identifiers, completed stages, and pending backend result reports.

The stages are:

1. `application_created`
2. `application_saved`
3. `workflow_created`
4. `workflow_started`

If the process stops after stage 1, the next run reuses the recorded PLA and starts at stage 2. An incomplete GSP application without this local proof is sent to manual review instead of being edited speculatively.

## Guarded GSP Write Contract

The deployed GSP frontend exposes these four separate side-effect endpoints:

```text
/dahua-b-pricing/priceListApplication/insertPriceListApplication
/dahua-b-pricing/priceListApplication/saveApplicationAndProduct
/dahua-b-pricing/priceWorkFlow/createPriceWork
/dahua-b-pricing/priceWorkFlow/startPriceWork
```

Endpoint names alone do not prove the payload schema. The worker therefore requires all four payload objects in `gsp_payload` before making the first write. It supports only these placeholders:

```text
{{effect_key}}
{{pla_no}}
{{workflow_id}}
```

Missing templates fail before any write. `workflow_enabled=true` only starts polling. `gsp_submission_enabled=true` is the separate switch that lets the worker advertise and claim `submit_gsp`.

## Rollout Checklist

1. Keep both workflow switches false and run unit tests.
2. Set `workflow_enabled=true`, keep `gsp_submission_enabled=false`, and verify claim/report connectivity.
3. Capture and review one exact successful request payload for each of the four GSP endpoints in a controlled test account.
4. Put those payload templates in the backend create request; never commit credentials or business secrets.
5. Authorize one controlled idempotency key with the Agent token, enable submission, and monitor the server trace plus Windows ledger.
6. Test a forced transport timeout and confirm the server enters `verifying` without creating a second PLA.
