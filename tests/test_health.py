from fastapi.testclient import TestClient


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_body(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body == {"status": "ok", "service": "llm-api-gateway", "version": "0.1.0"}
