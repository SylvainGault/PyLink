"""
Network streams handling driver using the asyncio module.
"""

import asyncio
import threading

from pylinkirc import world
from pylinkirc.log import log

__all__ = ['register', 'unregister', 'start']


SELECT_TIMEOUT = 0.5

loop = asyncio.get_event_loop()
async def _monitor_world():
    """
    Stop the event loop when the world is shutting down.
    """
    while not world.shutting_down.is_set():
        await asyncio.sleep(SELECT_TIMEOUT)

    loop.stop()

def _process_conn(irc):
    """
    Callback for when some data is available on a given socket.
    """
    try:
        if not irc._aborted.is_set():
            t = threading.Thread(target=irc._run_irc, name="_run_irc for %s" % irc.name)
            t.start()
    except:
        log.exception('Error in network event loop:')

def register(irc):
    """
    Registers a network to the asyncio event loop.
    """
    log.debug('selectdriver: registering %s for network %s', irc._socket, irc.name)
    loop.add_reader(irc._socket, _process_conn, irc)

def unregister(irc):
    """
    Removes a network from the asyncio event loop.
    """
    if irc._socket.fileno() != -1:
        log.debug('selectdriver: de-registering %s for network %s', irc._socket, irc.name)
        loop.remove_reader(irc._socket)
    else:
        log.debug('selectdriver: skipping de-registering %s for network %s', irc._socket, irc.name)

def start():
    """
    Starts the event loop.
    """
    asyncio.ensure_future(_monitor_world(), loop=loop)
    loop.run_forever()
