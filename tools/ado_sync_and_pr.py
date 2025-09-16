#!/usr/bin/env python3
import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests


def sh(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise RuntimeError(f"CMD failed: {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p


def ado_headers(pat: str) -> Dict[str, str]:
    auth = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {"Accept": "application/json", "Authorization": f"Basic {auth}"}


def ensure_ado_repo(org: str, project: str, name: str, headers: Dict[str, str]) -> Tuple[str, bool]:
    # list repos
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"List repos failed: {r.status_code} {r.text}")
    for repo in r.json().get("value", []):
        if repo["name"].lower() == name.lower():
            return repo["id"], False
    # create
    r = requests.post(url, headers=headers, json={"name": name}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Create repo failed: {r.status_code} {r.text}")
    return r.json()["id"], True


def ado_repo_https_url(org: str, project: str, name: str, pat: str) -> str:
    # https://dev.azure.com/{org}/{project}/_git/{repo}
    return f"https://pat:{pat}@dev.azure.com/{org}/{project}/_git/{name}"


def credentialize_source(url: str) -> str:
    token = os.environ.get("GIT_TOKEN") or os.environ.get("GH_PAT") or os.environ.get("GH_TOKEN")
    user = os.environ.get("GIT_USERNAME", "x-access-token")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    if token and url.startswith("https://github.com/"):
        return url.replace("https://github.com/", f"https://{user}:{token}@github.com/", 1)
    return url


def ado_default_branch(org: str, project: str, repo_id: str, headers: Dict[str, str]) -> str:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}?api-version=7.0"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        return "main"
    ref = r.json().get("defaultBranch") or "refs/heads/main"
    return ref.split("/")[-1]


def open_pr(org: str, project: str, repo_id: str, src: str, tgt: str, title: str, desc: str, headers: Dict[str, str]) -> Tuple[bool, str]:
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo_id}/pullrequests?api-version=7.0"
    body = {"sourceRefName": f"refs/heads/{src}", "targetRefName": f"refs/heads/{tgt}", "title": title, "description": desc}
    r = requests.post(url, headers=headers, json=body, timeout=60)
    if r.status_code not in (200, 201):
        return False, f"Create PR failed: {r.status_code} {r.text}"
    pr = r.json()
    return True, f"PR #{pr.get('pullRequestId')} created"


def repo_name_from_source(source: str) -> str:
    s = source.strip()
    if s.endswith(".git"):
        s = s[:-4]
    return Path(s).name


def main():
    ap = argparse.ArgumentParser(description="Mirror source repo to ADO, add azure-pipelines.yml on feature branch, open PR.")
    ap.add_argument("--source", required=True, help="Source repo URL (GitHub HTTPS/SSH supported)")
    ap.add_argument("--ado-org", required=True, help="ADO org slug")
    ap.add_argument("--ado-project", required=True, help="ADO project")
    ap.add_argument("--ado-repo", required=False, help="ADO repo name (defaults to source repo name)")
    ap.add_argument("--yaml-file", required=True, help="Path to azure-pipelines.yml produced by converter")
    ap.add_argument("--yaml-path", default="/azure-pipelines.yml", help="Path inside ADO repo (default: /azure-pipelines.yml)")
    ap.add_argument("--feature-prefix", default="jenkins-migration", help="Feature branch prefix")
    ap.add_argument("--create-if-missing", action="store_true", help="Create ADO repo if it doesn't exist")
    args = ap.parse_args()

    ado_pat = os.environ.get("ADO_PAT")
    if not ado_pat:
        raise SystemExit("Missing ADO_PAT in environment")

    headers = ado_headers(ado_pat)
    ado_repo_name = args.ado_repo or repo_name_from_source(args.source)

    # 1) Ensure ADO repo exists
    try:
        repo_id, created = ensure_ado_repo(args.ado_org, args.ado_project, ado_repo_name, headers)
    except Exception as e:
        if args.create_if_missing:
            raise
        raise SystemExit(f"Repo not found and create-if-missing is false: {e}")

    # 2) Mirror source -> ADO
    tmp = Path(tempfile.mkdtemp(prefix="mirror-"))
    try:
        src_url = credentialize_source(args.source)
        bare = tmp / "src.git"
        sh(["git", "clone", "--mirror", src_url, str(bare)])
        ado_url = ado_repo_https_url(args.ado_org, args.ado_project, ado_repo_name, ado_pat)
        sh(["git", "remote", "add", "ado", ado_url], cwd=str(bare))
        sh(["git", "push", "--mirror", "ado"], cwd=str(bare))

        # 3) Figure default branch on ADO
        base = ado_default_branch(args.ado_org, args.ado_project, repo_id, headers)

        # 4) Normal clone (not bare), create feature branch, add yaml, push
        work = tmp / "work"
        sh(["git", "clone", ado_url, str(work)])
        # config identity
        user_name = os.environ.get("GIT_USERNAME", "migration-bot")
        user_email = os.environ.get("GIT_EMAIL", "migration-bot@example.com")
        sh(["git", "config", "user.name", user_name], cwd=str(work))
        sh(["git", "config", "user.email", user_email], cwd=str(work))

        # checkout base
        sh(["git", "checkout", base], cwd=str(work))

        # create feature branch
        import time
        feat = f"{args.feature_prefix}-{int(time.time())}"
        sh(["git", "checkout", "-b", feat], cwd=str(work))

        # write yaml
        ydst = work / args.yaml_path.lstrip("/")
        ydst.parent.mkdir(parents=True, exist_ok=True)
        ysrc = Path(args.yaml_file)
        ydst.write_text(ysrc.read_text(encoding="utf-8"), encoding="utf-8")

        sh(["git", "add", args.yaml_path], cwd=str(work))
        sh(["git", "commit", "-m", "Add Azure Pipelines YAML (migrated from Jenkins)"], cwd=str(work))
        sh(["git", "push", "origin", feat], cwd=str(work))

        # 5) Open PR
        ok, msg = open_pr(
            args.ado_org, args.ado_project, repo_id,
            src=feat, tgt=base,
            title="Add Azure Pipelines YAML (migrated from Jenkins)",
            desc="This PR adds the Azure Pipelines YAML generated from the Jenkinsfile.",
            headers=headers
        )
        result = {"status": "success" if ok else "error", "message": msg, "ado_repo": ado_repo_name, "feature": feat, "base": base}
        print(json.dumps(result, indent=2))
        if not ok:
            raise SystemExit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
