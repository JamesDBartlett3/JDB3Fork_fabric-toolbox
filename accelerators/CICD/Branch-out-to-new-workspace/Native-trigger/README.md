# Native branch-out post-activity variant

This variant is additive and does **not** replace the existing accelerator flow.

- Existing flow remains available: `AzDO/Branch_out_workspace.yml` + `BranchOut-Feature-Workspace-Automation.py`.
- This variant adds an alternative trigger path for organizations that use Fabric's native **Branch out to new workspace** UI capability and only want downstream post-activity automation.

## What this variant does

1. Receives branch-created event context from Azure DevOps service hooks (or equivalent event source).
2. Correlates that event with Fabric workspaces connected to the same repo/branch.
3. Resolves:
   - source workspace,
   - target workspace,
   - git branch/repository context.
4. Invokes the existing post-activity notebook flow automatically.
5. Applies configurable options:
   - `copy_lakehouse_data`
   - `create_lakehouse_shortcuts`
   - `copy_warehouse_data`
   - `connections_from_to`
   - `has_wh_views_on_lh`
6. Writes an idempotency ledger so already-successful events are skipped on retries.

## Files

- `AzDO/Native_branch_out_post_activity.yml`
  - Azure DevOps pipeline for processing native branch-out events.
- `AzDO/scripts/DetectAndRunNativeBranchOutPostActivity.py`
  - Event processor, Fabric correlation, source/target resolution, post-activity invocation, and event ledger handling.

## Trigger options

### Primary (recommended): Azure DevOps service hook

Configure a service hook on **Git branch created** events and pass payload/fields to this pipeline. The YAML includes an incoming-webhook resource so the event-driven path is wired in Azure DevOps rather than relying on a manual queue.

- For direct payload usage, provide `event_payload_path` to JSON containing the service hook event body.
- For systems that map fields directly to pipeline parameters, pass:
  - `ado_org_name`
  - `ado_project_name`
  - `ado_repo_name`
  - `ado_new_branch`
  - `ado_new_branch_object_id`
  - `event_time`

The event bridge should write the payload to a secure queue or pass the mapped values through Azure DevOps service hook parameters; do not rely on a local temporary path for the ledger or queueing state.

### Fallback: manual run

You can run the pipeline manually by supplying the same event fields above.

## Idempotency and concurrency

- The script computes a deterministic event key from org/project/repo/branch/target-workspace.
- Event status is persisted to a ledger file (`ledger_file_path`).
- If an event is already marked `succeeded`, the script exits without re-running post-activity.
- `ledger_file_path` must point to shared persistent storage accessible by the runner; an empty or ephemeral local path is rejected.
- When multiple runs race, the ledger uses an atomic claim and stale `running` lease policy so a crashed worker does not block retries indefinitely.

## Configuration defaults and overrides

Pipeline defaults map directly to notebook parameters.

Optional source-workspace-specific overrides are supported through:

- `source_workspace_overrides_json`

Example:

```json
{
  "Dev Workspace": {
    "copy_lakehouse_data": "True",
    "create_lakehouse_shortcuts": "False",
    "copy_warehouse_data": "True",
    "connections_from_to": "(sourceConn,targetConn)",
    "has_wh_views_on_lh": "False"
  }
}
```

## Permissions

This variant does not create workspaces or branches.

It is designed to require only:
- read/access needed to inspect branch and workspace git metadata,
- permissions needed to invoke the post-activity notebook,
- viewer-or-higher on relevant source workspace where required by post-activity logic.

## Logging and monitoring

- Structured logs include a correlation ID per event.
- Event ledger keeps status (`running`, `failed`, `succeeded`) and error details.
- Notebook execution is polled and status is logged for troubleshooting.
