import os
from ruamel.yaml import YAML
import re

def parse_jenkinsfile(jenkinsfile_content: str):
    """
    Parse Jenkinsfile and emit a single-job GitHub Actions workflow.
    Supports .NET and Maven (Java). Prefers .NET if both are detected.
    """
    text = jenkinsfile_content or ""
    low  = text.lower()

    def has(pattern: str) -> bool:
        return pattern.lower() in low

    def has_stage(name: str) -> bool:
        # match stage('Name') or stage("Name"), ignore spacing/case
        return re.search(rf"stage\(\s*['\"]{re.escape(name)}['\"]\s*\)", low, re.IGNORECASE) is not None

    pipeline = {
        'name': 'CI Workflow',
        'on': {'push': {'branches': ['main']}},
        'jobs': {'ci': {'runs-on': 'ubuntu-latest', 'steps': []}}
    }
    steps = pipeline['jobs']['ci']['steps']

    # ---- Stack detection (case-insensitive) ----
    dotnet_markers = ['dotnet restore', 'dotnet build', 'dotnet test', 'dotnet publish', 'dotnet --info']
    is_dotnet = any(m in low for m in dotnet_markers)

    # Be strict: only call it Maven if we actually see 'mvn'
    is_maven = 'mvn' in low

    # Prefer .NET if both appear
    stack = 'dotnet' if is_dotnet else ('maven' if is_maven else None)
    print(f"[converter] Detected stack: {stack or 'unknown'}")

    # ---- Checkout ----
    if has_stage('Checkout') or has('checkout scm'):
        steps.append({'name': 'Checkout code', 'uses': 'actions/checkout@v4'})

    # ---- Toolchain setup ----
    if stack == 'dotnet':
        steps.insert(0, {
            'name': 'Set up .NET',
            'uses': 'actions/setup-dotnet@v4',
            'with': {'dotnet-version': '8.0.x'}
        })
    elif stack == 'maven':
        steps.insert(0, {
            'name': 'Set up JDK 11',
            'uses': 'actions/setup-java@v4',
            'with': {'distribution': 'temurin', 'java-version': '11'}
        })

    # ---- Build ----
    if has_stage('Build') or has('mvn clean compile') or has('dotnet build'):
        if stack == 'dotnet':
            if has('dotnet restore'):
                steps.append({'name': 'Restore packages', 'run': 'dotnet restore'})
                steps.append({'name': 'Build the project', 'run': 'dotnet build --configuration Release --no-restore'})
            else:
                steps.append({'name': 'Build the project', 'run': 'dotnet build --configuration Release'})
        elif stack == 'maven':
            steps.append({'name': 'Build the project', 'run': 'mvn clean compile'})
        else:
            # Unknown stack: be safe and do nothing (or choose a default you prefer)
            pass

    # ---- Test ----
    if has_stage('Test') or has('mvn test') or has('dotnet test'):
        if stack == 'dotnet':
            steps.append({'name': 'Run tests', 'run': 'dotnet test --configuration Release'})
        elif stack == 'maven':
            steps.append({'name': 'Run tests', 'run': 'mvn test'})
        else:
            pass

    # ---- Deploy (left generic) ----
    if has_stage('Deploy') or has('scp'):
        steps.append({
            'name': 'Deploy the project',
            'run': ('echo "Add your .NET deploy command here (e.g., az webapp deploy, dotnet publish + rsync/scp)"'
                    if stack == 'dotnet'
                    else 'scp target/myapp.war user@server:/path/to/deploy')
        })

    return pipeline

def convert_jenkinsfile_to_github_actions(jenkinsfile_path, output_dir):
    with open(jenkinsfile_path, 'r') as jenkinsfile:
        jenkinsfile_content = jenkinsfile.read()

    github_actions_yaml = parse_jenkinsfile(jenkinsfile_content)

    workflow_dir = os.path.join(output_dir, 'workflows')
    os.makedirs(workflow_dir, exist_ok=True)
    output_file_path = os.path.join(workflow_dir, 'ci-workflow.yml')

    yaml = YAML()
    yaml.default_flow_style = False
    with open(output_file_path, 'w') as yaml_file:
        yaml.dump(github_actions_yaml, yaml_file)

    print(f"GitHub Actions workflow generated and saved to {output_file_path}")

def main():
    jenkinsfile_path = 'Jenkinsfile'
    output_dir = '.github'
    if not os.path.exists(jenkinsfile_path):
        print(f"Jenkinsfile not found at {jenkinsfile_path}")
        return
    convert_jenkinsfile_to_github_actions(jenkinsfile_path, output_dir)

if __name__ == "__main__":
    main()
