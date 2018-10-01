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

"""Asterisk FastAGI server for use from the dialplan

You use an asterisk FastAGI like this from extensions.conf:

    exten => 1000,3,AGI(agi://127.0.0.1:4573,arg1,arg2)

Where 127.0.0.1 is the server and 4573 is the port on which
the server is listening.
"""

from twisted.internet.protocol import Factory
from twisted.internet.defer import Deferred
from twisted.internet.defer import maybeDeferred
from twisted.internet.error import ConnectionDone
from twisted.protocols.basic import LineOnlyReceiver
from twisted.logger import Logger

import re
import time

from .error import AGICommandFailure

FAILURE_CODE = -1

class FastAGIProtocol(LineOnlyReceiver):
    """Protocol for the interfacing with the Asterisk FastAGI application

    Attributes:

        variables -- for  connected protocol, the set of variables passed
            during initialisation, keys are all-lower-case, set of variables
            returned for an Asterisk 1.2.1 installation on Gentoo on a locally
            connected channel:

                agi_network = 'yes'
                agi_request = 'agi://localhost'
                agi_channel = 'SIP/mike-ccca'
                agi_language = 'en'
                agi_type = 'SIP'
                agi_uniqueid = '1139871605.0'
                agi_callerid = 'mike'
                agi_calleridname = 'Mike Fletcher'
                agi_callingpres = '0'
                agi_callingani2 = '0'
                agi_callington = '0'
                agi_callingtns = '0'
                agi_dnid = '1'
                agi_rdnis = 'unknown'
                agi_context = 'testing'
                agi_extension = '1'
                agi_priority = '1'
                agi_enhanced = '0.0'
                agi_accountcode = ''

        # Internal:
        reading_variables -- whether the instance is still in initialising by
            reading the setup variables from the connection
        messageCache -- stores incoming variables
        pendingMessages -- set of outstanding messages for which we expect
            replies
        lostConnectionDeferred -- deferred firing when the connection is lost
        delimiter -- uses bald newline instead of carriage-return-newline

    XXX Lots of problems with data-escaping, no docs on how to escape special
        characters that I can see...
    """

    log = Logger()

    delimiter = b'\n'

    def __init__(self, on_connect = None, log_commands_sent = False, log_lines_received = False):
        """Initialise the AGIProtocol, arguments are ignored"""

        self.on_connect = on_connect
        self.log_commands_sent = log_commands_sent
        self.log_lines_received = log_lines_received
        self.messageCache = []
        self.variables = {}
        self.pendingMessages = []
        self.reading_variables = False
        self.lostConnectionDeferred = None

    def connectionMade(self):
        """(Internal) Handle incoming connection (new AGI request)

        Initiates read of the initial attributes passed by the server
        """

        self.log.info('new connection')
        self.reading_variables = True

    def connectionLost(self, reason):
        """(Internal) Handle loss of the connection (remote hangup)"""

        self.log.info('connection lost')

        try:
            for df in self.pendingMessages:
                df.errback(ConnectionDone('FastAGI connection lost'))
        finally:
            if self.lostConnectionDeferred:
                self.lostConnectionDeferred.errback(reason)
            del self.pendingMessages[:]

    def onClose(self):
        """Return a deferred which will fire when the connection is lost"""
        if not self.lostConnectionDeferred:
            self.lostConnectionDeferred = Deferred()
        return self.lostConnectionDeferred

    agi_variable_re = re.compile('^(agi_.*): (.*)$')
    line_re = re.compile('^(\d{3})\s+(.*)$')

    def lineReceived(self, line):
        """(Internal) Handle Twisted's report of an incoming line from AMI"""
        if self.log_lines_received:
            self.log.debug('Line received: {line:}', line = repr(line))

        line = line.decode('utf-8')

        if self.reading_variables:
            if line == '':
                self.reading_variables = False
                if self.on_connect is not None:
                    self.on_connect(self)
            else:
                match = self.agi_variable_re.match(line)
                if not match:
                    self.log.error('Invalid variable line: {line:}', line = repr(line))
                else:
                    key = match.group(1)
                    value = match.group(2)
                    self.variables[key] = value
                    self.log.debug('"{key:}" = "{value:}"', key = key, value = value)
        else:
            match = self.line_re.match(line)
            if match is None:
                self.log.warn('Unexpected line: {line:}', line = line)
                return

            code = match.group(1)
            data = match.group(2)
            self.log.debug('code: {code:} data: {data:}', code = code, data = repr(data))
            try:
                df = self.pendingMessages.pop(0)
            except IndexError as err:
                self.log.warn('Line received without pending deferred: {line:}', line = repr(line))
            else:
                if code == '200':
                    df.callback(data)
                else:
                    df.errback(AGICommandFailure(code, data))

    def sendCommand(self, commandString):
        """(Internal) Send the given command to the other side"""
        commandString = commandString.encode('utf-8')
        if self.log_commands_sent:
            self.log.info('Sending command: {commandString:}', commandString = commandString)
        df = Deferred()
        self.pendingMessages.append(df)
        self.sendLine(commandString)
        return df

    result_re = re.compile('\Aresult=(.*?)(?: (.*))?\Z')

    def checkFailure(self, result, failure = -1):
        """(Internal) Check for a failure-code, raise error if == result"""
        self.log.debug('result: {result:}', result = result)
        match = self.result_re.match(result)
        if match:
            result_code = int(match.group(1))
            result_data = match.group(2)
            if result_code == failure:
                raise AGICommandFailure(FAILURE_CODE, result)

            return result

        else:
            raise AGICommandFailure(FAILURE_CODE, result)

    def resultAsInt(self, result):
        """(Internal) Convert result to an integer value"""
        self.log.debug('result: {result:}', result = result)

        try:
            match = self.result_re.match(result)
            if match:
                return int(match.group(1))

            raise AGICommandFailure(FAILURE_CODE, result)

        except ValueError as err:
            raise AGICommandFailure(FAILURE_CODE, result)

    def secondResultItem(self, result):
        """(Internal) Retrieve the second item on the result-line"""

        match = self.result_re.match(result)
        if match:
            result = int(match.group(1))
            data = match.group(2)
            return data

        raise AGICommandFailure(FAILURE_CODE, result)

    def resultPlusTimeoutFlag(self, line):
        """(Internal) Result followed by optional flag declaring timeout"""
        self.log.debug('XXX: {r:}', r = line)
        match = self.result_re.match(line)
        if match:
            result = match.group(1)
            data = match.group(2)
            if result == '-1':
                raise AGICommandFailure(FAILURE_CODE, line)
            return result, data == 'timeout'

        raise AGICommandFailure(FAILURE_CODE, line)

        #try:
        #    digits, timeout = resultLine.split(' ', 1)
        #    return digits.strip(), True

        #except ValueError as err:
        #    return resultLine.strip(), False

    def dateAsSeconds(self, date):
        """(Internal) Convert date to asterisk-compatible format"""

        if hasattr(date, 'timetuple'):
            # XXX values seem to be off here...
            date = time.mktime(date.timetuple())

        elif isinstance(date, time.struct_time):
            date = time.mktime(date)

        return date

    def onRecordingComplete(self, resultLine):
        """(Internal) Handle putative success

        Also watch for failure-on-load problems
        """

        try:
            digit, exitType, endposStuff = resultLine.split(' ', 2)

        except ValueError as err:
            pass

        else:
            digit = int(digit)
            exitType = exitType.strip('()')
            endposStuff = endposStuff.strip()
            if endposStuff.startswith('endpos='):
                endpos = int(endposStuff[7:].strip())
                return digit, exitType, endpos

        raise ValueError('Unexpected result on streaming completion: {}'.format(repr(resultLine)))

    endpos_re = re.compile('endpos=(\d+)')

    def onStreamingComplete(self, result, skipMS = 0):
        """(Internal) Handle putative success

        Also watch for failure-on-load problems
        """

        self.log.debug('result: {result:}', result = result)

        match = self.result_re.match(result)
        if not match:
            raise AGICommandFailure(FAILURE_CODE, "result line doesn't match")

        result_code = int(match.group(1))
        result_data = match.group(2)

        match = self.endpos_re.search(result_data)
        if not match:
            raise AGICommandFailure(FAILURE_CODE, "no endpos")

        endpos = int(match.group(1))
        if endpos == skipMS:
            # "likely" an error according to the wiki,
            # we'll raise an error...
            raise AGICommandFailure(FAILURE_CODE, "End position {} == original position, result code {}".format(endpos, result_code))

        return result_code, endpos

    def jumpOnError(self, reason, difference = 100, forErrors = None):
        """On error, jump to original priority+100

        This is intended to be registered as an errBack on a deferred for
        an end-user application.  It performs the Asterisk-standard-ish
        jump-on-failure operation, jumping to new priority of
        priority+difference.  It also forces return to the same context and
        extension, in case some other piece of code has changed those.

        difference -- priority jump to execute
        forErrors -- if specified, a tuple of error classes to which this
            particular jump is limited (i.e. only errors of this type will
            generate a jump & disconnect)

        returns deferred from the InSequence of operations required to reset
        the address...
        """

        if forErrors:
            if not isinstance(forErrors, (tuple, list)):
                forErrors = (forErrors,)
            reason.trap(*forErrors)

        sequence = InSequence()
        sequence.append(self.setContext, self.variables['agi_context'])
        sequence.append(self.setExtension, self.variables['agi_extension'])
        sequence.append(self.setPriority, int(self.variables['agi_priority']) + difference)
        sequence.append(self.finish)
        return sequence()

    # End-user API
    def finish(self):
        """Finish the AGI "script" (drop connection)

        This command simply drops the connection to the Asterisk server,
        which the FastAGI protocol interprets as a successful termination.

        Note: There *should* be a mechanism for sending a "result" code,
        but I haven't found any documentation for it.
        """

        self.transport.loseConnection()

    def answer(self):
        """Answer the channel (go off-hook)

        Returns deferred integer response code
        """

        d = self.sendCommand('ANSWER')
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def channelStatus(self, channel = None):
        """Retrieve the current channel's status

        Result integers (from the wiki):
            0 Channel is down and available
            1 Channel is down, but reserved
            2 Channel is off hook
            3 Digits (or equivalent) have been dialed
            4 Line is ringing
            5 Remote end is ringing
            6 Line is up
            7 Line is busy

        Returns deferred integer result code

        This could be used to decide if we can forward the channel to a given
        user, or whether we need to shunt them off somewhere else.
        """

        if channel is not None:
            command = 'CHANNEL STATUS "{}"'.format(channel)
        else:
            command = 'CHANNEL STATUS'
        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def onControlStreamFileComplete(self, resultLine):
        """(Internal) Handle CONTROL STREAM FILE results.

        Asterisk 12 introduces 'endpos=' to the result line.
        """
        parts = resultLine.split(' ', 1)
        result = int(parts[0])
        endpos = None # Default if endpos isn't specified
        if len(parts) == 2:
            endposStuff = parts[1].strip()
            if endposStuff.startswith('endpos='):
                endpos = int(endposStuff[7:])
            else:
                self.log.error("Unexpected response to 'control stream file': {result:}", result = resultLine)
        return result, endpos

    def controlStreamFile(self, filename, escapeDigits, skipMS = 0, ffChar = '*', rewChar = '#', pauseChar=None):
        """Playback specified file with ability to be controlled by user

        filename -- filename to play (on the asterisk server)
            (don't use file-type extension!)
        escapeDigits -- if provided,
        skipMS -- number of milliseconds to skip on FF/REW
        ffChar -- if provided, the set of chars that fast-forward
        rewChar -- if provided, the set of chars that rewind
        pauseChar -- if provided, the set of chars that pause playback

        returns deferred (digit,endpos) on success, or errors on failure,
            note that digit will be 0 if no digit was pressed AFAICS
        """

        command = 'CONTROL STREAM FILE "{}" "{}" {} "{}" "{}"'.format(filename, escapeDigits, skipMS, ffChar, rewChar)
        if pauseChar:
            command += ' "{}"'.format(pauseChar)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.onControlStreamFileComplete)
        return d

    def databaseDel(self, family, key):
        """Delete the given key from the database

        Returns deferred integer result code
        """
        command = 'DATABASE DEL "{}" "{}"'.format(family, key)
        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure, failure = '0')
        d = d.addCallback(self.resultAsInt)
        return d

    def databaseDeltree(self, family, keyTree=None):
        """Delete an entire family or a tree within a family from database

        Returns deferred integer result code
        """

        command = 'DATABASE DELTREE "{}"'.format(family)
        if keyTree:
            command += ' "{}"'.format(keytree)

        d = self.sendCommand(command)
        d = self.addCallback(self.checkFailure, failure = '0')
        d = self.addCallback(self.resultAsInt)
        return d

    def databaseGet(self, family, key):
        """Retrieve value of the given key from database

        Returns deferred string value for the key
        """
        command = 'DATABASE GET "{}" "{}"'.format(family, key)

        def returnValue(resultLine):
            # get the second item without the brackets...
            return resultLine[1:-1]

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure, failure='0')
        d = d.addCallback(self.secondResultItem)
        d = d.addCallback(returnValue)
        return d

    def databaseSet(self, family, key, value):
        """Set value of the given key to database

        a.k.a databasePut on the asterisk side

        Returns deferred integer result code
        """

        command = 'DATABASE PUT "{}" "{}" "{}"'.format(family, key, value)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure, failure = '0')
        d = d.addCallback(self.resultAsInt)
        return d

    databasePut = databaseSet

    def execute(self, application, *options, **kwargs):
        """Execute a dialplan application with given options

        Note: asterisk calls this "exec", which is Python keyword

        comma_delimiter -- Use new style comma delimiter for diaplan
        application arguments.  Asterisk uses pipes in 1.4 and older and
        prefers commas in 1.6 and up.  Pass comma_delimiter=True to avoid
        warnings from Asterisk 1.6 and up.

        Returns deferred string result for the application, which
        may have failed, result values are application dependant.
        """

        command = 'EXEC "{}"'.format(application)
        if options:
            if kwargs.pop('comma_delimiter', False) is True:
                delimiter = ","
            else:
                delimiter = "|"

            command += ' "{}"'.format(delimiter.join([str(x) for x in options]))

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure, failure = '-2')
        return d

    def getData(self, filename, timeout = 2.0, maxDigits = None):
        """Playback file, collecting up to maxDigits or waiting up to timeout

        filename -- filename without extension to play
        timeout -- timeout in seconds (Asterisk uses milliseconds)
        maxDigits -- maximum number of digits to collect

        returns deferred (str(digits), bool(timedOut))
        """
        timeout *= 1000
        command = 'GET DATA "{}" {}'.format(filename, timeout)

        if maxDigits is not None:
            command += ' {}'.format(maxDigits)

        d = self.sendCommand(command)
        #d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultPlusTimeoutFlag)
        return d

    def getOption(self, filename, escapeDigits, timeout=None):
        """Playback file, collect 1 digit or timeout (return 0)

        filename -- filename to play
        escapeDigits -- digits which cancel playback/recording
        timeout -- timeout in seconds (Asterisk uses milliseconds)

        returns (chr(option) or '' on timeout, endpos)
        """

        command = 'GET OPTION "{}" "{}"'.format(filename, escapeDigits)

        if timeout is not None:
            timeout *= 1000
            command += ' {}'.format(timeout)

        def charFirst(values):
            (c, position) = values
            if not c:  # returns 0 on timeout
                c = ''
            else:
                c = chr(c)
            return c, position

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.onStreamingComplete)
        d = d.addCallback(charFirst)
        return d

    def getVariable(self, variable):
        """Retrieve the given channel variable

        From the wiki, variables of interest:

            ACCOUNTCODE -- Account code, if specified
            ANSWEREDTIME -- Time call was answered
            BLINDTRANSFER -- Active SIP channel that dialed the number.
                This will return the SIP Channel that dialed the number when
                doing blind transfers
            CALLERID -- Current Caller ID (name and number) # deprecated?
            CALLINGPRES -- PRI Call ID Presentation variable for incoming calls
            CHANNEL -- Current channel name
            CONTEXT -- Current context name
            DATETIME -- Current datetime in format: DDMMYYYY-HH:MM:SS
            DIALEDPEERNAME -- Name of called party (Broken)
            DIALEDPEERNUMBER -- Number of the called party (Broken)
            DIALEDTIME -- Time number was dialed
            DIALSTATUS -- Status of the call
            DNID -- Dialed Number Identifier (limited apparently)
            EPOCH -- UNIX-style epoch-based time (seconds since 1 Jan 1970)
            EXTEN -- Current extension
            HANGUPCAUSE -- Last hangup return code on a Zap channel connected
                to a PRI interface
            INVALID_EXTEN -- Extension asked for when redirected to the i
                (invalid) extension
            LANGUAGE -- The current language setting. See Asterisk
                multi-language
            MEETMESECS -- Number of seconds user participated in a MeetMe
                conference
            PRIORITY -- Current priority
            RDNIS -- The current redirecting DNIS, Caller ID that redirected
                the call. Limitations apply.
            SIPDOMAIN -- SIP destination domain of an inbound call
                (if appropriate)
            SIP_CODEC -- Used to set the SIP codec for a call (apparently
                broken in Ver 1.0.1, ok in Ver. 1.0.3 & 1.0.4, not sure about
                1.0.2)
            SIPCALLID -- SIP dialog Call-ID: header
            SIPUSERAGENT -- SIP user agent header (remote agent)
            TIMESTAMP -- Current datetime in the format: YYYYMMDD-HHMMSS
            TXTCIDNAME -- Result of application TXTCIDName
            UNIQUEID -- Current call unique identifier
            TOUCH_MONITOR -- Used for "one touch record" (see features.conf,
                and wW dial flags). If is set on either side of the call then
                that var contains the app_args for app_monitor otherwise the
                default of WAV||m is used

        Returns deferred string value for the key
        """

        def returnValue(resultLine):
            # get the second item without the brackets...
            return resultLine[1:-1]

        command = 'GET VARIABLE "{}"'.format(variable)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure, failure = '0')
        d = d.addCallback(self.secondResultItem)
        d = d.addCallback(returnValue)
        return d

    def hangup(self, channel = None):
        """Cause the server to hang up on the channel

        Returns deferred integer response code
        """

        command = 'HANGUP'
        if channel is not None:
            command += ' "{}"'.format(channel)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def noop(self, message = None):
        """Send a null operation to the server.  Any message sent
        will be printed to the CLI.

        Returns deferred integer response code
        """

        command = 'NOOP'
        if message is not None:
            command += ' "{}"'.format(message)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def playback(self, filename, doAnswer = 1):
        """Playback specified file in foreground

        filename -- filename to play
        doAnswer -- whether to:
                -1: skip playback if the channel is not answered
                 0: playback the sound file without answering first
                 1: answer the channel before playback, if not yet answered

        Note: this just wraps the execute method to issue
        a PLAYBACK command.

        Returns deferred integer response code
        """

        try:
            option = {-1: 'skip', 0: 'noanswer', 1: 'answer'}[doAnswer]
        except KeyError:
            raise TypeError('doAnswer accepts values -1, 0, 1 only ({} given)'.format(doAnswer))

        command = 'PLAYBACK "{}"'.format(filename)
        if option:
            command += ' "{}"'.format(option)

        d = self.execute(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def receiveChar(self, timeout = None):
        """Receive a single text char on text-supporting channels (rare)

        timeout -- timeout in seconds (Asterisk uses milliseconds)

        returns deferred (char, bool(timeout))
        """

        command = 'RECEIVE CHAR'
        if timeout is not None:
            timeout *= 1000
            command += ' {}'.format(timeout)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultPlusTimeoutFlag)
        return d

    def receiveText(self, timeout = None):
        """Receive text until timeout

        timeout -- timeout in seconds (Asterisk uses milliseconds)

        Returns deferred string response value (unaltered)
        """
        command = 'RECEIVE TEXT'
        if timeout is not None:
            timeout *= 1000
            command += ' {}'.format(timeout)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        return d

    def recordFile(self, filename, format, escapeDigits, timeout = -1,
                   offsetSamples = None, beep = True, silence = None):
        """Record channel to given filename until escapeDigits or silence

        filename -- filename on the server to which to save
        format -- encoding format in which to save data
        escapeDigits -- digits which end recording
        timeout -- maximum time to record in seconds, -1 gives infinite
            (Asterisk uses milliseconds)
        offsetSamples - move into file this number of samples before recording?
            XXX check semantics here.
        beep -- if true, play a Beep on channel to indicate start of recording
        silence -- if specified, silence duration to trigger end of recording

        returns deferred (str(code/digits), typeOfExit, endpos)

        Where known typeOfExits include:
            hangup, code='0'
            dtmf, code=digits-pressed
            timeout, code='0'
        """

        timeout *= 1000
        command = 'RECORD FILE "{}" "{}" {} {}'.format(filename, format, escapeDigits, timeout)

        if offsetSamples is not None:
            command += ' {}'.format(offsetSamples)

        if beep:
            command += ' BEEP'

        if silence is not None:
            command += ' s={}'.format(silence)

        def onResult(resultLine):
            value, type, endpos = resultLine.split(' ')
            type = type.strip()[1:-1]
            endpos = int(endpos.split('=')[1])
            return (value, type, endpos)

        d = self.sendCommand(command)
        d = d.addCallback(self.onRecordingComplete)
        return d

    def sayXXX(self, baseCommand, value, escapeDigits = ''):
        """Underlying implementation for the common-api sayXXX functions"""

        command = '{} {} "{}"' % (baseCommand, value, escapeDigits or '')

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def sayAlpha(self, string, escapeDigits = None):
        """Spell out character string to the user until escapeDigits

        returns deferred 0 or the digit pressed
        """

        string = ''.join([x for x in string if x.isalnum()])
        return self.sayXXX('SAY ALPHA', string, escapeDigits)

    def sayDate(self, date, escapeDigits = None):
        """Spell out the date (with somewhat unnatural form)

        See sayDateTime with format 'ABdY' for a more natural reading

        returns deferred 0 or digit-pressed as integer
        """

        return self.sayXXX('SAY DATE', self.dateAsSeconds(date), escapeDigits)

    def sayDigits(self, number, escapeDigits = None):
        """Spell out the number/string as a string of digits

        returns deferred 0 or digit-pressed as integer
        """

        number = ''.join([x for x in str(number) if x.isdigit()])
        return self.sayXXX('SAY DIGITS', number, escapeDigits)

    def sayNumber(self, number, escapeDigits = None):
        """Say a number in natural form

         returns deferred 0 or digit-pressed as integer
        """

        number = ''.join([x for x in str(number) if x.isdigit()])
        return self.sayXXX('SAY NUMBER', number, escapeDigits)

    def sayPhonetic(self, string, escapeDigits = None):
        """Say string using phonetics

         returns deferred 0 or digit-pressed as integer
        """

        string = ''.join([x for x in string if x.isalnum()])
        return self.sayXXX('SAY PHONETIC', string, escapeDigits)

    def sayTime(self, time, escapeDigits = None):
        """Say string using phonetics

         returns deferred 0 or digit-pressed as integer
        """

        return self.sayXXX('SAY TIME', self.dateAsSeconds(time), escapeDigits)

    def sayDateTime(self, time, escapeDigits = '', format = None, timezone = None):
        """Say given date/time in given format until escapeDigits

        time -- datetime or float-seconds-since-epoch
        escapeDigits -- digits to cancel playback
        format -- strftime-style format for the date to be read
            'filename' -- filename of a soundfile (single ticks around the
                          filename required)
            A or a -- Day of week (Saturday, Sunday, ...)
            B or b or h -- Month name (January, February, ...)
            d or e -- numeric day of month (first, second, ..., thirty-first)
            Y -- Year
            I or l -- Hour, 12 hour clock
            H -- Hour, 24 hour clock (single digit hours preceded by "oh")
            k -- Hour, 24 hour clock (single digit hours NOT preceded by "oh")
            M -- Minute
            P or p -- AM or PM
            Q -- "today", "yesterday" or ABdY
                 (*note: not standard strftime value)
            q -- "" (for today), "yesterday", weekday, or ABdY
                 (*note: not standard strftime value)
            R -- 24 hour time, including minute

            Default format is "ABdY 'digits/at' IMp"
        timezone -- optional timezone name from /usr/share/zoneinfo

        returns deferred 0 or digit-pressed as integer
        """

        command = 'SAY DATETIME {} "{}"'.format(self.dateAsSeconds(time), escapeDigits)
        if format is not None:
            command += ' {}'.format(format)
            if timezone is not None:
                command += ' {}'.format(timezone)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def sendImage(self, filename):
        """Send image on those channels which support sending images (rare)

        returns deferred integer result code
        """

        command = 'SEND IMAGE "{}"'.format(filename)
        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def sendText(self, text):
        """Send text on text-supporting channels (rare)

        returns deferred integer result code
        """

        command = 'SEND TEXT {}'.format(repr(text))
        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def setAutoHangup(self, time):
        """Set channel to automatically hang up after time seconds

        time -- time in seconds in the future to hang up...

        returns deferred integer result code
        """

        command = 'SET AUTOHANGUP {}'.format(time)

        d = self.sendCommand(command)
        # docs don't show a failure case, actually
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.resultAsInt)
        return d

    def setCallerID(self, number):
        """Set channel's caller ID to given number

        returns deferred integer result code
        """
        command = 'SET CALLERID {}'.format(number)

        d = self.sendCommand(command)
        d = d.addCallback(self.resultAsInt)
        return d

    def setContext(self, context):
        """Move channel to given context (no error checking is performed)

        returns deferred integer result code
        """

        command = 'SET CONTEXT {}'.format(context)
        d = self.sendCommand(command)
        d = d.addCallback(self.resultAsInt)
        return d

    def setExtension(self, extension):
        """Move channel to given extension (or 'i' if invalid)

        The call will drop if neither the extension or 'i' are there.

        returns deferred integer result code
        """

        command = 'SET EXTENSION {}'.format(extension)

        d = self.sendCommand(command)
        d = d.addCallback(self.resultAsInt)
        return d

    def setMusic(self, on = True, musicClass = None):
        """Enable/disable and/or choose music class for channel's music-on-hold

        returns deferred integer result code
        """

        command = 'SET MUSIC {}'.format(['OFF', 'ON'][on])
        if musicClass is not None:
            command += ' {}'.format(musicClass)

        d = self.sendCommand(command)
        d = d.addCallback(self.resultAsInt)
        return d

    def setPriority(self, priority):
        """Move channel to given priority or drop if not there

        returns deferred integer result code
        """

        command = 'SET PRIORITY {}'.format(priority)

        d = self.sendCommand(command)
        d = d.addCallback(self.resultAsInt)
        return d

    def setVariable(self, variable, value):
        """Set given channel variable to given value

        variable -- the variable name passed to the server
        value -- the variable value passed to the server, will have
            any '"' characters removed in order to allow for " quoting
            of the value.

        returns deferred integer result code
        """

        value = '"{}"'.format(str(value).replace('"', ''))
        command = 'SET VARIABLE "{}" {}'.format(variable, value)

        d = self.sendCommand(command)
        d = d.addCallback(self.resultAsInt)
        return d

    def streamFile(self, filename, escapeDigits = '', offset = 0):
        """Stream given file until escapeDigits starting from offset

        returns deferred (str(digit), int(endpos)) for playback
        """

        command = 'STREAM FILE "{}" "{}"'.format(filename, escapeDigits)
        if offset is not None:
            command += ' {}'.format(offset)

        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        d = d.addCallback(self.onStreamingComplete, skipMS = offset)
        return d

    def tddMode(self, on = True):
        """Set TDD mode on the channel if possible (ZAP only ATM)

        on -- ON (True), OFF (False) or MATE (None)

        returns deferred integer result code
        """
        if on is True:
            on = 'ON'

        elif on is False:
            on = 'OFF'

        elif on is None:
            on = 'MATE'

        command = 'TDD MODE {}'.format(on)
        d = self.sendCommand(command)
        d = d.addCallback(self.checkFailure)
        # planned eventual failure case (not capable)
        d = d.addCallback(self.checkFailure, failure = '0')
        d = d.addCallback(self.resultAsInt)
        return d

    def verbose(self, message, level = None):
        """Send a logging message to the asterisk console for debugging etc

        message -- text to pass
        level -- 1-4 denoting verbosity level

        returns deferred integer result code
        """

        command = 'VERBOSE "{}"'.format(message)
        if level is not None:
            command += ' {}'.format(level)

        d = self.sendCommand(command)
        d = d.addCallback(self.resultAsInt)
        return d

    def waitForDigit(self, timeout):
        """Wait up to timeout seconds for single digit to be pressed

        timeout -- timeout in seconds or -1 for infinite timeout
            (Asterisk uses milliseconds)

        returns deferred 0 on timeout or digit
        """
        timeout *= 1000
        command = 'WAIT FOR DIGIT {}'.format(timeout)
        d = self.sendCommand(command)
        d = d.addCallback(self.checkWaitForDigit)
        return d

    def checkWaitForDigit(self, line):
        self.log.debug('XXX: {r:}', r = line)
        match = self.result_re.match(line)
        if match:
            result = match.group(1)
            data = match.group(2)
            if result == '-1':
                raise AGICommandFailure(FAILURE_CODE, line)
            if result == '0':
                return None, True
            return chr(int(result)), False

        raise AGICommandFailure(FAILURE_CODE, line)

class InSequence(object):
    """Single-shot item creating a set of actions to run in sequence"""

    log = Logger()

    def __init__(self):
        self.actions = []
        self.results = []
        self.finalDF = None

    def append(self, function, *args, **kwargs):
        """Append an action to the set of actions to process"""
        self.actions.append((function, args, kwargs))

    def __call__(self):
        """Return deferred that fires when finished processing all items"""
        return self._doSequence()

    def _doSequence(self):
        """Return a deferred that does each action in sequence"""
        finalDF = Deferred()
        self.onActionSuccess(None, finalDF = finalDF)
        return finalDF

    def recordResult(self, result):
        """Record the result for later"""
        self.results.append(result)
        return result

    def onActionSuccess(self, result, finalDF):
        """Handle individual-action success"""

        self.log.debug('onActionSuccess: {result:}', result = result)
        if self.actions:
            function, args, kwargs = self.actions.pop(0)
            self.log.debug('action {function:}, {args:}, {kwargs:}', function = function, args = args, kwargs = kwargs)
            df = maybeDeferred(function, *args, **kwargs)
            df.addCallback(self.recordResult)
            df.addCallback(self.onActionSuccess, finalDF = finalDF)
            df.addErrback(self.onActionFailure, finalDF = finalDF)
            return df

        else:
            finalDF.callback(self.results)

    def onActionFailure(self, reason, finalDF):
        """Handle individual-action failure"""

        self.log.failure('onActionFailure', failure = reason)
        reason.results = self.results
        finalDF.errback(reason)

class FastAGIFactory(Factory):
    """Factory generating FastAGI server instances
    """
    log = Logger()

    def __init__(self, on_connect, log_commands_sent = False, log_lines_received = False):
        """Initialise the factory

        mainFunction -- function taking a connected FastAGIProtocol instance
            this is the function that's run when the Asterisk server connects.
        """
        self.on_connect = on_connect
        self.log_commands_sent = log_commands_sent
        self.log_lines_received = log_lines_received

    def buildProtocol(self, addr):
        self.log.debug('building FastAGI protocol: {addr:}', addr = addr)
        return FastAGIProtocol(on_connect = self.on_connect,
                               log_commands_sent = self.log_commands_sent,
                               log_lines_received = self.log_lines_received)
