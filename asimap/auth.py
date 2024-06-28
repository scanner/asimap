"""
This module defines classes that are used by the main server to
authenticate users.
"""

# system imports
#
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Union

# 3rd party imports
#
import aiofiles.os

# asimapd imports
#
from asimap.exceptions import BadAuthentication, NoSuchUser
from asimap.hashers import acheck_password

if TYPE_CHECKING:
    from _typeshed import StrPath

logger = logging.getLogger("asimap.auth")

# We populate a dict of mappings from username to their user object.
# This is read in from the pw file.
#
USERS: Dict[str, "PWUser"] = {}

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
class PWUser:
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
async def authenticate(username: str, password: str) -> PWUser:
    """
    Authenticate the given username with the given password against our set
    of users.

    If the password file has been a modification time more recent then
    PW_FILE_LAST_TIMESTAMP, then before we lookup and authenticate a user we
    will re-read all the users into memory.

    NOTE: Obviously this is meant for "small" numbers of users in the hundreds
          range.
    """
    global PW_FILE_LAST_TIMESTAMP
    mtime = await aiofiles.os.path.getmtime(PW_FILE_LOCATION)
    if mtime > PW_FILE_LAST_TIMESTAMP:
        logger.info(
            "Reading password file due to last modified: %s", PW_FILE_LOCATION
        )
        await read_users_from_file(PW_FILE_LOCATION)
        PW_FILE_LAST_TIMESTAMP = mtime

    if username not in USERS:
        raise NoSuchUser(f"No such user '{username}'")

    user = USERS[username]
    if not await acheck_password(password, user.pw_hash):
        raise BadAuthentication
    return user


####################################################################
#
async def read_users_from_file(pw_file_name: "StrPath") -> None:
    """
    Reads all the user entries from the password file, construction User
    objects for each one. Then updates `USERS` with new dict of User objects.

    NOTE: If the `maildir` path in the password file is not absolute then the
          path is considered relative to the location of the password file.

    NOTE: We should put this in to a common "apricot systematic" module that
          can be shared by both asimapd and as_email_service.
    """
    pw_file_name = Path(pw_file_name)
    users = {}
    async with aiofiles.open(str(pw_file_name), "r") as f:
        async for line in f:
            line = line.strip()
            if not line or line[0] == "#":
                continue
            try:
                maildir: Union[str | Path]
                username, pw_hash, maildir = [
                    x.strip() for x in line.split(":")
                ]
                maildir = Path(maildir)
                if not maildir.is_absolute():
                    maildir = pw_file_name.parent / maildir
                users[username] = PWUser(username, maildir, pw_hash)
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


####################################################################
#
def write_pwfile(pwfile: Path, accounts: Dict[str, PWUser]) -> None:
    """
    we support a password file by email account with the password hash and
    maildir for that email account. This is for inteegration with other
    services (such as the asimap service)

    This will write all the entries in the accounts dict in to the indicated
    password file.
    """
    new_pwfile = pwfile.with_suffix(".new")
    with new_pwfile.open("w") as f:
        f.write(
            f"# File generated by asimap set_password at {datetime.now()}\n"
        )
        for account in sorted(accounts.keys()):
            # Maildir is written as a path relative to the location of the
            # pwfile. This is because we do not know how these files are rooted
            # when other services read them so we them relative to the pwfile.
            #
            user = accounts[account]
            f.write(f"{account}:{user.pw_hash}:{user.maildir}\n")
    new_pwfile.rename(pwfile)
