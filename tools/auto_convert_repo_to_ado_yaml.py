#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ---------------------------
# Helpers: shell & temp clone
# ---------------------------
def run(cmd: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def clone_or_use_path(repo: str) -> Path:
    """
    If repo is a local folder with a Jenkinsfile, use it.
    Otherwise, shallow clone to temp and return path.
    """
    p = Path(repo)
    if p.exists():
        return p.resolve()

    tmp = Path(tempfile.mkdtemp(prefix="jenkins2ado-"))
    # Try shallow clone
    cp = run(["git", "clone", "--depth", "1", repo, str(tmp / "src")])
    if cp.returncode != 0:
        raise RuntimeError(f"Clone failed: {cp.stderr or cp.stdout}")
    return (tmp / "src").resolve()

# ---------------------------
# Minimal Declarative parser
# ---------------------------
JENKINS_DECL_RE = re.compile(r"pipeline\s*\{(?P<body>[\s\S]*)\}\s*$", re.MULTILINE)

def _strip_comments(s: str) -> str:
    return re.sub(r"//.*", "", s)

def _extract_block(src: str, block: str) -> Optional[str]:
    m = re.search(rf"{block}\s*\{{", src)
    if not m:
        return None
    i, depth, start = m.end(), 1, m.end()
    while i < len(src):
        if src[i] == "{": depth += 1
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
    text = (block or "").strip()
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
    for line in (steps_block or "").splitlines():
        s = line.strip()
        if not s:
            continue
        ms = re.match(r"sh\s+['\"]([^'\"]+)['\"]", s)
        if ms:
            steps.append({"script": ms.group(1)})
            continue
        me = re.match(r"echo\s+['\"]([^'\"]+)['\"]", s)
        if me:
            steps.append({"script": f"echo {me.group(1)}"})
            continue
        # Fallback: escape single quotes outside f-string
        safe = s.replace("'", "'\"'\"'")  # classic shell-safe replacement
        steps.append({"script": f"echo 'UNHANDLED: {safe}'"})
    return steps

def parse_stages(stages_block: str) -> List[Dict[str, Any]]:
    stages: List[Dict[str, Any]] = []
    text = stages_block or ""
    for sm in re.finditer(r"stage\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\{", text):
        name = sm.group(1)
        i, depth, start = sm.end(), 1, sm.end()
        while i < len(text):
            if text[i] == "{": depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    block = text[start:i]
                    break
            i += 1
        else:
            block = ""
        steps_block = _extract_block(block, "steps") or ""
        stages.append({"name": name, "steps": parse_steps(steps_block)})
    return stages

def parse_jenkinsfile(jf_text: str) -> Dict[str, Any]:
    t = _strip_comments(jf_text)
    m = JENKINS_DECL_RE.search(t)
    if not m:
        raise ValueError("Only Declarative pipelines supported (no 'pipeline { ... }' found).")
    body = m.group("body")
    agent_block = _extract_block(body, "agent") or "any"
    env_block = _extract_block(body, "environment") or ""
    stages_block = _extract_block(body, "stages") or ""
    return {
        "agent": parse_agent(agent_block),
        "environment": parse_environment(env_block),
        "stages": parse_stages(stages_block),
    }

# ---------------------------
# Azure YAML rendering (with safe env YAML)
# ---------------------------
def env_to_yaml(env: Dict[str, str]) -> str:
    """
    Returns an indented YAML snippet for:
    variables:
      - name: KEY
        value: "VAL"
    """
    if not env:
        return ""
    variables = [{"name": k, "value": str(v)} for k, v in env.items()]
    yaml_text = yaml.safe_dump(variables, sort_keys=False, default_flow_style=False)
    # indent by 2 spaces to sit under 'variables:'
    return "".join(f"  {line}" for line in yaml_text.splitlines(True))

def guess_pool(agent: Dict[str, Any]) -> Dict[str, str]:
    if agent.get("type") == "label":
        lbl = (agent.get("label") or "").lower()
        if "windows" in lbl: return {"vmImage": "windows-latest"}
        if "mac" in lbl or "osx" in lbl: return {"vmImage": "macos-latest"}
    return {"vmImage": "ubuntu-latest"}

def render_ado_yaml(model: Dict[str, Any]) -> str:
    pool = guess_pool(model.get("agent", {}))
    variables_snippet = env_to_yaml(model.get("environment", {}))

    stages_yaml: List[str] = []
    for st in model.get("stages", []):
        steps_yaml = []
        for step in st.get("steps", []):
            script = step.get("script", "").strip()
            if script:
                # Use yaml to ensure proper quoting if needed
                steps_yaml.append(f"- script: {yaml.safe_dump(script, default_flow_style=False).strip()}\n  displayName: {yaml.safe_dump(script[:60]).strip()}")
        jobs_yaml = f"- job: job\n  pool:\n    vmImage: {pool['vmImage']}\n  steps:\n" + "\n".join(f"    {line}" for line in "\n".join(steps_yaml).splitlines(True))
        stages_yaml.append(f"- stage: {st['name']}\n  jobs:\n  {jobs_yaml}")

    body = []
    if variables_snippet:
        body.append("variables:\n" + variables_snippet)
    if stages_yaml:
        body.append("stages:\n" + "\n".join(stages_yaml))
    else:
        body.append("steps:\n- script: echo No stages parsed from Jenkinsfile\n  displayName: Fallback")
    return "\n".join(body) + "\n"

# ---------------------------
# Entry
# ---------------------------
def find_jenkinsfile(repo_root: Path) -> Path:
    # common places
    for cand in ["Jenkinsfile", "jenkins/Jenkinsfile", ".jenkins/Jenkinsfile", "ci/Jenkinsfile"]:
        p = repo_root / cand
        if p.is_file():
            return p
    # fallback: search shallow
    for p in repo_root.rglob("Jenkinsfile"):
        return p
    raise FileNotFoundError("Jenkinsfile not found in repository.")

def main():
    ap = argparse.ArgumentParser(description="Auto-convert Jenkins Declarative pipeline → Azure DevOps YAML")
    ap.add_argument("--repo", required=True, help="Local path or Git URL")
    ap.add_argument("--out-dir", required=True, help="Output folder for azure-pipelines.yml and summary.json")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clone or use existing
    repo_root = clone_or_use_path(args.repo)

    # Locate Jenkinsfile
    jf_path = find_jenkinsfile(repo_root)
    jf_text = jf_path.read_text(encoding="utf-8", errors="ignore")

    # Parse -> model -> render
    model = parse_jenkinsfile(jf_text)
    ado_yaml = render_ado_yaml(model)

    # Write outputs
    (out_dir / "azure-pipelines.yml").write_text(ado_yaml, encoding="utf-8")
    summary = {
        "repo": args.repo,
        "jenkinsfile": str(jf_path),
        "agent": model.get("agent"),
        "environment_keys": list(model.get("environment", {}).keys()),
        "stages_count": len(model.get("stages", [])),
        "out_yaml": str(out_dir / "azure-pipelines.yml"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Cleanup temp if we cloned into a temp dir
    # (If repo is local path we didn't create a tmp root folder.)
    # We created a temp dir if repo didn't exist locally and we cloned to .../src
    parent = repo_root.parent
    if "jenkins2ado-" in parent.name and parent.exists():
        shutil.rmtree(parent, ignore_errors=True)

    print(json.dumps({"status": "ok", "summary": summary}, indent=2))

if __name__ == "__main__":
    main()
