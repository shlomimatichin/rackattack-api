import threading
import logging
import pika
import simplejson
from rackattack.tcp import suicide
from rackattack.tcp import publish

_logger = logging.getLogger(__name__)


class Subscribe(threading.Thread):
    def __init__(self, amqpURL):
        self._connection = None
        self._channel = None
        self._channelLock = threading.Lock()
        self._amqpURL = amqpURL
        self._registered = dict()
        self._readyEvent = threading.Event()
        self._closed = False
        threading.Thread.__init__(self)
        self.daemon = True
        threading.Thread.start(self)

    def registerForInagurator(self, id, callback):
        exchange = self._exchangeForInaugurator(id)
        self._registerToExchange(exchange, callback)

    def unregisterForInaugurator(self, id):
        exchange = self._exchangeForInaugurator(id)
        self._unregisterFromExchange(exchange)

    def registerForAllocation(self, id, callback):
        exchange = publish.Publish.allocationExchange(id)
        self._registerToExchange(exchange, callback)

    def unregisterForAllocation(self, id):
        exchange = publish.Publish.allocationExchange(id)
        self._unregisterFromExchange(exchange)

    def registerForAllAllocations(self, callback):
        self._registerToExchange(publish.Publish.ALL_HOSTS_ALLOCATIONS_EXCHANGE_NAME, callback)

    def unregisterForAllAllocations(self):
        self._unregisterFromExchange(publish.Publish.ALL_HOSTS_ALLOCATIONS_EXCHANGE_NAME)

    def close(self):
        _logger.info("Closing connection")
        self._closed = True
        self._channel.close()
        self._connection.close()

    def _registerToExchange(self, exchange, callback):
        assert exchange not in self._registered
        _Subscribe(self._channel, self._channelLock, exchange, self._registered, callback)

    def _unregisterFromExchange(self, exchange):
        assert exchange in self._registered
        self._channel.basic_cancel(lambda *args: None, self._registered[exchange])
        del self._registered[exchange]

    def _exchangeForInaugurator(self, id):
        return "inaugurator_status__%s" % id

    def _onConnectionClosed(self, connection, reply_code, reply_text):
        self._channel = None
        if self._closed:
            self._connection.ioloop.stop()
        else:
            _logger.error("Connection closed, committing suicide: %(replyCode)s %(replyText)s", dict(
                replyCode=reply_code, replyText=reply_text))
            suicide.killSelf()

    def _onConnectionOpen(self, unused_connection):
        _logger.info('Connection opened')
        self._connection.add_on_close_callback(self._onConnectionClosed)
        self._connection.channel(on_open_callback=self._onChannelOpen)

    def _onChannelClosed(self, channel, reply_code, reply_text):
        _logger.error('Channel %(channel)i was closed: (%(replyCode)s) %(replyText)s', dict(
            channel=channel, replyCode=reply_code, replyText=reply_text))
        self._connection.close()

    def _onChannelOpen(self, channel):
        self._channel = channel
        self._channel.add_on_close_callback(self._onChannelClosed)
        self._readyEvent.set()

    def run(self):
        _logger.info('Connecting to %(amqpURL)s', dict(amqpURL=self._amqpURL))
        self._connection = pika.SelectConnection(
            pika.URLParameters(self._amqpURL),
            self._onConnectionOpen,
            stop_ioloop_on_close=False)
        self._connection.ioloop.start()


class _Subscribe:
    def __init__(self, channel, channelLock, exchangeName, registered, callback):
        self._channel = channel
        self._channelLock = channelLock
        self._exchangeName = exchangeName
        self._registered = registered
        self._callback = callback
        self._channelLock.acquire()
        self._channel.exchange_declare(self._onExchangeDeclared, self._exchangeName, 'fanout')

    def _onMessage(self, channel, basicDeliver, properties, body):
        self._channel.basic_ack(basicDeliver.delivery_tag)
        self._callback(simplejson.loads(body))

    def _onBind(self, *args):
        self._channel.basic_consume(self._onMessage, self._queueName, consumer_tag=self._consumerTag)
        self._channelLock.release()
        _logger.debug("Listening on exchange %(exchange)s", dict(exchange=self._exchangeName))

    def _onQueueDeclared(self, methodFrame):
        self._queueName = methodFrame.method.queue
        self._consumerTag = self._queueName + "__consumer"
        self._registered[self._exchangeName] = self._consumerTag
        self._channel.queue_bind(self._onBind, self._queueName, self._exchangeName, routing_key='')

    def _onExchangeDeclared(self, frame):
        self._channel.queue_declare(callback=self._onQueueDeclared, exclusive=True)
