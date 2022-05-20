#!/usr/bin/env python
#

from distutils.core import setup

from asimap import __version__

setup(
    name="asimap",
    version=__version__,
    description="A pure python based IMAP server using mailbox store",
    long_description=(
        "asimap is a python based IMAP server using local file "
        "stores, like MH as the mail store."
    ),
    author="Scanner",
    author_email="scanner@apricot.com",
    url="https://github.com/scanner/asimap",
    download_url="https://github.com/asimap/archives/master",
    packages=["asimap"],
    # scripts=["asimapd.py", "asimapd_user.py"]
    data_files=[
        ("etc/rc.d", ["utils/asimapd.sh"]),
        ("libexec", ["asimapd.py", "asimapd_user.py"]),
    ],
    setup_requires=["pytest-runner"],
    tests_require=["pytest"],
)
