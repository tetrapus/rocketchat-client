import hashlib
import json
import uuid
import sys

from collections import defaultdict, Iterable

from ws4py.client.threadedclient import WebSocketClient


class Message(dict):
    @classmethod
    def decoder(cls, object):
        return cls(**object)

    def __getattr__(self, attr):
        return self[attr]


def generate_id():
    """ Create a unique ID.

    This is passed in each message, and used to match requests
    to responses.
    """
    return uuid.uuid4().hex


class Call(Message):
    def __init__(self, method, *params, **args):
        super().__init__(
            **args,
            msg='method',
            method=method,
            id=generate_id(),
            params=params,
        )


class Client(WebSocketClient):
    def __init__(self, domain):
        super().__init__(
            "ws://{}/websocket".format(domain),
            protocols=['http-only', 'chat'])
        self.callbacks = defaultdict(list)
        self.handlers = defaultdict(list)

        # Set default handlers
        self.handlers.update({
            'connected': [self.on_connected],
            'ping': [self.on_ping]
        })

    # Websocket event handlers
    def opened(self):
        self.send(msg='connect', version='1', support=['1'])

    def received_message(self, msg):
        msg = json.loads(str(msg), object_hook=Message.decoder)
        print(msg)

        for handler in self.handlers[msg.get('msg')]:
            # Call the handler
            result = handler(msg)
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

        for callback in self.callbacks[msg.get('id')]:
            if hasattr(callback, 'send'):
                try:
                    message = callback.send(msg)
                    if 'id' in message:
                        self.callbacks[message['id']].append(callback)
                    self.send(**message)
                except StopIteration:
                    pass
            else:
                callback(msg)

    def closed(self, code, reason=None):
        pass

    # Default event handlers
    def on_ping(self, msg):
        self.send(msg="pong")

    def on_connected(self, msg):
        msg = yield Call('login', {
            "user": {"username": sys.argv[1]},
            "password": self._hash_password(sys.argv[2]),
        })

        if 'error' in msg:
            print("{errorType}: {message}".format(**msg.error))
            self.close()

        msg = yield Call('rooms/get', {'$date': 0})

        rooms = msg.result['update']
        for room in rooms:
            self.subscribe('stream-room-messages', room._id, False)

    # Helpers for different types of messages
    def send(self, **args):
        print(args)
        return super().send(json.dumps(args))

    def call(self, *args, callback=None, **kwargs):
        args = Call(*args, **kwargs)

        if callback is not None:
            self.callbacks[args.id].append(callback)

        return self.send(**args)

    def subscribe(self, name, *params):
        return self.send(
            id=generate_id(),
            msg='sub',
            name=name,
            params=params
        )

    @staticmethod
    def _hash_password(password):
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
