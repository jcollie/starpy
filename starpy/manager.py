# -*- mode: python; coding: utf-8 -*-
#
# StarPy -- Asterisk Protocols for Twisted
#
# Copyright © 2006, Michael C. Fletcher
# Copyright © 2017, Jeffrey C. Ollie
#
# Michael C. Fletcher <mcfletch@vrplumber.com>
# Jeffrey C. Ollie <jeff@ocjtech.us>
#
# See http://asterisk-org.github.com/starpy/ for more information about the
# StarPy project. Please do not directly contact any of the maintainers of this
# project for assistance; the project provides a web site, mailing lists and
# IRC channels for your use.
#
# This program is free software, distributed under the terms of the
# BSD 3-Clause License. See the LICENSE file at the top of the source tree for
# details.

"""Asterisk Manager Interface for the Twisted networking framework

The Asterisk Manager Interface is a simple line-oriented protocol that allows
for basic control of the channels active on a given Asterisk server.

"""

import sys
import uuid

from distutils.version import LooseVersion

from hashlib import md5

from twisted.application.internet import ClientService
from twisted.internet.defer import Deferred
from twisted.internet.endpoints import clientFromString
from twisted.internet.error import ConnectionDone
from twisted.internet.protocol import Factory
from twisted.logger import Logger
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python.failure import Failure

from .error import AMICommandFailure

has_prometheus = False
try:
    from prometheus_client import Counter
    lines_sent = Counter('ami_lines_sent','Number of lines sent to the Asterisk Manager Interface')
    lines_received = Counter('ami_lines_received','Number of lines received from the Asterisk Manager Interface')
    messages_sent = Counter('ami_messages_sent', 'Number of messages sent to the Asterisk Manager Interface')
    messages_received = Counter('ami_messages_received', 'Number of messages received from the Asterisk Manager Interface')
    messages_discarded = Counter('ami_messages_discarded', 'Number of messages that were discarded')
    events_received = Counter('ami_events_received', 'Number of events that were received from the Asterisk Manager Interface')
    has_prometheus = True

except ImportError:
    pass

class deferredErrorResp(Deferred):
    """A subclass of Deferred that adds a registerError method to handle
    function callback when an Error response"""

    log = Logger()

    def __init__(self):
        self._errorRespCallback = None
        Deferred.__init__(self)

    def registerError(self, function ):
        """Add function for Error response callback"""

        self._errorRespCallback = function
        self.log.debug('Registering function {function:} to handle Error response', function = function)

class AMIProtocol(LineOnlyReceiver):
    """Protocol for the interfacing with the Asterisk Manager Interface (AMI)

    Provides most of the AMI Action interfaces.
    Auto-generates ActionID fields for all calls.

    Events and messages are passed around as simple dictionaries with
    all-lowercase keys.  Values are case-sensitive.

    XXX Want to allow for timeouts

    Attributes:
        count -- total count of messages sent from this protocol
        hostName -- used along with count and ID to produce unique IDs
        messageCache -- stores incoming message fragments from the manager
        id -- An identifier for this instance
    """

    log = Logger()

    count = 0

    def __init__(self, factory,
                 log_lines_sent = False, log_lines_received = False,
                 log_messages_sent = False, log_messages_received = False):
        self.factory = factory
        self.log_lines_sent = log_lines_sent
        self.log_lines_received = log_lines_received
        self.log_messages_sent = log_messages_sent
        self.log_messages_received = log_messages_received

        self.amiVersion = None
        self.messageCache = []
        self.actionIDCallbacks = {}
        self.eventTypeCallbacks = {}
        self.ami_id = str(uuid.uuid4())

    def registerEvent(self, event, function):
        """Register callback for the given event-type

        event -- string name for the event, None to match all events, or
            a tuple of string names to match multiple events.

            See http://www.voip-info.org/wiki/view/asterisk+manager+events
            for list of events and the data they bear.  Includes:

                Newchannel -- note that you can receive multiple Newchannel
                    events for a single channel!
                Hangup
                Newexten
                Newstate
                Reload
                Shutdown
                ExtensionStatus
                Rename
                Newcallerid
                Alarm
                AlarmClear
                Agentcallbacklogoff
                Agentcallbacklogin
                Agentlogin
                Agentlogoff
                MeetmeJoin
                MeetmeLeave
                MessageWaiting
                Join
                Leave
                AgentCalled
                ParkedCall
                UnParkedCall
                ParkedCalls
                Cdr
                ParkedCallsComplete
                QueueParams
                QueueMember

            among other standard events.  Also includes user-defined events.
        function -- function taking (protocol,event) as arguments or None
            to deregister the current function.

        Multiple functions may be registered for a given event
        """
        #self.log.debug('Registering function {function:} to handle events of type {event:}',
        #               function = function, event = event)
        types = [str, type(None)]
        if sys.version_info <= (3, 0):
            types.append(unicode)
        if isinstance(event, tuple(types)):
            event = (event,)
        for ev in event:
            self.eventTypeCallbacks.setdefault(ev, []).append(function)

    def deregisterEvent(self, event, function=None):
        """Deregister callback for the given event-type

        event -- event name (or names) to be deregistered, see registerEvent
        function -- the function to be removed from the callbacks or None to
            remove all callbacks for the event

        returns success boolean
        """
        #self.log.debug('Deregistering handler {function:} for events of type {event:}',
        #               function = function, event = event)
        types = [str, type(None)]
        if sys.version_info <= (3, 0):
            types.append(unicode)
        if isinstance(event, tuple(types)):
            event = (event,)
        success = True
        for ev in event:
            try:
                set = self.eventTypeCallbacks[ev]
            except KeyError as err:
                success = False
            else:
                try:
                    while function in set:
                        set.remove(function)
                except (ValueError, KeyError) as err:
                    success = False
                if not set or function is None:
                    try:
                        del self.eventTypeCallbacks[ev]
                    except KeyError as err:
                        success = False
        return success

    def lineReceived(self, line):
        """Handle Twisted's report of an incoming line from the manager"""
        if has_prometheus:
            lines_received.inc()
        if self.log_lines_received:
            self.log.debug('Line in: {line:}', line = repr(line))
        self.messageCache.append(line.decode('utf-8'))
        if not line.strip():
            self.dispatchIncoming()  # does dispatch and clears cache

    def connectionMade(self):
        """Handle connection to the AMI port (auto-login)

        This is a Twisted customisation point, we use it to automatically
        log into the connection we've just established.

        XXX Should probably use proper Twisted-style credential negotiations
        """
        self.log.info('connection made')
        if self.factory.service.plaintext_login:
            df = self.login()
        else:
            df = self.loginChallengeResponse()

        def onComplete(message):
            """Check for success, errback or callback as appropriate"""
            if not message['response'] == 'Success':
                self.log.info('Login failure: {message:}', message = message)
                self.transport.loseConnection()

            else:
                self.log.info('Login complete: {message:}', message = message)
                if self.factory.service.on_connected is not None:
                    self.factory.service.on_connected(self)

        def onFailure(failure):
            """Handle failure to login (e.g. due to timeout)"""

            self.log.failure('Login call failure', failure = failure)
            self.transport.loseConnection()

        df.addCallbacks(onComplete, onFailure)

    def connectionLost(self, reason):
        """Connection lost, clean up callbacks"""
        self.log.info('connection lost')
        for key, callable in self.actionIDCallbacks.items():
            try:
                callable(ConnectionDone("Manager connection terminated"))
            except Exception as err:
                self.log.error("Failure during connectionLost for callable {callable:}: {err:}",
                               callable = callable, err = err)
        self.actionIDCallbacks.clear()
        self.eventTypeCallbacks.clear()

    VERSION_PREFIX = 'Asterisk Call Manager'
    END_DATA = '--END COMMAND--'

    def dispatchIncoming(self):
        """Dispatch any finished incoming events/messages"""
        message = {}
        while self.messageCache:
            line = self.messageCache.pop(0)

            line = line.strip()
            if line:
                if line.endswith(self.END_DATA):
                    # multi-line command results...
                    line = line[0:-len(self.END_DATA)]
                    message['output'] = '\r\n'.join(line.split('\n'))
                else:
                    # regular line...
                    if line.startswith(self.VERSION_PREFIX):
                        self.amiVersion = line[len(self.VERSION_PREFIX) + 1:].strip()

                    else:
                        try:
                            key, value = line.split(':', 1)
                        except ValueError as err:
                            # XXX data-safety issues, what prevents the
                            # VERSION_PREFIX from showing up in a data-set?
                            self.log.warn("Improperly formatted line received and ignored: {line:}", line = repr(line))
                        else:
                            key = key.lower().strip()
                            if key in message:
                                message[key] += '\r\n' + value.strip()
                            else:
                                message[key] = value.strip();

        if has_prometheus:
            messages_received.inc()

        if self.log_messages_received:
            self.log.debug('Message received: {message:}', message = repr(message))

        if 'actionid' in message:
            key = message['actionid']
            callback = self.actionIDCallbacks.get(key)
            if callback:
                try:
                    callback(message)
                except Exception as err:
                    self.log.failure('exception in callback', failure = Failure(err))

        # otherwise is a monitor message or something we didn't send...
        if 'event' in message:
            self.dispatchEvent(message)

    def dispatchEvent(self, event):
        """Given an incoming event, dispatch to registered handlers"""
        if has_prometheus:
            events_received.inc()
        for key in (event['event'], None):
            try:
                handlers = self.eventTypeCallbacks[key]
            except KeyError as err:
                pass
            else:
                for handler in handlers:
                    try:
                        handler(self, event)
                    except Exception as err:
                        # would like the getException code here...
                        self.log.error('Exception in event handler {handler:} on event {event:}: {err:}',
                                       handler = handler, event = event, err = err)

    def generateActionId(self):
        """Generate a unique action ID

        Generally speaking, you shouldn't need to know the action ID, as the
        protocol handles the management of them automatically.
        """
        self.count += 1
        return '{}-{}'.format(self.ami_id, self.count)

    def sendDeferred(self, message):
        """Send with a single-callback deferred object

        Returns deferred that fires when a response to this message is received
        """
        df = deferredErrorResp()
        actionid = self.sendMessage(message, df.callback)
        df.addCallbacks(
            self.checkErrorResponse, self.cleanup,
            callbackArgs=(actionid, df,), errbackArgs=(actionid,)
        )
        return df

    def checkErrorResponse(self, result, actionid, df):
        """Check for error response and callback"""

        self.cleanup( result, actionid)
        if isinstance(result, dict) and result.get('response') == 'Error' and df._errorRespCallback:
            df._errorRespCallback(result)
        return result

    def cleanup(self, result, actionid):
        """Cleanup callbacks on completion"""
        try:
            del self.actionIDCallbacks[actionid]
        except KeyError as err:
            pass
        return result

    def sendLine(self, line):
        if has_prometheus:
            lines_sent.inc()
        super(AMIProtocol, self).sendLine(line.encode('utf-8'))

    def sendMessage(self, message, responseCallback=None):
        """Send the message to the other side, return deferred for the result

        returns the actionid for the message
        """
        if has_prometheus:
            messages_sent.inc()

        if type(message) == list:
            actionid = None
            for header, value in message:
                if str(header.lower()) == 'actionid':
                    actionid = value
            if actionid is None:
                actionid = self.generateActionId()
                message.append(['actionid', str(actionid)])
            if responseCallback:
                self.actionIDCallbacks[actionid] = responseCallback
            if self.log_messages_sent:
                self.log.debug('Message out: {message:}', message = message)
            for item in message:
                line = '{}: {}'.format(str(item[0].lower()), str(item[1]))
                self.sendLine(line)
        else:
            message = dict([(k.lower(), v) for (k, v) in message.items()])
            if 'actionid' not in message:
                message['actionid'] = self.generateActionId()
            if responseCallback:
                self.actionIDCallbacks[message['actionid']] = responseCallback
            if self.log_messages_sent:
                self.log.debug('Message out: {message:}', message = message)
            for key, value in message.items():
                line = '{}: {}'.format(str(key.lower()), str(value))
                self.sendLine(line)
        self.sendLine('')
        if type(message) == list:
            return actionid
        else:
            return message['actionid']

    def collectDeferred(self, message, stopEvent):
        """Collect all responses to this message until stopEvent or error

        returns deferred returning sequence of events/responses
        """
        df = Deferred()
        cache = []

        def onEvent(event):
            if event.get('response') == 'Error':
                df.errback(AMICommandFailure(event))
            elif event.get('event') == stopEvent:
                cache.append(event)
                df.callback(cache)
            else:
                cache.append(event)

        actionid = self.sendMessage(message, onEvent)
        df.addCallbacks(
            self.cleanup, self.cleanup,
            callbackArgs=(actionid,), errbackArgs=(actionid,)
        )
        return df

    def errorUnlessResponse(self, message, expected='Success'):
        """Raise AMICommandFailure error unless message['response'] == expected

        If == expected, returns the message
        """
        if type(message) is dict and message['response'] != expected:
            raise AMICommandFailure(message)
        return message

    ## End-user API
    def absoluteTimeout(self, channel, timeout):
        """Set timeout value for the given channel (in seconds)"""
        message = {
            'action': 'absolutetimeout',
            'timeout': timeout,
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def agentLogoff(self, agent, soft):
        """Logs off the specified agent for the queue system."""
        if soft in (True, 'yes', 1):
            soft = 'true'
        else:
            soft = 'false'
        message = {
            'Action': 'AgentLogoff',
            'Agent': agent,
            'Soft': soft
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def agents(self):
        """Retrieve agents information"""
        message = {
            "action": "agents"
        }
        return self.collectDeferred(message, "AgentsComplete")

    def changeMonitor(self, channel, filename):
        """Change the file to which the channel is to be recorded"""
        message = {
            'action': 'changemonitor',
            'channel': channel,
            'filename': filename
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def command(self, command):
        """Run asterisk CLI command, return deferred result for list of lines

        returns deferred returning list of lines (strings) of the command
        output.

        See listCommands to see available commands
        """
        message = {
            'action': 'command',
            'command': command
        }
        df = self.sendDeferred(message)
        if LooseVersion(self.amiVersion) > LooseVersion('2.7.0'):
            df.addCallback(self.errorUnlessResponse)
        else:
            df.addCallback(self.errorUnlessResponse, expected='Follows')

        def onResult(message):
            if not isinstance(message, dict):
                return message
            return message['output'].split ('\r\n')

        return df.addCallback(onResult)

    def action(self, action, **action_args):
        """Sends an arbitrary action to the AMI"""
        #action_args will be ar least an empty dict so we build the message from it.
        action_args['action'] = action
        return self.sendDeferred(action_args).addCallback(self.errorUnlessResponse)


    def dbDel(self, family, key):
        """Delete key value in the AstDB database"""
        message = {
            'Action': 'DBDel',
            'Family': family,
            'Key': key
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def dbDelTree(self, family, key=None):
        """Delete key value or key tree in the AstDB database"""
        message = {
            'Action': 'DBDelTree',
            'Family': family
        }
        if key is not None:
            message['Key'] = key
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def dbGet(self, family, key):
        """This action retrieves a value from the AstDB database"""
        df = Deferred()

        def extractValue(ami, event):
            value = event['val']
            self.deregisterEvent("DBGetResponse", extractValue)
            return df.callback(value)
        def errorResponse( message ):
            self.deregisterEvent("DBGetResponse", extractValue)
            return df.callback(None)
        message = {
            'Action': 'DBGet',
            'family': family,
            'key': key
        }
        self.sendDeferred(message).registerError(errorResponse)
        self.registerEvent("DBGetResponse", extractValue)
        return df

    def dbPut(self, family, key, value):
        """Sets a key value in the AstDB database"""
        message = {
            'Action': 'DBPut',
            'Family': family,
            'Key': key,
            'Val': value
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def events(self, eventmask=False):
        """Determine whether events are generated"""
        if eventmask in ('off', False, 0):
            eventmask = 'off'
        elif eventmask in ('on', True, 1):
            eventmask = 'on'
        # otherwise is likely a type-mask
        message = {
            'action': 'events',
            'eventmask': eventmask
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def extensionState(self, exten, context):
        """Get extension state

        This command reports the extension state for the given extension.
        If the extension has a hint, this will report the status of the
        device connected to the extension.

        The following are the possible extension states:

        -2    Extension removed
        -1    Extension hint not found
         0    Idle
         1    In use
         2    Busy"""
        message = {
            'Action': 'ExtensionState',
            'Exten': exten,
            'Context': context
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def getConfig(self, filename):
        """Retrieves the data from an Asterisk configuration file"""
        message = {
            'Action': 'GetConfig',
            'filename': filename
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def getVar(self, channel, variable):
        """Retrieve the given variable from the channel.

        If channel is None, this gets a global variable."""

        def extractVariable(message):
            """When message comes in, extract the variable from it"""
            if variable.lower() in message:
                value = message[variable.lower()]
            elif 'value' in message:
                value = message['value']
            else:
                raise AMICommandFailure(message)
            if value == '(null)':
                value = None
            return value

        message = {
            'action': 'getvar',
            'variable': variable
        }
        # channel is optional
        if channel:
            message['channel'] = channel
        return self.sendDeferred(
            message
        ).addCallback(
            self.errorUnlessResponse
        ).addCallback(
            extractVariable,
        )

    def hangup(self, channel):
        """Tell channel to hang up"""
        message = {
            'action': 'hangup',
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def login(self):
        """Log into the AMI interface (done automatically on connection)
        """
        return self.sendDeferred({
            'action': 'login',
            'username': self.factory.service.username,
            'secret': self.factory.service.secret,
        }).addCallback(self.errorUnlessResponse)

    def loginChallengeResponse(self):
        """Log into the AMI interface with challenge-response.

        Follows the same approach as self.login() using factory.username and factory.secret.
        Also done automatically on connection: will be called instead of self.login() if
        factory.plaintext_login is False: see AMIFactory constructor.
        """
        def sendResponse(challenge):
            if not type(challenge) is dict or not 'challenge' in challenge:
                raise AMICommandFailure(challenge)

            key_value = md5('{}{}'.format(challenge['challenge'], self.factory.service.secret)).hexdigest()

            d = self.sendDeferred({'action': 'Login',
                                   'authtype': 'MD5',
                                   'username': self.factory.service.username,
                                   'key': key_value})
            d = d.addCallback(self.errorUnlessResponse)
            return d

        d = self.sendDeferred({'action': 'Challenge',
                               'authtype': 'MD5'})
        d = d.addCallback(sendResponse)
        return d

    def listCommands(self):
        """List the set of commands available

        Returns a single message with each command-name as a key
        """
        message = {
            'action': 'listcommands'
        }

        def removeActionId(message):
            try:
                del message['actionid']
            except KeyError as err:
                pass
            return message

        d = self.sendDeferred(message)
        d = d.addCallback(self.errorUnlessResponse)
        d = d.addCallback(removeActionId)
        return d

    def logoff(self):
        """Log off from the manager instance"""
        message = {
            'action': 'logoff'
        }

        d = self.sendDeferred(message)
        d = d.addCallback(self.errorUnlessResponse, expected='Goodbye')
        return d

    def mailboxCount(self, mailbox):
        """Get count of messages in the given mailbox"""
        message = {
            'action': 'mailboxcount',
            'mailbox': mailbox
        }
        d = self.sendDeferred(message)
        d = d.addCallback(self.errorUnlessResponse)
        return d

    def mailboxStatus(self, mailbox):
        """Get status of given mailbox"""
        message = {
            'action': 'mailboxstatus',
            'mailbox': mailbox
        }

        d = self.sendDeferred(message)
        d = d.addCallback(self.errorUnlessResponse)
        return d

    def meetmeMute(self, meetme, usernum):
        """Mute a user in a given meetme"""
        message = {
            'action': 'MeetMeMute',
            'meetme': meetme,
            'usernum': usernum
        }
        return self.sendDeferred(message)

    def meetmeUnmute(self, meetme, usernum):
        """ Unmute a specified user in a given meetme"""
        message = {
            'action': 'meetmeunmute',
            'meetme': meetme,
            'usernum': usernum
        }
        return self.sendDeferred(message)

    def monitor(self, channel, file, format, mix):
        """Record given channel to a file (or attempt to anyway)"""
        message = {
            'action': 'monitor',
            'channel': channel,
            'file': file,
            'format': format,
            'mix': mix
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def originate(self, channel, context = None, exten = None, priority = None,
                  timeout = None, callerid = None, account = None, application = None,
                  data = None, variable = {}, async = False, actionid = None, channelid = None,
		  otherchannelid = None):
        """Originate call to connect channel to given context/exten/priority

        channel -- the outgoing channel to which will be dialed
        context/exten/priority -- the dialplan coordinate to which to connect
            the channel (i.e. where to start the called person)
        timeout -- duration before timeout in seconds
                   (note: not Asterisk standard!)
        callerid -- callerid to display on the channel
        account -- account to which the call belongs
        application -- alternate application to Dial to use for outbound dial
        data -- data to pass to application
        variable -- variables associated to the call
        async -- make the origination asynchronous
        """

        message = [(k, v) for (k, v) in (('action', 'originate'),
                                         ('channel', channel),
                                         ('context', context),
                                         ('exten', exten),
                                         ('priority', priority),
                                         ('callerid', callerid),
                                         ('account', account),
                                         ('application', application),
                                         ('data', data),
                                         ('async', str(async)),
                                         ('actionid', actionid),
                                         ('channelid', channelid),
                                         ('otherchannelid', otherchannelid)) if v is not None]
        if timeout is not None:
            message.append(('timeout', timeout * 1000))

        for var_name, var_value in variable.items():
            message.append(('variable', '{}={}'.format(var_name, var_value)))

        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def park(self, channel, channel2, timeout):
        """Park channel"""
        message = {
            'action': 'park',
            'channel': channel,
            'channel2': channel2,
            'timeout': timeout
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def parkedCall(self):
        """Check for a ParkedCall event"""
        message = {
            'action': 'ParkedCall'
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def unParkedCall(self):
        """Check for an UnParkedCall event """
        message = {
            'action': 'UnParkedCall'
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def parkedCalls(self):
        """Retrieve set of parked calls via multi-event callback"""
        message = {
            'action': 'ParkedCalls'
        }
        return self.collectDeferred(message, 'ParkedCallsComplete')

    def pauseMonitor(self, channel):
        """Temporarily stop recording the channel"""
        message = {
            'action': 'pausemonitor',
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def ping(self):
        """Check to see if the manager is alive..."""
        message = {
            'action': 'ping'
        }

        d = self.sendDeferred(message)
        if self.amiVersion == "1.0":
            d = d.addCallback(self.errorUnlessResponse, expected='Pong')

        else:
            d = d.addCallback(self.errorUnlessResponse)

        return d

    def playDTMF(self, channel, digit):
        """Play DTMF on a given channel"""
        message = {
            'action': 'playdtmf',
            'channel': channel,
            'digit': digit
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def queueAdd(self, queue, interface, penalty=0, paused=True,
                 membername=None, stateinterface=None):
        """Add given interface to named queue"""
        if paused in (True, 'true', 1):
            paused = 'true'
        else:
            paused = 'false'
        message = {
            'action': 'queueadd',
            'queue': queue,
            'interface': interface,
            'penalty': penalty,
            'paused': paused
        }
        if membername is not None:
            message['membername'] = membername

        if stateinterface is not None:
            message['stateinterface'] = stateinterface

        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def queueLog(self, queue, event, uniqueid=None, interface=None, msg=None):
        """Adds custom entry in queue_log"""
        message = {
            'action': 'queuelog',
            'queue': queue,
            'event': event
        }
        if uniqueid is not None:
            message['uniqueid'] = uniqueid
        if interface is not None:
            message['interface'] = interface
        if msg is not None:
            message['message'] = msg
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def queuePause(self, queue, interface, paused=True, reason=None):
        if paused in (True, 'true', 1):
            paused = 'true'
        else:
            paused = 'false'
        message = {
            'action': 'queuepause',
            'queue': queue,
            'interface': interface,
            'paused': paused
        }
        if reason is not None:
            message['reason'] = reason
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def queuePenalty(self, interface, penalty, queue=None):
        """Set penalty for interface"""
        message = {
            'action': 'queuepenalty',
            'interface': interface,
            'penalty': penalty
        }
        if queue is not None:
            message.update({'queue': queue})
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def queueRemove(self, queue, interface):
        """Remove given interface from named queue"""
        message = {
            'action': 'queueremove',
            'queue': queue,
            'interface': interface
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def queues(self):
        """Retrieve information about active queues via multiple events"""
        # XXX AMI returns improperly formatted lines so this doesn't work now.
        message = {
            'action': 'queues'
        }
        #return self.collectDeferred(message, 'QueueStatusEnd')
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def queueStatus(self, queue=None, member=None):
        """Retrieve information about active queues via multiple events"""
        message = {
            'action': 'queuestatus'
        }
        if queue is not None:
            message.update({'queue': queue})
        if member is not None:
            message.update({'member': member})
        return self.collectDeferred(message, 'QueueStatusComplete')

    def redirect(self, channel, context, exten, priority, extraChannel=None):
        """Transfer channel(s) to given context/exten/priority"""
        message = {
            'action': 'redirect',
            'channel': channel,
            'context': context,
            'exten': exten,
            'priority': priority,
        }
        if extraChannel is not None:
            message['extrachannel'] = extraChannel
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def setCDRUserField(self, channel, userField, append=True):
        """Set/add to a user field in the CDR for given channel"""
        if append in (True, 'true', 1):
            append = 'true'
        else:
            append = 'false'
        message = {
            'channel': channel,
            'userfield': userField,
            'append': append,
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def setVar(self, channel, variable, value):
        """Set channel variable to given value.

        If channel is None, this sets a global variable."""
        message = {
            'action': 'setvar',
            'variable': variable,
            'value': value
        }
        # channel is optional
        if channel:
            message['channel'] = channel
        return self.sendDeferred(
            message
        ).addCallback(
            self.errorUnlessResponse
        )

    def sipPeers(self):
        """List all known sip peers"""
        # XXX not available on my box...
        message = {
            'action': 'sippeers'
        }
        return self.collectDeferred(message, 'PeerlistComplete')

    def sipShowPeers(self, peer):
        message = {
            'action': 'sipshowpeer',
            'peer': peer
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def pjsipEndpoints(self):
        """List all known pjsip endpoints"""
        message = {
            'action': 'pjsipshowendpoints'
        }
        return self.collectDeferred(message, 'EndpointListComplete')

    def pjsipShowEndpoints(self, endpoint):
        message = {
            'action': 'pjsipshowendpoint',
            'endpoint': endpoint
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def status(self, channel=None):
        """Retrieve status for the given (or all) channels

        The results come in via multi-event callback

        channel -- channel name or None to retrieve all channels

        returns deferred returning list of Status Events for each requested
        channel
        """
        message = {
            'action': 'Status'
        }
        if channel:
            message['channel'] = channel
        return self.collectDeferred(message, 'StatusComplete')

    def stopMonitor(self, channel):
        """Stop monitoring the given channel"""
        message = {
            'action': 'monitor',
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def unpauseMonitor(self, channel):
        """Resume recording a channel"""
        message = {
            'action': 'unpausemonitor',
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def updateConfig(self, srcfile, dstfile, reload, headers={}):
        """Update a configuration file

        headers should be a dictionary with the following keys
        Action-XXXXXX
        Cat-XXXXXX
        Var-XXXXXX
        Value-XXXXXX
        Match-XXXXXX
        """
        message = {}
        if reload in (True, 'yes', 1):
            reload = 'yes'
        else:
            reload = 'no'
        message = {
            'action': 'updateconfig',
            'srcfilename': srcfile,
            'dstfilename': dstfile,
            'reload': reload
        }
        for k, v in headers.items():
            message[k] = v
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def userEvent(self, event, **headers):
        """Sends an arbitrary event to the Asterisk Manager Interface."""
        message = {
            'Action': 'UserEvent',
            'userevent': event
        }
        for i, j in headers.items():
            message[i] = j
        return self.sendMessage(message)

    def waitEvent(self, timeout):
        """Waits for an event to occur

        After calling this action, Asterisk will send you a Success response as
        soon as another event is queued by the AMI
        """
        message = {
            'action': 'WaitEvent',
            'timeout': timeout
        }
        return self.collectDeferred(message, 'WaitEventComplete')

    def dahdiDNDoff(self, channel):
        """Toggles the DND state on the specified DAHDI channel to off"""
        message = {
            'action': 'DAHDIDNDoff',
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def dahdiDNDon(self, channel):
        """Toggles the DND state on the specified DAHDI channel to on"""
        message = {
            'action': 'DAHDIDNDon',
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def dahdiDialOffhook(self, channel, number):
        """Dial a number on a DAHDI channel while off-hook"""
        message = {
            'Action': 'DAHDIDialOffhook',
            'DAHDIChannel': channel,
            'Number': number
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def dahdiHangup(self, channel):
        """Hangs up the specified DAHDI channel"""
        message = {
            'Action': 'DAHDIHangup',
            'DAHDIChannel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def dahdiRestart(self, channel):
        """Restarts the DAHDI channels, terminating any calls in progress"""
        message = {
            'Action': 'DAHDIRestart',
            'DAHDIChannel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)

    def dahdiShowChannels(self):
        """List all DAHDI channels"""
        message = {
            'action': 'DAHDIShowChannels'
        }
        return self.collectDeferred(message, 'DAHDIShowChannelsComplete')

    def dahdiTransfer(self, channel):
        """Transfers DAHDI channel"""
        message = {
            'Action': 'DAHDITransfer',
            'channel': channel
        }
        return self.sendDeferred(message).addCallback(self.errorUnlessResponse)


class AMIFactory(Factory):
    """A factory for AMI protocols
    """
    log = Logger()

    def __init__(self, service, log_lines_sent = False, log_lines_received = False,
                 log_messages_sent = False, log_messages_received = False):
        self.service = service
        self.log_lines_sent = log_lines_sent
        self.log_lines_received = log_lines_received
        self.log_messages_sent = log_messages_sent
        self.log_messages_received = log_messages_received

    def buildProtocol(self, addr):
        self.log.debug('Building AMI protocol for {addr:}', addr = addr)
        return AMIProtocol(self,
                           log_lines_sent = self.log_lines_sent,
                           log_lines_received = self.log_lines_received,
                           log_messages_sent = self.log_messages_sent,
                           log_messages_received = self.log_messages_received)

class AMIService(object):
    def __init__(self, reactor, username, secret,
                 hostname = 'localhost', port = 5038, tls = False,
                 plaintext_login = True, on_connected = None,
                 log_lines_sent = False, log_lines_received = False,
                 log_messages_sent = False, log_messages_received = False):

        self.reactor = reactor
        self.username = username
        self.secret = secret
        self.hostname = hostname
        self.port = port
        self.tls = tls
        self.plaintext_login = plaintext_login
        self.on_connected = on_connected
        self.log_lines_sent = log_lines_sent
        self.log_lines_received = log_lines_received
        self.log_messages_sent = log_messages_sent
        self.log_messages_received = log_messages_received

        self.factory = AMIFactory(self,
                                  log_lines_sent = self.log_lines_sent,
                                  log_lines_received = self.log_lines_received,
                                  log_messages_sent = self.log_messages_sent,
                                  log_messages_received = self.log_messages_received)
        if self.tls:
            client_string = 'tls:host={}:port={}'.format(self.hostname, self.port)
        else:
            client_string = 'tcp:host={}:port={}'.format(self.hostname, self.port)
        self.endpoint = clientFromString(self.reactor, client_string)
        self.service = ClientService(self.endpoint, self.factory)
        self.service.setName('Asterisk Manager Interface')
        self.service.startService()

    def whenConnected(self):
        return self.service.whenConnected()
