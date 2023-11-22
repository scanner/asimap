"""
This module defines classes that are used by the main server to
authenticate users.
"""
# system imports
#
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict

# 3rd party imports
#
import aiofiles.os

# asimapd imports
#
from asimap.exceptions import BadAuthentication, NoSuchUser
from asimap.hashers import check_password

if TYPE_CHECKING:
    from _typeshed import StrPath

logger = logging.getLogger("asimap.auth")

# We populate a dict of mappings from username to their user object.
# This is read in from the pw file.
#
USERS: Dict[str, "User"] = {}

# We keep track of the last time we read the pw file so that if the underlying
# file has its timestamp changed we know to read the users in again. This value
# is floating point number giving the number of seconds since the epoch.
#
PW_FILE_LAST_TIMESTAMP = 0.0

# This is the default location for the password file. It is expected to be
# modified by the asimapd main module based on passed in parameters.
#
# This file is a text file of the format:
#    <username>:<password hash>:<mail dir>
#
# Lines begining with "#" are comments. whitespace is stripped from each
# element. The acceptable format of the password hash is determine by what the
# `asimap.hashers` module considers valid.
#
PW_FILE_LOCATION = "/var/db/asimapd_passwords.txt"


##################################################################
##################################################################
#
class User:
    """
    The basic user object. The user name, their password hash, and the path
    to their Mail dir.
    """

    ##################################################################
    #
    def __init__(self, username: str, maildir: "StrPath", password_hash: str):
        """ """
        self.username = username
        self.maildir = Path(maildir)
        self.pw_hash = password_hash

    ##################################################################
    #
    def __str__(self):
        return self.username


####################################################################
#
async def authenticate(username: str, password: str) -> User:
    """
    Authenticate the given username with the given password against our set
    of users.

    If the password file has been a modification time more recent then
    PW_FILE_LAST_TIMESTAMP, then before we lookup and authenticate a user we
    will re-read all the users into memory.

    NOTE: Obviously this is meant for "small" numbers of users in the hundreds
          range.
    """
    mtime = await aiofiles.os.path.getmtime(PW_FILE_LOCATION)
    if mtime > PW_FILE_LAST_TIMESTAMP:
        logger.info(
            "Reading password file due to last modified: %s", PW_FILE_LOCATION
        )
        await read_users_from_file(PW_FILE_LOCATION)

    if username not in USERS:
        raise NoSuchUser(f"No such user '{username}'")

    user = USERS[username]
    if not check_password(password, user.pw_hash):
        raise BadAuthentication
    return user


####################################################################
#
async def read_users_from_file(pw_file_name: str) -> None:
    """
    Reads all the user entries from the password file, construction User
    objects for each one. Then updates `USERS` with new dict of User objects.
    """
    users = {}
    async with aiofiles.open(pw_file_name, "r") as f:
        async for line in f:
            line = line.strip()
            if not line or line[0] == "#":
                continue
            try:
                username, pw_hash, maildir = [
                    x.strip() for x in line.split(":")
                ]
                users[username] = User(username, maildir, pw_hash)
            except ValueError as exc:
                logger.error(
                    "Unable to unpack password record %s: %s",
                    line,
                    exc,
                )
    for username, user in users.items():
        USERS[username] = users[username]

    # And delete any records from USER's that were not in the pwfile.
    #
    existing_users = set(USERS.keys())
    new_users = set(users.keys())
    for username in existing_users - new_users:
        del USERS[username]
