from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DB = Path('/tmp/adpilot-pytest.db')
for suffix in ('', '-wal', '-shm'):
    Path(f'{TEST_DB}{suffix}').unlink(missing_ok=True)
os.environ['DATABASE_URL'] = f'sqlite+pysqlite:///{TEST_DB}'
os.environ['ADPILOT_FORCE_SQLITE'] = '1'
os.environ['ADPILOT_SEED_ON_START'] = '1'
os.environ['LLM_PROVIDER'] = 'none'

from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture(scope='session')
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope='session')
def auth_headers(client):
    response = client.post(
        '/api/v1/auth/login',
        json={'email': 'admin@adpilot.io', 'password': 'AdPilot!2026'},
    )
    assert response.status_code == 200, response.text
    access_token = response.json()['access_token']
    return {'Authorization': f'Bearer {access_token}'}
