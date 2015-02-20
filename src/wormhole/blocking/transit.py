from __future__ import print_function
import threading, socket, SocketServer
from binascii import hexlify
from ..util import ipaddrs
from ..util.hkdf import HKDF

class TransitError(Exception):
    pass

# The beginning of each TCP connection consists of the following handshake
# messages. The sender transmits the same text regardless of whether it is on
# the initiating/connecting end of the TCP connection, or on the
# listening/accepting side. Same for the receiver.
#
#  sender -> receiver: transit sender TXID_HEX ready\n\n
#  receiver -> sender: transit receiver RXID_HEX ready\n\n
#
# Any deviations from this result in the socket being closed. The handshake
# messages are designed to provoke an invalid response from other sorts of
# servers (HTTP, SMTP, echo).
#
# If the sender is satisfied with the handshake, and this is the first socket
# to complete negotiation, the sender does:
#
#  sender -> receiver: go\n
#
# and the next byte on the wire will be from the application.
#
# If this is not the first socket, the sender does:
#
#  sender -> receiver: nevermind\n
#
# and closes the socket.

# So the receiver looks for "transit sender TXID_HEX ready\n\ngo\n" and hangs
# up upon the first wrong byte. The sender lookgs for "transit receiver
# RXID_HEX ready\n\n" and then makes a first/not-first decision about sending
# "go\n" or "nevermind\n"+close().

def build_receiver_handshake(key):
    hexid = HKDF(key, 32, CTXinfo=b"transit_receiver")
    return "transit receiver %s ready\n\n" % hexlify(hexid)

def build_sender_handshake(key):
    hexid = HKDF(key, 32, CTXinfo=b"transit_sender")
    return "transit sender %s ready\n\n" % hexlify(hexid)

TIMEOUT=10000

# 1: sender only transmits, receiver only accepts, both wait forever
# 2: sender also accepts, receiver also transmits
# 3: timeouts / stop when no more progress can be made
# 4: add relay
# 5: accelerate shutdown of losing sockets


class BadHandshake(Exception):
    pass

def connector(owner, hint, send_handshake, expected_handshake):
    if isinstance(hint, type(u"")):
        hint = hint.encode("ascii")
    addr,port = hint.split(",")
    skt = None
    try:
        skt = socket.create_connection((addr,port)) # timeout here
        skt.settimeout(TIMEOUT)
        #print("socket(%s) connected" % (hint,))
        skt.send(send_handshake)
        got = b""
        while len(got) < len(expected_handshake):
            got += skt.recv(1)
            if expected_handshake[:len(got)] != got:
                raise BadHandshake("got '%r' want '%r' on %s" %
                                   (got, expected_handshake, hint))
        #print("connector ready %r" % (hint,))
    except Exception as e:
        try:
            if skt:
                skt.shutdown(socket.SHUT_WR)
        except socket.error:
            pass
        if skt:
            skt.close()
        # ignore socket errors, warn about coding errors
        if not isinstance(e, (socket.error, socket.timeout, BadHandshake)):
            raise
        return
    # owner is now responsible for the socket
    owner._negotiation_finished(skt) # note thread

def handle(skt, client_address, owner, send_handshake, expected_handshake):
    try:
        #print("handle %r" %  (skt,))
        skt.settimeout(TIMEOUT)
        skt.send(send_handshake)
        got = b""
        # for the receiver, this includes the "go\n"
        while len(got) < len(expected_handshake):
            more = skt.recv(1)
            if not more:
                raise BadHandshake("disconnect after merely '%r'" % got)
            got += more
            if expected_handshake[:len(got)] != got:
                raise BadHandshake("got '%r' want '%r'" %
                                   (got, expected_handshake))
        #print("handler negotiation finished %r" % (client_address,))
    except Exception as e:
        #print("handler failed %r" % (client_address,))
        try:
            # this raises socket.err(EBADF) if the socket was already closed
            skt.shutdown(socket.SHUT_WR)
        except socket.error:
            pass
        skt.close() # this appears to be idempotent
        # ignore socket errors, warn about coding errors
        if not isinstance(e, (socket.error, socket.timeout, BadHandshake)):
            raise
        return
    # owner is now responsible for the socket
    owner._negotiation_finished(skt) # note thread

class MyTCPServer(SocketServer.TCPServer):
    allow_reuse_address = True

    def process_request(self, request, client_address):
        kc = self.owner._have_transit_key
        kc.acquire()
        while not self.owner._transit_key:
            kc.wait()
        # owner._transit_key is either None or set to a value. We don't
        # modify it from here, so we can release the condition lock before
        # grabbing the key.
        kc.release()

        # Once it is set, we can get handler_(send|receive)_handshake, which
        # is what we actually care about.
        t = threading.Thread(target=handle,
                             args=(request, client_address,
                                   self.owner,
                                   self.owner.handler_send_handshake,
                                   self.owner.handler_expected_handshake))
        t.daemon = True
        t.start()


class Common:
    def __init__(self):
        self.winning = threading.Event()
        self._negotiation_check_lock = threading.Lock()
        self._have_transit_key = threading.Condition()
        self._transit_key = None
        self._start_server()

    def _start_server(self):
        server = MyTCPServer(("", 0), None)
        _, port = server.server_address
        self.my_direct_hints = ["%s,%d" % (addr, port)
                                for addr in ipaddrs.find_addresses()]
        server.owner = self
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        self.listener = server

    def get_direct_hints(self):
        return self.my_direct_hints
    def get_relay_hints(self):
        return []

    def add_their_direct_hints(self, hints):
        self._their_direct_hints = hints
    def add_their_relay_hints(self, hints):
        self._their_relay_hints = hints # ignored

    def _send_this(self):
        if self.is_sender:
            return build_sender_handshake(self._transit_key)
        else:
            return build_receiver_handshake(self._transit_key)

    def _expect_this(self):
        if self.is_sender:
            return build_receiver_handshake(self._transit_key)
        else:
            return build_sender_handshake(self._transit_key) + "go\n"

    def set_transit_key(self, key):
        # This _have_transit_key condition/lock protects us against the race
        # where the sender knows the hints and the key, and connects to the
        # receiver's transit socket before the receiver gets relay message
        # (and thus the key).
        self._have_transit_key.acquire()
        self._transit_key = key
        self.handler_send_handshake = self._send_this() # no "go"
        self.handler_expected_handshake = self._expect_this()
        self._have_transit_key.notify_all()
        self._have_transit_key.release()

    def _start_outbound(self):
        self.connectors = []
        for hint in self._their_direct_hints:
            t = threading.Thread(target=connector,
                                 args=(self, hint,
                                       self._send_this(),
                                       self._expect_this()))
            t.daemon = True
            t.start()

    def establish_connection(self):
        self.winning_skt = None
        self._start_outbound()

        # we sit here until one of our inbound or outbound sockets succeeds
        flag = self.winning.wait(TIMEOUT)

        if not flag:
            # timeout: self.winning_skt will not be set. ish. race.
            pass
        if self.listener:
            self.listener.shutdown() # TODO: waits up to 0.5s. push to thread
        if self.winning_skt:
            return self.winning_skt
        raise TransitError

    def _negotiation_finished(self, skt):
        # inbound/outbound sockets call this when they finish negotiation.
        # The first one wins and gets a "go". Any subsequent ones lose and
        # get a "nevermind" before being closed.

        with self._negotiation_check_lock:
            if self.winning_skt:
                is_winner = False
            else:
                is_winner = True
                self.winning_skt = skt

        if is_winner:
            if self.is_sender:
                skt.send("go\n")
            self.winning.set()
        else:
            if self.is_sender:
                skt.send("nevermind\n")
            skt.close()

class TransitSender(Common):
    is_sender = True

class TransitReceiver(Common):
    is_sender = False
