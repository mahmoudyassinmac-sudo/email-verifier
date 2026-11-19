"""A fake mail server for testing stage 6 without touching the internet.

Behaviour by recipient domain / local part:
  catchall.test   -> accepts everything (catch-all)
  blocked.test    -> 554 5.7.1 ... blocked using zen.spamhaus.org
  grey.test       -> 451 greylisted on first try, then normal rules
  others          -> local starting with valid/ok, or info/sales/john = 250
                     local starting with full = 552 mailbox full
                     anything else = 550 5.1.1 user unknown
"""
from __future__ import annotations

import time

from aiosmtpd.controller import Controller


class Handler:
    def __init__(self):
        self.greylist = set()
        self.rcpt_log = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        address = address.lower()
        local, domain = address.split("@", 1)
        self.rcpt_log.append(address)
        if domain == "catchall.test":
            return "250 2.1.5 OK"
        if domain == "blocked.test":
            return "554 5.7.1 Service unavailable; Client host [10.0.0.1] blocked using zen.spamhaus.org"
        if domain == "grey.test" and address not in self.greylist:
            self.greylist.add(address)
            return "451 4.7.1 Greylisted, please try again later"
        if local.startswith("full"):
            return "552 5.2.2 Mailbox full"
        if local.startswith(("valid", "ok")) or local in ("info", "sales", "john"):
            envelope.rcpt_tos.append(address)
            return "250 2.1.5 OK"
        return "550 5.1.1 <%s>: Recipient address rejected: User unknown in virtual mailbox table" % address

    async def handle_DATA(self, server, session, envelope):
        raise AssertionError("DATA must never be sent by the verifier")


def start(port: int = 2525):
    handler = Handler()
    ctl = Controller(handler, hostname="127.0.0.1", port=port)
    ctl.start()
    return ctl, handler


if __name__ == "__main__":
    ctl, _ = start()
    print("mock SMTP on 127.0.0.1:2525 - Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ctl.stop()
