"""
pytest fixtures for testing `asimap`
"""
# system imports
#
import asyncio
import imaplib
import json
import ssl
import threading
import time
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Optional, Union

# 3rd party imports
#
import pytest
import trustme

# project imports
#
import asimap.auth

from ..server import IMAPServer
from ..user_server import set_user_server_program
from .factories import UserFactory


####################################################################
#
@pytest.fixture(scope="session")
def ssl_certs():
    """
    Creates certificates using `trustme`. What is returned is a tuple of a
    `trustme.CA()` instance, and the `trustme` issued server cert.
    """
    ca = trustme.CA()
    server_cert = ca.issue_cert("127.0.0.1", "localhost", "::1")
    return (ca, server_cert)


####################################################################
#
@pytest.fixture(autouse=True)
def mailbox_dir(tmp_path):
    """
    The directory all of the mail dirs will be in for our tests
    """
    mail_base_dir = tmp_path / "mail_base_dir"
    mail_base_dir.mkdir(parents=True, exist_ok=True)
    yield mail_base_dir


####################################################################
#
@pytest.fixture
def user_factory(mailbox_dir):
    def make_user(*args, **kwargs):
        user = UserFactory(*args, **kwargs)
        if "maildir" not in kwargs:
            maildir = mailbox_dir / user.username
            maildir.mkdir(parents=True, exist_ok=True)
            user.maildir = maildir
        return user

    yield make_user


####################################################################
#
@pytest.fixture
def password_file_factory(tmp_path):
    """
    Returns a function that when called will create a password file and
    setup the auth module to use it, given the users it is called with.
    """

    def make_pw_file(users):
        # XXX Maybe should randomize the file name?
        pw_file_location = tmp_path / "asimap_pwfile.txt"
        with pw_file_location.open("w") as f:
            for user in users:
                f.write(f"{user.username}:{user.pw_hash}:{user.maildir}\n")
                print(f"{user.username}:{user.pw_hash}:{user.maildir}\n")
        setattr(asimap.auth, "PW_FILE_LOCATION", pw_file_location)
        return pw_file_location

    orig_location = getattr(asimap.auth, "PW_FILE_LOCATION")
    yield make_pw_file
    setattr(asimap.auth, "PW_FILE_LOCATION", orig_location)


####################################################################
#
@pytest.fixture
def email_factory(faker):
    """
    Returns a factory that creates email.message.EmailMessages

    For now we will always create MIMEMultipart messages with a text part, html
    alternative, and a binary attachment.
    """

    # TODO: have this factory take kwargs for headers the caller can set in the
    #       generated email.
    #
    def make_email(**kwargs):
        """
        if kwargs for 'subject', 'from' or 'to' are provided use those in
        the message instead of faker generated ones.

        NOTE: `from` is a reserverd word in python so you need to specify
              `frm`
        """
        msg = EmailMessage()
        msg["Message-ID"] = faker.uuid4()
        msg["Subject"] = (
            faker.sentence() if "subject" not in kwargs else kwargs["subject"]
        )
        if "msg_from" not in kwargs:
            username, domain_name = faker.email().split("@")
            msg["From"] = Address(faker.name(), username, domain_name)
        else:
            msg["From"] = kwargs["msg_from"]

        if "to" not in kwargs:
            username, domain_name = faker.email().split("@")
            msg["To"] = Address(faker.name(), username, domain_name)
        else:
            msg["To"] = kwargs["to"]

        message_content = faker.paragraphs(nb=5)
        msg.set_content("\n".join(message_content))
        paragraphs = "\n".join([f"<p>{x}</p>" for x in message_content])
        msg.add_alternative(
            f"<html><head></head><body>{paragraphs}</body></html>",
            subtype="html",
        )
        return msg

    return make_email


####################################################################
#
@pytest.fixture
def good_received_imap_messages():
    """
    Loop over a file of imap messages
    """
    imap_messages_file = (
        Path(__file__).parent / "fixtures/good_received_imap_messages.json"
    )
    messages = json.loads(imap_messages_file.read_text())
    return messages


####################################################################
#
@pytest.fixture
def imap_server(faker, ssl_certs, user_factory, password_file_factory):
    """
    Starts an IMAP Server in a separate thread and yields an imaplib client
    connected to that server (along with other data like username, password,
    etc.)
    """
    password = faker.password()
    user = user_factory(password=password)
    pw_file = password_file_factory([user])

    ca, server_cert = ssl_certs
    host = "127.0.0.1"
    port = faker.pyint(min_value=1024, max_value=65535)

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_cert.configure_cert(ssl_context)
    server = IMAPServer(host, port, ssl_context, pw_file, debug=True)

    ############################
    #
    # start a mini server.. how cute
    #
    def start_server():
        asyncio.run(server.run())

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Sleep for a teeny bit to let our server actually start up
    #
    time.sleep(0.1)
    client_ssl_context = ssl.create_default_context()
    ca.configure_trust(client_ssl_context)
    imap = imaplib.IMAP4_SSL(
        host=host, port=port, ssl_context=client_ssl_context, timeout=1
    )

    yield {"client": imap, "user": user, "password": password, "server": server}

    try:
        server.asyncio_server.close()
    except Exception as exc:
        print(f"server exception: {exc}")
    server_thread.join(timeout=5.0)


####################################################################
#
@pytest.fixture()
def imap_user_server():
    """
    When running integration tests that need to log in as a user we need to
    say where the user server program is.
    """
    asimapd_user_prg = (
        Path(__file__).parent.parent.parent / "scripts/asimapd_user.py"
    )
    set_user_server_program(asimapd_user_prg)


####################################################################
#
@pytest.fixture
def mock_time(mocker):
    """
    in the throttle module mock out `time.time()` to return the values we
    want it to.

    This fixture is intended to let the user define the values that are
    returned whenver `time()` is called in the throttle module.
    """
    mck_time = mocker.Mock("asimap.throttle.time.time")
    mocker.patch("asimap.throttle.time.time", new=mck_time)
    return mck_time


####################################################################
#
@pytest.fixture
def bunch_of_email_in_folder(email_factory, tmp_path):
    """
    Create a function that will create a specified number of emails in the
    specified folder. You can also supply a function that generates the keys to
    use for the messages (so you can test things like 'pack')

    Returns the path to the maildir (the parent of all the folders)
    """
    mh_dir = tmp_path / "Mail"

    def create_emails(
        num_emails: int = 20,
        folder: str = "inbox",
        sequence: Optional[Union[list, tuple, Iterable]] = None,
    ):
        folder_path = mh_dir / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        if sequence is None:
            sequence = list(range(1, num_emails + 1))
        for i, key in zip(range(num_emails), sequence):
            msg = email_factory()
            msg_file = folder_path / str(key)
            msg_file.write_bytes(msg.as_bytes())

        return mh_dir

    return create_emails
