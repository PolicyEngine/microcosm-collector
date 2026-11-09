"""The collector validates every change and deploys only once deployment is enabled."""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
VALIDATION_WORKFLOW = WORKFLOWS / "ci.yml"
PULL_REQUEST_WORKFLOW = WORKFLOWS / "pr.yaml"
PUSH_WORKFLOW = WORKFLOWS / "push.yaml"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"
DEPLOY_GATE = "vars.COLLECTOR_DEPLOY_ENABLED == 'true'"
# The Workload Identity provider accepts this repo's tokens only when
# assertion.ref == 'refs/heads/main', so jobs that authenticate check it first.
MAIN_PUSH_GUARD = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
DEPLOY_CONDITION = f"{MAIN_PUSH_GUARD} && {DEPLOY_GATE}"
DEPLOY_JOBS = {"deploy-staging", "deploy-production"}
# The job that publishes the read API client holds an OIDC token for npm's
# trusted publishing, in the npm environment, and never authenticates to Google.
PUBLISH_CLIENT_JOB = "publish-client"
DEPLOYMENT_ENVIRONMENTS = {"staging", "production"}
# The Workload Identity provider's condition once it also requires a deployment
# environment, so that a token from a job in any other environment, such as
# publish-client's npm, cannot obtain Google credentials.
PROVIDER_CONDITION = (
    "assertion.repository=='PolicyEngine/microcosm-collector'"
    " && assertion.ref=='refs/heads/main'"
    " && assertion.environment in ['staging', 'production']"
)
README = ROOT / "README.md"
CLIENT_DIRECTORY = "packages/collector-client"
WORKLOAD_IDENTITY_SECRETS = {
    "TELEMETRY_GCP_WIF_PROVIDER",
    "TELEMETRY_GCP_DEPLOY_SERVICE_ACCOUNT",
}
# npm's trusted publishing takes this job's GitHub OIDC token; it never
# authenticates to Google Cloud (tests/test_calibration_tree_package.py).
NPM_PUBLISH_JOB = ("publish-calibration-tree.yaml", "publish")
CALIBRATION_TREE_PACKAGE = Path("packages") / "calibration-tree"


def load_workflow(workflow_path: Path) -> dict:
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def workflow_paths() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.y*ml"))


def copied_unchanged_package_files() -> set[Path]:
    provenance = json.loads(
        (ROOT / CALIBRATION_TREE_PACKAGE / "provenance.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        ROOT / CALIBRATION_TREE_PACKAGE / copied_file
        for copied_file in provenance["files"]
        if copied_file not in provenance["changed"]
    }


def authenticates_to_google_cloud(job: dict) -> bool:
    return any(
        step.get("uses", "").startswith("google-github-actions/auth@")
        for step in job.get("steps", [])
    )


def holds_oidc_token(job: dict) -> bool:
    return job.get("permissions", {}).get("id-token") == "write"


def push_job_text(job_name: str, next_job_name: str | None) -> str:
    workflow = PUSH_WORKFLOW.read_text(encoding="utf-8")
    assert f"  {job_name}:" in workflow, job_name
    if next_job_name:
        assert f"  {next_job_name}:" in workflow, next_job_name
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
        assert jobs[job_name]["if"] == DEPLOY_CONDITION, job_name
    environment_jobs = {
        (workflow_path.name, job_name): job["environment"]
        for workflow_path in workflow_paths()
        for job_name, job in load_workflow(workflow_path)["jobs"].items()
        if "environment" in job
    }
    assert environment_jobs == {
        ("push.yaml", "deploy-staging"): "staging",
        ("push.yaml", "deploy-production"): "production",
        ("push.yaml", PUBLISH_CLIENT_JOB): "npm",
        NPM_PUBLISH_JOB: "npm",
    }


def test_google_cloud_authentication_runs_only_on_pushes_to_main() -> None:
    def jobs_where(condition) -> dict:
        return {
            (workflow_path.name, job_name): (workflow, job)
            for workflow_path in workflow_paths()
            for workflow in [load_workflow(workflow_path)]
            for job_name, job in workflow["jobs"].items()
            if condition(job)
        }

    authenticating_jobs = jobs_where(authenticates_to_google_cloud)
    oidc_jobs = jobs_where(holds_oidc_token)
    # The calibration tree package's publish job can also be run by hand from
    # main, to retry a publication; tests/test_calibration_tree_package.py
    # checks its triggers and its main-only condition.
    push_only_oidc_jobs = {
        job_key: workflow_and_job
        for job_key, workflow_and_job in oidc_jobs.items()
        if job_key != NPM_PUBLISH_JOB
    }

    # Only the release pass of push.yaml reaches these jobs, and pull requests,
    # tags and reusable calls can never run them.
    assert set(authenticating_jobs) == {
        ("push.yaml", "deploy-staging"),
        ("push.yaml", "deploy-production"),
    }
    assert set(oidc_jobs) == set(authenticating_jobs) | {
        ("push.yaml", PUBLISH_CLIENT_JOB),
        NPM_PUBLISH_JOB,
    }
    for (workflow_name, job_name), (workflow, job) in push_only_oidc_jobs.items():
        assert workflow["on"] == {"push": {"branches": ["main"]}}, workflow_name
        assert job["if"].startswith(f"{MAIN_PUSH_GUARD} && "), job_name


def test_deployment_uses_main_only_staging_then_production_environments() -> None:
    workflow = load_workflow(PUSH_WORKFLOW)
    jobs = workflow["jobs"]

    assert workflow["on"] == {"push": {"branches": ["main"]}}
    assert jobs["deploy-staging"]["environment"] == "staging"
    assert jobs["deploy-production"]["environment"] == "production"
    assert jobs["deploy-staging"]["needs"] == "tag"
    assert jobs["deploy-production"]["needs"] == "deploy-staging"
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    for job_name in DEPLOY_JOBS:
        assert jobs[job_name]["concurrency"]["cancel-in-progress"] == "false"


def test_oidc_tokens_and_workload_identity_secrets_stay_with_their_jobs() -> None:
    for workflow_path in workflow_paths():
        workflow = load_workflow(workflow_path)
        assert "id-token" not in workflow.get("permissions", {}), workflow_path.name
        oidc_jobs = {
            job_name
            for job_name, job in workflow["jobs"].items()
            if job.get("permissions", {}).get("id-token") == "write"
        }
        expected_jobs = (
            DEPLOY_JOBS | {PUBLISH_CLIENT_JOB}
            if workflow_path == PUSH_WORKFLOW
            else set()
        )
        if workflow_path.name == NPM_PUBLISH_JOB[0]:
            expected_jobs = {NPM_PUBLISH_JOB[1]}
        assert oidc_jobs == expected_jobs, workflow_path.name
    assert NPM_PUBLISH_JOB[0] in {path.name for path in workflow_paths()}

    jobs = load_workflow(PUSH_WORKFLOW)["jobs"]
    for job_name in DEPLOY_JOBS:
        job_text = yaml.safe_dump(jobs[job_name])
        assert set(re.findall(r"secrets\.(\w+)", job_text)) == (
            WORKLOAD_IDENTITY_SECRETS
        )
    publish_text = yaml.safe_dump(jobs[PUBLISH_CLIENT_JOB])
    assert re.findall(r"secrets\.(\w+)", publish_text) == []
    assert "NODE_AUTH_TOKEN" not in publish_text


def readme_section(heading: str) -> str:
    readme = README.read_text(encoding="utf-8")
    assert f"\n{heading}\n" in readme, heading
    section = readme.split(f"\n{heading}\n", 1)[1]
    return re.split(r"\n#{2,3} ", section, maxsplit=1)[0]


def test_publishing_waits_for_the_provider_to_check_the_environment() -> None:
    jobs = load_workflow(PUSH_WORKFLOW)["jobs"]
    google_access = readme_section("### Google Cloud access")
    client_section = readme_section("### The TypeScript client")

    assert {jobs[job_name]["environment"] for job_name in DEPLOY_JOBS} == (
        DEPLOYMENT_ENVIRONMENTS
    )
    assert jobs[PUBLISH_CLIENT_JOB]["environment"] not in DEPLOYMENT_ENVIRONMENTS
    assert PROVIDER_CONDITION in google_access
    assert "No other job requests an OIDC token" not in google_access
    # The manual steps change the provider's condition before publishing is
    # switched on.
    assert "**Switch.**" in client_section
    assert PROVIDER_CONDITION in client_section
    assert client_section.index(PROVIDER_CONDITION) < client_section.index(
        "**Switch.**"
    )


def test_tree_publishing_waits_for_the_provider_to_check_the_environment() -> None:
    workflow_name, job_name = NPM_PUBLISH_JOB
    tree_publish_job = load_workflow(WORKFLOWS / workflow_name)["jobs"][job_name]
    google_access = readme_section("### Google Cloud access")
    tree_publishing = readme_section("### Publishing to npm")

    # The calibration tree package's publish job also holds an OIDC token
    # outside the deployment environments, so until the provider checks the
    # environment its token would pass the condition as publish-client's does.
    assert tree_publish_job["environment"] not in DEPLOYMENT_ENVIRONMENTS
    assert f"`{PUBLISH_CLIENT_JOB}`" in google_access
    assert f"`{workflow_name}`" in google_access
    # The package's one-time setup changes the provider's condition before its
    # publishing is switched on.
    assert "**The switch.**" in tree_publishing
    assert PROVIDER_CONDITION in tree_publishing
    assert tree_publishing.index(PROVIDER_CONDITION) < tree_publishing.index(
        "**The switch.**"
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
    assert set(required["needs"]) == {
        "lint-workflows",
        "changelog",
        "verify",
        "calibration-tree",
    }
    assert required["steps"][-1]["run"] == (
        'test "$RESULTS" = "success success success success"'
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


def test_validation_tests_and_packs_the_read_api_client() -> None:
    validation_jobs = load_workflow(VALIDATION_WORKFLOW)["jobs"]
    assert "client" in validation_jobs
    client = validation_jobs["client"]
    commands = [step["run"] for step in client["steps"] if "run" in step]
    push_jobs = load_workflow(PUSH_WORKFLOW)["jobs"]
    assert "pack-client" in push_jobs
    release_pack_commands = [
        step["run"]
        for step in push_jobs["pack-client"]["steps"]
        if step.get("run", "").startswith("npm pack")
    ]
    setup_node = next(
        step
        for step in client["steps"]
        if step.get("uses", "").startswith("actions/setup-node@")
    )

    assert client["defaults"]["run"]["working-directory"] == CLIENT_DIRECTORY
    assert setup_node["with"]["node-version"] == "24.22.1"
    assert setup_node["with"]["package-manager-cache"] == "false"
    assert commands == [
        "npm ci",
        "npm run generate",
        "git diff --exit-code -- src/schema.ts",
        "npm run typecheck",
        "npm test",
        'npm pack --pack-destination "$RUNNER_TEMP"',
    ]
    # Every pull request packs the client exactly as a release does, writing
    # the tarball, so a pack command that cannot work fails CI.
    assert release_pack_commands == [commands[-1]]


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
    production_job = push_job_text("deploy-production", "pack-client")

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
        "INTAKE_BUCKET": "TELEMETRY_INTAKE_BUCKET",
        "WORKER_QUEUE": "TELEMETRY_WORKER_QUEUE",
        "WORKER_JOB": "TELEMETRY_WORKER_JOB",
        "RECOVERY_JOB": "TELEMETRY_RECOVERY_JOB",
        "WORKER_SERVICE_ACCOUNT": "TELEMETRY_WORKER_SERVICE_ACCOUNT",
        "WORKER_INVOKER_SERVICE_ACCOUNT": "TELEMETRY_WORKER_INVOKER_SERVICE_ACCOUNT",
        "RECOVERY_SERVICE_ACCOUNT": "TELEMETRY_RECOVERY_SERVICE_ACCOUNT",
    }
    for environment_name, github_variable_name in expected_variables.items():
        assert (
            workflow.count(
                f"{environment_name}: ${{{{ vars.{github_variable_name} }}}}"
            )
            == 2
        ), environment_name


def test_release_switches_intake_on_without_a_per_environment_switch() -> None:
    jobs = load_workflow(PUSH_WORKFLOW)["jobs"]

    # Every release switches intake on in both environments together; the
    # deploy script's tests check the setting each revision gets.
    assert "TELEMETRY_INTAKE_ENABLED" not in PUSH_WORKFLOW.read_text(encoding="utf-8")
    for job_name in DEPLOY_JOBS:
        assert "INTAKE_ENABLED" not in jobs[job_name]["env"], job_name


def test_both_deploy_jobs_read_their_hugging_face_read_token_secret_name() -> None:
    jobs = load_workflow(PUSH_WORKFLOW)["jobs"]
    mapping = "${{ vars.TELEMETRY_HF_READ_SECRET_NAME }}"

    # Each environment names its own secret, which only its worker may read.
    for job_name in DEPLOY_JOBS:
        assert jobs[job_name]["env"].get("HF_READ_SECRET_NAME") == mapping, job_name
    assert PUSH_WORKFLOW.read_text(encoding="utf-8").count(mapping) == 2


def test_both_deploy_jobs_read_their_calibration_tree_blob_secret_name() -> None:
    jobs = load_workflow(PUSH_WORKFLOW)["jobs"]
    mapping = "${{ vars.TELEMETRY_CALIBRATION_TREE_BLOB_SECRET_NAME }}"

    # Each environment names its own store's token, which only its worker reads.
    for job_name in DEPLOY_JOBS:
        assert (
            jobs[job_name]["env"].get("CALIBRATION_TREE_BLOB_SECRET_NAME") == mapping
        ), job_name
    assert PUSH_WORKFLOW.read_text(encoding="utf-8").count(mapping) == 2


def deploy_step(job: dict, command: str) -> dict:
    matching_steps = [
        step
        for step in job["steps"]
        if step.get("run") == f"scripts/deploy.sh {command}"
    ]
    assert len(matching_steps) == 1, (command, len(matching_steps))
    return matching_steps[0]


def step_position(job: dict, command: str) -> int:
    return job["steps"].index(deploy_step(job, command))


@pytest.mark.parametrize(
    ("job_name", "image_source"),
    [
        ("deploy-staging", "${{ steps.image.outputs.uri }}"),
        ("deploy-production", "${{ needs.deploy-staging.outputs.image_uri }}"),
    ],
)
def test_release_updates_both_intake_jobs_between_migration_and_candidate(
    job_name: str, image_source: str
) -> None:
    job = load_workflow(PUSH_WORKFLOW)["jobs"][job_name]

    # Both jobs are checked before anything changes, and updated to
    # the released image once the database has its schema, before a revision
    # that can dispatch to them is deployed.
    assert step_position(job, "check-intake-jobs") < step_position(
        job, "deploy-maintenance"
    )
    for command in ("configure-worker-job", "configure-recovery-job"):
        assert step_position(job, "apply-migrations") < step_position(job, command)
        assert step_position(job, command) < step_position(job, "deploy-candidate")
        assert deploy_step(job, command)["env"] == {"IMAGE_URI": image_source}
    assert deploy_step(job, "deploy-candidate")["env"] == {"IMAGE_URI": image_source}


def test_collector_uses_store_vocabulary() -> None:
    forbidden_term = "repo" + "sitory"
    # GitHub's expression context and the OIDC claim and attribute names in
    # Workload Identity conditions use the word; the collector's own words do not.
    external_names = [
        f"{prefix}.{forbidden_term}" for prefix in ("github", "assertion", "attribute")
    ]
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
    # node_modules holds installed npm packages, such as the Orrery validator's.
    ignored_parts = {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
    # Files of the calibration tree package that provenance.json records as
    # copied unchanged from calibration-diagnostics keep that repo's words:
    # src/provenance.test.ts requires their recorded blob IDs, and a fixture
    # generator writes the word as a key of the fixture it reproduces.
    copied_unchanged = copied_unchanged_package_files()
    source_files = [
        source_path
        for source_path in ROOT.rglob("*")
        if source_path.is_file()
        and source_path.suffix in source_suffixes
        and not ignored_parts.intersection(source_path.relative_to(ROOT).parts)
        and source_path not in copied_unchanged
    ]

    def own_words(source_path: Path) -> str:
        source_text = source_path.read_text(encoding="utf-8").lower()
        for external_name in external_names:
            source_text = source_text.replace(external_name, "")
        return source_text

    matches = [
        str(source_path.relative_to(ROOT))
        for source_path in source_files
        if forbidden_term in own_words(source_path)
        or forbidden_term in source_path.name.lower()
    ]

    assert WORKFLOWS / "push.yaml" in source_files
    assert ROOT / CALIBRATION_TREE_PACKAGE / "README.md" in source_files
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
    assert workflow.count("run: scripts/deploy.sh") == 34
    for command in (
        "require-configuration",
        "validate-environment-target",
        "check-secrets-exist",
        "check-intake-jobs",
        "configure-worker-job",
        "configure-recovery-job",
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
