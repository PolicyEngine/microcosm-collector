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
    assert workflow.index("Resolve and verify the candidate revision") < workflow.index(
        "Promote the verified revision"
    )
    assert "Restore the previous revision after failed stable verification" in workflow


def test_service_workflow_runs_postgres_tests_and_migrates_before_deploy() -> None:
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
    assert workflow.index("Apply database migrations") < workflow.index(
        "Deploy an isolated candidate revision"
    )
    assert workflow.count("steps.image.outputs.uri") >= 2


def test_service_workflow_has_no_multiline_run_blocks() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "run: |" not in workflow
    assert "run: >" not in workflow
    assert workflow.count("run: telemetry-service/scripts/deploy.sh") == 10

    for command in (
        "require-configuration",
        "build-and-push-image",
        "capture-production-revision",
        "configure-migration-job",
        "apply-migrations",
        "deploy-candidate",
        "resolve-and-verify-candidate",
        "route-production-traffic",
        "verify-stable-service",
    ):
        assert f"run: telemetry-service/scripts/deploy.sh {command}" in workflow


def test_service_image_contains_alembic_runtime_files() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
