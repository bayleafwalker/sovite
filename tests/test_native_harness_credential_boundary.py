"""An isolated native harness must refuse inherited control authority before effects."""
import pytest

from sovite import harness_api


@pytest.mark.parametrize('name', [
    'SOVITE_DATABASE_URL', 'SOVITE_DATABASE_SCHEMA', 'SOVITE_DATABASE_MIGRATE',
    'SOVITE_ARTIFACT_PATH', 'SOVITE_S3_ENDPOINT', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
    'SOVITE_OPERATOR_TOKEN', 'SOVITE_AUTHOR_TOKEN', 'SOVITE_ACTIVATOR_TOKEN',
    'SOVITE_AUTHORITY_TOKEN', 'SOVITE_CALLER_TOKENS', 'SOVITE_EXECUTION_TOKENS',
    'SOVITE_EXECUTION_TOKEN',
    'SOVITE_ARTIFACT_BACKEND', 'SOVITE_S3_BUCKET', 'SOVITE_S3_REGION',
    'AWS_PROFILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_WEB_IDENTITY_TOKEN_FILE',
    'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'SOVITE_QUALIFICATION_PRIVATE_KEY',
])
def test_inherited_control_authority_refuses_before_configuration_or_state(monkeypatch, name):
    canary = 'synthetic-control-authority-must-never-be-disclosed'
    monkeypatch.setenv(name, canary)
    effects = []
    def configuration(*args, **kwargs):
        effects.append('configuration-loaded')
        raise AssertionError('configuration loaded before credential boundary')
    monkeypatch.setattr(harness_api, 'load_bundle', configuration)
    with pytest.raises(ValueError, match='isolated environment') as caught:
        harness_api.create_app()
    assert effects == []
    assert canary not in str(caught.value)


def test_own_token_provider_and_hosting_settings_reach_normal_configuration(monkeypatch):
    monkeypatch.setenv('SOVITE_HARNESS_TOKEN', 'synthetic-dedicated-harness-token')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-provider-token')
    monkeypatch.setenv('SOVITE_HOSTING_ENVELOPE', '/synthetic/envelope.json')
    monkeypatch.setenv('SOVITE_QUALIFICATION_PUBLIC_KEYS', '["/synthetic/public.pem"]')
    class ConfigurationReached(Exception):
        pass
    def configuration(*args, **kwargs):
        raise ConfigurationReached()
    monkeypatch.setattr(harness_api, 'load_bundle', configuration)
    with pytest.raises(ConfigurationReached):
        harness_api.create_app()
