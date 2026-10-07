from __future__ import annotations


def test_health(client):
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
    assert response.json()['database'] == 'sqlite'


def test_demo_login_and_current_user(client, auth_headers):
    response = client.get('/api/v1/auth/me', headers=auth_headers)
    assert response.status_code == 200
    assert response.json()['email'] == 'admin@adpilot.io'


def test_overview_is_authenticated_and_seeded(client, auth_headers):
    denied = client.get('/api/v1/overview')
    assert denied.status_code == 401
    response = client.get('/api/v1/overview', headers=auth_headers)
    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_optimizer_and_budget_whatif(client, auth_headers):
    optimized = client.post('/api/v1/optimizer/run', headers=auth_headers)
    assert optimized.status_code == 200, optimized.text
    result = optimized.json()
    assert result['allocation']

    changes = {
        str(row['campaign_id']): round(row['current_daily_spend'] * 1.05, 2)
        for row in result['allocation'][:3]
    }
    whatif = client.post(
        '/api/v1/whatif', headers=auth_headers, json={'allocations': changes}
    )
    assert whatif.status_code == 200, whatif.text
    estimate = whatif.json()
    assert 'current_daily_contribution' in estimate
    assert 'what_if_daily_contribution' in estimate
    assert estimate['constraints']
