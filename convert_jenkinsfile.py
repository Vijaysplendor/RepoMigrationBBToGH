import os
from ruamel.yaml import YAML

def parse_jenkinsfile(jenkinsfile_content):
    """
    Parses a Jenkinsfile content and converts it into a structured dictionary for GitHub Actions.
    """
    pipeline = {
        'name': 'CI Workflow',
        'on': {
            'push': {
                'branches': ['main']  # Triggers the workflow on push events to the 'main' branch
            }
        },
        'jobs': {
            'ci': {
                'runs-on': 'ubuntu-latest',
                'steps': []
            }
        }
    }
    
    # Check for stages and add steps accordingly
    if 'stage(\'Checkout\')' in jenkinsfile_content or 'checkout scm' in jenkinsfile_content:
        pipeline['jobs']['ci']['steps'].append({
            'name': 'Checkout code',
            'uses': 'actions/checkout@v2'
        })
    
    if 'stage(\'Build\')' in jenkinsfile_content or 'mvn clean compile' in jenkinsfile_content:
        pipeline['jobs']['ci']['steps'].append({
            'name': 'Build the project',
            'run': 'mvn clean compile'
        })
    
    if 'stage(\'Test\')' in jenkinsfile_content or 'mvn test' in jenkinsfile_content:
        pipeline['jobs']['ci']['steps'].append({
            'name': 'Run tests',
            'run': 'mvn test'
        })
    
    if 'stage(\'Deploy\')' in jenkinsfile_content or 'scp' in jenkinsfile_content:
        pipeline['jobs']['ci']['steps'].append({
            'name': 'Deploy the project',
            'run': 'scp target/myapp.war user@server:/path/to/deploy'
        })
    
    # Add JDK setup if Java-related stages are present
    if 'mvn' in jenkinsfile_content:
        pipeline['jobs']['ci']['steps'].insert(0, {
            'name': 'Set up JDK 11',
            'uses': 'actions/setup-java@v2',
            'with': {
                'java-version': '11'
            }
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
    os.makedirs(workflow_dir, exist_ok=True)
    
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
