#!/usr/bin/env python3
import argparse, base64, csv, json, logging, os, re, sys, time
from typing import Dict, Any, Tuple, Optional, List

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------- ADO HTTP helpers ----------

def _headers(pat: str) -> Dict[str, str]:
    auth = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {"Accept": "application/json", "Authorization": f"Basic {auth}"}

def _s() -> requests.Session:
    s = requests.Session()
    return s

def list_projects(org: str, headers: Dict[str,str]) -> List[str]:
    url = f"https://dev.azure.com/{org}/_apis/projects?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return [p["name"] for p in r.json().get("value", [])]

def list_repos(org: str, project: str, headers: Dict[str,str]) -> Dict[str,str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return {repo["name"]: repo["id"] for repo in r.json().get("value", [])}

def get_repo_meta(org: str, project: str, repo_id: str, headers: Dict[str,str]) -> Dict[str,Any]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def branch_tip(org: str, project: str, repo_id: str, branch: str, headers: Dict[str,str]) -> Optional[str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/refs?filter=heads/{branch}&api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        return None
    data = r.json()
    return data["value"][0]["objectId"] if data.get("value") else None

def create_repo(org: str, project: str, name: str, headers: Dict[str,str]) -> Tuple[bool,str,str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=7.0"
    r = requests.post(url, headers=headers, json={"name": name}, timeout=30)
    if r.status_code not in (200,201):
        return False, f"Create repo failed: {r.status_code} {r.text}", ""
    repo = r.json()
    return True, "Repo created", repo["id"]

def push_new_branch(org: str, project: str, repo_id: str, yaml_path: str, yaml_content: str,
                    base_branch: str, new_branch: str, headers: Dict[str,str]) -> Tuple[bool,str]:
    tip = branch_tip(org, project, repo_id, base_branch, headers)
    if not tip:
        meta = get_repo_meta(org, project, repo_id, headers)
        default_ref = (meta.get("defaultBranch") or "refs/heads/main").split("/")[-1]
        tip = branch_tip(org, project, repo_id, default_ref, headers)
        if not tip:
            return False, f"No tip for base/default branch in repo"
        base_branch = default_ref

    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/pushes?api-version=7.0"
    payload = {
        "refUpdates": [{"name": f"refs/heads/{new_branch}", "oldObjectId": tip}],
        "commits": [{
            "comment": "Add Azure Pipelines YAML migrated from Jenkins",
            "changes": [{
                "changeType": "add",
                "item": {"path": yaml_path},
                "newContent": {"content": yaml_content, "contentType": "rawText"}
            }]
        }]
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code not in (200,201):
        return False, f"Push failed: {r.status_code} {r.text}"
    return True, "Branch pushed"

def open_pr(org: str, project: str, repo_id: str, source_branch: str, target_branch: str,
            title: str, description: str, headers: Dict[str,str]) -> Tuple[bool,str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/pullrequests?api-version=7.0"
    body = {
        "sourceRefName": f"refs/heads/{source_branch}",
        "targetRefName": f"refs/heads/{target_branch}",
        "title": title,
        "description": description
    }
    r = requests.post(url, headers=headers, json=body, timeout=60)
    if r.status_code not in (200,201):
        return False, f"Create PR failed: {r.status_code} {r.text}"
    pr = r.json()
    return True, f"PR #{pr.get('pullRequestId')} created"

# ---------- Mapping logic ----------

def repo_name_from_source(source: Optional[str]) -> Optional[str]:
    """
    Return the repository name (without .git) from a URL or path.
    Handles:
      - https://github.com/org/repo.git
      - git@github.com:org/repo.git
      - /some/local/path/repo
      - org/repo
    """
    if not source:
        return None

    s = source.strip()
    if not s:
        return None

    # SSH form: git@host:org/repo(.git)
    if s.startswith("git@"):
        after_colon = s.split(":", 1)[-1]
        base = os.path.basename(after_colon.rstrip("/"))
    else:
        # URL or path: take last path segment
        # Strip any protocol
        m = re.match(r"^[a-zA-Z]+://(.+)$", s)
        if m:
            s = m.group(1)
        # Remaining: maybe host/org/repo or local/dir/repo
        base = os.path.basename(s.rstrip("/"))

    if base.endswith(".git"):
        base = base[:-4]
    return base or None

def load_targets_csv(path: str) -> Dict[str, Dict[str,str]]:
    if not path or not os.path.exists(path):
        return {}
    out: Dict[str, Dict[str,str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k,v in row.items()}
            out[row["source"]] = row
    return out

# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser(description="Push generated YAMLs to ADO and open PRs (auto-resolving targets).")
    ap.add_argument("--in-root", required=True, help="Folder containing per-repo azure-pipelines.yml + summary.json")
    ap.add_argument("--targets", required=False, default="", help="Optional CSV overrides")
    ap.add_argument("--ado-org", required=True, help="Default ADO org for auto-resolution")
    ap.add_argument("--ado-project", required=True, help="Default ADO project for auto-resolution")
    ap.add_argument("--autodiscover-projects", default="false", help="Scan all org projects to find repo by name")
    ap.add_argument("--create-if-missing", default="false", help="Create repo in default project if not found")
    args = ap.parse_args()

    pat = os.environ.get("ADO_PAT")
    if not pat:
        print("Missing ADO_PAT env", file=sys.stderr); sys.exit(1)
    headers = _headers(pat)

    overrides = load_targets_csv(args.targets)
    auto_scan_all = args.autodiscover_projects.lower() == "true"
    create_missing = args.create_if_missing.lower() == "true"

    # collect all results
    entries = []
    for root, _, files in os.walk(args.in_root):
        if "summary.json" in files and "azure-pipelines.yml" in files:
            with open(os.path.join(root, "summary.json"), "r", encoding="utf-8") as f:
                summary = json.load(f)
            with open(os.path.join(root, "azure-pipelines.yml"), "r", encoding="utf-8") as f:
                yaml_content = f.read()
            entries.append((summary, yaml_content))

    # optional discovery cache
    project_list = []
    if auto_scan_all:
        try:
            project_list = list_projects(args.ado_org, headers)
        except Exception as e:
            logging.error("List projects failed: %s", e)

    results = []
    for summary, yaml_content in entries:
        source = summary.get("repo")
        name_guess = repo_name_from_source(source)
        row = overrides.get(source, {})

        org = row.get("ado_org") or args.ado_org
        project = row.get("ado_project") or args.ado_project
        repo_name = row.get("ado_repo") or name_guess
        yaml_path = row.get("yaml_path") or "/azure-pipelines.yml"
        base_branch = row.get("base_branch") or "main"
        new_branch = row.get("new_branch") or f"jenkins-migration-{int(time.time())}"

        repo_id = None

        # 1) Try default project
        try:
            repos = list_repos(org, project, headers)
            repo_id = repos.get(repo_name)
        except Exception as e:
            logging.error("List repos failed for %s/%s: %s", org, project, e)

        # 2) If not found and autodiscover enabled, scan all projects
        if not repo_id and auto_scan_all and project_list:
            for proj in project_list:
                try:
                    repos = list_repos(org, proj, headers)
                    if repo_name in repos:
                        project = proj
                        repo_id = repos[repo_name]
                        break
                except Exception:
                    pass

        # 3) If still not found and create allowed, create in default project
        if not repo_id and create_missing:
            ok, msg, rid = create_repo(org, project, repo_name, headers)
            if ok:
                repo_id = rid
                logging.info("Created repo %s/%s/%s", org, project, repo_name)
            else:
                results.append({"source": source, "status": "error", "message": msg})
                continue

        if not repo_id:
            results.append({"source": source, "status": "skipped", "message": f"Repo '{repo_name}' not found; provide CSV override or enable create/autodiscover"})
            continue

        ok_push, msg_push = push_new_branch(org, project, repo_id, yaml_path, yaml_content, base_branch, new_branch, headers)
        if not ok_push:
            results.append({"source": source, "status": "error", "message": msg_push})
            continue

        title = "Add Azure Pipelines YAML (migrated from Jenkins)"
        desc = f"Automated migration.\n\nDetection: {summary.get('stack')} (confidence {summary.get('confidence')})\nReasons: {', '.join(summary.get('reasons') or [])}\n"
        ok_pr, msg_pr = open_pr(org, project, repo_id, new_branch, base_branch, title, desc, headers)
        results.append({"source": source, "status": "success" if ok_pr else "error", "message": msg_pr})

    print(json.dumps({"results": results}, indent=2))

if __name__ == "__main__":
    main()
