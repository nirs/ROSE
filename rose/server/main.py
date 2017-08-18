import json
import socket

from twisted.internet import reactor, protocol
from twisted.protocols import basic
from twisted.web import http, resource, server, static, xmlrpc

from autobahn.twisted.resource import WebSocketResource
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol

from rose.common import config, error, message
import game


class Hub(object):

    def __init__(self, game):
        game.hub = self
        self.game = game
        self.players = set()

    # Player client interface

    def add_player(self, player):
        # First add player, will raise if there are too many players or this
        # name is already taken.
        self.game.add_player(player.name)
        self.players.add(player)

    def remove_player(self, player):
        if player in self.players:
            self.players.remove(player)
            self.game.remove_player(player.name)

    def drive_player(self, player, info):
        self.game.drive_player(player.name, info)

    # Game hub interface

    def broadcast(self, msg):
        data = str(msg)
        for player in self.players:
            player.send_message(data)

class Player(basic.LineReceiver):

    def __init__(self, hub):
        self.hub = hub
        self.name = None

    # LineReceiver interface

    def connectionLost(self, reason):
        self.hub.remove_player(self)

    def lineReceived(self, line):
        try:
            msg = message.parse(line)
            self.dispatch(msg)
        except error.Error as e:
            print "Error handling message: %s" % e
            msg = message.Message('error', {'message': str(e)})
            self.sendLine(str(msg))
            self.transport.loseConnection()

    # Hub client interface

    def send_message(self, data):
        self.sendLine(data)

    # Disaptching messages

    def dispatch(self, msg):
        if self.name is None:
            # New player
            if msg.action != 'join':
                raise error.ActionForbidden(msg.action)
            if 'name' not in msg.payload:
                raise error.InvalidMessage("name required")
            self.name = msg.payload['name']
            self.hub.add_player(self)
        else:
            # Registered player
            if msg.action == 'drive':
                self.hub.drive_player(self, msg.payload)
            else:
                raise error.ActionForbidden(msg.action)

class PlayerFactory(protocol.ServerFactory):

    def __init__(self, hub):
        self.hub = hub

    def buildProtocol(self, addr):
        p  = Player(self.hub)
        p.factory = self
        return p

class XMLRPC(xmlrpc.XMLRPC):

    def __init__(self, game):
        self.game = game
        xmlrpc.XMLRPC.__init__(self, allowNone=True)

    def xmlrpc_start(self):
        try:
            self.game.start()
        except error.GameAlreadyStarted as e:
            raise xmlrpc.Fault(1, str(e))

    def xmlrpc_stop(self):
        try:
            self.game.stop()
        except error.GameNotStarted as e:
            raise xmlrpc.Fault(1, str(e))

    def xmlrpc_set_rate(self, rate):
        self.game.rate = rate

class Watcher(WebSocketServerProtocol):

    # WebSocketServerProtocol interface

    def onConnect(self, request):
        print "watcher connected from %s" % request
        self.factory.add_watcher(self)

    def onOpen(self):
        msg = message.Message("update", self.factory.game_state())
        self.send_message(str(msg))

    def onClose(self, wasClean, code, reason):
        print ("watcher closed (wasClean=%s, code=%s, reason=%s)"
               % (wasClean, code, reason))
        self.factory.remove_watcher(self)

    # Hub client interface

    def send_message(self, data):
        self.sendMessage(data, False)

class WatcherFactory(WebSocketServerFactory):

    protocol = Watcher

    def __init__(self, url, game):
        WebSocketServerFactory.__init__(self, url)
        game.watcher = self
        self.game = game
        self.watchers = set()

    def add_watcher(self, w):
        self.watchers.add(w)

    def remove_watcher(self, w):
        self.watchers.remove(w)

    def game_state(self):
        return self.game.state()

    def broadcast(self, msg):
        data = str(msg)
        for w in self.watchers:
            w.send_message(data)

class WebAdmin(resource.Resource):

    def __init__(self, game):
        self.game = game
        resource.Resource.__init__(self)

    def render_GET(self, request):
        request.setHeader(b"content-type", b"application/json")
        return json.dumps(self.game.state())

    def render_POST(self, request):
        if "running" in request.args:
            value = request.args["running"][0]
            if value == "1":
                self.game.start()
            elif value == "0":
                if self.game.started:
                    self.game.stop()
            else:
                request.setResponseCode(http.BAD_REQUEST)
                return b"Invalid running value %r, expected (1, 0)" % value
        if "rate" in request.args:
            value = request.args["rate"][0]
            try:
                self.game.rate = float(value)
            except ValueError:
                request.setResponseCode(http.BAD_REQUEST)
                return b"Invalid rate value %r, expected number" % value
        return ""

def main():
    g = game.Game()
    h = Hub(g)
    reactor.listenTCP(config.game_port, PlayerFactory(h))
    root = static.File(config.web_root)
    root.putChild('admin', WebAdmin(g))
    root.putChild('res', static.File(config.res_root))
    wsuri = u"ws://%s:%s" % (socket.gethostname(), config.web_port)
    watcher = WatcherFactory(wsuri, g)
    root.putChild("ws", WebSocketResource(watcher))
    root.putChild('rpc2', XMLRPC(g))
    site = server.Site(root)
    reactor.listenTCP(config.web_port, site)
    reactor.run()
