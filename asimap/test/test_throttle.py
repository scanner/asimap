"""
Test our throttle of clients that attempt to login too much.
"""

# System imports
#
from time import time
from unittest.mock import MagicMock

# 3rd party imports
#
from faker import Faker

# Project imports
#
from ..throttle import (
    MAX_ADDR_ATTEMPTS,
    MAX_USER_ATTEMPTS,
    PURGE_TIME,
    check_allow,
    login_failed,
)


####################################################################
#
def test_throttle_by_user(faker: Faker, mock_time: MagicMock) -> None:
    user = faker.email()
    ip_addr = faker.ipv4()

    # No failures, it is going to succeed.
    #
    now = time()
    mock_time.return_value = now
    assert check_allow(user, ip_addr)

    # Now register several failures such that we exceed the allowed number of
    # failures. Each of these comes from a different address to not trigger
    # failing by address.
    #
    for _i in range(MAX_USER_ATTEMPTS):
        now += 1
        mock_time.return_value = now
        ip_addr = faker.ipv4()
        login_failed(user, ip_addr)
        assert check_allow(user, ip_addr)

    # The next login check by user will fail.
    #
    now += 1
    mock_time.return_value = now
    ip_addr = faker.ipv4()
    login_failed(user, ip_addr)
    assert check_allow(user, ip_addr) is False

    # Move time forward by the PURGE_TIME. This user should no longer be
    # throttled.
    #
    now += PURGE_TIME + 1
    mock_time.return_value = now
    assert check_allow(user, ip_addr)


####################################################################
#
def test_throttle_by_address(faker: Faker, mock_time: MagicMock) -> None:
    user = faker.email()
    ip_addr = faker.ipv4()

    # No failures, it is going to succeed.
    #
    now = time()
    mock_time.return_value = now
    assert check_allow(user, ip_addr)

    # Now register several failures such that we exceed the allowed number of
    # failures. Each of these comes from a different address to not trigger
    # failing by address.
    #
    for _i in range(MAX_ADDR_ATTEMPTS):
        now += 1
        mock_time.return_value = now
        user = faker.email()
        login_failed(user, ip_addr)
        assert check_allow(user, ip_addr)

    # The next login check by user will fail.
    #
    now += 1
    mock_time.return_value = now
    user = faker.email()
    login_failed(user, ip_addr)
    assert check_allow(user, ip_addr) is False

    # Move time forward by the PURGE_TIME. This address should no longer be
    # throttled.
    #
    now += PURGE_TIME + 1
    mock_time.return_value = now
    user = faker.email()
    assert check_allow(user, ip_addr)
