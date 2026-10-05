from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "telemetry-service.yml"
DOCKERFILE = ROOT / "telemetry-service" / "Dockerfile"


def test_service_workflow_is_path_scoped_and_uses_an_isolated_build_context() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("- telemetry-service/**") == 2
    assert "branches: [main]" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "working-directory: telemetry-service" in workflow
    assert (
        "docker build --file telemetry-service/Dockerfile "
        '--tag "$image" telemetry-service'
    ) in workflow
    assert (
        'docker build --file telemetry-service/Dockerfile --tag "$image" .'
        not in workflow
    )


def test_service_deployment_uses_oidc_and_verifies_before_promotion() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "id-token: write" in workflow
    assert "uses: google-github-actions/auth@v2" in workflow
    assert "--default-url" in workflow
    assert "httpGet.path=/health" in workflow
    assert '"${url}/ready"' in workflow
    assert '"${stable_url}/ready"' in workflow
    assert "--no-traffic" in workflow
    assert workflow.index("Resolve and verify the candidate revision") < workflow.index(
        "Promote the verified revision"
    )
    assert "Restore the previous revision after failed stable verification" in workflow


def test_service_workflow_runs_postgres_tests_and_migrates_before_deploy() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "image: postgres:16-alpine" in workflow
    assert "TEST_DATABASE_URL:" in workflow
    assert "uv run alembic check" in workflow
    assert "--set-cloudsql-instances" in workflow
    assert '"run,--no-sync,python,-m,telemetry_collector.migrate"' in workflow
    assert "--max-retries 0" in workflow
    assert "--wait" in workflow
    assert "--max 4" in workflow
    assert workflow.index("Apply database migrations") < workflow.index(
        "Deploy an isolated candidate revision"
    )
    assert workflow.count("steps.image.outputs.uri") >= 2


def test_service_image_contains_alembic_runtime_files() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
