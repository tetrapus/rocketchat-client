''' RocketChat Realtime API. '''

import inspect
import hashlib
import json
import logging
import uuid

from abc import ABC, abstractmethod
from functools import partial
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Union,
)

from ws4py.client.threadedclient import WebSocketClient
from ws4py.messaging import TextMessage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class HandlerSignal:
    pass


DONE = HandlerSignal()


class Message(dict):
    """ Represents a message to/from the RocketChat server.

    This is just a regular dictionary that allows you to address contents
    as attributes.
    """
    @classmethod
    def decoder(cls, obj: Dict[str, Any]):
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


Handler = Callable[[Message], Optional[HandlerSignal]]
Task = Generator['Runnable', Message, Optional[HandlerSignal]]
TaskFactory = Callable[[Message], Task]
Listener = Union[Handler, TaskFactory]


def handles(**kwargs: Any) -> Callable[[Listener], Listener]:
    def decorator(funct: Listener) -> Listener:
        try:
            funct.handles.append(kwargs)
        except AttributeError:
            funct.handles = [kwargs]
        return funct
    return decorator


class WebsocketWrapper(WebSocketClient):
    def __init__(self, client, *args, **kwargs):
        self.client = client
        super().__init__(*args, **kwargs)

    def opened(self) -> None:
        return self.client.opened()

    def received_message(self, message: TextMessage) -> None:
        return self.client.received_message(message)

    def closed(self, code: str, reason: str = None) -> None:
        return self.client.closed(code, reason)


class Client:
    ''' Base client for RocketChat

    Handles login & join-on-connect
    '''
    # Handlers are called when messages of a given type are received
    handlers: List[Handler]

    user_id: str = None

    def __init__(
            self,
            domain: str,
            username: str = None,
            password: str = None) -> None:

        self.username = username
        self.password = password

        self.ws = WebsocketWrapper(
            self,
            f'wss://{domain}/websocket',
            protocols=['http-only', 'chat'])

        self.handlers = []
        for method_name in dir(self):
            method = getattr(self, method_name)
            if hasattr(method, 'handles'):
                # TODO: validate type
                self.register_handler(method)

        self.ws.connect()

    def run(self):
        return self.ws.run_forever()

    # Websocket event handlers
    def opened(self) -> None:
        self.send_message(msg='connect', version='1', support=['1'])

    def received_message(self, message: TextMessage) -> None:
        event = json.loads(message.data, object_hook=Message.decoder)

        # Find all the handlers for this event
        handlers = [
            handler for handler in self.handlers
            if any(
                all(
                    key in event and event[key] == value
                    for key, value in trigger.items()
                )
                for trigger in handler.handles
            )
        ]

        for handler in handlers:
            # Call the handler
            result = handler(event)
            if result is DONE:
                self.handlers.remove(handler)

    def closed(self, code: str, reason: str = None) -> None:
        pass

    def close(self):
        self.ws.close()

    # Default event handlers
    @handles(msg='ping')
    def on_ping(self, _: Message) -> None:
        ''' Replies to pings '''
        self.send_message(msg="pong")

    @handles(msg='connected')
    def on_connected(self, msg: Message) -> Task:
        ''' Log in and subscribe to all joined rooms '''
        print(1)
        msg = yield Call('login', {
            "user": {"username": self.username},
            "password": self._hash_password(self.password),
        })

        if 'error' in msg:
            logging.error("%s: %s", msg.error.errorType, msg.error.message)
            self.close()

        self.user_id = msg.result.id

        msg = yield Call('rooms/get', {'$date': 0})

        def on_join(message: Message) -> None:
            ''' Subscribe to newly joined rooms '''
            event, room = message.fields.args
            if event == 'inserted':
                self.subscribe('stream-room-messages', room['_id'])

        self.subscribe(
            'stream-notify-user',
            self.user_id + '/rooms-changed',
            listener=on_join)

        rooms = msg.result['update']
        for room in rooms:
            if room['_id'] != 'o58ac5XBk6xLXXKST':
                self.subscribe('stream-room-messages', room['_id'])

    # Helpers for different types of messages
    def send_message(self, **args) -> None:
        ''' Send a message to the server '''
        data = json.dumps(args)
        return self.ws.send(data)

    def call(self, *args, callback=None, **kwargs) -> None:
        ''' Call a realtime API method.

        If a callback is specified, it is called with the response to this
        message.
        '''
        message = Call(*args, **kwargs)

        if callback is not None:
            self.register_handler(callback, id=message.id)

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
            self.register_handler(listener, collection=name)

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
            "algorithm": "sha-256"
        }

    def create_handler(self, callable) -> Handler:
        if not inspect.isgeneratorfunction(callable):
            return callable

        # If we have a generator function, let's wrap it.
        def handler(message: Message):
            # Grab the generator from the callable
            generator = callable(message)

            try:
                # Take the first value from the generator.
                result = next(generator)
            except StopIteration:
                pass
            else:
                if result:
                    # The result must implement a `run` method.
                    # Since the generator itself does not have access to
                    # it's own object, we pass it to the result runner.
                    result.run(self, generator)
            return DONE
        handler.handles = callable.handles
        return handler

    def register_handler(self, callable, **handles):
        if handles:
            if hasattr(callable, 'handles'):
                callable.handles.append(handles)
            else:
                callable.handles = [handles]
        self.handlers.append(self.create_handler(callable))


class Runnable(ABC):
    @abstractmethod
    def run(self, client: Client, task: Task) -> None: ...


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

    def run(self, client: 'Client', task: Task) -> None:
        def resume_task(message: Message):
            try:
                result = task.send(message)
            except StopIteration:
                pass
            else:
                if result:
                    result.run(client, task)
            return DONE

        resume_task.handles = [{'id': self.id}]
        client.register_handler(resume_task)
        client.send_message(**self)
