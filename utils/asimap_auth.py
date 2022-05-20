#!/usr/bin/env python
#
# File: $Id$
#
"""
A utility script to manage the asimap simple authentication file

XXX We really need to do some sort of locking on the pw file to prevent
    conflicts.
"""

import getpass
import hashlib
import optparse
import os
import os.path
import random
import shutil
import stat

# system imports
#
import sys
import time

# XXX NOTE: We copy the hash utilities out of the asimap module because it is
#           likely that this program is being run in an environment where they
#           are not installed (ie: not in the virtualenv used by the asimap
#           server itself.
#

# XXX Not configurable! this is where the password db for the asimap server goes
#     Letting it be configurable is a security as well.
#
PASSWORD_DB_LOCATION = "/var/db/asimapd_passwords.txt"

############################################################################
#
def setup_option_parser():
    """
    This function uses the python OptionParser module to define an option
    parser for parsing the command line options for this script. This does not
    actually parse the command line options. It returns the parser object that
    can be used for parsing them.
    """
    parser = optparse.OptionParser(usage="%prog [options]", version="1.0")

    parser.set_defaults(user=None)

    parser.add_option(
        "--user",
        action="store",
        type="string",
        dest="user",
        help="The user to set the password for",
    )
    return parser


############################################################################
#
def get_hexdigest(algorithm, salt, raw_password):
    """
    Returns a string of the hexdigest of the given plaintext password and salt
    using the given algorithm ('md5', 'sha1' or 'crypt').

    Borrowed from the django User auth model.
    """
    if algorithm == "crypt":
        try:
            import crypt
        except ImportError:
            raise ValueError(
                '"crypt" password algorithm not supported in this environment'
            )
        return crypt.crypt(raw_password, salt)

    if algorithm == "md5":
        return hashlib.md5(salt + raw_password).hexdigest()
    elif algorithm == "sha1":
        return hashlib.sha1(salt + raw_password).hexdigest()
    raise ValueError("Got unknown password algorithm type in password.")


####################################################################
#
def hash_password(raw_password):
    """
    Convert the given raw password in to the hex digest we store.

    Arguments:
    - `raw_password`: The plain text password
    """
    algo = "sha1"
    salt = get_hexdigest(algo, str(random.random()), str(random.random()))[:5]
    hsh = get_hexdigest(algo, salt, raw_password)
    return "%s$%s$%s" % (algo, salt, hsh)


####################################################################
#
def write_password(user, password_hash):
    """
    Read in the password file writing out to the new password file a line at a
    time.

    When we encounter a line that is the password entry for 'user' instead of
    writing out the existing line write out a new line based on the user and
    password that was passed in.

    If we go through the whole file without encountering this user then at the
    end write out this new user to the file.

    Finally replace the old password file with the new one.

    Arguments:
    - `user`: user whose password is being added or changed.
    - `password_hash`: The hashed and salted password to write to the file
    """

    new_pw_file = PASSWORD_DB_LOCATION + ".new"
    wrote_account = False

    with open(new_pw_file, "w+") as newf:
        if os.path.exists(PASSWORD_DB_LOCATION):
            # Backup the old file before we do anything.
            #
            backup_db = PASSWORD_DB_LOCATION + time.strftime(
                "%Y.%m.%d-%H.%M.%s"
            )
            shutil.copy(PASSWORD_DB_LOCATION, backup_db)

            with open(PASSWORD_DB_LOCATION, "r") as oldf:
                for line in oldf:
                    line = line.strip()
                    # If the line has a ':' in it then it is a password file
                    # entry.
                    #
                    if ":" in line:
                        u, pw = line.split(":")
                        if u.strip() == user:
                            wrote_account = True
                            newf.write("%s:%s\n" % (user, password_hash))
                        else:
                            newf.write(line + "\n")
                    else:
                        newf.write(line + "\n")

        # We have written the new password file with the contents of the old
        # password. If we have not yet written this user out to the file then
        # do that now.
        if not wrote_account:
            newf.write("%s:%s\n" % (user, password_hash))

    # we have now written the new password file. replace the old file with the
    # new file.
    #
    os.rename(new_pw_file, PASSWORD_DB_LOCATION)

    # Make sure that only the owner has r access to the db file.
    #
    os.chmod(PASSWORD_DB_LOCATION, stat.S_IRUSR)
    return


#############################################################################
#
def main():
    """
    Very simple program we do all the work of prompting for the new password in
    main.

    XXX Right now this program requires you run it as the owner of the password
       db file and have permission to create files in the directory where it
       exists. You MUST specify the user to set the password for on the command
       line.

       In the future this may be a setuid program where you can only specify
       the user if you are running it as root, otherwise you can only set the
       password of the user that ran the program and you can not specify the
       user at all.
    """
    parser = setup_option_parser()
    (options, args) = parser.parse_args()

    # You must pass the user name in. In the future when running setuid we
    # would only allow you to set the user if running as root, otherwise it
    # woudl be the user that invoked this program.
    #
    if options.user is None:
        sys.stderr.write(
            "You must specify the user to set the password for "
            "on the command line.\n"
        )
        sys.exit(1)

    print("Setting the password for '%s'" % options.user)
    while True:
        pw1 = getpass.getpass("Password: ")
        pw2 = getpass.getpass("Enter password again to verify: ")
        if pw1 == pw2:
            break
        print("Passwords do NOT match! Re-enter please.")

    write_password(options.user, hash_password(pw1))
    return


############################################################################
############################################################################
#
# Here is where it all starts
#
if __name__ == "__main__":
    main()
#
#
############################################################################
############################################################################
