"""The collector validates every change and deploys only once deployment is enabled."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
VALIDATION_WORKFLOW = WORKFLOWS / "ci.yml"
PULL_REQUEST_WORKFLOW = WORKFLOWS / "pr.yaml"
PUSH_WORKFLOW = WORKFLOWS / "push.yaml"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"
DEPLOY_GATE = "vars.COLLECTOR_DEPLOY_ENABLED == 'true'"
DEPLOY_JOBS = {"deploy-staging", "deploy-production"}
WORKLOAD_IDENTITY_SECRETS = {
    "TELEMETRY_GCP_WIF_PROVIDER",
    "TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT",
}


def load_workflow(workflow_path: Path) -> dict:
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def workflow_paths() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.y*ml"))


def push_job_text(job_name: str, next_job_name: str | None) -> str:
    workflow = PUSH_WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index(f"  {job_name}:")
    end = workflow.index(f"  {next_job_name}:") if next_job_name else len(workflow)
    return workflow[start:end]


def test_service_image_builds_from_the_collector_root() -> None:
    deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "docker build \\" in deploy_script
    assert "--file Dockerfile \\" in deploy_script
    assert '--tag "$image" \\' in deploy_script
    assert re.search(r'--tag "\$image" \\\n\s+\.\n', deploy_script)
    assert "telemetry-service" not in deploy_script


def test_deployment_runs_only_when_collector_deploy_is_enabled() -> None:
    jobs = load_workflow(PUSH_WORKFLOW)["jobs"]

    for job_name in DEPLOY_JOBS:
        assert jobs[job_name]["if"] == DEPLOY_GATE, job_name
    for workflow_path in workflow_paths():
        for job_name, job in load_workflow(workflow_path)["jobs"].items():
            if "environment" in job:
                assert job_name in DEPLOY_JOBS, (workflow_path.name, job_name)


def test_deployment_uses_main_only_staging_then_production_environments() -> None:
    workflow = load_workflow(PUSH_WORKFLOW)
    jobs = workflow["jobs"]

    assert workflow["on"] == {"push": {"branches": ["main"]}}
    assert jobs["deploy-staging"]["environment"] == "staging"
    assert jobs["deploy-production"]["environment"] == "production"
    assert jobs["deploy-staging"]["needs"] == "verify"
    assert jobs["deploy-production"]["needs"] == "deploy-staging"
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    for job_name in DEPLOY_JOBS:
        assert jobs[job_name]["concurrency"]["cancel-in-progress"] == "false"


def test_only_deployment_jobs_hold_oidc_tokens_and_workload_identity_secrets() -> None:
    for workflow_path in workflow_paths():
        workflow = load_workflow(workflow_path)
        assert "id-token" not in workflow.get("permissions", {}), workflow_path.name
        oidc_jobs = {
            job_name
            for job_name, job in workflow["jobs"].items()
            if job.get("permissions", {}).get("id-token") == "write"
        }
        expected_jobs = DEPLOY_JOBS if workflow_path == PUSH_WORKFLOW else set()
        assert oidc_jobs == expected_jobs, workflow_path.name

    jobs = load_workflow(PUSH_WORKFLOW)["jobs"]
    for job_name in DEPLOY_JOBS:
        job_text = yaml.safe_dump(jobs[job_name])
        assert set(re.findall(r"secrets\.(\w+)", job_text)) == (
            WORKLOAD_IDENTITY_SECRETS
        )


def test_pull_requests_report_one_required_check() -> None:
    workflow = load_workflow(PULL_REQUEST_WORKFLOW)
    jobs = workflow["jobs"]

    assert set(workflow["on"]) == {"pull_request"}
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert "actionlint" in jobs["lint-workflows"]["steps"][-1]["uses"]
    required = jobs["required"]
    assert required["name"] == "Required checks"
    assert required["if"] == "always()"
    assert set(required["needs"]) == {"lint-workflows", "changelog", "verify"}
    assert required["steps"][-1]["run"] == (
        'test "$RESULTS" = "success success success"'
    )


def test_validation_is_reusable_and_runs_against_postgres() -> None:
    workflow = load_workflow(VALIDATION_WORKFLOW)
    validate = workflow["jobs"]["validate"]
    commands = [step["run"] for step in validate["steps"] if "run" in step]

    assert set(workflow["on"]) == {"workflow_call"}
    assert validate["services"]["postgres"]["image"] == "postgres:16-alpine"
    assert validate["env"]["TEST_DATABASE_URL"].endswith("/telemetry_test")
    assert "uv run pytest -q" in commands
    assert 'DATABASE_URL="$TEST_DATABASE_URL" uv run alembic check' in commands
    assert "docker build --tag microcosm-collector:validation ." in commands
    assert "defaults" not in validate


def test_service_deployment_uses_oidc_and_verifies_before_promotion() -> None:
    workflow = PUSH_WORKFLOW.read_text(encoding="utf-8")
    deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "id-token: write" in workflow
    assert "uses: google-github-actions/auth@" in workflow
    assert "--default-url" in deploy_script
    assert "httpGet.path=/health" in deploy_script
    assert '"${url}/ready"' in deploy_script
    assert '"${stable_url}/ready"' in deploy_script
    assert "--no-traffic" in deploy_script
    assert "TELEMETRY_MAINTENANCE_MODE=1" in deploy_script
    assert "TELEMETRY_MAINTENANCE_MODE=0" in deploy_script
    assert '"${url}/health"' in deploy_script
    assert '"${url}/v1/auth/huggingface/exchange"' in deploy_script
    assert workflow.index("Resolve and verify the candidate revision") < workflow.index(
        "Promote the verified revision"
    )
    assert "Restore the previous revision" not in workflow


def test_service_deployment_checks_secret_existence_before_changing_traffic() -> None:
    staging_job = push_job_text("deploy-staging", "deploy-production")
    production_job = push_job_text("deploy-production", None)

    assert "environment: staging" in staging_job
    assert "image_uri: ${{ steps.image.outputs.uri }}" in staging_job
    assert "build-and-push-image" in staging_job

    assert "needs: deploy-staging" in production_job
    assert "environment: production" in production_job
    assert "needs.deploy-staging.outputs.image_uri" in production_job
    assert "build-and-push-image" not in production_job
    assert "create-database-backup" in production_job
    for job in (staging_job, production_job):
        assert job.index("google-github-actions/setup-gcloud@") < job.index(
            "check-secrets-exist"
        )
        assert job.index("check-secrets-exist") < job.index("deploy-maintenance")
        assert job.index("check-secrets-exist") < job.index("apply-migrations")


def test_service_deployment_reads_resource_names_from_environment_variables() -> None:
    workflow = PUSH_WORKFLOW.read_text(encoding="utf-8")

    expected_variables = {
        "PROJECT_ID": "TELEMETRY_GCP_PROJECT_ID",
        "REGION": "TELEMETRY_GCP_REGION",
        "ARTIFACT_REGISTRY_NAME": "TELEMETRY_ARTIFACT_REGISTRY_NAME",
        "CLOUD_SQL_CONNECTION": "TELEMETRY_CLOUD_SQL_CONNECTION",
        "RUNTIME_SERVICE_ACCOUNT": "TELEMETRY_RUNTIME_SERVICE_ACCOUNT",
        "SERVICE": "TELEMETRY_SERVICE",
        "MIGRATION_JOB": "TELEMETRY_MIGRATION_JOB",
        "IMAGE_NAME": "TELEMETRY_IMAGE_NAME",
        "DATABASE_SECRET_NAME": "TELEMETRY_DATABASE_SECRET_NAME",
        "JWT_SECRET_NAME": "TELEMETRY_JWT_SECRET_NAME",
        "READ_SECRET_NAME": "TELEMETRY_READ_SECRET_NAME",
        "MIGRATION_SERVICE_ACCOUNT": "TELEMETRY_MIGRATION_SERVICE_ACCOUNT",
        "MIGRATION_DATABASE_SECRET_NAME": ("TELEMETRY_MIGRATION_DATABASE_SECRET_NAME"),
        "PRODUCTION_CLOUD_SQL_CONNECTION": (
            "TELEMETRY_PRODUCTION_CLOUD_SQL_CONNECTION"
        ),
        "PRODUCTION_SERVICE": "TELEMETRY_PRODUCTION_SERVICE",
    }
    for environment_name, github_variable_name in expected_variables.items():
        assert (
            workflow.count(
                f"{environment_name}: ${{{{ vars.{github_variable_name} }}}}"
            )
            == 2
        )


def test_collector_uses_store_vocabulary() -> None:
    forbidden_term = "repo" + "sitory"
    # GitHub names this expression context; the collector's own words do not use it.
    github_context = "github." + forbidden_term
    source_suffixes = {
        ".env",
        ".ini",
        ".mako",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
    }
    ignored_parts = {".git", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
    source_files = [
        source_path
        for source_path in ROOT.rglob("*")
        if source_path.is_file()
        and source_path.suffix in source_suffixes
        and not ignored_parts.intersection(source_path.relative_to(ROOT).parts)
    ]

    matches = [
        str(source_path.relative_to(ROOT))
        for source_path in source_files
        if forbidden_term
        in source_path.read_text(encoding="utf-8").lower().replace(github_context, "")
        or forbidden_term in source_path.name.lower()
    ]

    assert WORKFLOWS / "push.yaml" in source_files
    assert matches == []


def test_service_workflow_hard_cuts_over_before_migration() -> None:
    workflow = PUSH_WORKFLOW.read_text(encoding="utf-8")
    deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "--set-cloudsql-instances" in deploy_script
    assert '"run,--no-sync,python,-m,telemetry_collector.migrate"' in deploy_script
    assert "--max-retries 0" in deploy_script
    assert "--wait" in deploy_script
    assert "--max 4" in deploy_script
    assert workflow.index("Route traffic to maintenance mode") < workflow.index(
        "Apply database migrations"
    )
    assert workflow.index("Wait for old requests to finish") < workflow.index(
        "Apply database migrations"
    )
    assert workflow.index("Apply database migrations") < workflow.index(
        "Deploy an isolated candidate revision"
    )
    assert workflow.count("steps.image.outputs.uri") >= 2


def test_workflow_shell_logic_lives_in_scripts() -> None:
    for workflow_path in workflow_paths():
        for job in load_workflow(workflow_path)["jobs"].values():
            for step in job.get("steps", []):
                if "run" in step:
                    assert "\n" not in step["run"].strip(), (workflow_path.name, step)

    workflow = PUSH_WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("run: scripts/deploy.sh") == 28
    for command in (
        "require-configuration",
        "validate-environment-target",
        "check-secrets-exist",
        "build-and-push-image",
        "deploy-maintenance",
        "resolve-and-verify-maintenance",
        "wait-for-cutover",
        "configure-migration-job",
        "apply-migrations",
        "deploy-candidate",
        "resolve-and-verify-candidate",
        "route-service-traffic",
        "verify-stable-service",
        "create-database-backup",
    ):
        assert f"run: scripts/deploy.sh {command}" in workflow


def test_service_image_contains_alembic_runtime_files() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
