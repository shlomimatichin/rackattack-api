from rackattack import api
from rackattack.tcp import node
from rackattack.tcp import suicide
import threading
import logging


class Allocation(api.Allocation):
    def __init__(self, id, requirements, ipcClient, subscribe, heartbeat):
        self._id = id
        self._requirements = requirements
        self._ipcClient = ipcClient
        self._heartbeat = heartbeat
        self._subscribe = subscribe
        self._forceReleaseCallback = None
        self._dead = None
        self._progressCallback = None
        self._progressPercent = dict()
        self._inauguratorsIDs = []
        self._waitEvent = threading.Event()
        self._subscribe.registerForAllocation(self._id, self._allocationEventBroadcasted)
        self._refetchInauguratorIDs()
        self._heartbeat.register(id)
        if self.dead() or self.done():
            self._waitEvent.set()
        logging.info("allocation created")

    def _idForNodeIPC(self):
        assert not self._dead
        return self._id

    def registerProgressCallback(self, callback):
        assert self._progressCallback is None
        self._progressCallback = callback

    def done(self):
        assert not self._dead
        return self._ipcClient.call('allocation__done', id=self._id)

    def dead(self):
        if self._dead:
            return self._dead
        result = self._ipcClient.call('allocation__dead', id=self._id)
        self._dead = result
        return self._dead

    def wait(self, timeout=None):
        self._waitEvent.wait(timeout=timeout)
        if not self._waitEvent.isSet():
            raise Exception("Timeout waiting for allocation")
        death = self.dead()
        if death is not None:
            raise Exception(death)

    def nodes(self):
        assert not self._dead
        assert self.done()
        allocatedMap = self._ipcClient.call('allocation__nodes', id=self._id)
        result = {}
        for name, info in allocatedMap.iteritems():
            nodeInstance = node.Node(
                ipcClient=self._ipcClient, allocation=self, name=name, info=info)
            result[name] = nodeInstance
        return result

    def fetchPostMortemPack(self):
        connection = self._ipcClient.urlopen("/allocation/%s/postMortemPack" % self._id)
        try:
            return "postMortemPack.txt", connection.read()
        finally:
            connection.close()

    def free(self):
        logging.info("freeing allocation")
        self._ipcClient.call('allocation__free', id=self._id)
        self._dead = "freed"
        self._close()

    def _close(self):
        self._heartbeat.unregister(self._id)
        for id in self._inauguratorsIDs.values():
            self._subscribe.unregisterForInaugurator(id)
        self._subscribe.unregisterForAllocation(self._id)
        self._ipcClient.allocationClosed(self)
        self._waitEvent.set()

    def setForceReleaseCallback(self, callback):
        self._forceReleaseCallback = callback

    def connectionToProviderInterrupted(self):
        self._dead = "connection to provider terminated"
        self._close()

    def _allocationEventBroadcasted(self, event):
        if event.get('event', None) == "changedState":
            self._waitEvent.set()
        elif event.get('event', None) == "providerMessage":
            logging.info("Rackattack provider says: %(message)s", dict(message=event['message']))
        elif event.get('event', None) == "withdrawn":
            if self._forceReleaseCallback is None:
                logging.error(
                    "Rackattack provider widthdrew allocation: '%(message)s'. No ForceRelease callback is "
                    "registered. Commiting suicide", dict(message=event['message']))
                suicide.killSelf()
            else:
                logging.warning(
                    "Rackattack provider widthdrew allocation: '%(message)s'. ForceRelease callback is "
                    "registered. Calling...", dict(message=event['message']))
                self._forceReleaseCallback()

    def _inauguratorEventBroadcasted(self, event):
        logging.debug("Inaugurator '%(id)s' event: %(event)s", dict(event=event, id=event['id']))
        if event['status'] == 'progress':
            logging.info("Inaugurator '%(id)s' percent %(percent)s", dict(
                id=event['id'], percent=event['progress']['percent']))
            self._progressPercent[event['id']] = event['progress']['percent']
        else:
            logging.info("Inaugurator '%(id)s' status %(status)s", event)
        if self._progressCallback is not None:
            self._progressCallback(overallPercent=self._overallPercent(), event=event)

    def _overallPercent(self):
        if len(self._progressPercent) == 0:
            return 0
        return sum(self._progressPercent.values()) / (len(self._progressPercent))

    def _refetchInauguratorIDs(self):
        previous = self._inauguratorsIDs
        self._inauguratorsIDs = self._ipcClient.call('allocation__inauguratorsIDs', id=self._id)
        for id in self._inauguratorsIDs.values():
            if id not in previous:
                logging.info("Adding inaugurator to listen to: %(id)s", dict(id=id))
                self._subscribe.registerForInagurator(id, self._inauguratorEventBroadcasted)
