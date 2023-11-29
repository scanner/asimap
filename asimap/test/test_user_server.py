"""
Test the user server.
"""
# system imports
#

# 3rd party imports
#
import pytest

# Project imports
#
from ..user_server import IMAPUserServer


####################################################################
#
@pytest.mark.asyncio
async def test_user_server_instantiate(mh_folder):
    (mh_dir, _, _) = mh_folder()
    user_server = await IMAPUserServer.new(mh_dir)
    assert user_server
