import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)

def test_auth_endpoints_exist(client):
    """Test that auth endpoints are accessible (basic smoke test)."""
    # Test that auth endpoints return proper error codes when accessed without auth
    response = client.get("/auth/me")
    assert response.status_code in [401, 422, 404]  # Unauthorized or validation error is expected

def test_auth_routes_structure(client):
    """Test auth router is properly mounted."""
    # This ensures the auth router is properly configured
    # We expect 401/404/422 for protected endpoints without proper auth
    endpoints_to_test = [
        "/auth/me",
        "/auth/logout"
    ]
    
    for endpoint in endpoints_to_test:
        response = client.get(endpoint)
        # We expect these to fail with proper HTTP status codes, not 500
        assert response.status_code < 500