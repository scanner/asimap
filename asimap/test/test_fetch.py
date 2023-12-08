"""
Fetch.. the part that gets various bits and pieces of messages.
"""
# System imports
#
from typing import Any, Dict, List, Tuple

# Project imports
#
from ..fetch import STR_TO_FETCH_OP, FetchAtt


####################################################################
#
def test_fetch_create_and_str():
    """
    The FetchAtt's __str__ method is part of the reply to the IMAP client,
    so we need to make sure we get back the right strings for the right
    fetches.
    """
    # We are going to create a bunch of FetchAtt objects. These are the args,
    # kwargs for those objects as well as the expected `str()` of the objects.
    #
    inputs: List[Tuple[str, Dict[str, Any], str]] = [
        ("body", {"section": [], "actual_command": "RFC822"}, "RFC822"),
        ("rfc822.size", {}, "RFC822.SIZE"),
        (
            "body",
            {
                "section": ["header"],
                "peek": True,
                "actual_command": "RFC822.HEADER",
            },
            "RFC822.HEADER",
        ),
        (
            "body",
            {"section": ["text"], "actual_command": "RFC822.TEXT"},
            "RFC822.TEXT",
        ),
        (
            "bodystructure",
            {"ext_data": False, "actual_command": "BODY"},
            "BODY",
        ),
        ("body", {"section": [1, 2, 3, 4, "header"]}, "BODY[1.2.3.4.HEADER]"),
        ("body", {"section": [3, "text"]}, "BODY[3.TEXT]"),
        ("body", {"section": [], "partial": (0, 1024)}, "BODY[]<0.1024>"),
        ("flags", {}, "FLAGS"),
        ("internaldate", {}, "INTERNALDATE"),
        ("envelope", {}, "ENVELOPE"),
    ]

    for fetch_op, kwargs, expected in inputs:
        f = FetchAtt(STR_TO_FETCH_OP[fetch_op], **kwargs)
        print(str(f))
        assert str(f) == expected
