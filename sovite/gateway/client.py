from __future__ import annotations

import asyncio
import time

import httpx

from ..connector.protocol import PERMIT_SECONDS

AUTHORITY_TIMEOUT = 1


class GatewayError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


AUTHORITY_CODES = {"execution_capability_invalid", "workload_denied", "execution_fenced", "cancelled",
                   "revision_superseded", "grant_unavailable", "grant_disabled", "caller_denied", "class_denied",
                   "grant_inhibited", "qualification_required", "binding_context_denied", "binding_disabled",
                   "binding_inhibited", "model_identity_mismatch", "model_host_unavailable", "inference_request_denied",
                   "binding_busy", "binding_session_busy", "request_replayed", "permit_expired", "gateway_fenced",
                   "model_protocol_error"}


class AuthorityClient:
    def __init__(self, url, token, *, transport=None, verify=True):
        self.client = httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer " + token}, transport=transport,
                                        verify=verify, trust_env=False, follow_redirects=False, timeout=AUTHORITY_TIMEOUT)

    async def call(self, action, **fields):
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(self.client.post("/internal/authority/" + action, json=fields), AUTHORITY_TIMEOUT)
            if response.status_code != 200:
                if response.status_code == 422:
                    raise GatewayError("inference_request_denied")
                payload = response.json()
                detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
                code = detail.get("code") if isinstance(detail, dict) else None
                raise GatewayError(code if code in AUTHORITY_CODES else "authority_unavailable")
            value = response.json()
            if action in {"authorize", "renew"}:
                if (type(value) is not dict or set(value) != {"permit", "permit_ttl_ms"}
                        or type(value["permit"]) is not str or not 1 <= len(value["permit"]) <= 128):
                    raise GatewayError("authority_unavailable")
                ttl = value["permit_ttl_ms"]
                if type(ttl) is not int or not 1 <= ttl <= PERMIT_SECONDS * 1000:
                    raise GatewayError("authority_unavailable")
                # Conservatively charge the entire RTT against the relative permit.
                value["valid_until"] = started + ttl / 1000
                if value["valid_until"] <= time.monotonic():
                    raise GatewayError("permit_expired")
            return value
        except GatewayError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise GatewayError("authority_unavailable") from None

    async def close(self):
        await self.client.aclose()
