''' Run a client using credentials from stdin, and block until it exits '''

import sys

from . import Client

try:
    ws = Client(sys.argv[3], username=sys.argv[1], password=sys.argv[2])
    ws.connect()
    ws.run_forever()
except KeyboardInterrupt:
    ws.close()
