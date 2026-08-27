import argparse
import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple

import msal
import requests

try:
    import fcntl  # Linux agents
except ImportError:  # pragma: no cover
    fcntl = None

FABRIC_API_URL = "https://api.fabric.microsoft.com/v1"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - corr=%(correlation_id)s - %(message)s",
)


class CorrelationAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.get("extra", {})
        kwargs["extra"] = {"correlation_id": self.extra["correlation_id"], **extra}
        return msg, kwargs


class EventLedger:
    def __init__(self, path: str):
        self.path = path
        self.lock_path = f"{path}.lock"
        self._lock_handle = None
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)

    def _acquire_lock(self):
        self._lock_handle = open(self.lock_path, "a", encoding="utf-8")
        if fcntl:
            fcntl.flock(self._lock_handle, fcntl.LOCK_EX)

    def _release_lock(self):
        if self._lock_handle:
            if fcntl:
                fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    @staticmethod
    def _is_stale_running(record: Dict, max_age_seconds: int = 1800) -> bool:
        updated_at = record.get("updatedAtUtc")
        if not updated_at:
            return True
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        age_seconds = (datetime.now(timezone.utc) - updated).total_seconds()
        return age_seconds > max_age_seconds

    def _read(self) -> Dict[str, Dict]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Ledger file '{self.path}' is corrupted. Refusing to discard deduplication history."
                ) from exc

    def _write(self, data: Dict[str, Dict]):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        temp_path = f"{self.path}.tmp.{os.getpid()}.{time.time_ns()}"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, self.path)

    def get(self, event_key: str) -> Optional[Dict]:
        self._acquire_lock()
        try:
            data = self._read()
            return data.get(event_key)
        finally:
            self._release_lock()

    def claim(self, event_key: str, record: Dict, max_age_seconds: int = 1800) -> bool:
        self._acquire_lock()
        try:
            data = self._read()
            existing = data.get(event_key)
            if existing and existing.get("status") == "succeeded":
                return False
            if existing and existing.get("status") == "running" and not self._is_stale_running(existing, max_age_seconds):
                return False
            data[event_key] = record
            self._write(data)
            return True
        finally:
            self._release_lock()

    def upsert(self, event_key: str, record: Dict):
        self._acquire_lock()
        try:
            data = self._read()
            data[event_key] = record
            self._write(data)
        finally:
            self._release_lock()

    def claim(self, event_key: str, running_record: Dict, stale_running_seconds: int = 3600) -> Optional[Dict]:
        """Atomically check status and write 'running' under one lock.

        Returns None if the claim was granted (record written as 'running').
        Returns the existing record if the event was already succeeded or is
        actively running (within the stale threshold).  Stale 'running' records
        (older than *stale_running_seconds*) are treated as abandoned and the
        new claim is granted so that crash-interrupted events are retried.
        """
        self._acquire_lock()
        try:
            data = self._read()
            existing = data.get(event_key)
            if existing:
                status = existing.get("status")
                if status == "succeeded":
                    return existing
                if status == "running":
                    updated_at = existing.get("updatedAtUtc")
                    if updated_at:
                        try:
                            age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)).total_seconds()
                            if age < stale_running_seconds:
                                return existing
                        except (ValueError, TypeError):
                            pass
            data[event_key] = running_record
            self._write(data)
            return None
        finally:
            self._release_lock()


def acquire_fabric_token(tenant_id: str, client_id: str, user_name: str, password: str) -> Optional[str]:
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.PublicClientApplication(client_id, authority=authority)
    scopes = ["https://api.fabric.microsoft.com/.default"]
    result = app.acquire_token_by_username_password(user_name, password, scopes)
    return result.get("access_token")


def acquire_ado_token_or_pat(
    tenant_id: str,
    client_id: str,
    user_name: str,
    password: str,
    ado_pat_token: str,
) -> Tuple[str, str]:
    if ado_pat_token:
        encoded = base64.b64encode(f":{ado_pat_token}".encode("utf-8")).decode("utf-8")
        return "Basic", encoded

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.PublicClientApplication(client_id, authority=authority)
    scopes = ["499b84ac-1321-427f-aa17-267ca6975798/.default"]
    result = app.acquire_token_by_username_password(user_name, password, scopes)
    token = result.get("access_token")
    if not token:
        raise ValueError(f"Could not acquire Azure DevOps token: {result}")
    return "Bearer", token


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


def parse_event_payload(path: str) -> Dict[str, str]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    resource = payload.get("resource", {})
    repo = resource.get("repository", {})
    ref_updates = resource.get("refUpdates") or []
    first_ref = ref_updates[0] if ref_updates else {}

    branch_ref = first_ref.get("name", "")
    branch_name = branch_ref.replace("refs/heads/", "")

    account_base_url = payload.get("resourceContainers", {}).get("account", {}).get("baseUrl", "")
    org_name = ""
    if account_base_url:
        parsed = urlparse(account_base_url)
        org_name = parsed.path.strip("/").split("/")[0]

    project_name = (
        repo.get("project", {}).get("name")
        or payload.get("resourceContainers", {}).get("project", {}).get("name", "")
        or payload.get("resourceContainers", {}).get("project", {}).get("id", "")
    )

    return {
        "ado_org_name": org_name,
        "ado_project_name": project_name,
        "ado_repo_name": repo.get("name", ""),
        "ado_repo_id": repo.get("id", ""),
        "ado_new_branch": branch_name,
        "ado_new_branch_object_id": first_ref.get("newObjectId", ""),
        "event_time": payload.get("createdDate", ""),
    }


def list_ado_branches(ado_api_url: str, org_name: str, project_name: str, repo_name: str, token_type: str, token: str) -> List[Dict]:
    headers = {"Authorization": f"{token_type} {token}"}
    url = f"{ado_api_url}/{org_name}/{project_name}/_apis/git/repositories/{repo_name}/refs?filter=heads/&api-version=7.1"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json().get("value", [])


def infer_source_branch(branches: List[Dict], new_branch: str, new_branch_object_id: str) -> Optional[str]:
    candidates = []
    for branch in branches:
        name = branch.get("name", "").replace("refs/heads/", "")
        object_id = branch.get("objectId", "")
        if name == new_branch:
            continue
        if object_id == new_branch_object_id:
            candidates.append(name)
    if len(candidates) == 1:
        return candidates[0]
    return None


def list_fabric_workspaces(token: str) -> List[Dict]:
    headers = {"Authorization": f"******"}
    url = f"{FABRIC_API_URL}/workspaces"
    all_items = []
    continuation = None

    while True:
        params = {"continuationToken": continuation} if continuation else None
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        all_items.extend(payload.get("value", []))
        continuation = payload.get("continuationToken")
        if not continuation:
            break

    return all_items


def get_workspace_git_connection(workspace_id: str, token: str) -> Optional[Dict]:
    headers = {"Authorization": f"******"}
    url = f"{FABRIC_API_URL}/workspaces/{workspace_id}/git/connection"
    response = requests.get(url, headers=headers, timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def correlate_workspaces(
    workspaces: List[Dict],
    token: str,
    repo_name: str,
    new_branch: str,
    source_branch: Optional[str],
    org_name: str = "",
    project_name: str = "",
    repo_id: str = "",
) -> Tuple[Optional[Dict], Optional[Dict], List[Dict]]:
    target_candidates = []
    source_candidates = []
    connected = []

    for ws in workspaces:
        ws_id = ws.get("id")
        if not ws_id:
            continue
        try:
            conn = get_workspace_git_connection(ws_id, token)
        except requests.RequestException:
            continue
        if not conn:
            continue

        details = conn.get("gitProviderDetails", {})
        if details.get("repositoryName") != repo_name:
            continue

        if org_name:
            details_org = str(details.get("organizationName") or "").lower()
            if details_org and details_org != org_name.lower():
                continue
        if project_name:
            details_project = str(details.get("projectName") or "").lower()
            if details_project and details_project != project_name.lower():
                continue
        if repo_id:
            details_repo_id = str(details.get("repositoryId") or "").lower()
            if details_repo_id and details_repo_id != str(repo_id).lower():
                continue

        branch_name = details.get("branchName")
        enriched = {
            "workspaceId": ws_id,
            "workspaceName": ws.get("displayName"),
            "branchName": branch_name,
            "gitProviderDetails": details,
        }
        connected.append(enriched)

        if branch_name == new_branch:
            target_candidates.append(enriched)
        if source_branch and branch_name == source_branch:
            source_candidates.append(enriched)

    if len(target_candidates) > 1:
        raise ValueError(
            f"Ambiguous target workspace correlation for repo '{repo_name}' and branch '{new_branch}': "
            + ", ".join(sorted(item["workspaceName"] or item["workspaceId"] for item in target_candidates))
        )
    if len(source_candidates) > 1:
        raise ValueError(
            f"Ambiguous source workspace correlation for repo '{repo_name}' and source branch '{source_branch}': "
            + ", ".join(sorted(item["workspaceName"] or item["workspaceId"] for item in source_candidates))
        )

    return (target_candidates[0] if target_candidates else None), (source_candidates[0] if source_candidates else None), connected


def build_event_key(org: str, project: str, repo: str, branch: str, target_workspace_id: str) -> str:
    raw = f"{org}|{project}|{repo}|{branch}|{target_workspace_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def resolve_options(
    source_workspace_name: str,
    defaults: Dict[str, str],
    overrides_json: str,
) -> Dict[str, str]:
    if not overrides_json:
        return defaults

    overrides = json.loads(overrides_json)
    for key, value in overrides.items():
        if key.lower() == source_workspace_name.lower():
            merged = defaults.copy()
            merged.update(value)
            return merged
    return defaults


def invoke_post_activity(
    token: str,
    notebook_workspace_id: str,
    notebook_id: str,
    source_workspace_name: str,
    target_workspace_name: str,
    options: Dict[str, str],
    correlation_logger: CorrelationAdapter,
) -> str:
    plurl = (
        f"{FABRIC_API_URL}/workspaces/{notebook_workspace_id}/items/{notebook_id}/jobs/instances"
        "?jobType=RunNotebook"
    )
    headers = {"Authorization": f"******", "Content-Type": "application/json"}

    payload = {
        "executionData": {
            "parameters": {
                "_inlineInstallationEnabled": {"value": "True", "type": "bool"},
                "source_ws": {"value": source_workspace_name, "type": "string"},
                "copy_lakehouse_data": {"value": options["copy_lakehouse_data"], "type": "bool"},
                "create_lakehouse_shortcuts": {"value": options["create_lakehouse_shortcuts"], "type": "bool"},
                "copy_warehouse_data": {"value": options["copy_warehouse_data"], "type": "bool"},
                "target_ws": {"value": target_workspace_name, "type": "string"},
                "connections_from_to": {"value": options["connections_from_to"], "type": "string"},
                "has_wh_views_on_lh": {"value": options["has_wh_views_on_lh"], "type": "bool"},
                "_runStandalone": {"value": "False", "type": "bool"},
            }
        }
    }

    correlation_logger.info("Invoking post-activity notebook")
    response = requests.post(plurl, headers=headers, json=payload, timeout=30)
    if response.status_code != 202:
        raise ValueError(
            f"Could not invoke notebook: status={response.status_code}, body={response.text}"
        )

    location_url = response.headers.get("Location")
    retry_after = int(response.headers.get("Retry-After", 10))
    if not location_url:
        raise ValueError("Fabric API did not return Location header for notebook job polling")

    while True:
        time.sleep(max(retry_after, 5))
        op_response = requests.get(location_url, headers=headers, timeout=30)
        op_response.raise_for_status()
        op = op_response.json()
        status = op.get("status")
        correlation_logger.info(f"Notebook operation status: {status}")
        if status in ["NotStarted", "Running"]:
            continue
        if status not in ["Succeeded", "Completed"]:
            failure = op.get("failureReason", {}).get("message", "Unknown failure")
            raise ValueError(f"Post-activity notebook ended with status {status}: {failure}")
        return status


def normalize_event_time(event_time_raw: str) -> str:
    if not event_time_raw:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.fromisoformat(event_time_raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ADO_API_URL", type=str, default="https://dev.azure.com")
    parser.add_argument("--ADO_ORG_NAME", type=str, default="")
    parser.add_argument("--ADO_PROJECT_NAME", type=str, default="")
    parser.add_argument("--ADO_REPO_NAME", type=str, default="")
    parser.add_argument("--ADO_REPO_ID", type=str, default="")
    parser.add_argument("--ADO_NEW_BRANCH", type=str, default="")
    parser.add_argument("--ADO_NEW_BRANCH_OBJECT_ID", type=str, default="")
    parser.add_argument("--EVENT_TIME", type=str, default="")
    parser.add_argument("--EVENT_PAYLOAD_PATH", type=str, default="")

    parser.add_argument("--TENANT_ID", type=str, required=True)
    parser.add_argument("--CLIENT_ID", type=str, required=True)
    parser.add_argument("--USER_NAME", type=str, required=True)
    parser.add_argument("--PASSWORD", type=str, required=True)
    parser.add_argument("--FABRIC_TOKEN", type=str, default="")
    parser.add_argument("--ADO_PAT_TOKEN", type=str, default="")

    parser.add_argument("--NOTEBOOK_WORKSPACE_ID", type=str, required=True)
    parser.add_argument("--NOTEBOOK_ID", type=str, required=True)

    parser.add_argument("--DEFAULT_COPY_LAKEHOUSE", type=str, default="False")
    parser.add_argument("--DEFAULT_CREATE_SHORTCUTS", type=str, default="True")
    parser.add_argument("--DEFAULT_COPY_WAREHOUSE", type=str, default="False")
    parser.add_argument("--DEFAULT_CONNECTIONS_FROM_TO", type=str, default="()")
    parser.add_argument("--DEFAULT_HAS_WH_VIEWS_ON_LH", type=str, default="False")
    parser.add_argument("--SOURCE_WORKSPACE_OVERRIDES_JSON", type=str, default="")

    parser.add_argument("--LEDGER_FILE_PATH", type=str, default="")

    args = parser.parse_args()
    if not args.LEDGER_FILE_PATH:
        raise ValueError(
            "A durable shared ledger path is required for idempotency; set --LEDGER_FILE_PATH to a persistent network or shared storage location."
        )

    event_details = {}
    if args.EVENT_PAYLOAD_PATH:
        event_details = parse_event_payload(args.EVENT_PAYLOAD_PATH)

    ado_org_name = args.ADO_ORG_NAME or event_details.get("ado_org_name", "")
    ado_project_name = args.ADO_PROJECT_NAME or event_details.get("ado_project_name", "")
    ado_repo_name = args.ADO_REPO_NAME or event_details.get("ado_repo_name", "")
    ado_repo_id = args.ADO_REPO_ID or event_details.get("ado_repo_id", "")
    ado_new_branch = args.ADO_NEW_BRANCH or event_details.get("ado_new_branch", "")
    ado_new_branch_object_id = args.ADO_NEW_BRANCH_OBJECT_ID or event_details.get("ado_new_branch_object_id", "")
    event_time = normalize_event_time(args.EVENT_TIME or event_details.get("event_time", ""))

    if not all([ado_org_name, ado_project_name, ado_repo_name, ado_new_branch]):
        raise ValueError(
            "Missing required event details. Supply --EVENT_PAYLOAD_PATH or explicit --ADO_ORG_NAME, "
            "--ADO_PROJECT_NAME, --ADO_REPO_NAME, --ADO_NEW_BRANCH."
        )

    correlation_seed = f"{ado_org_name}:{ado_project_name}:{ado_repo_name}:{ado_new_branch}:{event_time}"
    correlation_id = hashlib.sha256(correlation_seed.encode("utf-8")).hexdigest()[:12]
    log = CorrelationAdapter(logging.getLogger(__name__), {"correlation_id": correlation_id})

    log.info("Starting native branch-out event processing")

    fabric_token = args.FABRIC_TOKEN or acquire_fabric_token(
        args.TENANT_ID, args.CLIENT_ID, args.USER_NAME, args.PASSWORD
    )
    if not fabric_token:
        raise ValueError("Could not acquire Fabric token")

    token_type, ado_token = acquire_ado_token_or_pat(
        args.TENANT_ID, args.CLIENT_ID, args.USER_NAME, args.PASSWORD, args.ADO_PAT_TOKEN
    )

    branches = list_ado_branches(
        args.ADO_API_URL,
        ado_org_name,
        ado_project_name,
        ado_repo_name,
        token_type,
        ado_token,
    )

    source_branch = infer_source_branch(branches, ado_new_branch, ado_new_branch_object_id)
    log.info(f"Inferred source branch: {source_branch}")

    workspaces = list_fabric_workspaces(fabric_token)
    target_ws = None
    source_ws = None
    connected_candidates = []
    for attempt in range(1, 6):
        try:
            target_ws, source_ws, connected_candidates = correlate_workspaces(
                workspaces,
                fabric_token,
                ado_repo_name,
                ado_new_branch,
                source_branch,
                ado_org_name,
                ado_project_name,
                ado_repo_id,
            )
        except ValueError:
            raise
        if target_ws:
            break
        if attempt < 5:
            log.info(
                "Target workspace not yet visible in Fabric metadata; retrying correlation with backoff (%s/5)",
                attempt,
            )
            time.sleep(min(5 * attempt, 30))
            workspaces = list_fabric_workspaces(fabric_token)

    if not target_ws:
        raise ValueError(
            f"No Fabric workspace connected to repo '{ado_repo_name}' and branch '{ado_new_branch}' was found."
        )

    if not source_ws:
        if source_branch:
            raise ValueError(
                f"Could not resolve source workspace connected to source branch '{source_branch}'."
            )
        raise ValueError("Could not infer source branch, therefore source workspace could not be resolved.")

    event_key = build_event_key(
        ado_org_name,
        ado_project_name,
        ado_repo_name,
        ado_new_branch,
        target_ws["workspaceId"],
    )

    ledger = EventLedger(args.LEDGER_FILE_PATH)

    in_flight = {
        "status": "running",
        "eventTimeUtc": event_time,
        "correlationId": correlation_id,
        "sourceWorkspace": source_ws,
        "targetWorkspace": target_ws,
        "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
    }
    if not ledger.claim(event_key, in_flight):
        current = ledger.get(event_key)
        if current and current.get("status") == "running":
            log.info("Event already claimed for processing by another run. Skipping duplicate execution.")
            return

    defaults = {
        "copy_lakehouse_data": str(parse_bool(args.DEFAULT_COPY_LAKEHOUSE)),
        "create_lakehouse_shortcuts": str(parse_bool(args.DEFAULT_CREATE_SHORTCUTS)),
        "copy_warehouse_data": str(parse_bool(args.DEFAULT_COPY_WAREHOUSE)),
        "connections_from_to": args.DEFAULT_CONNECTIONS_FROM_TO,
        "has_wh_views_on_lh": str(parse_bool(args.DEFAULT_HAS_WH_VIEWS_ON_LH)),
    }

    selected_options = resolve_options(
        source_ws["workspaceName"], defaults, args.SOURCE_WORKSPACE_OVERRIDES_JSON
    )

    existing = ledger.claim(
        event_key,
        {
            "status": "running",
            "eventTimeUtc": event_time,
            "correlationId": correlation_id,
            "sourceWorkspace": source_ws,
            "targetWorkspace": target_ws,
            "options": selected_options,
            "candidateWorkspaceCount": len(connected_candidates),
            "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
        },
    )
    if existing is not None:
        status = existing.get("status")
        if status == "succeeded":
            log.info("Event already processed successfully. Skipping duplicate execution.")
        else:
            log.info("Event is currently being processed by another invocation. Skipping.")
        return

    try:
        notebook_status = invoke_post_activity(
            fabric_token,
            args.NOTEBOOK_WORKSPACE_ID,
            args.NOTEBOOK_ID,
            source_ws["workspaceName"],
            target_ws["workspaceName"],
            selected_options,
            log,
        )

        ledger.upsert(
            event_key,
            {
                "status": "succeeded",
                "eventTimeUtc": event_time,
                "correlationId": correlation_id,
                "sourceWorkspace": source_ws,
                "targetWorkspace": target_ws,
                "options": selected_options,
                "notebookStatus": notebook_status,
                "candidateWorkspaceCount": len(connected_candidates),
                "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
            },
        )
        log.info("Native branch-out event processed successfully")
    except Exception as ex:
        ledger.upsert(
            event_key,
            {
                "status": "failed",
                "eventTimeUtc": event_time,
                "correlationId": correlation_id,
                "sourceWorkspace": source_ws,
                "targetWorkspace": target_ws,
                "options": selected_options,
                "error": str(ex),
                "candidateWorkspaceCount": len(connected_candidates),
                "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
            },
        )
        raise


if __name__ == "__main__":
    main()
