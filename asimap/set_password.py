#!/usr/bin/env python
#
"""
A script to set passwords for asimap accounts (creates the account if it
does not exist.)

This is primarily used for setting up a test development environment. In the
Apricot Systematic typical deployment the password file is managed by the
`as_email_service`

If the `password` is not supplied an unuseable password is set effectively
disabling the account.

If the account does not already exist `maildir` must be specified (as it
indicates the users mail directory root)

NOTE: `maildir` is in relation to the root when asimapd is running.

Usage:
  set_password [--pwfile=<pwfile>] <username> [<password>] [<maildir>]

Options:
  --version
  -h, --help         Show this text and exit
  --pwfile=<pwfile>  The file that contains the users and their hashed passwords
                     The env. var is `PWFILE`. Defaults to `/opt/asimap/pwfile`
"""

# system imports
#
import asyncio
import getpass
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# 3rd party imports
#
from docopt import docopt
from dotenv import load_dotenv

# asimapd imports
#
from asimap import __version__ as VERSION
from asimap.auth import (
    PW_FILE_LOCATION,
    USERS,
    PWUser,
    read_users_from_file,
    write_pwfile,
)
from asimap.hashers import make_password

if TYPE_CHECKING:
    from _typeshed import StrPath

logger = logging.getLogger("asimap.set_password")


####################################################################
#
async def update_pw_file(
    pwfile: Path,
    username: str,
    password: Optional[str] = None,
    maildir: Optional[Path] = None,
) -> None:
    """
    Read in the password file. If the given user does not exist, add it to the
    """
    pwfile = Path(pwfile)
    pw_hash = make_password(password) if password else "XXX"

    await read_users_from_file(pwfile)
    if username not in USERS:
        if maildir is None:
            raise RuntimeError(
                f"'{username}': maildir must be specified when creating account"
            )
        user = PWUser(username, maildir, pw_hash)
        USERS[username] = user
    else:
        user = USERS[username]
        user.pw_hash = pw_hash
        if maildir:
            user.maildir = maildir
    write_pwfile(pwfile, USERS)


#############################################################################
#
def main():
    """ """
    args = docopt(__doc__, version=VERSION)
    pwfile: StrPath = args["--pwfile"]
    username = args["<username>"]
    password: Optional[str] = args["<password>"]
    maildir_str: str = args["<maildir>"]

    load_dotenv()

    if not password:
        while True:
            pw1 = getpass.getpass("Password: ")
            pw2 = getpass.getpass("Enter password again to verify: ")
            if pw1 == pw2:
                password = pw1
                break
            print("Passwords do NOT match! Re-enter please.")

    if pwfile is None:
        pwfile = (
            os.environ["PWFILE"] if "PWFILE" in os.environ else PW_FILE_LOCATION
        )

    pwfile = Path(pwfile)
    if not pwfile.exists():
        pwfile.write_text("")

    # NOTE: We do not validate the path to `maildir` because we do not know in
    #       what context 'set password' is being run vs in what context asimapd
    #       is being run.
    #
    maildir = Path(maildir_str) if maildir_str else None

    asyncio.run(update_pw_file(pwfile, username, password, maildir))


############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
############################################################################
############################################################################
