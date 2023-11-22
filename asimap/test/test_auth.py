"""
Test the auth modules.. users, password file, password checking
"""
# System imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..auth import authenticate
from ..exceptions import BadAuthentication, NoSuchUser


####################################################################
#
@pytest.mark.asyncio
async def test_authenticate(faker, user_factory, password_file_factory):
    password = faker.pystr(min_chars=8, max_chars=32)
    user = user_factory(password=password)
    password_file_factory([user])
    auth_user = await authenticate(user.username, password)
    assert auth_user.username == user.username
    assert auth_user.pw_hash == user.pw_hash
    assert auth_user.maildir == user.maildir

    with pytest.raises(BadAuthentication):
        _ = await authenticate(user.username, faker.password())

    with pytest.raises(NoSuchUser):
        _ = await authenticate(faker.email(), password)
