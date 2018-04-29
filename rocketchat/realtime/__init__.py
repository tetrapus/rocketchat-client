''' RocketChat Realtime API. '''

import hashlib
import json
import logging
import uuid

from abc import ABC, abstractmethod
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
    '''
    Sentinels handlers may return to signal various behaviours
    '''
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

    def __getattr__(self, attr: str) -> Any:
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
Task = Generator['Runnable', Message, None]
TaskFactory = Callable[[Message], Task]
Listener = Union[Handler, TaskFactory]


def get_handles(funct: Callable) -> List[Dict[str, Any]]:
    ''' Get handles registered for a function.

    This is a dirty hack to add metadata to a function in a type-safe way.

    Python takes advantage of the fact that function arguments cannot be
    keywords to put the return annotation in the 'return' key.
    We're doing the same with 'for'.

    Think of it like, trigger this handler 'for' these events!
    '''
    return funct.__annotations__.get('for', [])


def add_handle(funct: Callable, handle: Dict[str, Any]) -> None:
    funct.__annotations__.setdefault('for', []).append(handle)


def handles(**kwargs: Any) -> Callable[[Callable], Callable]:
    '''Register a method as an event handler.

    When applied to a method of `Client`, tagged methods will be registered
    during class initialisation.
    '''
    def decorator(funct: Callable) -> Callable:
        ''' Save the handles on the function object. '''
        add_handle(funct, kwargs)
        return funct
    return decorator


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
            if 'for' in getattr(method, '__annotations__', []):
                self.register_handler(method)

        self.ws.connect()

    def run(self) -> None:
        return self.ws.run_forever()

    # Websocket event handlers
    def opened(self) -> None:
        ''' Called when the socket is opened. '''
        self.send_message(msg='connect', version='1', support=['1'])

    def received_message(self, message: TextMessage) -> None:
        ''' Called when a message is received from the server.

        This method deserialises the message and dispatches it to registered
        event handlers.
        '''
        event = json.loads(message.data, object_hook=Message.decoder)

        # Find all the handlers for this event
        handlers = [
            handler for handler in self.handlers
            if any(
                self._trigger_matches(trigger, event)
                for trigger in get_handles(handler)
            )
        ]

        for handler in handlers:
            # Call the handler
            result = handler(event)
            if result is DONE:
                self.handlers.remove(handler)

    def closed(self, code: str, reason: str = None) -> None:
        ''' Called when the socket is closed. '''
        pass

    def close(self) -> None:
        ''' Close the socket. '''
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
    def send_message(self, **args: Any) -> None:
        ''' Send a message to the server '''
        data = json.dumps(args)
        return self.ws.send(data)

    def call(
            self,
            *args: Any,
            callback: Listener = None,
            **kwargs: Any) -> None:
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
            listener: Listener = None) -> None:
        ''' Subscribe to a stream.

        The optional parameter listener specifies a handler to call when
        corresponding messages are received.

        If a listener is given, it is triggered by events with the matching
        collection and event names.
        '''
        if listener:
            self.register_handler(
                listener,
                collection=name,
                fields={'eventName': event}
            )

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

    @classmethod
    def _trigger_matches(cls, trigger: Any, event: Any) -> bool:
        if isinstance(trigger, dict):
            for key, value in trigger.items():
                try:
                    if not cls._trigger_matches(value, event[key]):
                        return False
                except (KeyError, IndexError):
                    return False
            return True
        elif isinstance(trigger, list):
            for value in trigger:
                if not cls._trigger_matches(value, event[key]):
                    return False
            return True

        return trigger == event

    def create_handler(self, listener: Listener) -> Handler:
        def handler(message: Message) -> Optional[HandlerSignal]:
            # Grab the generator from the listener
            generator = listener(message)

            # Function cases. We wrap to satisfy mypy :'(
            if generator is None:
                return None
            elif isinstance(generator, HandlerSignal):
                return generator

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
        for handle in get_handles(listener):
            add_handle(handler, handle)
        return handler

    def register_handler(self, callable: Listener, **handles: Any) -> None:
        if handles:
            add_handle(callable, handles)
        self.handlers.append(self.create_handler(callable))


class WebsocketWrapper(WebSocketClient):
    ''' Wrapper around WebSocketClient which proxies events to `client` '''

    def __init__(self, client: Client, *args: Any, **kwargs: Any) -> None:
        self.client = client
        super().__init__(*args, **kwargs)

    def opened(self) -> None:
        return self.client.opened()

    def received_message(self, message: TextMessage) -> None:
        return self.client.received_message(message)

    def closed(self, code: str, reason: str = None) -> None:
        return self.client.closed(code, reason)


class Runnable(ABC):
    @abstractmethod
    def run(self, client: Client, task: Task) -> None: ...


class Call(Message, Runnable):
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

    def run(self, client: Client, task: Task) -> None:
        ''' Sends the message, with `Task` registered as it's handler. '''
        def resume_task(message: Message) -> HandlerSignal:
            ''' Resume the given task. '''
            try:
                result = task.send(message)
            except StopIteration:
                pass
            else:
                if result:
                    result.run(client, task)
            return DONE

        add_handle(resume_task, {'id': self.id})
        client.register_handler(resume_task)
        client.send_message(**self)
