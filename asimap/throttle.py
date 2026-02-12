#!/usr/bin/env python
#
# File: $Id$
#
"""
This module has some simple logic to deal with failed login attempt throttling.
"""

# system imports
#
import logging
import time

# We use a pair of dicts that track how often we have had login attempts
# against a username or attempts for user names that do not exist.
#
# If a specific login has a bunch of login failures against it in rapid
# succession then we will actually automatically fail any further login
# attempts against this user for a short amount of time greatly impairing any
# brute force attempts to guess passwords.
#
# Also if a specific IP address attempts to login as users that do not exist
# more than a certain amount in a certain time period then we lock out that IP
# address for attempting to authenticate for a period of time greatly impairing
# any brute force attempts to suss out accounts.
#
# XXX Maybe when a remote connection hits one of those limits we
#     should just not respond (no BAD, no NO, just dead air..)
#

# Key is the user name, value is a tuple of number of attempts within the
# timeout period, and the last time they tried to authenticate this user and
# failed.
#
BAD_USER_AUTHS: dict[str, tuple[int, float]] = {}

# Key is the ip address of the IMAP client, value is a tuple of number of
# attempts within the timeout period, and the last time they tried to
# authenticate this and failed for any reason.
#
BAD_IP_AUTHS: dict[str, tuple[int, float]] = {}

# How many seconds before we purge an entry from the dicts.
#
PURGE_TIME = 60

# How many attempts are they allowed within PURGE_TIME before we decide that
# they are trying to brute force something?
#
# We allow one more attempt for a given address in case an address is basically
# mulitple different users (like behind a home gateway). This way the bad user
# will get locked out after 4 attempts but we will allow other users to login
# successfully from the same ip address.
#
MAX_USER_ATTEMPTS = 4
MAX_ADDR_ATTEMPTS = 5

logger = logging.getLogger("asimap.throttle")


####################################################################
#
def login_failed(user: str, addr: str) -> None:
    """
    We had a login attempt that failed, likely due to a bad password.
    Record this attempt.

    The failure is recorded both for the username and the address it came from.

    So a number of bad attempts locks both that username from being logged in
    from and the address the login attempt came from.

    XXX There is a fundamental flaw with this in that a malicious agent that
        knows how our throttling works can esssentially conduct a denial of
        service against usernames it knows about.

        To mitigate this somewhat we will block an IP address that has too many
        failures before we will block a username that has too many failures.

    NOTE: The purpose of the address based lockout is to shut down IP addresses
          that are trying a bunch of different user names.

    Arguments:
    - `user`: The username that they tried to login with
    - `addr`: The IP address of the client that tried to login
    """
    now = time.time()
    if user in BAD_USER_AUTHS:
        BAD_USER_AUTHS[user] = (BAD_USER_AUTHS[user][0] + 1, now)
    else:
        BAD_USER_AUTHS[user] = (1, now)

    if addr in BAD_IP_AUTHS:
        BAD_IP_AUTHS[addr] = (BAD_IP_AUTHS[addr][0] + 1, now)
    else:
        BAD_IP_AUTHS[addr] = (1, now)


####################################################################
#
def check_allow(user: str, addr: str) -> bool:
    """
    Check the given user and client address to see if they are ones
    that are currently being throttled. Retrun True if either the
    username or client address is being throttled.

    Arguments:
    - `user`: The username that they are trying to login with
    - `addr`: The IP address that they are trying to login from
    """

    # If user and/or client addr are in the tracking dicts, but the
    # last attempt time is more than <n> seconds ago we clear those
    # entries and return True.
    #
    now = time.time()
    if user in BAD_USER_AUTHS and now - BAD_USER_AUTHS[user][1] > PURGE_TIME:
        logger.info(f"clearing '{user}' from BAD_USER_AUTHS")
        del BAD_USER_AUTHS[user]
    if addr in BAD_IP_AUTHS and now - BAD_IP_AUTHS[addr][1] > PURGE_TIME:
        logger.info(f"clearing '{addr}' from BAD_IP_AUTHS")
        del BAD_IP_AUTHS[addr]

    # if the user or client addr is NOT in either of the tracking dicts
    # then we return True.
    #
    if user not in BAD_USER_AUTHS and addr not in BAD_IP_AUTHS:
        return True

    # The entries are still in the dict and not expired (ie: older than the
    # PURGE_TIME). See if they have exceeded the number of allowable attempts.
    #
    if user in BAD_USER_AUTHS and BAD_USER_AUTHS[user][0] > MAX_USER_ATTEMPTS:
        logger.warning(
            "Deny: too many attempts for user: '%s', from address: %s",
            user,
            addr,
        )
        return False

    if addr in BAD_IP_AUTHS and BAD_IP_AUTHS[addr][0] > MAX_ADDR_ATTEMPTS:
        logger.warning(f"Deny: too many attempts from address: {addr}")
        return False

    # Otherwise they are not yet blocked from attempting to login.
    #
    return True
