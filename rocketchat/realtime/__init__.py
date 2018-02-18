import hashlib
import json
import uuid
import sys

import logging

from typing import DefaultDict, Callable, Optional, List, Union, Any, Tuple, Generator, Dict
from collections import defaultdict, Iterable

from ws4py.client.threadedclient import WebSocketClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Message(dict):
    """ Represents a message to/from the RocketChat server.

    This is just a regular dictionary that allows you to address contents
    as attributes.
    """
    @classmethod
    def decoder(cls, object: dict):
        ''' Constructor for `json.load`'s object hook'''
        return cls(**object)

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
    def __init__(self, domain: str) -> None:
        super().__init__(
            "ws://{}/websocket".format(domain),
            protocols=['http-only', 'chat'])
        # Handlers are called when messages of a given type are received
        self.handlers = defaultdict(list)  # type: DefaultDict[str, List[Handler]]
        # Listeners are called when messages in a given collection are received
        self.listeners = defaultdict(list)  # type: DefaultDict[Tuple[str, str], List[Handler]]

        # Callbacks are called when messages with a given ID
        # are received
        self.callbacks = defaultdict(list)  # type: DefaultDict[str, List[Callback]]

        # Set default handlers
        self.handlers.update({
            'connected': [self.on_connected],
            'ping': [self.on_ping]
        })

    # Websocket event handlers
    def opened(self) -> None:
        self.send(msg='connect', version='1', support=['1'])

    def received_message(self, data: str) -> None:
        logger.info("Received: %s", data)
        event = json.loads(str(data), object_hook=Message.decoder)

        if 'msg' not in event:
            return

        handlers = self.handlers[event.msg]
        if 'collection' in event:
            key = event.collection, event.fields.eventName
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
                self.send(**message)
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
                    self.send(**message)
                except StopIteration:
                    pass

    def closed(self, code: str, reason: str=None) -> None:
        pass

    # Default event handlers
    def on_ping(self, msg: Message) -> None:
        self.send(msg="pong")

    def on_connected(self, msg: Message) -> WaitingHandler:
        msg = yield Call('login', {
            "user": {"username": sys.argv[1]},
            "password": self._hash_password(sys.argv[2]),
        })

        if 'error' in msg:
            logging.error("{errorType}: {message}".format(**msg.error))
            self.close()

        self.user_id = msg.result.id

        msg = yield Call('rooms/get', {'$date': 0})

        self.subscribe(
            'stream-notify-user',
            self.user_id + '/rooms-changed',
            self.on_join)

        rooms = msg.result['update']
        for room in rooms:
            if room._id != 'o58ac5XBk6xLXXKST':
                self.subscribe('stream-room-messages', room._id)

    def on_join(self, message: Message) -> None:
        event, room = message.fields.args
        if event == 'inserted':
            self.subscribe('stream-room-messages', room._id)

    # Helpers for different types of messages
    def send(self, **args) -> None:
        data = json.dumps(args)
        logging.info("Sending: %s", data)
        return super().send(data)

    def call(self, *args, callback: Callback=None, **kwargs) -> None:
        message = Call(*args, **kwargs)

        if callback is not None:
            self.callbacks[message.id].append(callback)

        return self.send(**message)

    def subscribe(self, name: str, event: str, listener: Handler=None) -> None:
        if listener:
            self.listeners[name, event].append(listener)

        return self.send(
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


if __name__ == '__main__':
    try:
        ws = Client(sys.argv[3])
        ws.connect()
        ws.run_forever()
    except KeyboardInterrupt:
        ws.close()
