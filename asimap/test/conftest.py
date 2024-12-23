"""
pytest fixtures for testing `asimap`
"""

# System imports
#
import asyncio
import imaplib
import ssl
import threading
import time
from email import message_from_bytes
from email.header import decode_header
from email.headerregistry import Address
from email.message import EmailMessage, Message
from email.policy import SMTP, default
from email.utils import format_datetime
from mailbox import MH, MHMessage
from pathlib import Path
from typing import (
    Any,
    Callable,
    Generator,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeAlias,
    Union,
)

# 3rd party imports
#
import pytest
import pytest_asyncio
import trustme

# project imports
#
import asimap.auth

from ..server import IMAPClient, IMAPServer
from ..user_server import (
    IMAPClientProxy,
    IMAPUserServer,
    set_user_server_program,
)
from .factories import UserFactory

EmailFactoryType: TypeAlias = Callable[..., EmailMessage]

REPLACE_LINESEP = {ord("\r"): None, ord("\n"): None}

TESTS_PATH = Path(__file__).parent
PROBLEMATIC_EMAIL_FIXTURE_DIR = TESTS_PATH / "fixtures" / "mhdir" / "problems"
PROBLEMATIC_EMAIL_MSG_KEYS = sorted(
    int(str(x.name))
    for x in PROBLEMATIC_EMAIL_FIXTURE_DIR.iterdir()
    if not x.is_dir()
)
STATIC_EMAIL_FIXTURE_DIR = TESTS_PATH / "fixtures" / "mhdir" / "one"
STATIC_EMAIL_MSG_KEYS = sorted(
    int(str(x.name))
    for x in STATIC_EMAIL_FIXTURE_DIR.iterdir()
    if not x.is_dir()
)


####################################################################
#
def client_push_responses(
    client: Union[IMAPClient, IMAPClientProxy], strip: bool = True
) -> List[str]:
    """
    A helper function that returns all of the push's for this client since
    the last time this function was called.
    """
    results = [
        y for x in client.push.call_args_list for y in x.args  # type: ignore [attr-defined]
    ]
    if strip:
        results = [x.strip() for x in results]
    client.push.call_args_list = []  # type: ignore [attr-defined]
    return results


####################################################################
#
def decode_headers(headers: List[str]) -> List[str]:
    """
    Given a list of headers, decode each of them. If, after decoding, make
    sure that they are decoded back in to strings if they were encoded
    """
    result: List[str] = []
    for hdr in headers:
        h = decode_header(hdr)
        tb = []
        for th, encoding in h:
            if encoding is not None:
                hdr_text = str(th, encoding)
            else:
                hdr_text = th
            tb.append(hdr_text)
        result.append("".join(tb))
    return result


####################################################################
#
def assert_email_equal(
    msg1: Message, msg2: Message, ignore_headers: Optional[List[str]] = None
):
    """
    Because we can not directly compare a Message and EmailMessage object
    we need to compare their parts. Since an EmailMessage is a sub-class of
    Message it will have all the same methods necessary for comparison.
    """
    # Compare all headers, unless we are ignoring them.
    #
    if ignore_headers:
        ignore_headers = [x.lower() for x in ignore_headers]
        for header, value in msg1.items():
            if header.lower() in ignore_headers:
                continue
            h1, h2 = decode_headers([msg2[header], value])
            h1 = h1.translate(REPLACE_LINESEP).strip()
            h2 = h2.translate(REPLACE_LINESEP).strip()
            assert h1 == h2
    else:
        assert len(msg1.keys()) == len(msg2.keys())
        keys = set(msg1.keys())
        for header in sorted(list(keys)):
            value1 = decode_headers(msg1.get_all(header, failobj=[]))
            value2 = decode_headers(msg2.get_all(header, failobj=[]))
            value1 = sorted(
                [x.translate(REPLACE_LINESEP).strip() for x in value1]
            )
            value2 = sorted(
                [x.translate(REPLACE_LINESEP).strip() for x in value2]
            )
            assert value1 == value2

    assert msg1.is_multipart() == msg2.is_multipart()

    # If not multipart, the payload should be the same.
    #
    if not msg1.is_multipart():
        payload1 = msg1.get_payload().translate(REPLACE_LINESEP).strip()
        payload2 = msg2.get_payload().translate(REPLACE_LINESEP).strip()
        assert payload1 == payload2

    # Otherwise, compare each part.
    #
    parts1 = msg1.get_payload()
    parts2 = msg2.get_payload()
    if isinstance(parts1, str) and isinstance(parts2, str):
        assert (
            parts1.translate(REPLACE_LINESEP).strip()
            == parts2.translate(REPLACE_LINESEP).strip()
        )
        return

    assert len(parts1) == len(parts2)

    for part1, part2 in zip(parts1, parts2):
        payload1 = part1.get_payload()
        payload2 = part1.get_payload()
        assert payload1 == payload2


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
            inbox = user.maildir / "inbox"
            inbox.mkdir()
            mh_seq = inbox / ".mh_sequences"
            mh_seq.touch()
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

    def make_pw_file(users: List[asimap.auth.PWUser]):
        pw_file_location = tmp_path / "asimap_pwfile.txt"
        accounts = {x.username: x for x in users}
        asimap.auth.write_pwfile(pw_file_location, accounts)
        setattr(asimap.auth, "PW_FILE_LOCATION", pw_file_location)
        return pw_file_location

    orig_location = getattr(asimap.auth, "PW_FILE_LOCATION")
    yield make_pw_file
    setattr(asimap.auth, "PW_FILE_LOCATION", orig_location)


####################################################################
#
@pytest.fixture
def email_factory(faker: Any) -> EmailFactoryType:
    """
    Returns a factory that creates email.message.EmailMessages

    For now we will always create MIMEMultipart messages with a text part, html
    alternative, and a binary attachment.
    """

    # TODO: have this factory take kwargs for headers the caller can set in the
    #       generated email.
    #
    def make_email(
        msg_from: Optional[str] = None,
        to: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> EmailMessage:
        """
        NOTE: `from` is a reserverd word in python so you need to specify
              `frm`
        """
        msg = EmailMessage()
        msg["Date"] = format_datetime(
            faker.date_time_between(start_date="-1yr")
        )
        msg["Message-ID"] = faker.uuid4()
        msg["Subject"] = subject if subject else faker.sentence()
        msg["From"] = (
            msg_from if msg_from else Address(faker.name(), faker.email())
        )
        msg["To"] = to if to else Address(faker.name(), faker.email())

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
def imap_server(
    faker,
    ssl_certs,
    user_factory,
    password_file_factory,
    bunch_of_email_in_folder,
):
    """
    Starts an IMAP Server in a separate thread and yields an imaplib client
    connected to that server (along with other data like username, password,
    etc.)

    NOTE: This is for pretty high level integration tests that rely on poking
          this server with an actual IMAP client.
    """
    password = faker.password()
    user = user_factory(password=password)
    pw_file = password_file_factory([user])

    bunch_of_email_in_folder(mh_dir=user.maildir)

    ca, server_cert = ssl_certs
    host = "127.0.0.1"
    port = faker.pyint(min_value=1024, max_value=65535)

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_cert.configure_cert(ssl_context)
    # XXX incorrect args.. pwfile is not password to the IMAPServer.
    server = IMAPServer(host, port, ssl_context, pw_file, debug=True)

    ############################
    #
    # start a mini server.. how cute
    #
    def start_server():
        try:
            asyncio.run(server.run())
        except Exception:
            return

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Sleep for a bit to let our server actually start up
    #
    time.sleep(2)
    client_ssl_context = ssl.create_default_context()
    ca.configure_trust(client_ssl_context)
    imap = imaplib.IMAP4_SSL(
        host=host, port=port, ssl_context=client_ssl_context, timeout=10
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
def imap_user_server_program():
    """
    When running integration tests that need to log in as a user we need to
    say where the user server program is.
    """
    asimapd_user_prg = Path(__file__).parent.parent / "asimapd_user.py"
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
def mh_folder(
    tmp_path: Path,
) -> Callable[[str, Optional[Path]], Tuple[Path, MH, MH]]:
    """
    Create the Mail dir and the inbox dir inside that mail dir.
    """

    def mk_folder(folder: str = "inbox", mh_dir: Optional[Path] = None):
        mh_dir = tmp_path / "Mail" if mh_dir is None else mh_dir
        mh = MH(mh_dir)
        m_folder = mh.add_folder(folder)
        return (mh_dir, mh, m_folder)

    return mk_folder


####################################################################
#
@pytest.fixture
def incr_email(
    faker: Generator,
    email_factory: EmailFactoryType,
    mh_folder: Callable[[str, Optional[Path]], Tuple[Path, MH, MH]],
    mh_dir: Optional[Path] = None,
) -> Callable[[Optional[str], Optional[Iterable[str]]], MHMessage]:
    """
    Returns a factory function that will add a single email to a specified
    MH folder. If no folder is specified it defaults to `inbox`. You can
    specify the sequences to add the message to via the `sequences`
    parameter. If no sequences are specified, adds the message to the `unseen`
    sequence.
    """

    def add_one_mail_to_folder(
        folder: Optional[str] = None, sequences: Optional[Iterable[str]] = None
    ) -> MHMessage:
        folder = folder if folder else "inbox"
        sequences = sequences if sequences is not None else ["unseen"]
        _, _, m_folder = mh_folder(folder, mh_dir)
        msg = MHMessage(email_factory())
        msg.set_sequences(sequences)
        m_folder.add(msg)
        return msg

    return add_one_mail_to_folder


####################################################################
#
@pytest.fixture
def bunch_of_email_in_folder(
    email_factory: EmailFactoryType,
    mh_folder: Callable[[str, Optional[Path]], Tuple[Path, MH, MH]],
) -> None:
    """
    Create a function that will create a specified number of emails in the
    specified folder. You can also supply a function that generates the keys to
    use for the messages (so you can test things like 'pack')

    Returns the path to the maildir (the parent of all the folders)
    """

    def create_emails(
        num_emails: int = 20,
        folder: str = "inbox",
        sequence: Optional[Union[list, tuple, Iterable]] = None,
        mh_dir: Optional[Path] = None,
    ):
        (mh_dir, _, m_folder) = mh_folder(folder, mh_dir)
        set_msgs_by_seq = True
        if sequence is None:
            set_msgs_by_seq = False
            sequence = list(range(1, num_emails + 1))
        seqs = m_folder.get_sequences()
        unseen_seq = seqs["unseen"] if "unseen" in seqs else []

        # We add messages to the folder one of two ways: if no sequence was
        # provied as an argument, then just add the messages to the folder
        # using the mailbox native `add` method.
        #
        # If sequences WAS provided the caller wants us to put messages in
        # specific keys.
        #
        for i, key in zip(range(num_emails), sequence):
            msg = MHMessage(email_factory())
            if set_msgs_by_seq:
                msg_path = Path(m_folder._path) / str(key)
                msg_path.write_text(msg.as_string(policy=SMTP))
                unseen_seq.append(key)
            else:
                msg.add_sequence("unseen")
                m_folder.add(msg)

        if set_msgs_by_seq:
            m_folder.set_sequences({"unseen": unseen_seq})
        return mh_dir

    return create_emails


####################################################################
#
@pytest_asyncio.fixture
async def imap_user_server(mh_folder):
    """
    The Mailbox tests need to create a mailbox instance, which needs an
    IMAPUserServer.
    """
    (mh_dir, _, _) = mh_folder()
    server = await IMAPUserServer.new(mh_dir)
    try:
        yield server
    finally:
        await server.shutdown()


####################################################################
#
@pytest_asyncio.fixture
async def imap_client_proxy(faker, mocker, imap_user_server):
    """
    Creates an IMAPClientProxy object that can be used by our client handlers
    for tests.

    There is no network stream reader/writer. This is intended for testing
    mbox.Mailbox and client.BaseClientHandler type objects where we only care
    about the `push` method on the imap proxy client. This push method is an
    async mock (ie: this is inteded for testing the responses from directly
    invoking methods on `mbox.Mailbox` and `client.BaseClientHandler` type
    objects that are expected to generate IMAP protocol responses.)

    This returns a generator so the caller can make multiple IMAPClientProxy
    objects (necessary for testing what happens when multiple clients are
    connected to the server.)
    """
    writers: List[asyncio.StreamWriter] = []

    # NOTE: We can just create a stream reader and feed it data if we need to:
    #       https://www.pythonfixing.com/2021/10/fixed-writing-pytest-testcases-for.html
    #
    # NOTE: For the stream writer we attach it to /dev/null.  Since we are
    #       patching the `push` method on the IMAPClientProxy we never expect
    #       the stream writer to get any data.. but we still we need one to
    #       create our IMAPClientProxy.
    #
    # XXX If we cared we should probably attach it to a text file or find
    #     someway to attach it to a text buffer.
    #
    async def _make_imap_client_proxy():
        rem_addr = "127.0.0.1"
        port = faker.pyint(min_value=1024, max_value=65535)
        name = f"{rem_addr}:{port}"
        server = imap_user_server

        loop = asyncio.get_event_loop()
        devnull_writer = open("/dev/null", "wb")
        writer_transport, writer_protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(loop=loop), devnull_writer
        )

        writer = asyncio.StreamWriter(
            writer_transport, writer_protocol, None, loop
        )
        reader = asyncio.StreamReader()
        imap_client_proxy = IMAPClientProxy(
            server, name, server.next_client_num, rem_addr, port, reader, writer
        )
        server.next_client_num += 1
        mocker.patch.object(imap_client_proxy, "push", mocker.AsyncMock())
        writers.append(writer)
        return imap_client_proxy

    def finalizer():
        """
        Called at the end of the test via pytest's finalizer mechanism this
        closes all the IMAPClientProxy StreamWriter's that were created.
        """
        for writer in writers:
            writer.close()

    return _make_imap_client_proxy


####################################################################
#
@pytest_asyncio.fixture
async def imap_user_server_and_client(imap_user_server, imap_client_proxy):
    """
    when doing simple client tests we only need one IMAPClientProxy so this
    fixture makes our tests a teeny bit simpler if you need both a client and a
    user server.

    NOTE: We will probably remove this fixture. It is here currently for legacy
          purposes because we use to create both the client proxy and user
          server in the same fixture.
    """
    server = imap_user_server
    client_proxy = await imap_client_proxy()
    try:
        yield (server, client_proxy)
    finally:
        # Give various tasks a chance to shutdown after being cancelled.
        await asyncio.sleep(0.5)


####################################################################
#
@pytest_asyncio.fixture
def mailbox_instance(bunch_of_email_in_folder, imap_user_server):
    """
    create a Mailbox which has email in it.
    """

    async def create_mailbox(name: str = "inbox", with_messages: bool = False):
        bunch_of_email_in_folder(folder=name)
        server = imap_user_server
        mbox = await server.get_mailbox(name)
        return mbox

    return create_mailbox


####################################################################
#
@pytest.fixture
def static_email_factory_bytes() -> Callable[[int], bytes]:
    """
    `email_factory` is good for a number of things but we want some tests
    with fixed inputs that have a richer variety of input.

    We have a bunch of pre-generated emails from MimeKit. This fixture will
    yield those messages as bytes.
    """

    def _factory(msg_key: int) -> bytes:
        msg_file = STATIC_EMAIL_FIXTURE_DIR / str(msg_key)
        return msg_file.read_bytes()

    return _factory


####################################################################
#
@pytest.fixture
def problematic_email_factory_bytes() -> Callable[[int], bytes]:
    """
    in our time on the internet we have seen lots of problematic email with
    various issues.We need to make sure that we handle these reasonably well
    """

    def _factory(msg_key: int) -> bytes:
        msg_file = PROBLEMATIC_EMAIL_FIXTURE_DIR / str(msg_key)
        return msg_file.read_bytes()

    return _factory


####################################################################
#
@pytest.fixture
def lots_of_headers_email() -> str:
    """
    Just get one email with lots of headers.
    """
    msg_file = Path(__file__).parent / "fixtures" / "mhdir" / "one" / "16"
    return msg_file.read_text()


####################################################################
#
@pytest.fixture
def big_static_email_bytes(static_email_factory_bytes) -> bytes:
    """
    A message with lots of parts with encodings. Mainly so we can test more
    complicated `FETCH` commands.
    """
    return static_email_factory_bytes(10)


####################################################################
#
@pytest_asyncio.fixture
async def mailbox_with_big_static_email(
    mh_folder, big_static_email_bytes, imap_user_server
):
    """
    Fixture for making `FETCH` tests a little easier.
    There will be _1_ message in the Mailbox `inbox`
    The mailbox is what this fixture returns.
    """
    NAME = "inbox"
    server = imap_user_server
    (mh_dir, _, m_folder) = mh_folder(NAME)
    msg = message_from_bytes(big_static_email_bytes, policy=default)
    msg_key = m_folder.add(msg)
    seqs = m_folder.get_sequences()
    seqs["unseen"] = [msg_key]
    m_folder.set_sequences(seqs)
    mbox = await server.get_mailbox(NAME)
    return mbox


####################################################################
#
@pytest_asyncio.fixture
async def mailbox_with_mimekit_email(
    mh_folder, static_email_factory_bytes, imap_user_server
):
    """
    Create a mailbox filled with all of our static email fixtures
    (originally all from the MimeKit fixture test data)
    """
    NAME = "inbox"
    server = imap_user_server
    (mh_dir, _, m_folder) = mh_folder(NAME)
    for msg_key in STATIC_EMAIL_MSG_KEYS:
        msg = message_from_bytes(
            static_email_factory_bytes(msg_key), policy=default
        )
        m_folder.add(msg)
    seqs = m_folder.get_sequences()
    seqs["unseen"] = STATIC_EMAIL_MSG_KEYS
    m_folder.set_sequences(seqs)
    mbox = await server.get_mailbox(NAME)
    return mbox


####################################################################
#
@pytest_asyncio.fixture
async def mailbox_with_problematic_email(
    mh_folder, problematic_email_factory_bytes, imap_user_server
):
    """
    Create a mailbox filled with all of our static email fixtures
    (originally all from the MimeKit fixture test data)
    """
    NAME = "inbox"
    server = imap_user_server
    (mh_dir, _, m_folder) = mh_folder(NAME)
    for msg_key in PROBLEMATIC_EMAIL_MSG_KEYS:
        msg = message_from_bytes(
            problematic_email_factory_bytes(msg_key), policy=default
        )
        m_folder.add(msg)
    seqs = m_folder.get_sequences()
    seqs["unseen"] = STATIC_EMAIL_MSG_KEYS
    m_folder.set_sequences(seqs)
    mbox = await server.get_mailbox(NAME)
    return mbox


####################################################################
#
@pytest_asyncio.fixture
async def mailbox_with_bunch_of_email(
    bunch_of_email_in_folder, imap_user_server
):
    """
    Email factory emails. For tests where we are not stressing about
    headers and email contents being anything fancy or complex.
    """
    NAME = "inbox"
    bunch_of_email_in_folder(folder=NAME)
    server = imap_user_server
    mbox = await server.get_mailbox(NAME)
    return mbox
