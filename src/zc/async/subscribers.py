##############################################################################
#
# Copyright (c) 2006-2008 Zope Corporation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
import threading
import signal
import transaction
import twisted.internet.selectreactor
import zope.component
import zope.event
import zc.twist

import zc.async.interfaces
import zc.async.queue
import zc.async.agent
import zc.async.dispatcher
import zc.async.utils

class QueueInstaller(object):

    def __init__(self, queues=('',),
                 factory=lambda *args: zc.async.queue.Queue(),
                 db_name=None):
        # This IDatabaseOpenedEvent will be from zope.app.appsetup if that
        # package is around
        zope.component.adapter(zc.async.interfaces.IDatabaseOpenedEvent)(self)
        self.db_name = db_name
        self.factory = factory
        self.queues = queues

    def __call__(self, ev):
        db = ev.database
        tm = transaction.TransactionManager()
        conn = db.open(transaction_manager=tm)
        tm.begin()
        try:
            try:
                root = conn.root()
                if zc.async.interfaces.KEY not in root:
                    if self.db_name is not None:
                        other = conn.get_connection(self.db_name)
                        queues = other.root()[
                            zc.async.interfaces.KEY] = zc.async.queue.Queues()
                        other.add(queues)
                    else:
                        queues = zc.async.queue.Queues()
                    root[zc.async.interfaces.KEY] = queues
                    zope.event.notify(zc.async.interfaces.ObjectAdded(
                        queues, root, zc.async.interfaces.KEY))
                    tm.commit()
                    zc.async.utils.log.info('queues collection added')
                else:
                    queues = root[zc.async.interfaces.KEY]
                for queue_name in self.queues:
                    if queue_name not in queues:
                        queue = self.factory(conn, queue_name)
                        queues[queue_name] = queue
                        zope.event.notify(zc.async.interfaces.ObjectAdded(
                            queue, queues, queue_name))
                        tm.commit()
                        zc.async.utils.log.info('queue %r added', queue_name)
            except:
                tm.abort()
                raise
        finally:
            conn.close()

queue_installer = QueueInstaller()
multidb_queue_installer = QueueInstaller(db_name='async')
signal_handlers = {} # id(dispatcher) -> signal -> (prev handler, curr handler)

def restore_signal_handlers(dispatcher):
    key = id(dispatcher)
    sighandlers = signal_handlers.get(key)
    if sighandlers:
        for _signal, handlers in sighandlers.items():
            prev, cur = handlers
            # The previous signal handler is only restored if the currently
            # registered handler is the one we originally installed.
            if signal.getsignal(_signal) is cur:
                signal.signal(_signal, prev)
        del signal_handlers[key]


class ThreadedDispatcherInstaller(object):
    def __init__(self,
                 poll_interval=5,
                 reactor_factory=twisted.internet.selectreactor.SelectReactor,
                 uuid=None): # optional uuid is really just for tests; see
                             # catastrophes.txt, for instance, which runs
                             # two dispatchers simultaneously.
        self.poll_interval = poll_interval
        self.reactor_factory = reactor_factory
        self.uuid = uuid
        # This IDatabaseOpenedEvent will be from zope.app.appsetup if that
        # package is around
        zope.component.adapter(zc.async.interfaces.IDatabaseOpenedEvent)(self)

    def __call__(self, ev):
        reactor = self.reactor_factory()
        dispatcher = zc.async.dispatcher.Dispatcher(
            ev.database, reactor, poll_interval=self.poll_interval,
            uuid=self.uuid)
        def start():
            dispatcher.activate()
            reactor.run(installSignalHandlers=0)
        # we stash the thread object on the dispatcher so functional tests
        # can do the following at the end of a test:
        # dispatcher = zc.async.dispatcher.get()
        # dispatcher.reactor.callFromThread(dispatcher.reactor.stop)
        # dispatcher.thread.join(3)
        dispatcher.thread = thread = threading.Thread(target=start)
        thread.setDaemon(True)
        thread.start()

        # The above is really sufficient. This signal registration, below, is
        # an optimization. The dispatcher, on its next run, will eventually
        # figure out that it is looking at a previous incarnation of itself if
        # these handlers don't get to clean up.
        # We do this with signal handlers rather than atexit.register because
        # we want to clean up before the database is closed, if possible. ZODB
        # does not provide an appropriate hook itself as of this writing.
        curr_sigint_handler = signal.getsignal(signal.SIGINT)
        def sigint_handler(*args):
            reactor.callFromThread(reactor.stop)
            thread.join(3)
            curr_sigint_handler(*args)

        def handler(*args):
            reactor.callFromThread(reactor.stop)
            raise SystemExit()

        # We keep a record of the current signal handler and the one we're
        # installing so that our handler can later be uninstalled and the old
        # one reinstated (for instance, by zc.async.ftesting.tearDown).
        key = id(dispatcher)
        handlers = signal_handlers[key] = {}

        handlers[signal.SIGINT] = (
            signal.signal(signal.SIGINT, sigint_handler), sigint_handler,)
        handlers[signal.SIGTERM] = (signal.signal(signal.SIGTERM, handler),
                                    handler,)
        # Catch Ctrl-Break in windows
        if getattr(signal, "SIGBREAK", None) is not None:
            handlers[signal.SIGBREAK] = (
                signal.signal(signal.SIGBREAK, handler), handler,)

threaded_dispatcher_installer = ThreadedDispatcherInstaller()

class TwistedDispatcherInstaller(object):

    def __init__(self, poll_interval=5):
        self.poll_interval = poll_interval
        # This IDatabaseOpenedEvent will be from zope.app.appsetup if that
        # package is around
        zope.component.adapter(zc.async.interfaces.IDatabaseOpenedEvent)(self)

    def __call__(self, ev):
        dispatcher = zc.async.dispatcher.Dispatcher(
            ev.database, poll_interval=self.poll_interval)
        dispatcher.activate(threaded=True)

twisted_dispatcher_installer = TwistedDispatcherInstaller()

class AgentInstaller(object):

    def __init__(self, agent_name, chooser=None, size=3, queue_names=None,
                 filter=None):
        zope.component.adapter(
            zc.async.interfaces.IDispatcherActivated)(self)
        self.queue_names = queue_names
        self.agent_name = agent_name
        if filter is not None and chooser is not None:
            raise ValueError('cannot set both chooser and filter to non-None')
        self.chooser = chooser
        self.filter = filter
        self.size = size

    def __call__(self, ev):
        dispatcher = ev.object
        if (self.queue_names is None or
            dispatcher.parent.name in self.queue_names):
            if self.agent_name not in dispatcher:
                dispatcher[self.agent_name] = zc.async.agent.Agent(
                    chooser=self.chooser, filter=self.filter, size=self.size)
                zc.async.utils.log.info(
                    'agent %r added to queue %r',
                    self.agent_name,
                    dispatcher.parent.name)
            else:
                zc.async.utils.log.info(
                    'agent %r already in queue %r',
                    self.agent_name,
                    dispatcher.parent.name)

agent_installer = AgentInstaller('main')
