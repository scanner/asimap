#!/usr/bin/env python
#
"""
POP3 command parsing module.

POP3 commands are simple single-line messages: COMMAND [args]\\r\\n
This is significantly simpler than IMAP parsing (no literals, no
continuations, no complex fetch attributes).
"""


########################################################################
#
class BadPOP3Command(Exception):
    """Raised when a POP3 command cannot be parsed."""

    def __init__(self, value: str = "bad command"):
        self.value = value
        super().__init__(value)

    def __str__(self) -> str:
        return f"BadPOP3Command: {self.value}"


VALID_POP3_COMMANDS = {
    "USER",
    "PASS",
    "STAT",
    "LIST",
    "RETR",
    "DELE",
    "NOOP",
    "RSET",
    "QUIT",
    "TOP",
    "UIDL",
    "CAPA",
}


########################################################################
#
class POP3Command:
    """
    A parsed POP3 command.

    Attributes:
        command: The command keyword, uppercased (e.g., "STAT", "RETR")
        args: The argument string after the command, stripped
        raw: The original raw command line
    """

    def __init__(self, raw_line: str):
        self.raw = raw_line.strip()
        parts = self.raw.split(None, 1)
        if not parts:
            raise BadPOP3Command("empty command")
        self.command = parts[0].upper()
        if self.command not in VALID_POP3_COMMANDS:
            raise BadPOP3Command(f"unknown command: {self.command}")
        self.args = parts[1].strip() if len(parts) > 1 else ""

    def __str__(self) -> str:
        return self.raw

    def __repr__(self) -> str:
        return f"POP3Command({self.command!r}, {self.args!r})"


########################################################################
#
def parse_pop3_command(msg: bytes | str) -> POP3Command:
    """
    Parse a raw POP3 command line into a POP3Command.

    Args:
        msg: Raw command bytes or string from the client

    Returns:
        A parsed POP3Command object

    Raises:
        BadPOP3Command: If the command cannot be parsed
    """
    if isinstance(msg, bytes):
        try:
            line = msg.decode("latin-1")
        except UnicodeDecodeError as e:
            raise BadPOP3Command("unable to decode command") from e
    else:
        line = msg
    return POP3Command(line)
