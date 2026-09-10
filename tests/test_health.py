from fastapi.testclient import TestClient

from gateway.main import app

client = TestClient(app)


def test_health_returns_200() -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_body() -> None:
    response = client.get("/health")
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "llm-api-gateway"
    assert body["version"] == "0.1.0"
