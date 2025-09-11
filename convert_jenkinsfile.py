import os
from ruamel.yaml import YAML

def parse_jenkinsfile(jenkinsfile_content):
    """
    Parses a Jenkinsfile content and converts it into a structured dictionary for GitHub Actions.
    Now supports both Maven (Java) and .NET projects, based on simple substring detection.
    """
    pipeline = {
        'name': 'CI Workflow',
        'on': {
            'push': {
                'branches': ['main']
            }
        },
        'jobs': {
            'ci': {
                'runs-on': 'ubuntu-latest',
                'steps': []
            }
        }
    }

    steps = pipeline['jobs']['ci']['steps']

    # -----------------------
    # Detect technology stack
    # -----------------------
    # Maven/Java detection (original behavior)
    is_maven = 'mvn' in jenkinsfile_content or "stage('Build')" in jenkinsfile_content and 'mvn clean compile' in jenkinsfile_content

    # .NET detection
    dotnet_markers = ['dotnet restore', 'dotnet build', 'dotnet test', 'dotnet publish', 'dotnet --info']
    is_dotnet = any(marker in jenkinsfile_content for marker in dotnet_markers)

    # If both are detected, prefer Maven to keep backward compatibility with original logic
    # (You can flip this if you prefer .NET to win when both are present.)
    stack = 'maven' if is_maven else ('dotnet' if is_dotnet else None)

    # --------------------------------
    # Checkout detection (unchanged)
    # --------------------------------
    if "stage('Checkout')" in jenkinsfile_content or 'checkout scm' in jenkinsfile_content:
        steps.append({
            'name': 'Checkout code',
            'uses': 'actions/checkout@v4'
        })

    # --------------------------------
    # Toolchain setup based on stack
    # --------------------------------
    if stack == 'maven':
        # Preserve original Java setup behavior when 'mvn' is present
        steps.insert(0, {
            'name': 'Set up JDK 11',
            'uses': 'actions/setup-java@v4',
            'with': {
                'distribution': 'temurin',
                'java-version': '11'
            }
        })
    elif stack == 'dotnet':
        # Add .NET SDK setup for .NET pipelines
        steps.insert(0, {
            'name': 'Set up .NET',
            'uses': 'actions/setup-dotnet@v4',
            'with': {
                'dotnet-version': '8.0.x'  # adjust if your repo needs a different SDK
            }
        })

    # --------------------------------
    # Build stage mapping
    # --------------------------------
    if "stage('Build')" in jenkinsfile_content or 'mvn clean compile' in jenkinsfile_content:
        if stack == 'dotnet':
            # Include restore if the Jenkinsfile had it; otherwise build usually restores implicitly if needed
            if 'dotnet restore' in jenkinsfile_content:
                steps.append({
                    'name': 'Restore packages',
                    'run': 'dotnet restore'
                })
            steps.append({
                'name': 'Build the project',
                'run': 'dotnet build --configuration Release --no-restore' if 'dotnet restore' in jenkinsfile_content else 'dotnet build --configuration Release'
            })
        else:
            # Default/back-compat to Maven behavior
            steps.append({
                'name': 'Build the project',
                'run': 'mvn clean compile'
            })

    # --------------------------------
    # Test stage mapping
    # --------------------------------
    if "stage('Test')" in jenkinsfile_content or 'mvn test' in jenkinsfile_content or 'dotnet test' in jenkinsfile_content:
        if stack == 'dotnet':
            steps.append({
                'name': 'Run tests',
                'run': 'dotnet test --configuration Release'
            })
        else:
            steps.append({
                'name': 'Run tests',
                'run': 'mvn test'
            })

    # --------------------------------
    # Deploy stage mapping (unchanged)
    # --------------------------------
    if "stage('Deploy')" in jenkinsfile_content or 'scp' in jenkinsfile_content:
        # Keep the original SCP example for simplicity
        steps.append({
            'name': 'Deploy the project',
            'run': 'scp target/myapp.war user@server:/path/to/deploy' if stack != 'dotnet' else 'echo "Add your .NET deploy command here (e.g., az webapp deploy, dotnet publish + rsync/scp)"'
        })

    return pipeline
    
def convert_jenkinsfile_to_github_actions(jenkinsfile_path, output_dir):
    """
    Converts the Jenkinsfile into a GitHub Actions YAML workflow with all steps in a single job.
    """
    # Read the Jenkinsfile
    with open(jenkinsfile_path, 'r') as jenkinsfile:
        jenkinsfile_content = jenkinsfile.read()
    
    # Parse the Jenkinsfile and convert it to GitHub Actions format
    github_actions_yaml = parse_jenkinsfile(jenkinsfile_content)
    
    # Ensure the output directory exists
    workflow_dir = os.path.join(output_dir, 'workflows')
    os.makedirs(workflow_dir, exist_ok=True)  # Ensure the directory exists
    
    # Define the output path for the GitHub Actions YAML file
    output_file_path = os.path.join(workflow_dir, 'ci-workflow.yml')
    
    # Write the YAML file using ruamel.yaml
    yaml = YAML()
    yaml.default_flow_style = False
    with open(output_file_path, 'w') as yaml_file:
        yaml.dump(github_actions_yaml, yaml_file)
    
    print(f"GitHub Actions workflow generated and saved to {output_file_path}")

def main():
    # Define paths
    jenkinsfile_path = 'Jenkinsfile'  # Adjust this path if your Jenkinsfile has a different name or location
    output_dir = '.github'  # This will create the '.github/workflows' directory

    # Check if Jenkinsfile exists
    if not os.path.exists(jenkinsfile_path):
        print(f"Jenkinsfile not found at {jenkinsfile_path}")
        return

    # Convert the Jenkinsfile to GitHub Actions workflow and save it in the .github directory
    convert_jenkinsfile_to_github_actions(jenkinsfile_path, output_dir)

if __name__ == "__main__":
    main()
