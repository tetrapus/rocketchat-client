import hashlib
import json
import uuid
import sys

from collections import defaultdict

from ws4py.client.threadedclient import WebSocketClient


class Client(WebSocketClient):
    def __init__(self, domain):
        super().__init__(
            "ws://{}/websocket".format(domain),
            protocols=['http-only', 'chat']))
        self.callbacks = defaultdict(list)
        self.handlers = defaultdict(list)

    def opened(self):
        self.send({
            "msg": "connect",
            "version": "1",
            "support": ["1"]
        })

    def received_message(self, msg):
        msg = json.loads(str(msg))

        for handler in self.handlers[msg.get('msg')]:
            handler(message)

        for callback in self.callbacks[msg.get('id')]:
            callback(msg)

    def on_ping(self, msg):
        self.send(msg="pong")

    def on_connected(self, msg):
        self.call('login', {
            "user": {"username": sys.argv[1]},
            "password": self._hash_password(sys.argv[2]),
        }, callback=self.init_data)

    def init_data(self, _):
        self.call('rooms/get', {'$date': 0}, callback=self.subscribe_to_rooms)

    def subscribe_to_rooms(self, msg):
        rooms = msg['result']['update']
        for room in rooms:
            self.subscribe('stream-room-messages', room['_id'], False)

    def send(self, **args):
        return super().send(json.dumps(args))

    def call(self, method, *params, callback=None, **args):
        args['msg'] = 'method'
        args['method'] = method
        args['id'] = generate_id()
        args['params'] = params
        if callback is not None:
            self.callbacks.setdefault(args['id'], []).append(callback)
        return self.send(**args)

    def subscribe(self, name, *params):
        return self.send(
            id=generate_id(),
            msg='sub',
            name=name,
            params=params
        )

    @staticmethod
    def generate_id():
        """ Create a unique ID.

        This is passed in each message, and used to match requests
        to responses.
        """
        return uuid.uuid4().hex

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
