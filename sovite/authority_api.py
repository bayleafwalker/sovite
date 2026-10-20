"""Private gateway workload surface. Helper certificates cannot authenticate here."""
import secrets
from typing import Annotated

from fastapi import Header, HTTPException
from pydantic import Field

from .authority import Authority
from .gateway.requests import Authorize, Id, Secret, Start
from .service import PlatformError


class Open(Start):
    certificate_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SessionRequest(Start):
    session_id: Id


class Hello(SessionRequest):
    identity: dict


class Renew(Start):
    request_id: Id
    capability: Secret
    workload: Annotated[str, Field(min_length=1, max_length=160)]
    permit: Secret
    preparation: dict | None = None


class Finish(Renew):
    permit: Secret | None = None
    outcome: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z_]+$")]
    stopped: bool


def install_routes(app, platform, settings):
    authority = Authority(platform)

    def authenticate(authorization: str = Header(default="")):
        expected = settings.authority_token
        if not expected or not secrets.compare_digest(authorization.encode(), ("Bearer " + expected).encode()):
            raise HTTPException(403, {"code": "workload_denied", "message": "Inference authority denied"})

    from fastapi import Depends
    actions = {"start": (Start, authority.start), "open": (Open, authority.open_session),
               "hello": (Hello, authority.hello), "touch": (SessionRequest, authority.touch),
               "fence": (SessionRequest, authority.fence_session), "authorize": (Authorize, authority.authorize_request),
               "renew": (Renew, authority.renew_permit), "finish": (Finish, authority.finish)}
    def route(model, method):
        def endpoint(body, authenticated=Depends(authenticate)):
            try:
                return method(**body.model_dump(exclude={"preparation"} if model is Finish else set()))
            except PlatformError:
                raise
            except Exception:
                raise PlatformError("authority_unavailable", "Inference authority unavailable", 503) from None
        endpoint.__annotations__["body"] = model
        return endpoint
    for action, (model, method) in actions.items():
        app.post("/internal/authority/" + action)(route(model, method))
