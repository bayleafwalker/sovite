"""Keep the missing worker API visible until a safe remote contract lands."""
from inspect import getsource

from sovite.api import create_app
from sovite.lease_service import LeaseService
from sovite.settings import Settings
from sovite.worker import Worker


def test_execution_owner_is_not_yet_a_remote_worker_or_artifact_api():
    app = create_app(Settings(operator_token="synthetic-operator-token-000000"))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/internal/authority/start" in paths  # Gateway authority only.
    assert not any(path.startswith("/internal/execution/") for path in paths)
    assert not any(path.startswith("/api/worker/") for path in paths)

    lease_source = getsource(LeaseService)
    worker_execute_source = getsource(Worker.execute)
    assert "self.platform.db.sessions.begin()" in lease_source
    assert "self.platform.store.put" in worker_execute_source
