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
from ..auth import authenticate, logger


####################################################################
#
@pytest.mark.asyncio
async def test_authenticate(faker, user_factory, password_file_factory):
    password = faker.pystr(min_chars=8, max_chars=32)
    user = user_factory(password=password)
    pw_file = password_file_factory([user])
    print(f"pwfile: {pw_file}")
    print(f"user: {user}")
    auth_user = await authenticate(user.username, password)
    assert auth_user.username == user.username
    assert auth_user.pw_hash == user.pw_hash
    assert auth_user.maildir == user.maildir
    await logger.shutdown()
