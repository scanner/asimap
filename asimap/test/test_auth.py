"""
Test the auth modules.. users, password file, password checking
"""

# System imports
#
from collections.abc import Callable
from pathlib import Path

# 3rd party imports
#
import pytest
from faker import Faker

# Project imports
#
from ..auth import PWUser, authenticate
from ..exceptions import BadAuthentication, NoSuchUser


####################################################################
#
@pytest.mark.asyncio
async def test_authenticate(
    faker: Faker,
    user_factory: Callable[..., PWUser],
    password_file_factory: Callable[[list[PWUser]], Path],
) -> None:
    password = faker.password()
    user = user_factory(password=password)
    users = [user_factory(password=faker.password()) for _ in range(10)]
    users.append(user)
    password_file_factory(users)
    auth_user = await authenticate(user.username, password)
    assert auth_user.username == user.username
    assert auth_user.pw_hash == user.pw_hash
    assert auth_user.maildir == user.maildir

    with pytest.raises(BadAuthentication):
        _ = await authenticate(user.username, faker.password())

    with pytest.raises(NoSuchUser):
        _ = await authenticate(faker.email(), password)
