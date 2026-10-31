"""Offline resource schema7; freeze v6 storage and preserve its rows exactly."""
from __future__ import annotations

from sqlalchemy import (Boolean, Column, Float, Index, JSON, MetaData, String, Table, Text,
                        inspect, select)
from sqlalchemy.schema import CreateTable


SCOPE_CHECK = '(target_catalog IS NULL AND observation_id IS NOT NULL AND namespace IS NOT NULL AND pod_name IS NOT NULL) OR (target_catalog IS NOT NULL AND observation_id IS NULL AND namespace IS NULL AND pod_name IS NULL)'
REQUEST_CHECK = '(namespace IS NULL AND pod_name IS NULL) OR (namespace IS NOT NULL AND pod_name IS NOT NULL)'


class ResourceSchemaError(ValueError):
    pass


def legacy_tables():
    """Actual preceding v6 definitions, independent of mutable runtime models."""
    metadata=MetaData()
    enrollment=Table('resource_gateway_enrollments',metadata,
        Column('id',String(64),primary_key=True),
        *[Column(name,String(size),nullable=False) for name,size in (
            ('gateway_id',96),('audience',96),('tenant_id',128),('workspace_id',128),('domain_id',128))],
        Column('token_digest',String(64),unique=True,nullable=False),
        Column('expires_at',Float,nullable=False),Column('revoked',Boolean,nullable=False),
        Column('created_at',Float,nullable=False))
    grant=Table('resource_output_grants',metadata,
        Column('id',String(64),primary_key=True),
        *[Column(name,String(size),nullable=False) for name,size in (
            ('workload_id',160),('gateway_id',96),('principal_id',160),('tenant_id',128),
            ('workspace_id',128),('domain_id',128),('observation_id',128),('schema_version',96),
            ('destination',96),('policy_revision',71))],
        Column('active',Boolean,nullable=False),Column('created_at',Float,nullable=False))
    scope=Table('resource_workload_scopes',metadata,
        Column('workload_id',String(160),primary_key=True),
        Column('gateway_id',String(96),nullable=False),
        Column('tenant_id',String(128),nullable=False),
        Column('workspace_id',String(128),nullable=False),
        Column('domain_id',String(128),nullable=False),
        Column('observation_id',String(128),nullable=False),
        Column('namespace',String(63),nullable=False),
        Column('pod_name',String(253),nullable=False),
        Column('updated_at',Float,nullable=False))
    columns=[Column('id',String(64),primary_key=True)]
    columns += [Column(name,String(size),nullable=False) for name,size in (
        ('job_id',64),('instance_id',64),('generation',80),('workload_id',160),
        ('gateway_id',96),('workload_token_digest',64),('principal_id',160),('fence',64),
        ('tenant_id',128),('workspace_id',128),('domain_id',128),('observation_id',128),
        ('namespace',63),('pod_name',253))]
    columns += [Column('request',JSON,nullable=False),Column('state',String(24),nullable=False),
        Column('observation',Text),Column('observation_digest',String(64)),Column('release',String(24)),
        Column('enrollment_id',String(64)),Column('output_schema',String(96)),
        Column('output_destination',String(96)),Column('output_policy_revision',String(71)),
        Column('output_actor',String(160)),Column('created_at',Float,nullable=False),
        Column('updated_at',Float,nullable=False)]
    request=Table('resource_owner_requests',metadata,*columns)
    Index('ix_resource_owner_requests_job_id',request.c.job_id)
    return enrollment,scope,grant,request


def _name(connection, name, schema):
    quote=connection.dialect.identifier_preparer.quote
    return (quote(schema)+'.' if schema else '')+quote(name)


def _exact_old(connection,tables,schema):
    inspector=inspect(connection)
    for table in tables:
        columns=inspector.get_columns(table.name,schema=schema)
        if {row['name'] for row in columns} != set(table.columns.keys()):
            raise ResourceSchemaError('resource_schema6_incompatible')
        if (inspector.get_foreign_keys(table.name,schema=schema)
                or inspector.get_check_constraints(table.name,schema=schema)
                or inspector.get_unique_constraints(table.name,schema=schema)):
            raise ResourceSchemaError('resource_schema6_incompatible')
        actual=inspector.get_indexes(table.name,schema=schema)
        expected=[(tuple(column.name for column in index.columns),bool(index.unique))
            for index in table.indexes]
        actual_core=[(tuple(row['column_names']),bool(row['unique'])) for row in actual]
        if (any(any(column is None for column in row['column_names']) for row in actual)
                or len(actual) != len(expected) or sorted(actual_core) != sorted(expected)):
            raise ResourceSchemaError('resource_schema6_incompatible')
        for row in actual:
            if (row.get('include_columns')
                    or row.get('column_sorting')):
                raise ResourceSchemaError('resource_schema6_incompatible')
            options=row.get('dialect_options') or {}
            if any((key.endswith('_where') and value is not None)
                   or (key.endswith(('_include','_ops')) and bool(value))
                   for key,value in options.items()):
                raise ResourceSchemaError('resource_schema6_incompatible')
        if any(row.get('default') is not None for row in columns):
            raise ResourceSchemaError('resource_schema6_incompatible')
    names=[table.name for table in tables]
    for name in inspector.get_table_names(schema=schema):
        for foreign in inspector.get_foreign_keys(name,schema=schema):
            if foreign['referred_table'] in names and foreign.get('referred_schema') in {None,schema}:
                raise ResourceSchemaError('resource_schema6_incompatible')
    if connection.dialect.name=='sqlite':
        rows=connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?)",tuple(names)).all()
    else:
        from sqlalchemy import text
        rows=connection.execute(text("SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal "
            "AND n.nspname=:schema AND c.relname IN (:first,:second)"),
            {'schema':schema or connection.exec_driver_sql('SELECT current_schema()').scalar_one(),
             'first':names[0],'second':names[1]}).all()
    if rows:
        raise ResourceSchemaError('resource_schema6_incompatible')


def migrate(connection, *, schema, old_scope, old_request, scope, request):
    """Only two existing tables change; migration and version stamp share tx."""
    if connection.dialect.name not in {'sqlite','postgresql'}:
        raise ResourceSchemaError('resource_schema_backend_unsupported')
    if connection.dialect.name=='postgresql':
        connection.exec_driver_sql("SELECT set_config('lock_timeout',CASE WHEN current_setting('lock_timeout')::interval=interval '0' OR current_setting('lock_timeout')::interval>interval '1 second' THEN '1000' ELSE current_setting('lock_timeout') END,true)")
        connection.exec_driver_sql("SELECT set_config('statement_timeout',CASE WHEN current_setting('statement_timeout')::interval=interval '0' OR current_setting('statement_timeout')::interval>interval '5 seconds' THEN '5000' ELSE current_setting('statement_timeout') END,true)")
        connection.exec_driver_sql('LOCK TABLE '+', '.join(_name(connection,t.name,schema)
            for t in sorted((old_scope,old_request),key=lambda t:t.name))+' IN ACCESS EXCLUSIVE MODE')
    _exact_old(connection,(old_scope,old_request),schema)
    if connection.dialect.name=='postgresql':
        owner=_name(connection,scope.name,schema)
        connection.exec_driver_sql(f'ALTER TABLE {owner} ADD COLUMN target_catalog JSON')
        for field in ('observation_id','namespace','pod_name'):
            connection.exec_driver_sql(f'ALTER TABLE {owner} ALTER COLUMN {field} DROP NOT NULL')
        connection.exec_driver_sql(f'ALTER TABLE {owner} ADD CONSTRAINT resource_scope_target_shape CHECK ({SCOPE_CHECK})')
        owner=_name(connection,request.name,schema)
        for field in ('namespace','pod_name'):
            connection.exec_driver_sql(f'ALTER TABLE {owner} ALTER COLUMN {field} DROP NOT NULL')
        connection.exec_driver_sql(f'ALTER TABLE {owner} ADD CONSTRAINT resource_request_target_shape CHECK ({REQUEST_CHECK})')
    else:
        for old,current in ((old_scope,scope),(old_request,request)):
            temporary=current.to_metadata(MetaData(),name=current.name+'_schema7')
            connection.execute(CreateTable(temporary))
            columns=[column.name for column in old.columns]
            connection.execute(temporary.insert().from_select(columns,select(*(old.c[name] for name in columns))))
            connection.exec_driver_sql('DROP TABLE '+_name(connection,old.name,schema))
            connection.exec_driver_sql('ALTER TABLE '+_name(connection,temporary.name,schema)+' RENAME TO '+connection.dialect.identifier_preparer.quote(current.name))
            for index in current.indexes:
                index.create(connection)
