from fastapi.testclient import TestClient
from orion.main import app

client = TestClient(app)


def test_health_check() -> None:
    """Test that the /health endpoint returns 200 OK and status healthy."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}
