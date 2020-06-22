"""
Network streams handling driver using the asyncio module.
"""

import asyncio

from pylinkirc import world
from pylinkirc.log import log

__all__ = ['register', 'unregister', 'start', 'create_task',
           'create_delayed_task', 'to_thread']


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
            create_task(irc._run_irc(), name="_run_irc for %s" % irc.name)
    except:
        log.exception('Error in network event loop:')

def register(irc):
    """
    Registers a network to the asyncio event loop.
    """
    log.debug('eventloop: registering %s for network %s', irc._socket, irc.name)
    loop.add_reader(irc._socket, _process_conn, irc)

def unregister(irc):
    """
    Removes a network from the asyncio event loop.
    """
    if irc._socket.fileno() != -1:
        log.debug('eventloop: de-registering %s for network %s', irc._socket, irc.name)
        loop.remove_reader(irc._socket)
    else:
        log.debug('eventloop: skipping de-registering %s for network %s', irc._socket, irc.name)

def start():
    """
    Starts the event loop.
    """
    asyncio.ensure_future(_monitor_world(), loop=loop)
    loop.run_forever()

def create_task(coro, name=None):
    """
    Create a new task from a coroutine and schedule it for future execution in
    the event loop. Return immediately.
    """
    return asyncio.run_coroutine_threadsafe(coro, loop)

def create_delayed_task(coro, secs, name=None):
    """
    Create and schedule a task that wait for an amount of seconds and run the
    coroutine. Return immediately.
    """
    async def _task():
        await asyncio.sleep(secs)
        await coro

    return create_task(_task(), name)

async def to_thread(func, *args, **kwargs):
    """
    Calls func(*args) in a thread and await for the result.
    """
    # This intermediate function is only used to create a closure because
    # run_in_executor doesn't support kwargs.
    def wrap():
        return func(*args, **kwargs)
    return await loop.run_in_executor(None, wrap)
