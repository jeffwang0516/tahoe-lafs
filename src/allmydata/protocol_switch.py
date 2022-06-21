"""
Support for listening with both HTTP and Foolscap on the same port.
"""

from enum import Enum
from typing import Optional

from twisted.internet.protocol import Protocol
from twisted.python.failure import Failure

from foolscap.negotiate import Negotiation

class ProtocolMode(Enum):
    """Listening mode."""
    UNDECIDED = 0
    FOOLSCAP = 1
    HTTP = 2


class PretendToBeNegotiation(type):
    """😱"""

    def __instancecheck__(self, instance):
        return (instance.__class__ == self) or isinstance(instance, Negotiation)


class FoolscapOrHttp(Protocol, metaclass=PretendToBeNegotiation):
    """
    Based on initial query, decide whether we're talking Foolscap or HTTP.

    Pretends to be a ``foolscap.negotiate.Negotiation`` instance.
    """
    _foolscap : Optional[Negotiation] = None
    _protocol_mode : ProtocolMode = ProtocolMode.UNDECIDED
    _buffer: bytes = b""

    def __init__(self, *args, **kwargs):
        self._foolscap = Negotiation(*args, **kwargs)

    def __setattr__(self, name, value):
        if name in {"_foolscap", "_protocol_mode", "_buffer", "transport"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._foolscap, name, value)

    def __getattr__(self, name):
        return getattr(self._foolscap, name)

    def makeConnection(self, transport):
        Protocol.makeConnection(self, transport)
        self._foolscap.makeConnection(transport)

    def initServer(self, *args, **kwargs):
        return self._foolscap.initServer(*args, **kwargs)

    def initClient(self, *args, **kwargs):
        assert not self._buffer
        self._protocol_mode = ProtocolMode.FOOLSCAP
        return self._foolscap.initClient(*args, **kwargs)

    def dataReceived(self, data: bytes) -> None:
        if self._protocol_mode == ProtocolMode.FOOLSCAP:
            return self._foolscap.dataReceived(data)
        if self._protocol_mode == ProtocolMode.HTTP:
            raise NotImplementedError()

        # UNDECIDED mode.
        self._buffer += data
        if len(self._buffer) < 8:
            return

        # Check if it looks like Foolscap request. If so, it can handle this
        # and later data:
        if self._buffer.startswith(b"GET /id/"):
            self._protocol_mode = ProtocolMode.FOOLSCAP
            buf, self._buffer = self._buffer, b""
            return self._foolscap.dataReceived(buf)
        else:
            self._protocol_mode = ProtocolMode.HTTP
            raise NotImplementedError("")

    def connectionLost(self, reason: Failure) -> None:
        if self._protocol_mode == ProtocolMode.FOOLSCAP:
            return self._foolscap.connectionLost(reason)
