from pathlib import Path

import yaml


def test_compose_defines_database_migration_api_and_worker() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"db", "migrate", "api", "worker"} <= services.keys()
    assert services["db"]["image"] == "pgvector/pgvector:pg16"
    for name in ("api", "worker"):
        dependencies = services[name]["depends_on"]
        assert dependencies["db"]["condition"] == "service_healthy"
        assert dependencies["migrate"]["condition"] == "service_completed_successfully"
    for service in services.values():
        assert service.get("environment", {}).get("OPENAI_API_KEY") != "OPENAI_API_KEY"
