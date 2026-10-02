from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "telemetry-service.yml"


def test_service_workflow_is_path_scoped_and_uses_an_isolated_build_context() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert workflow.count("- telemetry-service/**") == 2
    assert "branches: [main]" in workflow
    assert "if: github.event_name != 'pull_request'" in workflow
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
    assert "--no-traffic" in workflow
    assert workflow.index("Resolve and verify the candidate revision") < workflow.index(
        "Promote the verified revision"
    )
    assert "Restore the previous revision after failed stable verification" in workflow
