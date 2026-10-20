"""Cross-check desired package bindings against a concrete Helm installation."""
from __future__ import annotations

import copy
import ipaddress
from urllib.parse import urlsplit

import yaml


def merge(left, right):
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            merge(left[key], value)
        else:
            left[key] = copy.deepcopy(value)
    return left


def load_values(chart, paths):
    values = yaml.safe_load((chart / "values.yaml").read_text())
    for path in paths:
        merge(values, yaml.safe_load(path.read_text()) or {})
    return values


def service_hosts(name, namespace, domain):
    return {name, f"{name}.{namespace}", f"{name}.{namespace}.svc", f"{name}.{namespace}.svc.{domain}"}


def endpoint(value):
    parsed = urlsplit(value)
    return (parsed.hostname or "").rstrip("."), parsed.port or (443 if parsed.scheme == "https" else 80)


def peer_scoped(peer, host):
    if not peer.get("cidrs"):
        return bool(peer.get("namespaceSelector") and peer.get("podSelector"))
    if peer.get("namespaceSelector") or peer.get("podSelector"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # A name could resolve outside the allowed CIDRs.
    networks = [ipaddress.ip_network(cidr, strict=True) for cidr in peer["cidrs"]]
    return all(network.prefixlen == network.max_prefixlen for network in networks) and any(address in network for network in networks)


def peer_allows(peers, host, port):
    return any(host in peer.get("hosts", []) and port in peer.get("ports", []) and peer_scoped(peer, host) for peer in peers)


def validate_bindings(bundle, values, namespace, *, release="sovite", live_api_addresses=None, credentials=None):
    if bundle["mode"] != values["mode"]:
        raise ValueError("Bundle mode and deployment mode must be identical")
    pools = {row["id"]: row for row in bundle["compute"]}
    agents = {row["id"]: row for row in bundle["agents"]}
    models = {row["id"]: row for row in bundle["models"]}
    harnesses = {row["id"]: row for row in bundle["harnesses"]}
    tools = {row["id"]: row for row in bundle["tools"]}
    pool_id = values["worker"]["computeId"]
    if pool_id not in pools:
        raise ValueError("Worker compute ID does not identify a declared compute package")
    if pools[pool_id]["kind"] != "kubernetes":
        raise ValueError("A Kubernetes installation requires a Kubernetes compute package")
    if pools[pool_id].get("namespace", namespace) != namespace:
        raise ValueError("Compute namespace and installation namespace differ")
    declared_replicas = pools[pool_id].get("profile", {}).get("replicas")
    if declared_replicas is not None and declared_replicas != values["worker"]["replicas"]:
        raise ValueError("Worker replicas differ from the declared compute package")
    active = [row for row in bundle["grants"] if row.get("enabled", True)]
    if any(row["compute"] != pool_id for row in active):
        raise ValueError("This release must provision a worker for every enabled grant's compute pool")
    active_agents = {row["agent"] for row in active}
    name = (values.get("fullnameOverride") or (release + "-sovite"))[:63].rstrip("-")
    domain = values["networkPolicy"]["clusterDomain"]
    ollama_hosts = service_hosts(name + "-ollama", namespace, domain)
    native_hosts = service_hosts(name + "-native-harness", namespace, domain)
    native_agents = set()
    checked_models, checked_tools = set(), set()
    for agent_id in active_agents:
        agent = agents[agent_id]
        model = models[agent["model"]]
        harness = harnesses[agent["harness"]]
        if model["adapter"] != "demo":
            for url in {model["endpoint"], model.get("identity_endpoint", model["endpoint"])}:
                host, port = endpoint(url)
                if host in ollama_hosts:
                    if not values["ollama"]["enabled"] or port != 11434:
                        raise ValueError("A model references the bundled Ollama service but that endpoint is not provisioned")
                elif not peer_allows(values["networkPolicy"].get("modelPeers", []), host, port):
                    raise ValueError("An external model endpoint lacks a matching explicit model network peer")
            if model.get("api_key_env") and model["api_key_env"] not in {row.get("env") for row in values.get("modelCredentials", [])}:
                raise ValueError("A model credential is not provisioned by modelCredentials")
            checked_models.add(model["id"])
        if harness["kind"] == "native-codex":
            raise ValueError("Kubernetes native execution requires the isolated agent-http service")
        if harness["kind"] == "agent-http":
            for field in ("endpoint", "cancel_endpoint"):
                if field not in harness:
                    continue
                host, port = endpoint(harness[field])
                if host in native_hosts:
                    if not values["nativeHarness"]["enabled"] or port != 8090:
                        raise ValueError("An enabled grant references a native service that is not provisioned")
                    if urlsplit(harness[field]).path != ("/run" if field == "endpoint" else "/cancel"):
                        raise ValueError("Native harness URL path does not match the installed handler")
                    native_agents.add(agent_id)
                    if bundle["mode"] == "enforced" and harness.get("digest") != values["nativeHarness"]["image"].get("digest"):
                        raise ValueError("Native harness digest and deployed native image disagree")
                elif not peer_allows(values["networkPolicy"].get("harnessPeers", []), host, port):
                    raise ValueError("An external agent endpoint lacks a matching explicit harness network peer")
        elif bundle["mode"] == "enforced" and harness.get("digest") != values["image"].get("digest"):
            raise ValueError("Bounded harness digest and deployed worker image disagree")
        for tool_id in agent["tools"]:
            tool = tools[tool_id]
            host, port = endpoint(tool.get("endpoint", "https://kubernetes.default.svc"))
            if tool["kind"] == "kubernetes-read":
                if not values["kubernetesTools"]["enabled"] or tool.get("namespace", "default") != values["kubernetesTools"]["namespace"]:
                    raise ValueError("Kubernetes tool namespace/enablement does not match the installed Role and service account")
                if host not in service_hosts("kubernetes", "default", domain) or port != 443:
                    raise ValueError("The namespaced Kubernetes tool must bind the qualified in-cluster API service")
            elif not peer_allows(values["networkPolicy"].get("toolPeers", []), host, port):
                raise ValueError("A tool endpoint lacks a matching explicit tool network peer")
            checked_tools.add(tool_id)
    if values["nativeHarness"]["enabled"]:
        declared_agent = values["nativeHarness"]["agentId"]
        if declared_agent not in agents or (native_agents and native_agents != {declared_agent}):
            raise ValueError("The native service's fixed agent identity does not match the declared active agent binding")
    networks = []
    if values["kubernetesTools"]["enabled"]:
        for cidr in values["kubernetesTools"]["apiServerCIDRs"]:
            network = ipaddress.ip_network(cidr, strict=True)
            if network.prefixlen != network.max_prefixlen:
                raise ValueError("Kubernetes API allowances must be exact host CIDRs (/32 or /128)")
            networks.append(network)
        if not networks:
            raise ValueError("Actual Kubernetes API endpoint host CIDRs are required")
        if live_api_addresses is not None:
            live = {ipaddress.ip_address(value) for value in live_api_addresses}
            configured = {network.network_address for network in networks}
            if not live or live != configured:
                raise ValueError("API CIDRs do not exactly match the selected cluster's observed Kubernetes service endpoints")
    if credentials is not None:
        from sqlalchemy.engine import make_url
        database = make_url(credentials["SOVITE_DATABASE_URL"])
        if values["postgres"]["enabled"] and (database.host not in service_hosts(name + "-postgres", namespace, domain) or (database.port or 5432) != 5432):
            raise ValueError("Database secret points outside the provisioned PostgreSQL endpoint")
        if values["postgres"]["enabled"] and (database.username != "sovite" or database.database != "sovite" or database.password != credentials.get("POSTGRES_PASSWORD")):
            raise ValueError("Database credentials do not match the provisioned PostgreSQL account and database")
        if not values["postgres"]["enabled"] and not values["networkPolicy"]["stateEgress"]:
            raise ValueError("External database state requires explicit destination CIDRs and ports")
    artifact_endpoint = values["artifacts"].get("endpoint")
    if values["garage"]["enabled"]:
        if artifact_endpoint:
            host, port = endpoint(artifact_endpoint)
            if host not in service_hosts(name + "-garage", namespace, domain) or port != 3900:
                raise ValueError("Artifact endpoint differs from the provisioned Garage service")
        if values["artifacts"]["bucket"] != "sovite-private" or values["artifacts"]["region"] != "garage":
            raise ValueError("Artifact bucket and region must match the provisioned Garage configuration")
    elif not artifact_endpoint or not values["networkPolicy"]["stateEgress"]:
        raise ValueError("External artifact state needs an endpoint and explicit destination CIDRs and ports")
    return {"compute": pool_id, "active_grants": len(active), "model_bindings": sorted(checked_models),
            "tool_bindings": sorted(checked_tools), "native_agent_bindings": sorted(native_agents),
            "api_binding_observed": live_api_addresses is not None}
