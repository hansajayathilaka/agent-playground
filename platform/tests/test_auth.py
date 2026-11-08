from __future__ import annotations

import pytest

from playground_core.errors import UnauthenticatedError
from playground_core.services.auth import AuthService

from .conftest import ADMIN_PASSWORD, ADMIN_USER


async def test_login_and_token_lifecycle(auth_service: AuthService) -> None:
    session = await auth_service.login(ADMIN_USER, ADMIN_PASSWORD)
    assert len(session.token) >= 40
    assert (await auth_service.authenticate_token(session.token)).username == ADMIN_USER
    await auth_service.logout(session.token)
    with pytest.raises(UnauthenticatedError):
        await auth_service.authenticate_token(session.token)


@pytest.mark.parametrize(("user", "pw"), [(ADMIN_USER, "wrong"), ("root", ADMIN_PASSWORD)])
async def test_bad_credentials(auth_service: AuthService, user: str, pw: str) -> None:
    with pytest.raises(UnauthenticatedError):
        await auth_service.login(user, pw)


async def test_missing_token(auth_service: AuthService) -> None:
    with pytest.raises(UnauthenticatedError):
        await auth_service.authenticate_token("")
