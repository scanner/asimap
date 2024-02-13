[![Build Status](https://drone.apricot.com/api/badges/scanner/asimap/status.svg?ref=refs/heads/main)](https://drone.apricot.com/scanner/asimap)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

'asimap' is a python IMAP server that uses python's mailbox module to
provide the backing store. This lets us export things like an MH mail
store via IMAP. Actually the way it is coded right now it ONLY support
'mailbox.MH' style folders.

We go to various lengths to actually work alongside currently running
MH clients accessing the same files at the same time.

It uses a multiprocess model where there is a main server process
running as root and for any logged in user there is a sub-process
running as that user. If a user logs in via more than one mail client
all of their connections will be handled by the same
sub-process. Sub-processes stick around for a short while after a user
disconnects to avoid startup time if they connect again within, say, 30
minutes.

This is not a high performance server being pure python and it is not
a highly scaleable one requiring a process for every logged in user.
