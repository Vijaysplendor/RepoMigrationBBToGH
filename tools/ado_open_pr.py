#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------- ADO HTTP helpers ----------

def _headers(pat: str) -> Dict[str, str]:
    auth = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {"Accept": "application/json", "Authorization": f"Basic {auth}"}

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
                    base_branch: str, new_branch: str, headers: Dict[str,str]) -> Tuple[bool,str,str,str]:
    """
    Returns (ok, msg, effective_base_branch, created_mode)
    created_mode in {"normal", "initialized_base"}.
    - normal: repo had commits; we pushed feature branch from base tip
    - initialized_base: repo was empty; we created the first commit on base_branch
    """
    # Try the requested base branch
    tip = branch_tip(org, project, repo_id, base_branch, headers)

    # If not found, try the repo default branch
    if not tip:
        meta = get_repo_meta(org, project, repo_id, headers)
        default_ref = (meta.get("defaultBranch") or "refs/heads/main")
        default_branch = default_ref.split("/")[-1]
        if default_branch != base_branch:
            base_branch = default_branch
        tip = branch_tip(org, project, repo_id, base_branch, headers)

    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/pushes?api-version=7.0"

    # If still not found, assume EMPTY repo -> initialize base branch with first commit
    if not tip:
        payload = {
            "refUpdates": [{
                "name": f"refs/heads/{base_branch}",
                "oldObjectId": "0000000000000000000000000000000000000000"  # magic zero for first commit
            }],
            "commits": [{
                "comment": "Initialize repo with Azure Pipelines YAML (migrated from Jenkins)",
                "changes": [{
                    "changeType": "add",
                    "item": {"path": yaml_path},
                    "newContent": {"content": yaml_content, "contentType": "rawText"}
                }]
            }]
        }
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code not in (200, 201):
            return False, f"Initial push (empty repo) failed: {r.status_code} {r.text}", base_branch, "normal"
        return True, f"Initialized empty repo on '{base_branch}' with first commit", base_branch, "initialized_base"

    # Non-empty repo: push a new feature branch from tip
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
    if r.status_code not in (200, 201):
        return False, f"Push failed: {r.status_code} {r.text}", base_branch, "normal"
    return True, "Branch pushed", base_branch, "normal"


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

# ---------- Mapping & inputs ----------

def repo_name_from_source(source: Optional[str]) -> Optional[str]:
    """
    Extract repository name (without .git) from URL/SSH/path.
    Returns None if source is empty or cannot be parsed.
    """
    if not source:
        return None
    s = source.strip()
    if not s:
        return None

    # SSH like git@host:org/repo(.git)
    if s.startswith("git@"):
        after_colon = s.split(":", 1)[-1]
        base = os.path.basename(after_colon.rstrip("/"))
    else:
        # Strip protocol if present
        m = re.match(r"^[a-zA-Z]+://(.+)$", s)
        if m:
            s = m.group(1)
        base = os.path.basename(s.rstrip("/"))

    if base.endswith(".git"):
        base = base[:-4]
    return base or None

def load_targets_csv(path: str) -> Dict[str, Dict[str,str]]:
    """
    Return a mapping keyed by 'source' (full source string) to override rows.
    If file missing, returns {}.
    """
    if not path or not os.path.exists(path):
        return {}
    out: Dict[str, Dict[str,str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k,v in row.items()}
            src = row.get("source")
            if src:
                out[src] = row
    return out

def boolish(val: str) -> bool:
    return str(val).strip().lower() in ("1","true","yes","y","on")

def collect_conversion_outputs(in_root: str):
    """
    Yields dicts with:
      - slug
      - yaml_path
      - summary_path
      - source (from summary.json: repo/source/origin; fallback slug)
      - summary (parsed dict or {})
    Searches common layouts:
      pr_inputs/<any>/{azure-pipelines.yml,summary.json}
      pr_inputs/<any>/out/<slug>/{azure-pipelines.yml,summary.json}
      pr_inputs/**/azure-pipelines.yml (recursive)
    """
    root = Path(in_root)
    if not root.exists():
        logging.error("Input root does not exist: %s", in_root)
        return

    # Strategy: find every azure-pipelines.yml recursively, then look for a sibling summary.json
    for yml in root.rglob("azure-pipelines.yml"):
        base_dir = yml.parent

        # Try sibling summary.json; if not found, try parent
        summary_path = base_dir / "summary.json"
        if not summary_path.is_file():
            maybe = base_dir.parent / "summary.json"
            if maybe.is_file():
                summary_path = maybe
            else:
                summary_path = None

        # Derive slug from the closest meaningful folder
        # Prefer the immediate containing folder; strip known prefixes
        slug = base_dir.name
        if slug.startswith("ado-yaml-"):
            slug = slug[len("ado-yaml-"):]
        # If we’re under .../out/<slug>, pick that slug
        if base_dir.name != "out" and base_dir.parent.name == "out":
            slug = base_dir.name

        summary = {}
        source = None
        if summary_path and summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                source = summary.get("repo") or summary.get("source") or summary.get("origin")
            except Exception as e:
                logging.warning("Failed to parse %s: %s", summary_path, e)

        if not source:
            source = slug  # last resort

        yield {
            "slug": slug,
            "yaml_path": str(yml),
            "summary_path": str(summary_path) if summary_path and summary_path.is_file() else None,
            "source": source,
            "summary": summary or {},
        }

# ---------- Main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Push generated YAMLs to ADO and open PRs (auto-resolving targets).")
    ap.add_argument("--in-root", required=True, help="Folder containing per-repo azure-pipelines.yml + summary.json")
    ap.add_argument("--targets", required=False, default="", help="Optional CSV overrides")
    ap.add_argument("--ado-org", required=True, help="Default ADO org for auto-resolution (slug, not URL)")
    ap.add_argument("--ado-project", required=True, help="Default ADO project for auto-resolution")
    ap.add_argument("--autodiscover-projects", default="false", help="Scan all org projects to find repo by name (true/false)")
    ap.add_argument("--create-if-missing", default="false", help="Create repo in default project if not found (true/false)")
    args = ap.parse_args()

    pat = os.environ.get("ADO_PAT")
    if not pat:
        print("Missing ADO_PAT env", file=sys.stderr)
        return 1
    headers = _headers(pat)

    overrides = load_targets_csv(args.targets)
    auto_scan_all = boolish(args.autodiscover_projects)
    create_missing = boolish(args.create_if_missing)

    records = list(collect_conversion_outputs(args.in_root))
    if not records:
        logging.error("No conversion outputs found under %s", args.in_root)
        print(json.dumps({"results": [], "message": "no inputs"}, indent=2))
        return 1

    project_list: List[str] = []
    if auto_scan_all:
        try:
            project_list = list_projects(args.ado_org, headers)
            logging.info("Autodiscover enabled; found %d projects", len(project_list))
        except Exception as e:
            logging.error("List projects failed: %s", e)

    results = []
    for rec in records:
        slug         = rec["slug"]
        yaml_path    = rec["yaml_path"]
        summary      = rec["summary"]
        source       = rec["source"]
        name_guess   = repo_name_from_source(source)

        if not name_guess:
            msg = f"Could not infer repo name from source '{source}' (slug={slug}); skipping."
            logging.warning(msg)
            results.append({"source": source, "slug": slug, "status": "skipped", "message": msg})
            continue

        # Prefer exact override by source; fall back to override by repo name
        row = overrides.get(source) or overrides.get(name_guess) or {}

        org         = row.get("ado_org")     or args.ado_org
        project     = row.get("ado_project") or args.ado_project
        repo_name   = row.get("ado_repo")    or name_guess
        yaml_repo_path = row.get("yaml_path") or "/azure-pipelines.yml"
        base_branch = row.get("base_branch")  or "main"
        new_branch  = row.get("new_branch")   or f"jenkins-migration-{int(time.time())}"

        yaml_content = Path(yaml_path).read_text(encoding="utf-8")

        # 1) Try default project first
        repo_id = None
        try:
            repos = list_repos(org, project, headers)
            repo_id = repos.get(repo_name)
            logging.info("Lookup %s/%s repo '%s' -> %s", org, project, repo_name, repo_id or "not-found")
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
                        logging.info("Found repo '%s' in project %s via autodiscover", repo_name, proj)
                        break
                except Exception:
                    continue

        # 3) If still not found and create allowed, create in default project
        if not repo_id and create_missing:
            ok, msg, rid = create_repo(org, project, repo_name, headers)
            if ok:
                repo_id = rid
                logging.info("Created repo %s/%s/%s", org, project, repo_name)
            else:
                logging.error("Create repo failed for %s/%s/%s: %s", org, project, repo_name, msg)
                results.append({"source": source, "slug": slug, "status": "error", "message": msg})
                continue

        if not repo_id:
            msg = f"Repo '{repo_name}' not found; provide CSV override or enable create/autodiscover"
            logging.warning(msg)
            results.append({"source": source, "slug": slug, "status": "skipped", "message": msg})
            continue

        # 4) Push branch with YAML
        ok_push, msg_push, base_branch_effective, mode = push_new_branch(org, project, repo_id, yaml_repo_path, yaml_content, base_branch, new_branch, headers)
        if not ok_push:
            logging.error("Push failed for %s/%s/%s: %s", org, project, repo_name, msg_push)
            results.append({"source": source, "slug": slug, "status": "error", "message": msg_push})
            continue

        if mode == "initialized_base":
            # Repo was empty; we created the first commit on base branch.
            # PR doesn't make sense yet (there's only one branch/commit).
            msg = f"{msg_push}; PR skipped (repo just initialized on '{base_branch_effective}')."
            logging.info(msg)
            results.append({"source": source, "slug": slug, "status": "success", "message": msg})
            continue

        # 5) Open PR
        title = "Add Azure Pipelines YAML (migrated from Jenkins)"
        stack = summary.get("stack")
        conf  = summary.get("confidence")
        reasons = summary.get("reasons") or []
        reasons_txt = ", ".join(reasons) if isinstance(reasons, list) else str(reasons)

        desc_lines = [
            "Automated migration.",
            "",
        ]
        if stack:
            desc_lines.append(f"Detected stack: {stack}" + (f" (confidence {conf})" if conf is not None else ""))
        if reasons_txt:
            desc_lines.append(f"Reasons: {reasons_txt}")
        description = "\n".join(desc_lines) + "\n"

        ok_pr, msg_pr = open_pr(org, project, repo_id, new_branch, base_branch, title, description, headers)
        status = "success" if ok_pr else "error"
        logging.log(logging.INFO if ok_pr else logging.ERROR, "PR result for %s/%s/%s: %s", org, project, repo_name, msg_pr)
        results.append({"source": source, "slug": slug, "status": status, "message": msg_pr})

    print(json.dumps({"results": results}, indent=2))
    # Non-zero if any error
    if any(r.get("status") == "error" for r in results):
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
