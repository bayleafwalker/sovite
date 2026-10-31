"""Real SQLite/PostgreSQL schema6→7 preservation and failed cutover histories."""
import hashlib
import os

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError

from sovite import resource_schema
from sovite.db import AuditEvent, Database, DatabaseSchemaError, MetadataRow, MIGRATIONS
from test_migrations import database


@pytest.fixture(scope="session",autouse=True)
def required_pg():
    if os.environ.get('SOVITE_RESOURCE_REQUIRE_PG')=='1':
        assert os.environ.get('SOVITE_TEST_POSTGRES_URL'),'required disposable PostgreSQL fixture missing'
        from sqlalchemy import create_engine
        url=os.environ['SOVITE_TEST_POSTGRES_URL'].replace('postgresql://','postgresql+psycopg://',1)
        engine=create_engine(url)
        try:
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls FROM pg_roles WHERE rolname=current_user").scalar_one(), 'Resource tests require unprivileged disposable owner'
        finally:engine.dispose()


def schema6(db):
    with db.engine.begin() as connection:
        for number,tables in MIGRATIONS:
            if number>6:break
            for table in tables:table.create(connection)
        connection.execute(MetadataRow.__table__.insert(),[
            {'key':'schema_version','value':{'version':6}},
            {'key':'audit_head','value':{'sequence':0,'hash':'0'*64}}])
        enrollment,scope,grant,request=MIGRATIONS[5][1]
        connection.execute(enrollment.insert().values(id='old-enrollment',gateway_id='old-gateway',
            audience='resource-gateway',tenant_id='tenant',workspace_id='workspace',domain_id='domain',
            token_digest=hashlib.sha256(b'fixture-old-gateway').hexdigest(),expires_at=1000,
            revoked=False,created_at=100))
        connection.execute(scope.insert().values(workload_id='old-workload',gateway_id='old-gateway',
            tenant_id='tenant',workspace_id='workspace',domain_id='domain',observation_id='old-observation',
            namespace='old-namespace',pod_name='old-pod',updated_at=100))
        connection.execute(grant.insert().values(id='old-output-grant',workload_id='old-workload',
            gateway_id='old-gateway',principal_id='old-caller',tenant_id='tenant',workspace_id='workspace',
            domain_id='domain',observation_id='old-observation',schema_version='sovite-resource-pod-observation/v1',
            destination='workbench',policy_revision='sha256:'+'a'*64,active=True,created_at=100))
        connection.execute(request.insert().values(id='old-request',job_id='old-job',instance_id='old-instance',
            generation='sha256:'+'b'*64,workload_id='old-workload',gateway_id='old-gateway',
            workload_token_digest=hashlib.sha256(b'fixture-old-workload').hexdigest(),principal_id='old-caller',
            fence='c'*32,tenant_id='tenant',workspace_id='workspace',domain_id='domain',
            observation_id='old-observation',namespace='old-namespace',pod_name='old-pod',
            request={'workspace_id':'workspace','resource_domain_id':'domain','observation_id':'old-observation'},
            state='observed',observation='  {"retained":"exact authored bytes"}\n',observation_digest='d'*64,
            release='allowed',enrollment_id='old-enrollment',output_schema='sovite-resource-pod-observation/v1',
            output_destination='workbench',output_policy_revision='sha256:'+'a'*64,output_actor='old-caller',
            created_at=100,updated_at=101))


def snapshot(db):
    with db.engine.connect() as connection:
        return {table.name:[dict(row) for row in connection.execute(select(table)).mappings()]
            for table in MIGRATIONS[5][1]}


def version(db):
    with db.sessions() as session:return session.get(MetadataRow,'schema_version').value['version']


def test_existing_schema6_requires_offline_cutover_preserves_rows_and_v1_meaning(database):
    schema6(database)
    with database.sessions.begin() as session:
        database.audit(session,'fixture-operator','migration_fixture_audit','schema6-migration-fixture',
            {'schema_version':6,'fixture':'recovery-retention-marker'})
    before=snapshot(database)
    audit_before=database.verify_audit()
    with database.engine.connect() as connection:
        audit_rows_before=[dict(row) for row in connection.execute(select(AuditEvent.__table__)).mappings()]
    statements=[]
    def capture(connection,cursor,statement,parameters,context,executemany):
        statements.append(statement)
        assert not context.isddl and statement.lstrip().split()[0].upper() in {'SELECT','PRAGMA'}
    event.listen(database.engine,'before_cursor_execute',capture)
    try:
        with pytest.raises(DatabaseSchemaError) as error:database.initialize()
        assert error.value.code=='database_schema_older'
    finally:event.remove(database.engine,'before_cursor_execute',capture)
    assert statements and version(database)==6 and snapshot(database)==before
    database.migrate(); assert version(database)==7
    assert snapshot(database)==before
    assert database.verify_audit()==audit_before
    with database.engine.connect() as connection:
        assert [dict(row) for row in connection.execute(select(AuditEvent.__table__)).mappings()]==audit_rows_before
    with database.sessions() as session:
        from sovite.db import ResourceWorkloadScope
        row=session.get(ResourceWorkloadScope,'old-workload')
        assert row.target_catalog is None
        assert (row.observation_id,row.namespace,row.pod_name)==('old-observation','old-namespace','old-pod')
    database.initialize('never'); database.migrate()
    assert version(database)==7 and snapshot(database)==before
    class Older(Database):schema_version=6
    old=Older(database.engine.url.render_as_string(hide_password=False),schema=database.schema)
    try:
        with pytest.raises(DatabaseSchemaError) as error:old.check()
        assert error.value.code=='database_schema_newer'
    finally:old.engine.dispose()


@pytest.mark.parametrize(('table_name','constraint_name'),[
    ('resource_workload_scopes','resource_scope_target_shape'),
    ('resource_owner_requests','resource_request_target_shape'),
])
@pytest.mark.parametrize('mutation',['drop','weaken','not_valid'])
def test_schema7_startup_refuses_missing_or_weakened_shape_check_read_only(database,table_name,constraint_name,mutation):
    if database.engine.dialect.name!='postgresql':
        pytest.skip('PostgreSQL supports direct CHECK removal for this compatibility test')
    schema6(database);database.migrate()
    quote=database.engine.dialect.identifier_preparer.quote
    owner=(quote(database.schema)+'.' if database.schema else '')+quote(table_name)
    constraint=quote(constraint_name)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f'ALTER TABLE {owner} DROP CONSTRAINT {constraint}')
        if mutation=='weaken':
            connection.exec_driver_sql(f'ALTER TABLE {owner} ADD CONSTRAINT {constraint} CHECK (true)')
        elif mutation=='not_valid':
            expression=(resource_schema.SCOPE_CHECK if constraint_name=='resource_scope_target_shape'
                        else resource_schema.REQUEST_CHECK)
            connection.exec_driver_sql(f'ALTER TABLE {owner} ADD CONSTRAINT {constraint} CHECK ({expression}) NOT VALID')
    statements=[]
    def capture(connection,cursor,statement,parameters,context,executemany):
        statements.append(statement)
        assert not context.isddl and statement.lstrip().split()[0].upper()=='SELECT'
    event.listen(database.engine,'before_cursor_execute',capture)
    try:
        with pytest.raises(DatabaseSchemaError) as error:database.initialize('never')
        assert error.value.code=='database_schema_incomplete'
    finally:event.remove(database.engine,'before_cursor_execute',capture)
    assert statements and version(database)==7


@pytest.mark.parametrize('replace_expected_index',[False,True])
def test_schema6_sqlite_refuses_partial_index_that_rebuild_would_drop(database,replace_expected_index):
    if database.engine.dialect.name!='sqlite':
        pytest.skip('SQLite rebuilds resource tables; PostgreSQL alters them in place')
    schema6(database)
    with database.engine.begin() as connection:
        name='ix_resource_owner_requests_job_id'
        if replace_expected_index:
            connection.exec_driver_sql(f'DROP INDEX {name}')
        else:
            name='extra_partial_job'
        connection.exec_driver_sql(f"CREATE INDEX {name} ON resource_owner_requests(job_id) WHERE state='pending'")
    with pytest.raises(resource_schema.ResourceSchemaError,match='resource_schema6_incompatible'):
        database.migrate()
    indexes=inspect(database.engine).get_indexes('resource_owner_requests')
    names={row['name'] for row in indexes}
    if replace_expected_index:
        assert names=={'ix_resource_owner_requests_job_id'}
        assert indexes[0]['dialect_options']['sqlite_where'] is not None
    else:
        assert {'ix_resource_owner_requests_job_id','extra_partial_job'}<=names
    assert version(database)==6


def test_schema6_postgres_migration_preserves_target_table_owners_and_acls(database):
    if database.engine.dialect.name!='postgresql':
        pytest.skip('PostgreSQL owner and ACL catalog check')
    schema6(database)
    quote=database.engine.dialect.identifier_preparer.quote
    for table_name in ('resource_workload_scopes','resource_owner_requests'):
        owner=(quote(database.schema)+'.' if database.schema else '')+quote(table_name)
        with database.engine.begin() as connection:
            connection.exec_driver_sql(f'GRANT SELECT, INSERT ON TABLE {owner} TO PUBLIC')
            if table_name=='resource_workload_scopes':
                connection.exec_driver_sql(f'GRANT UPDATE (observation_id) ON TABLE {owner} TO PUBLIC')

    def ownership_acl():
        result={}
        with database.engine.connect() as connection:
            for table_name in ('resource_workload_scopes','resource_owner_requests'):
                row=connection.execute(text(
                    'SELECT c.relowner::text,c.relacl::text, '
                    "COALESCE((SELECT jsonb_agg(jsonb_build_array(a.attnum,a.attname,a.attacl::text) ORDER BY a.attnum) "
                    "FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped AND a.attacl IS NOT NULL),'[]'::jsonb)::text "
                    'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                    'WHERE n.nspname=:schema AND c.relname=:name'),
                    {'schema':database.schema,'name':table_name}).one()
                result[table_name]=tuple(row)
        return result

    before=ownership_acl()
    database.migrate()
    assert ownership_acl()==before


def test_failure_after_actual_resource_ddl_rolls_back_data_shape_and_version(database,monkeypatch):
    schema6(database); before=snapshot(database)
    original=resource_schema.migrate
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('injected after both resource table changes')
    monkeypatch.setattr(resource_schema,'migrate',fail)
    with pytest.raises(RuntimeError,match='injected'):database.migrate()
    assert version(database)==6 and snapshot(database)==before
    columns=inspect(database.engine).get_columns('resource_workload_scopes',schema=database.schema)
    assert 'target_catalog' not in {c['name'] for c in columns}
    assert all(not c['nullable'] for c in columns if c['name'] in {'observation_id','namespace','pod_name'})
    assert not any(name.endswith('_schema7') for name in inspect(database.engine).get_table_names(schema=database.schema))


def test_foreign_extra_storage_refuses_without_erasing_it_or_stamping(database):
    schema6(database); before=snapshot(database)
    quote=database.engine.dialect.identifier_preparer.quote
    target=(quote(database.schema)+'.' if database.schema else '')+'resource_workload_scopes'
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f'ALTER TABLE {target} ADD COLUMN foreign_owner_data TEXT')
        connection.exec_driver_sql(f"UPDATE {target} SET foreign_owner_data='keep this data'")
    with pytest.raises(resource_schema.ResourceSchemaError):database.migrate()
    assert version(database)==6 and snapshot(database)==before
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql(f'SELECT foreign_owner_data FROM {target}').scalar_one()=='keep this data'


def test_database_rejects_mixed_catalog_pod_scope_and_half_null_request(database):
    schema6(database);database.migrate()
    from sovite.db import ResourceOwnerRequest,ResourceWorkloadScope
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            row=session.get(ResourceWorkloadScope,'old-workload');row.target_catalog={}
    with database.sessions.begin() as session:
        row=session.get(ResourceWorkloadScope,'old-workload')
        row.target_catalog={'schema_version':'sovite-resource-catalog/v2','entries':[{
            'kind':'model-peer-health','target_id':'synthetic-health','target_revision':'sha256:'+'e'*64,
            'read_profile':{'source_class':'C0','output_class':'C0','destination':'workbench'}}]}
        row.observation_id=row.namespace=row.pod_name=None
    with database.sessions() as session:
        row=session.get(ResourceWorkloadScope,'old-workload')
        assert row.namespace is None and row.pod_name is None and row.observation_id is None
    with pytest.raises(IntegrityError):
        with database.sessions.begin() as session:
            row=session.get(ResourceOwnerRequest,'old-request');row.namespace=None
    with database.sessions.begin() as session:
        row=session.get(ResourceOwnerRequest,'old-request');row.namespace=row.pod_name=None



def test_schema6_incoming_foreign_key_is_refused_before_any_table_rebuild(database):
    from sqlalchemy import Column,ForeignKey,MetaData,String,Table
    schema6(database);before=snapshot(database)
    metadata=MetaData(schema=database.schema)
    old=MIGRATIONS[5][1][1].to_metadata(metadata)
    foreign=Table('foreign_resource_reader',metadata,Column('id',String,primary_key=True),
        Column('workload',String,ForeignKey(old.c.workload_id,ondelete='CASCADE')))
    with database.engine.begin() as connection:
        foreign.create(connection)
        connection.execute(foreign.insert().values(id='retained',workload='old-workload'))
    with pytest.raises(resource_schema.ResourceSchemaError):database.migrate()
    assert version(database)==6 and snapshot(database)==before
    with database.engine.connect() as connection:
        assert connection.execute(select(foreign.c.id)).scalar_one()=='retained'
