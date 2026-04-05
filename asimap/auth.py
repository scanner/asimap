"""
This module defines classes that are used by the main server to
authenticate users.
"""

# system imports
#
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
USERS: dict[str, "PWUser"] = {}

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
        """
        Args:
            username: The user's login name.
            maildir: Path to the root of the user's MH mail directory.
            password_hash: A Django-compatible password hash string.
        """
        self.username = username
        self.maildir = Path(maildir)
        self.pw_hash = password_hash

    ##################################################################
    #
    def __str__(self) -> str:
        return self.username


####################################################################
#
async def authenticate(username: str, password: str) -> PWUser:
    """Authenticate a user by username and plaintext password.

    If the password file has a modification time more recent than
    ``PW_FILE_LAST_TIMESTAMP``, the user list is reloaded before the lookup.

    NOTE: This is designed for small deployments (hundreds of users at most).

    Args:
        username: The login name to authenticate.
        password: The plaintext password to verify.

    Returns:
        The authenticated :class:`PWUser` object.

    Raises:
        NoSuchUser: If ``username`` does not exist in the password file.
        BadAuthentication: If the password does not match.
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
    """Read and parse the password file, replacing the in-memory user list.

    Each non-comment line must have the format::

        <username>:<password_hash>:<maildir>

    If ``maildir`` is a relative path it is resolved relative to the
    directory containing the password file. After reading, any users no
    longer present in the file are removed from ``USERS``.

    NOTE: If the ``maildir`` path in the password file is not absolute it is
          treated as relative to the directory containing the password file.

    Args:
        pw_file_name: Path to the password file to read.
    """
    pw_file_name = Path(pw_file_name)
    users = {}
    async with aiofiles.open(str(pw_file_name)) as f:
        async for line in f:
            line = line.strip()
            if not line or line[0] == "#":
                continue
            try:
                maildir: str | Path
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
    for username, _user in users.items():
        USERS[username] = users[username]

    # And delete any records from USER's that were not in the pwfile.
    #
    existing_users = set(USERS.keys())
    new_users = set(users.keys())
    for username in existing_users - new_users:
        del USERS[username]


####################################################################
#
def write_pwfile(pwfile: Path, accounts: dict[str, PWUser]) -> None:
    """Write a set of user accounts to the asimap password file.

    Writes atomically by first writing to ``<pwfile>.new`` and then renaming
    it over the destination. The maildir paths are stored relative to the
    password file's parent directory so that they remain valid regardless of
    where external services mount the file.

    Args:
        pwfile: Destination path for the password file.
        accounts: Mapping of username → :class:`PWUser` to persist.
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
