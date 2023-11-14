#!/usr/bin/env python
#
# File: $Id$
#
"""
Run through a bunch of messages in an asimap trace file passed as the first
argument and see how fast it takes us to parse the messages in that file
"""

import json

# system imports
#
import sys
from pathlib import Path

# 3rd party imports
#
from codetiming import Timer

# Project imports
#
from asimap.parse import BadCommand, IMAPClientCommand


#############################################################################
#
def main():
    """
    parse options.. read trace file.. for each "msg_type" of
    "RECEIVED" parse the message. Compare length of message being
    parsed with time to parse.
    """
    msg_results = {}
    trace_file = Path(sys.argv[1])
    timer = Timer("message_parser", text="Elapsed time: {:.7f}")
    with open(trace_file, "r") as traces:
        for idx, line in enumerate(traces):
            msg = line[22:].strip()
            trace = json.loads(msg)
            if (
                "data" in trace
                and "msg_type" in trace
                and trace["msg_type"] == "RECEIVED"
            ):
                data = trace["data"]
                if data.upper() == "DONE":
                    # 'DONE' are not processed by the imap message parser.
                    continue
                try:
                    with timer:
                        p = IMAPClientCommand(data)
                        p.parse()
                except BadCommand:
                    print(f"Parse failed on: '{data}'")
                    raise
                msg_results[f"{timer.last:.7f}"] = data
    max_time = f"{Timer.timers.max('message_parser'):.7f}"
    print(f"Timer max: {max_time}")
    print(f"Timer mean: {Timer.timers.mean('message_parser'):.7f}")
    print(f"Timer std dev: {Timer.timers.stdev('message_parser'):.7f}")
    print(f"Timer total: {Timer.timers.total('message_parser'):.7f}")
    # print(f"Message for max time: {msg_results[max_time]}")


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
