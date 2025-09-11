#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import logging
import os
import re
import sys
from typing import Dict, List, Any, Optional, Tuple

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ==========================
# Jenkinsfile Parsing (80/20)
# ==========================

JENKINS_DECLARATIVE_RE = re.compile(
    r"pipeline\s*\{(?P<body>[\s\S]*)\}\s*$",
    re.MULTILINE
)

def _strip_comments(s: str) -> str:
    # Remove // comments (basic)
    return re.sub(r"//.*", "", s)

def _extract_block(src: str, block_name: str) -> Optional[str]:
    # Finds content inside block_name { ... }
    pattern = re.compile(rf"{block_name}\s*\{{", re.MULTILINE)
    m = pattern.search(src)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i]
        i += 1
    return None

def parse_environment(block: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    flat = " ".join(block.splitlines())
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]+)['\"]", flat):
        env[m.group(1)] = m.group(2)
    return env

def parse_agent(block: str) -> Dict[str, Any]:
    text = block.strip()
    if text.startswith("any"):
        return {"type": "any"}
    m = re.search(r"label\s+['\"]([^'\"]+)['\"]", text)
    if m:
        return {"type": "label", "label": m.group(1)}
    m = re.search(r"image\s+['\"]([^'\"]+)['\"]", text)
    if m:
        return {"type": "docker", "image": m.group(1)}
    return {"type": "any"}

def parse_steps(steps_block: str) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for line in steps_block.splitlines():
        line = line.strip()
        if not line:
            continue
        msh = re.match(r"sh\s+['\"]([^'\"]+)['\"]", line)
        if msh:
            steps.append({"script": msh.group(1)})
            continue
        me = re.match(r"echo\s+['\"]([^'\"]+)['\"]", line)
        if me:
            steps.append({"script": f"echo {me.group(1)}"})
            continue
        steps.append({"script": f"echo 'UNHANDLED: {line.replace(\"'\", \"\\'\")}'"})
    return steps

def parse_stages(stages_block: str) -> List[Dict[str, Any]]:
    stages: List[Dict[str, Any]] = []
    for sm in re.finditer(r"stage\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\{", stages_block):
        name = sm.group(1)
        start = sm.end()
        depth = 1
        i = start
        while i < len(stages_block):
            if stages_block[i] == "{":
                depth += 1
            elif stages_block[i] == "}":
                depth -= 1
                if depth == 0:
                    stage_content = stages_block[start:i]
                    break
            i += 1
        else:
            stage_content = ""

        steps_block = _extract_block(stage_content, "steps") or ""
        stages.append({
            "name": name,
            "steps": parse_steps(steps_block)
        })
    return stages

def parse_jenkinsfile(text: str) -> Dict[str, Any]:
    text = _strip_comments(text)
    m = JENKINS_DECLARATIVE_RE.search(text)
    if not m:
        raise ValueError("Only Declarative Jenkins pipelines are supported (no 'pipeline { ... }' block found).")
    body = m.group("body")

    agent_block = _extract_block(body, "agent") or "any"
    env_block = _extract_block(body, "environment") or ""
    stages_block = _extract_block(body, "stages") or ""

    model = {
        "agent": parse_agent(agent_block),
        "environment": parse_environment(env_block),
        "stages": parse_stages(stages_block)
    }
    return model

# ==========================
# Azure YAML conversion
# ==========================

def model_to_azure_yaml(model: Dict[str, Any]) -> str:
    variables = [{"name": k, "value": v} for k, v in model.get("environment", {}).items()]

    pool = None
    agent = model.get("agent", {})
    if agent.get("type") == "any":
        pool = {"vmImage": "ubuntu-latest"}
    elif agent.get("type") == "label":
        label = agent.get("label", "")
        guess = "ubuntu-latest"
        if "windows" in label.lower():
            guess = "windows-latest"
        elif "mac" in label.lower() or "osx" in label.lower():
            guess = "macos-latest"
        pool = {"vmImage": guess}
    elif agent.get("type") == "docker":
        pool = {"vmImage": "ubuntu-latest"}

    azure_stages: List[Dict[str, Any]] = []
    for st in model.get("stages", []):
        steps = []
        for s in st.get("steps", []):
            script = s.get("script", "").strip()
            if script:
                steps.append({"script": script, "displayName": script[:60]})
        job = {"job": "job", "steps": steps}
        if pool:
            job["pool"] = pool
        azure_stages.append({"stage": st["name"], "jobs": [job]})

    azure_yaml: Dict[str, Any] = {}
    if variables:
        azure_yaml["variables"] = variables
    if azure_stages:
        azure_yaml["stages"] = azure_stages
    else:
        azure_yaml["steps"] = [{"script": "echo No stages parsed from Jenkinsfile"}]

    return yaml.safe_dump(azure_yaml, sort_keys=False)

# ==========================
# Azure DevOps REST helpers
# ==========================

def _ado_headers(pat: str) -> Dict[str, str]:
    auth = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {
        "Accept": "application/json",
        "Authorization": f"Basic {auth}"
    }

def _get_latest_commit(org: str, project: str, repo_id: str, headers: Dict[str, str]) -> Optional[str]:
    for branch in ["main", "master"]:
        url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/refs?filter=heads/{branch}&api-version=7.0"
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if data.get("value"):
                return data["value"][0]["objectId"]
    return None

def _get_repos(org: str, project: str, headers: Dict[str, str]) -> Dict[str, str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        logging.error("Failed to list repos: %s %s", r.status_code, r.text)
        return {}
    data = r.json()
    return {repo["name"]: repo["id"] for repo in data.get("value", [])}

def push_yaml_to_azure(
    org: str,
    project: str,
    repo_name: str,
    pat: str,
    yaml_content: str,
    yaml_path: str = "/azure-pipelines.yml",
    branch_suffix: str = "jenkins-migration"
) -> Tuple[bool, str]:
    headers = _ado_headers(pat)
    repos = _get_repos(org, project, headers)
    if repo_name not in repos:
        msg = f"Repository '{repo_name}' not found in project '{project}'"
        logging.error(msg)
        return False, msg
    repo_id = repos[repo_name]

    base_url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}"
    latest = _get_latest_commit(org, project, repo_id, headers)
    if not latest:
        msg = f"Could not resolve base commit for main/master in repo '{repo_name}'"
        logging.error(msg)
        return False, msg

    new_branch = f"refs/heads/{branch_suffix}"
    url = f"{base_url}/pushes?api-version=7.0"
    payload = {
        "refUpdates": [{"name": new_branch, "oldObjectId": latest}],
        "commits": [{
            "comment": "Add Azure Pipelines YAML migrated from Jenkinsfile",
            "changes": [{
                "changeType": "add",
                "item": {"path": yaml_path},
                "newContent": {"content": yaml_content, "contentType": "rawText"}
            }]
        }]
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code in (200, 201):
        msg = f"Pushed YAML to branch '{branch_suffix}' at path '{yaml_path}'"
        logging.info(msg)
        return True, msg
    msg = f"Push failed: {r.status_code} {r.text}"
    logging.error(msg)
    return False, msg

# ==========================
# Manifest loading
# ==========================

def load_manifest(manifest_path: str) -> List[Dict[str, Any]]:
    """
    Supports JSON (list of objects) or CSV (headers must match keys below).
    Each item should include:
      jenkinsfile        : path to Jenkinsfile
      out                : local output yaml path (optional; default derived)
      ado_org            : Azure DevOps org
      ado_project        : Azure DevOps project
      ado_repo           : Azure DevOps repository name
      ado_branch         : branch name suffix to create (optional; default 'jenkins-migration')
      yaml_path          : repo path to place yaml (optional; default '/azure-pipelines.yml')
    """
    if manifest_path.lower().endswith(".json"):
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("JSON manifest must be a list of objects.")
            return data
    elif manifest_path.lower().endswith(".csv"):
        rows: List[Dict[str, Any]] = []
        with open(manifest_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
        return rows
    else:
        raise ValueError("Manifest must be .json or .csv")

# ==========================
# Batch processor
# ==========================

def process_item(
    item: Dict[str, Any],
    pat_env: str
) -> Dict[str, Any]:
    """
    Process a single Jenkinsfile -> YAML -> push.
    Returns a per-item result dict for summary.
    """
    required = ["jenkinsfile", "ado_org", "ado_project", "ado_repo"]
    missing = [k for k in required if not item.get(k)]
    if missing:
        msg = f"Missing required fields: {', '.join(missing)}"
        logging.error(msg)
        return {"jenkinsfile": item.get("jenkinsfile"), "status": "error", "message": msg}

    jenkinsfile = item["jenkinsfile"]
    out_local = item.get("out") or os.path.splitext(os.path.basename(jenkinsfile))[0] + ".azure-pipelines.yml"
    org = item["ado_org"]
    project = item["ado_project"]
    repo = item["ado_repo"]
    branch = item.get("ado_branch") or "jenkins-migration"
    yaml_path = item.get("yaml_path") or "/azure-pipelines.yml"

    try:
        with open(jenkinsfile, "r", encoding="utf-8") as f:
            jf = f.read()
    except Exception as e:
        msg = f"Read Jenkinsfile failed: {e}"
        logging.error("%s (%s)", msg, jenkinsfile)
        return {"jenkinsfile": jenkinsfile, "status": "error", "message": msg}

    try:
        model = parse_jenkinsfile(jf)
        azure_yaml = model_to_azure_yaml(model)
    except Exception as e:
        msg = f"Conversion failed: {e}"
        logging.error("%s (%s)", msg, jenkinsfile)
        return {"jenkinsfile": jenkinsfile, "status": "error", "message": msg}

    # write local yaml
    try:
        with open(out_local, "w", encoding="utf-8") as f:
            f.write(azure_yaml)
        logging.info("Wrote YAML to %s", out_local)
    except Exception as e:
        msg = f"Write YAML failed: {e}"
        logging.error("%s (%s)", msg, jenkinsfile)
        return {"jenkinsfile": jenkinsfile, "status": "error", "message": msg}

    # push to Azure
    pat = os.environ.get(pat_env)
    if not pat:
        msg = f"Azure PAT environment variable '{pat_env}' not set"
        logging.error(msg)
        return {"jenkinsfile": jenkinsfile, "status": "error", "message": msg}

    ok, push_msg = push_yaml_to_azure(
        org=org,
        project=project,
        repo_name=repo,
        pat=pat,
        yaml_content=azure_yaml,
        yaml_path=yaml_path,
        branch_suffix=branch
    )
    return {
        "jenkinsfile": jenkinsfile,
        "status": "success" if ok else "error",
        "message": push_msg,
        "ado_org": org,
        "ado_project": project,
        "ado_repo": repo,
        "ado_branch": branch,
        "yaml_path": yaml_path,
        "out_local": out_local
    }

def main():
    ap = argparse.ArgumentParser(
        description="Batch convert Jenkinsfiles (Declarative) to Azure DevOps YAML and push each to its repo."
    )
    ap.add_argument("--manifest", required=True, help="Path to manifest.json or manifest.csv")
    ap.add_argument("--ado-pat-env", default="ADO_PAT", help="Env var containing Azure DevOps PAT (default: ADO_PAT)")
    args = ap.parse_args()

    try:
        items = load_manifest(args.manifest)
    except Exception as e:
        logging.error("Failed to load manifest: %s", e)
        sys.exit(1)

    results: List[Dict[str, Any]] = []
    for idx, item in enumerate(items, 1):
        logging.info("Processing [%d/%d]: %s", idx, len(items), item.get("jenkinsfile"))
        res = process_item(item, args.ado_pat_env)
        results.append(res)

    # Print summary
    total = len(results)
    success = sum(1 for r in results if r.get("status") == "success")
    logging.info("Summary: %d/%d pushed successfully.", success, total)

    # Emit machine-readable summary to stdout (optional)
    print(json.dumps({
        "status": "complete",
        "processed": total,
        "successful": success,
        "results": results
    }, indent=2))

if __name__ == "__main__":
    main()
