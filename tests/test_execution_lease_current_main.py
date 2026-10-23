"""Current-main lineage denial versus unavailable owner at the actual boundary."""
import pytest
from test_execution_leases import owner, database, gateway_bundle  # noqa: F401
from test_execution_leases import submit, pull, snapshot
from sovite.service import PlatformError


@pytest.mark.parametrize('status,code',[(404,'previous_attempt_unavailable'),(409,'previous_attempt_unfinished'),(409,'previous_effect_uncertain')])
def test_current_lineage_owner_denial_blocks_without_attempt(owner,monkeypatch,status,code):
    job=submit(owner)
    def denied(*args,**kwargs):raise PlatformError(code,'Unavailable previous attempt',status)
    monkeypatch.setattr(owner.platform,'authorize_existing_job',denied)
    assert pull(owner)=={'state':'idle'}
    row=owner.platform.get_job(job.id)
    assert row.status=='blocked' and row.error_code==code and row.attempts==0
    assert row.lease_owner is None and row.lease_until is None
    assert owner.platform.db.verify_audit()['valid']


@pytest.mark.parametrize('status,code',[(503,'previous_effect_uncertain'),(409,'unknown_current_owner_failure'),(409,'revision_superseded')])
def test_other_owner_failures_cannot_be_observed_idle_or_mutate_queue(owner,monkeypatch,status,code):
    submit(owner)
    before=snapshot(owner.platform)
    def unavailable(*args,**kwargs):raise PlatformError(code,'Current owner unavailable',status)
    monkeypatch.setattr(owner.platform,'authorize_existing_job',unavailable)
    with pytest.raises(PlatformError) as caught:pull(owner)
    assert caught.value.code==code and caught.value.status==status
    assert snapshot(owner.platform)==before
