import re
from ruamel.yaml import YAML

def extract_block(content, start_pattern, end_pattern=None):
    """Extracts a block from content given a starting keyword and optional end pattern."""
    pattern = re.compile(start_pattern, re.MULTILINE)
    match = pattern.search(content)
    if not match:
        return None
    start = match.end()
    if end_pattern:
        end = re.search(end_pattern, content[start:])
        end_idx = end.start() + start if end else len(content)
        return content[start:end_idx].strip()
    else:
        return content[start:].strip()

def parse_env_block(env_block):
    """Parse environment block into a dictionary."""
    env = {}
    for line in env_block.split('\n'):
        line = line.strip()
        if '=' in line:
            key, value = line.split('=', 1)
            env[key.strip()] = value.strip().strip('\'"')
    return env

def parse_stage_blocks(jenkinsfile):
    """Extract each stage name and its steps (very basic)."""
    stages = []
    stage_pattern = re.compile(r"stage\s*\(\s*['\"](.+?)['\"]\s*\)\s*\{([\s\S]+?)}", re.MULTILINE)
    for match in stage_pattern.finditer(jenkinsfile):
        name = match.group(1)
        body = match.group(2)
        steps = []
        steps_block_match = re.search(r"steps\s*\{([\s\S]+?)}", body)
        if steps_block_match:
            steps_block = steps_block_match.group(1)
            for cmd in re.findall(r"(echo\s+['\"].+?['\"]|sh\s+['\"].+?['\"]|junit\s+['\"].+?['\"])", steps_block):
                steps.append(cmd.strip())
        stages.append({'name': name, 'steps': steps, 'raw': body})
    return stages

def parse_post_block(jenkinsfile):
    post_block = extract_block(jenkinsfile, r'post\s*\{', r'\}')
    if not post_block:
        return []
    steps = []
    for line in post_block.split('\n'):
        line = line.strip()
        if line.startswith('echo '):
            steps.append(line)
    return steps

def jenkinsfile_to_github_actions(jenkinsfile_content):
    # 1. Agent
    agent_match = re.search(r"agent\s*\{\s*label\s+['\"](.+?)['\"]\s*\}", jenkinsfile_content)
    runner = agent_match.group(1) if agent_match else 'ubuntu-latest'
    # 2. Environment
    env_block = extract_block(jenkinsfile_content, r'environment\s*\{', r'\}')
    env = parse_env_block(env_block) if env_block else {}
    # 3. Stages & Steps
    stages = parse_stage_blocks(jenkinsfile_content)
    # 4. Post
    post_steps = parse_post_block(jenkinsfile_content)
    # 5. Build Github Actions Workflow
    workflow = {
        'name': 'CI Workflow (Converted)',
        'on': {'push': {'branches': ['main']}},
        'jobs': {
            'build': {
                'runs-on': runner,
                'env': env,
                'steps': []
            }
        }
    }
    steps = workflow['jobs']['build']['steps']
    for stage in stages:
        for step in stage['steps']:
            if step.startswith('echo'):
                steps.append({'name': f"{stage['name']} - echo", 'run': step[5:].strip(' "\'')})
            elif step.startswith('sh'):
                steps.append({'name': f"{stage['name']} - shell", 'run': step[3:].strip(' "\'')})
            elif step.startswith('junit'):
                # Map to actions/upload-artifact or actions/upload-test-results
                steps.append({'name': f"{stage['name']} - upload junit", 
                              'uses': 'actions/upload-artifact@v3',
                              'with': {'name': 'junit-results', 'path': step[6:].strip(' "\'')}})
            else:
                steps.append({'name': f"{stage['name']} - unmapped", 'run': step})
    # Post block (as always steps at the end)
    for post_step in post_steps:
        steps.append({'name': 'Post - echo', 'run': post_step[5:].strip(' "\''), 'if': 'always()'})
    return workflow

# Example usage:
if __name__ == "__main__":
    with open("Jenkinsfile") as f:
        content = f.read()
    gha = jenkinsfile_to_github_actions(content)
    print(yaml.dump(gha, sort_keys=False))
