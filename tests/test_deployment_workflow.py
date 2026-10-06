from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "telemetry-service.yml"
DOCKERFILE = ROOT / "telemetry-service" / "Dockerfile"
DEPLOY_SCRIPT = ROOT / "telemetry-service" / "scripts" / "deploy.sh"


def test_service_workflow_is_path_scoped_and_uses_an_isolated_build_context() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert workflow.count("- telemetry-service/**") == 2
    assert "branches: [main]" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "working-directory: telemetry-service" in workflow
    assert "docker build \\" in deploy_script
    assert "--file telemetry-service/Dockerfile \\" in deploy_script
    assert '--tag "$image" \\' in deploy_script
    assert "telemetry-service" in deploy_script
    assert (
        'docker build --file telemetry-service/Dockerfile --tag "$image" .'
        not in deploy_script
    )


def test_service_deployment_uses_oidc_and_verifies_before_promotion() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "id-token: write" in workflow
    assert "uses: google-github-actions/auth@v2" in workflow
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


def test_service_deployment_qualifies_staging_before_production() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    staging_start = workflow.index("  deploy-staging:")
    production_start = workflow.index("  deploy-production:")
    staging_job = workflow[staging_start:production_start]
    production_job = workflow[production_start:]

    assert "environment: staging" in staging_job
    assert "image_uri: ${{ steps.image.outputs.uri }}" in staging_job
    assert "build-and-push-image" in staging_job
    assert "qualify-staging.sh" in staging_job
    assert "HF_TOKEN: ${{ secrets.HF_TOKEN }}" in staging_job
    assert (
        "STAGING_READ_TOKEN: "
        "${{ secrets.MICROCOSM_TELEMETRY_COLLECTOR_READ_TOKEN }}" in staging_job
    )

    assert "needs: deploy-staging" in production_job
    assert "environment: Production" in production_job
    assert "needs.deploy-staging.outputs.image_uri" in production_job
    assert "build-and-push-image" not in production_job
    assert "create-database-backup" in production_job


def test_service_deployment_reads_resource_names_from_environment_variables() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    expected_variables = {
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
        assert f"{environment_name}: ${{{{ vars.{github_variable_name} }}}}" in workflow


def test_service_workflow_hard_cuts_over_before_migration() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "image: postgres:16-alpine" in workflow
    assert "TEST_DATABASE_URL:" in workflow
    assert "uv run alembic check" in workflow
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


def test_service_workflow_has_no_multiline_run_blocks() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "run: |" not in workflow
    assert "run: >" not in workflow
    assert workflow.count("run: telemetry-service/scripts/deploy.sh") == 26

    for command in (
        "require-configuration",
        "validate-environment-target",
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
        assert f"run: telemetry-service/scripts/deploy.sh {command}" in workflow

    assert "run: telemetry-service/scripts/qualify-staging.sh" in workflow


def test_service_image_contains_alembic_runtime_files() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
