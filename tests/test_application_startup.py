"""Check the real empty application lifespan without accounts or network."""
import os
from pathlib import Path
import subprocess
import sys


def test_empty_application_starts_and_health_is_serializable(tmp_path):
    environment={**os.environ,'AK_DATA_DIR':str(tmp_path),'AK_DATABASE_PATH':str(tmp_path/'startup.db'),
        'AK_API_KEY':'offline-startup-fixture','AK_ADMIN_TOKEN':'offline-startup-admin',
        'AK_PROXY_POOL_ENABLED':'false','AK_DAILY_CHECKIN_ENABLED':'false'}
    source='''
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as client:
 response=client.get('/health')
 assert response.status_code==200,response.text
 body=response.json()
 assert body['status']=='ok' and body['service']=='ak2api'
 assert body['interactive_browser_count']==0
 assert client.get('/api/account-login').status_code==401
'''
    result=subprocess.run([sys.executable,'-B','-c',source],cwd=Path(__file__).resolve().parents[1],
                          env=environment,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
