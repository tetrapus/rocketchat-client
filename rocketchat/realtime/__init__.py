''' RocketChat Realtime API. '''

import hashlib
import json
import logging
import sys
import uuid

from collections import defaultdict, Iterable
from functools import partial
from typing import (
    Any,
    Callable,
    DefaultDict,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
    Union,
)

from ws4py.client.threadedclient import WebSocketClient
from ws4py.messaging import TextMessage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Message(dict):
    """ Represents a message to/from the RocketChat server.

    This is just a regular dictionary that allows you to address contents
    as attributes.
    """
    @classmethod
    def decoder(cls, obj: dict):
        ''' Constructor for `json.load`'s object hook'''
        return cls(**obj)

    def __getattr__(self, attr: str):
        ''' Allow contents access via attribute lookup.

        Beware access via this method, as it is shadowed by dict operations
        e.g `update`
        '''
        return self[attr]


def generate_id() -> str:
    """ Create a unique ID.

    This is passed in each message, and used to match requests
    to responses.
    """
    return uuid.uuid4().hex


class Call(Message):
    ''' A realtime API call message '''
    def __init__(self, method: str, *params, **args) -> None:
        ''' Special constructor for a method call message '''
        super().__init__(
            **args,
            msg='method',
            method=method,
            id=generate_id(),
            params=params,
        )

WaitingHandler = Generator[Message, Message, None]
Handler = Callable[[Message], Optional[WaitingHandler]]
Callback = Union[Callable[[Message], Any], WaitingHandler]


class Client(WebSocketClient):
    ''' Base client for RocketChat

    Handles login & join-on-connect
    '''
    # Handlers are called when messages of a given type are received
    handlers: DefaultDict[str, List[Handler]]
    # Listeners are called when messages in a given collection are received
    listeners: DefaultDict[Tuple[str, str], List[Handler]]
    # Callbacks are called when messages with a given ID are received
    callbacks: DefaultDict[str, List[Callback]]

    user_id: str = None

    def __init__(
            self,
            domain: str,
            username: str = None,
            password: str = None) -> None:
        super().__init__(
            f'ws://{domain}/websocket',
            protocols=['http-only', 'chat'])

        self.handlers = defaultdict(list)
        self.listeners = defaultdict(list)
        self.callbacks = defaultdict(list)

        # Set default handlers
        self.handlers.update({
            'connected': [
                partial(
                    self.on_connected,
                    username=username,
                    password=password
                )
            ],
            'ping': [self.on_ping]
        })

    # Websocket event handlers
    def opened(self) -> None:
        self.send_message(msg='connect', version='1', support=['1'])

    def received_message(self, message: TextMessage) -> None:
        logger.info("Received: %s", message)
        event = json.loads(message.data, object_hook=Message.decoder)

        if 'msg' not in event:
            return

        handlers = self.handlers[event.msg]
        if 'collection' in event:
            key = event.collection, event.fields.get('eventName')
            handlers = handlers + self.listeners[key]

        for handler in handlers:
            # Call the handler
            result = handler(event)
            # If this handler returns a generator, we try to run it.
            # Send the return value down the websocket and register the
            # generator as a handler for the response.
            if not (isinstance(result, Iterable) and hasattr(result, 'send')):
                continue
            try:
                message = next(result)
                if 'id' in message and hasattr(result, 'send'):
                    self.callbacks[message['id']].append(result)
                self.send_message(**message)
            except StopIteration:
                pass

        for callback in self.callbacks[event.get('id')]:
            if callable(callback):
                callback(event)
            else:
                try:
                    message = callback.send(event)
                    if 'id' in message:
                        self.callbacks[message['id']].append(callback)
                    self.send_message(**message)
                except StopIteration:
                    pass

    def closed(self, code: str, reason: str = None) -> None:
        pass

    # Default event handlers
    def on_ping(self, _: Message) -> None:
        ''' Replies to pings '''
        self.send_message(msg="pong")

    def on_connected(
            self,
            msg: Message,
            username=None,
            password=None) -> WaitingHandler:
        ''' Log in and subscribe to all joined rooms '''
        msg = yield Call('login', {
            "user": {"username": username},
            "password": self._hash_password(password),
        })

        if 'error' in msg:
            logging.error("%s: %s", msg.error.errorType, msg.error.message)
            self.close()

        self.user_id = msg.result.id

        msg = yield Call('rooms/get', {'$date': 0})

        self.subscribe(
            'stream-notify-user',
            self.user_id + '/rooms-changed',
            self.on_join)

        rooms = msg.result['update']
        for room in rooms:
            if room['_id'] != 'o58ac5XBk6xLXXKST':
                self.subscribe('stream-room-messages', room['_id'])

    def on_join(self, message: Message) -> None:
        ''' Subscribe to newly joined rooms '''
        event, room = message.fields.args
        if event == 'inserted':
            self.subscribe('stream-room-messages', room['_id'])

    # Helpers for different types of messages
    def send_message(self, **args) -> None:
        ''' Send a message to the server '''
        data = json.dumps(args)
        logging.info("Sending: %s", data)
        return self.send(data)

    def call(self, *args, callback: Callback = None, **kwargs) -> None:
        ''' Call a realtime API method.

        If a callback is specified, it is called with the response to this
        message.
        '''
        message = Call(*args, **kwargs)

        if callback is not None:
            self.callbacks[message.id].append(callback)

        return self.send_message(**message)

    def subscribe(
            self,
            name: str,
            event: str,
            listener: Handler = None) -> None:
        ''' Subscribe to a stream.

        The optional parameter listener specifies a handler to call when
        corresponding messages are received.
        '''
        if listener:
            self.listeners[name, event].append(listener)

        return self.send_message(
            id=generate_id(),
            msg='sub',
            name=name,
            params=[event, False]
        )

    @staticmethod
    def _hash_password(password: str) -> Dict[str, str]:
        """ The realtime API expects a hashed password """
        return {
            "digest": hashlib.sha256(password.encode('utf-8')).hexdigest(),
            "algorithm":"sha-256"
        }
