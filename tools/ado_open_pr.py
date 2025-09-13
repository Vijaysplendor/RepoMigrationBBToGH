#!/usr/bin/env python3
import argparse, base64, csv, json, logging, os, sys, time
from typing import Dict, Any, Tuple, Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def _headers(pat: str) -> Dict[str, str]:
    auth = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {"Accept": "application/json", "Authorization": f"Basic {auth}"}

def _session() -> requests.Session:
    s = requests.Session()
    return s

def _repos(org: str, project: str, headers: Dict[str, str]) -> Dict[str, str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    return {repo["name"]: repo["id"] for repo in data.get("value", [])}

def _repo_meta(org: str, project: str, repo_id: str, headers: Dict[str, str]) -> Dict[str, Any]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def _resolve_branch_tip(org: str, project: str, repo_id: str, branch: str, headers: Dict[str, str]) -> Optional[str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/refs?filter=heads/{branch}&api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        return None
    data = r.json()
    if data.get("value"):
        return data["value"][0]["objectId"]
    return None

def push_file_new_branch(org: str, project: str, repo_name: str, pat: str,
                         yaml_content: str, yaml_path: str,
                         base_branch: str, new_branch: str) -> Tuple[bool, str]:
    headers = _headers(pat)
    repos = _repos(org, project, headers)
    if repo_name not in repos:
        return False, f"Repo '{repo_name}' not found in {org}/{project}"
    repo_id = repos[repo_name]

    tip = _resolve_branch_tip(org, project, repo_id, base_branch, headers)
    if not tip:
        # try repo defaultBranch
        meta = _repo_meta(org, project, repo_id, headers)
        default_ref = (meta.get("defaultBranch") or "refs/heads/main").split("/")[-1]
        tip = _resolve_branch_tip(org, project, repo_id, default_ref, headers)
        if not tip:
            return False, f"Cannot resolve base tip for {base_branch} or default branch in {repo_name}"
        base_branch = default_ref

    pushes_url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/pushes?api-version=7.0"
    payload = {
        "refUpdates": [{"name": f"refs/heads/{new_branch}", "oldObjectId": tip}],
        "commits": [{
            "comment": f"Add Azure Pipelines YAML migrated from Jenkinsfile",
            "changes": [{
                "changeType": "add",
                "item": {"path": yaml_path},
                "newContent": {"content": yaml_content, "contentType": "rawText"}
            }]
        }]
    }
    r = requests.post(pushes_url, headers=headers, json=payload, timeout=60)
    if r.status_code not in (200, 201):
        return False, f"Push failed: {r.status_code} {r.text}"
    return True, f"Pushed branch {new_branch} with {yaml_path}"

def open_pull_request(org: str, project: str, repo_name: str, pat: str,
                      source_branch: str, target_branch: str,
                      title: str, description: str) -> Tuple[bool, str]:
    headers = _headers(pat)
    repos = _repos(org, project, headers)
    if repo_name not in repos:
        return False, f"Repo '{repo_name}' not found in {org}/{project}"
    repo_id = repos[repo_name]
    pr_url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/pullrequests?api-version=7.0"
    body = {
        "sourceRefName": f"refs/heads/{source_branch}",
        "targetRefName": f"refs/heads/{target_branch}",
        "title": title,
        "description": description
    }
    r = requests.post(pr_url, headers=headers, json=body, timeout=60)
    if r.status_code not in (200, 201):
        return False, f"Create PR failed: {r.status_code} {r.text}"
    pr = r.json()
    return True, f"PR #{pr.get('pullRequestId')} created: {pr.get('url')}"

def load_targets_csv(path: str) -> Dict[str, Dict[str, str]]:
    """
    CSV headers:
      source,ado_org,ado_project,ado_repo,yaml_path,base_branch,new_branch
    'source' MUST match the repo string in repos-list.txt (exact).
    """
    out: Dict[str, Dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            out[row["source"]] = row
    return out

def main():
    ap = argparse.ArgumentParser(description="Push generated YAMLs to ADO and open PRs.")
    ap.add_argument("--targets", required=True, help="Path to ado-targets.csv")
    ap.add_argument("--in-root", required=True, help="Folder containing per-repo azure-pipelines.yml and summary.json")
    args = ap.parse_args()

    pat = os.environ.get("ADO_PAT")
    if not pat:
        print("Missing ADO_PAT env", file=sys.stderr); sys.exit(1)

    map_rows = load_targets_csv(args.targets)
    # Walk all summary.json files to get the source key (the original repo string)
    summaries = []
    for root, _, files in os.walk(args.in_root):
        if "summary.json" in files and "azure-pipelines.yml" in files:
            with open(os.path.join(root, "summary.json"), "r", encoding="utf-8") as f:
                summaries.append((root, json.load(f)))

    results = []
    for root, summary in summaries:
        source = summary.get("repo")  # this matches the entry in repos-list.txt
        if source not in map_rows:
            results.append({"source": source, "status": "skipped", "message": "No mapping in targets CSV"})
            continue

        row = map_rows[source]
        org = row["ado_org"]
        project = row["ado_project"]
        repo_name = row["ado_repo"]
        yaml_path = row.get("yaml_path") or "/azure-pipelines.yml"
        base_branch = row.get("base_branch") or "main"
        new_branch = row.get("new_branch") or f"jenkins-migration-{int(time.time())}"

        with open(os.path.join(root, "azure-pipelines.yml"), "r", encoding="utf-8") as f:
            yaml_content = f.read()

        ok, msg = push_file_new_branch(org, project, repo_name, pat, yaml_content, yaml_path, base_branch, new_branch)
        if not ok:
            results.append({"source": source, "status": "error", "message": msg})
            continue

        title = "Add Azure Pipelines YAML (migrated from Jenkins)"
        desc = f"Automated migration.\n\nDetection: {summary.get('stack')} (confidence {summary.get('confidence')})\nReasons: {', '.join(summary.get('reasons') or [])}\n"
        ok2, msg2 = open_pull_request(org, project, repo_name, pat, new_branch, base_branch, title, desc)
        status = "success" if ok2 else "error"
        results.append({"source": source, "status": status, "message": msg if ok2 else f"{msg} | {msg2}"})

    print(json.dumps({"results": results}, indent=2))

if __name__ == "__main__":
    main()
