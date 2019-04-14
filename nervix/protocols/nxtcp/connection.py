import socket
import errno
import logging

from enum import Enum, auto

from nervix import verbs
from nervix.protocols.base import BaseConnection
from . import decoder
from . import encoder

logger = logging.getLogger(__name__)


class State(Enum):
    IDLE = auto()
    CONNECTING = auto()
    WAIT_WELCOME = auto()
    READY = auto()
    FAILED = auto()


class NxtcpConnection(BaseConnection):

    def __init__(self, mainloop, address):
        self.mainloop = mainloop
        self.address = address

        self.ready_handler = None
        self.downstream_handler = None

        self.timeout_timeout = 3.0
        self.timeout_timer = mainloop.timer()
        self.timeout_timer.set_handler(self.evaluate_state)

        self.welcome_timeout = 2.0
        self.welcome_timer = mainloop.timer()
        self.welcome_timer.set_handler(self.evaluate_state)

        # self.cooldown_timeout = 5.0
        self.cooldown_timeouts = [5.0, 5.0, 5.0, 10.0, 10.0, 20.0, 30.0, 60.0]
        self.cooldown_timeout_i = 0
        self.cooldown_timer = mainloop.timer()
        self.cooldown_timer.set_handler(self.evaluate_state)

        self.auto_connect = True

        self.connect_success = False
        self.connect_failed = False

        self.welcome_received = False
        self.byebye_received = False

        self.socket = None
        self.proxy = None

        self.encoder = None
        self.decoder = None

        self.packet_handlers = {
            decoder.WelcomePacket: self.__on_welcome_packet,
            decoder.PingPacket: self.__on_ping_packet,
            decoder.ByeByePacket: self.__on_byebye_packet,
            decoder.SessionPacket: self.__on_session_packet,
            decoder.CallPacket: self.__on_call_packet,
            decoder.MessagePacket: self.__on_message_packet,
            decoder.InterestPacket: self.__on_interest_packet,
        }

        self.verb_handlers = {
            verbs.LoginVerb: self.__on_login_verb,
            verbs.LogoutVerb: self.__on_logout_verb,
            verbs.RequestVerb: self.__on_request_verb,
            verbs.PostVerb: self.__on_post_verb,
            verbs.SubscribeVerb: self.__on_subscribe_verb,
            verbs.UnsubscribeVerb: self.__on_unsubscribe_verb,
        }

        # flag that indicates weather or not the connection is ready
        self.ready = None
        self.__update_ready(False)

        self.state = State.IDLE
        self.evaluate_state()

    def set_ready_handler(self, handler):
        self.ready_handler = handler

        if self.ready_handler:
            self.ready_handler(self.ready)

    def set_downstream_handler(self, handler):
        self.downstream_handler = handler

    def send_verb(self, verb):
        """ Called from Core when a verb should be send upstream.
        """

        handler = self.verb_handlers.get(type(verb), None)

        if not handler:
            raise NotImplementedError(f"No handler implemented for {type(verb).__name__}")

        if not self.encoder:
            raise RuntimeError("Connection not ready")

        handler(verb)

    def evaluate_state(self):

        # print(f"evaluating state {self.state}")

        if self.state == State.IDLE:
            """ Idle state.
            """

            if self.auto_connect:
                """ The auto_connect flag is set so we will start the connecting process.
                """
                self.do_connect()

        elif self.state == State.CONNECTING:
            """ Connection in progress.
            """

            if self.timeout_timer.has_expired():
                """ The timeout timer has expired, this means that the connection was not set up
                in time and we will treat this as a failure. 
                """
                logger.info("Connection attempt timed out")

                self.do_failed()

            elif self.connect_failed:
                """ The connection process has failed because the OS told us so. Regardless the reason
                we will treat this as a failure. 
                """
                logger.info("Connection attempt failed")
                self.do_failed()

            elif self.connect_success:
                """ The connection process was successful and the socket is now connected. Next step
                is to wait for the welcome message.
                """
                self.do_wait_welcome()

        elif self.state == State.WAIT_WELCOME:
            """ Socket is connected, waiting for welcome message from server.
            """

            if self.welcome_received:
                """ The welcome message has been received, everything is ready now for the connection
                to be used."""

                logger.info("Welcome message received")
                self.do_ready()

            elif self.welcome_timer.has_expired():
                """ We didn't receive the welcome message in time. Handle this failure.
                """
                logger.info("No welcome message received")
                self.do_failed()

            elif self.connect_failed:
                """ Connection was made but has now failed.
                """
                logger.info("Connection lost")
                self.do_failed()

            elif self.byebye_received:
                """ Server send byebye instead of welcome.
                """
                logger.info("Server says byebye instead of welcome, closing connection")
                self.do_failed()

        elif self.state == State.READY:
            """ Socket is connected and welcome message is received.
            """

            if self.connect_failed:
                """ Connection was ready but has now failed.
                """
                logger.info("Connection lost")
                self.do_failed()

            elif self.byebye_received:
                """ Server send byebye instead of welcome.
                """
                logger.info("Server says byebye, closing connection")
                self.do_failed()

        elif self.state == State.FAILED:
            """ Something has gone wrong, wait for the cooldown timer to expire before going back
            to idle.
            """

            if self.cooldown_timer.has_expired():
                """ The cooldown period has expired, lets go to idle now.
                """
                self.do_idle()

    def do_connect(self):
        """ Setup socket and initiate the connection to the server.
        """

        # create socket
        self.socket = socket.socket()
        self.socket.setblocking(False)

        # register on mainloop
        self.proxy = self.mainloop.register(self.socket)
        self.proxy.set_write_handler(self.__on_connect)
        self.proxy.set_interest(write=True)

        # reset error and success flags
        self.connect_failed = False
        self.connect_success = False

        res = self.socket.connect_ex(self.address)

        if res == errno.EINPROGRESS:
            # connection is now in progress

            # arm timeout timer
            self.timeout_timer.set(self.timeout_timeout)

        else:
            # connect has failed
            self.connect_failed = True

        logger.info("Initiating connection to %s:%s", self.address[0], self.address[1])
        self.state = State.CONNECTING

    def do_wait_welcome(self):
        """ Setup the welcome timeout.
        """

        self.welcome_timer.set(self.welcome_timeout)

        self.welcome_received = False
        self.byebye_received = False

        logger.info("Connection successful, waiting for welcome message")
        self.state = State.WAIT_WELCOME

    def do_ready(self):
        """ Called when the welcome packet has been received.
        Call ready handler.
        """

        self.__update_ready(True)

        self.state = State.READY

    def do_failed(self):
        """
        """

        self.decoder = None
        self.encoder = None

        self.proxy.unregister()
        self.socket.close()

        # update ready flag
        self.__update_ready(False)

        timeout = self.cooldown_timeouts[self.cooldown_timeout_i]
        if self.cooldown_timeout_i < len(self.cooldown_timeouts) - 1:
            self.cooldown_timeout_i += 1

        logger.info("Cooling down for %ss", timeout)
        self.cooldown_timer.set(timeout)

        self.state = State.FAILED

    def do_disconnect(self):
        """ Disconnect the socket (if it was connected) and clean up socket resources.
        """

        self.state = State.DISCONNECTED

    def do_idle(self):
        self.state = State.IDLE
        self.evaluate_state()

    def handle_packet(self, packet):

        handler = self.packet_handlers.get(type(packet), None)

        if not handler:
            raise NotImplementedError(f"Handler not implemented for {type(packet).__name__}")

        handler(packet)

    def __on_connect(self):
        """ Called when something has happened during the connection process.
        """

        res = self.socket.connect_ex(self.address)

        if res == 0:
            # connect was successful
            self.connect_success = True

            self.cooldown_timeout_i = 0

            self.encoder = encoder.Encoder()
            self.decoder = decoder.Decoder()

            self.proxy.set_write_handler(self.__on_write)
            self.proxy.set_read_handler(self.__on_read)
            self.proxy.set_interest(read=True, write=False)

        else:
            # connect failed
            self.connect_failed = True

        self.evaluate_state()

    def __on_read(self):

        n = self.decoder.read_from_socket(self.socket)

        while self.decoder:
            packet = self.decoder.decode()

            if not packet:
                break

            self.handle_packet(packet)

        if n == 0:
            self.connect_failed = True
            self.evaluate_state()

    def __on_write(self):

        n = self.encoder.write_to_socket(self.socket)

        if n == 0:
            self.proxy.stop_writing()

    def __on_welcome_packet(self, packet):

        if packet.protocol_version != 1:
            logger.error("Unsupported protocol version %s", packet.protocol_version)

        self.welcome_received = True

        self.evaluate_state()

    def __on_ping_packet(self, _packet):
        """ Called when a ping packet is received
        """

        logger.debug("Ping packet received, sending pong back to server")

        self.encoder.encode(
            encoder.PongPacket()
        )

        self.proxy.start_writing()

    def __on_byebye_packet(self, _packet):
        """ Called when a byebye packet is received.
        """

        self.byebye_received = True
        self.evaluate_state()

    def __on_session_packet(self, packet):

        verb = verbs.SessionVerb(
            name=packet.name,
            state=[verbs.SessionVerb.STATE_ENDED,
                   verbs.SessionVerb.STATE_STANDBY,
                   verbs.SessionVerb.STATE_ACTIVE]
            [packet.state]
        )

        if self.downstream_handler:
            self.downstream_handler(verb)

    def __on_call_packet(self, packet):

        verb = verbs.CallVerb(
            unidirectional=packet.unidirectional,
            postref=packet.postref if not packet.unidirectional else None,
            name=packet.name,
            payload=packet.payload,
        )

        if self.downstream_handler:
            self.downstream_handler(verb)

    def __on_message_packet(self, packet):

        verb = verbs.MessageVerb(
            messageref=packet.messageref,
            status=[verbs.MessageVerb.STATUS_OK,
                    verbs.MessageVerb.STATUS_TIMEOUT,
                    verbs.MessageVerb.STATUS_UNREACHABLE]
            [packet.status],
            payload=packet.payload,
        )

        if self.downstream_handler:
            self.downstream_handler(verb)

    def __on_interest_packet(self, packet):

        verb = verbs.InterestVerb(
            postref=packet.postref,
            name=packet.name,
            status=[verbs.InterestVerb.STATUS_NO_INTEREST,
                    verbs.InterestVerb.STATUS_INTEREST]
            [packet.status],
            topic=packet.topic,
        )

        if self.downstream_handler:
            self.downstream_handler(verb)

    def __on_login_verb(self, verb):

        self.encoder.encode(encoder.LoginPacket(
            name=verb.name,
            enforce=verb.enforce,
            standby=verb.standby,
            persist=verb.persist,
        ))

        self.proxy.start_writing()

    def __on_logout_verb(self, verb):

        self.encoder.encode(encoder.LogoutPacket(
            name=verb.name,
        ))

        self.proxy.start_writing()

    def __on_request_verb(self, verb):

        self.encoder.encode(encoder.RequestPacket(
            name=verb.name,
            unidirectional=verb.unidirectional,
            messageref=verb.messageref,
            timeout=verb.timeout,
            payload=verb.payload,
        ))

        self.proxy.start_writing()

    def __on_post_verb(self, verb):

        self.encoder.encode(encoder.PostPacket(
            postref=verb.postref,
            payload=verb.payload,
        ))

        self.proxy.start_writing()

    def __on_subscribe_verb(self, verb):

        self.encoder.encode(encoder.SubscribePacket(
            messageref=verb.messageref,
            name=verb.name,
            topic=verb.topic
        ))

        self.proxy.start_writing()

    def __on_unsubscribe_verb(self, verb):

        self.encoder.encode(encoder.UnsubscribePacket(
            name=verb.name,
            topic=verb.topic
        ))

        self.proxy.start_writing()

    def __update_ready(self, state):
        prev = self.ready
        self.ready = state

        if prev != state:

            if self.ready_handler:
                self.ready_handler(self.ready)
