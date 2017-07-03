import time
import threading
import base64

from pylinkirc import utils, conf
from pylinkirc.log import log
from pylinkirc.protocols.ircs2s_common import *
from pylinkirc.classes import *

FALLBACK_REALNAME = 'PyLink Relay Mirror Client'
IRCV3_CAPABILITIES = {'multi-prefix', 'sasl'}

class ClientbotWrapperProtocol(IRCCommonProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.protocol_caps = {'clear-channels-on-leave', 'slash-in-nicks', 'slash-in-hosts', 'underscore-in-hosts'}

        self.has_eob = False

        # Remove conf key checks for those not needed for Clientbot.
        self.conf_keys -= {'recvpass', 'sendpass', 'sid', 'sidrange', 'hostname'}

        # This is just a fallback. Actual casemapping is fetched by handle_005()
        self.casemapping = 'ascii'

        self._caps = {}
        self.ircv3_caps = set()
        self.ircv3_caps_available = {}

        # Tracks the users sent in a list of /who replies, so that users can be bursted all at once
        # when ENDOFWHO is received.
        self.who_received = set()

        # This stores channel->Timer object mappings for users that we're waiting for a kick
        # acknowledgement for. The timer is set to send a NAMES request to the uplink to prevent
        # things like failed KICK attempts from desyncing plugins like relay.
        self.kick_queue = {}

        # Aliases: 463 (ERR_NOPERMFORHOST), 464 (ERR_PASSWDMISMATCH), and 465 (ERR_YOUREBANNEDCREEP)
        # are essentially all fatal errors for connections.
        self.handle_463 = self.handle_464 = self.handle_465 = self.handle_error

        self._use_builtin_005_handling = True

    def post_connect(self):
        """Initializes a connection to a server."""
        # (Re)initialize counter-based pseudo UID generators
        super().post_connect()
        self.uidgen = utils.PUIDGenerator('PUID')
        self.sidgen = utils.PUIDGenerator('ClientbotInternalSID')

        self.has_eob = False
        ts = self.start_ts
        f = lambda text: self.send(text, queue=False)

        # Enumerate our own server
        self.sid = self.sidgen.next_sid()

        # Clear states from last connect
        self.who_received.clear()
        self.kick_queue.clear()
        self._caps.clear()
        self.ircv3_caps.clear()
        self.ircv3_caps_available.clear()

        sendpass = self.serverdata.get("sendpass")
        if sendpass:
            f('PASS %s' % sendpass)

        f('CAP LS 302')

        # Start a timer to call CAP END if registration freezes (e.g. if AUTHENTICATE for SASL is
        # never replied to).
        def _do_cap_end_wrapper():
            log.info('(%s) Skipping SASL due to timeout; are the IRCd and services configured '
                     'properly?', self.name)
            self._do_cap_end()
        self._cap_timer = threading.Timer(self.serverdata.get('sasl_timeout') or 15, _do_cap_end_wrapper)
        self._cap_timer.start()

        # This is a really gross hack to get the defined NICK/IDENT/HOST/GECOS.
        # But this connection stuff is done before any of the spawn_client stuff in
        # services_support fires.
        self.conf_nick = self.serverdata.get('pylink_nick') or conf.conf["bot"].get("nick", "PyLink")
        f('NICK %s' % (self.conf_nick))
        ident = self.serverdata.get('pylink_ident') or conf.conf["bot"].get("ident", "pylink")
        f('USER %s 8 * :%s' % (ident, # TODO: per net realnames or hostnames aren't implemented yet.
                              conf.conf["bot"].get("realname", "PyLink Clientbot")))

    # Note: clientbot clients are initialized with umode +i by default
    def spawn_client(self, nick, ident='unknown', host='unknown.host', realhost=None, modes={('i', None)},
            server=None, ip='0.0.0.0', realname='', ts=None, opertype=None,
            manipulatable=False):
        """
        STUB: Pretends to spawn a new client with a subset of the given options.
        """

        server = server or self.sid
        uid = self.uidgen.next_uid(prefix=nick)

        ts = ts or int(time.time())

        log.debug('(%s) spawn_client stub called, saving nick %s as PUID %s', self.name, nick, uid)
        u = self.users[uid] = User(nick, ts, uid, server, ident=ident, host=host, realname=realname,
                                          manipulatable=manipulatable, realhost=realhost, ip=ip)
        self.servers[server].users.add(uid)

        self.apply_modes(uid, modes)

        return u

    def spawn_server(self, name, sid=None, uplink=None, desc=None, endburst_delay=0, internal=True):
        """
        STUB: Pretends to spawn a new server with a subset of the given options.
        """
        name = name.lower()
        if internal:
            # Use a custom pseudo-SID format for internal servers to prevent any server name clashes
            sid = self.sidgen.next_sid(prefix=name)
        else:
            # For others servers, just use the server name as the SID.
            sid = name

        self.servers[sid] = Server(uplink, name, internal=internal)
        return sid

    def away(self, source, text):
        """STUB: sets away messages for clients internally."""
        log.debug('(%s) away: target is %s, internal client? %s', self.name, source, self.is_internal_client(source))

        if self.users[source].away != text:
            if not self.is_internal_client(source):
                log.debug('(%s) away: sending AWAY hook from %s with text %r', self.name, source, text)
                self.call_hooks([source, 'AWAY', {'text': text}])

            self.users[source].away = text

    def invite(self, client, target, channel):
        """Invites a user to a channel."""
        self.send('INVITE %s %s' % (self.get_friendly_name(target), channel))

    def join(self, client, channel):
        """STUB: Joins a user to a channel."""
        channel = self.to_lower(channel)

        # Only joins for the main PyLink client are actually forwarded. Others are ignored.
        # Note: we do not automatically add our main client to the channel state, as we
        # rely on the /NAMES reply to sync it up properly.
        if self.pseudoclient and client == self.pseudoclient.uid:
            self.send('JOIN %s' % channel)
            # Send /names and /who requests right after
            self.send('MODE %s' % channel)
            self.send('NAMES %s' % channel)
            self.send('WHO %s' % channel)
        else:
            self.channels[channel].users.add(client)
            self.users[client].channels.add(channel)

            log.debug('(%s) join: faking JOIN of client %s/%s to %s', self.name, client,
                      self.get_friendly_name(client), channel)
            self.call_hooks([client, 'CLIENTBOT_JOIN', {'channel': channel}])

    def kick(self, source, channel, target, reason=''):
        """Sends channel kicks."""

        log.debug('(%s) kick: checking if target %s (nick: %s) is an internal client? %s',
                  self.name, target, self.get_friendly_name(target),
                  self.is_internal_client(target))
        if self.is_internal_client(target):
            # Target was one of our virtual clients. Just remove them from the state.
            self.handle_part(target, 'KICK', [channel, reason])

            # Send a KICK hook for message formatting.
            self.call_hooks([source, 'CLIENTBOT_KICK', {'channel': channel, 'target': target, 'text': reason}])
            return

        self.send('KICK %s %s :<%s> %s' % (channel, self._expandPUID(target),
                      self.get_friendly_name(source), reason))

        # Don't update our state here: wait for the IRCd to send an acknowledgement instead.
        # There is essentially a 3 second wait to do this, as we send NAMES with a delay
        # to resync any users lost due to kicks being blocked, etc.
        if (channel not in self.kick_queue) or (not self.kick_queue[channel][1].is_alive()):
            # However, only do this if there isn't a NAMES request scheduled already.
            t = threading.Timer(3, lambda: self.send('NAMES %s' % channel))
            log.debug('(%s) kick: setting NAMES timer for %s on %s', self.name, target, channel)

            # Store the channel, target UID, and timer object in the internal kick queue.
            self.kick_queue[channel] = ({target}, t)
            t.start()
        else:
            log.debug('(%s) kick: adding %s to kick queue for channel %s', self.name, target, channel)
            self.kick_queue[channel][0].add(target)

    def message(self, source, target, text, notice=False):
        """Sends messages to the target."""
        command = 'NOTICE' if notice else 'PRIVMSG'

        if self.pseudoclient and self.pseudoclient.uid == source:
            self.send('%s %s :%s' % (command, self._expandPUID(target), text))
        else:
            self.call_hooks([source, 'CLIENTBOT_MESSAGE', {'target': target, 'is_notice': notice, 'text': text}])

    def mode(self, source, channel, modes, ts=None):
        """Sends channel MODE changes."""
        if utils.isChannel(channel):
            extmodes = []
            # Re-parse all channel modes locally to eliminate anything invalid, such as unbanning
            # things that were never banned. This prevents the bot from getting caught in a loop
            # with IRCd MODE acknowledgements.
            # FIXME: More related safety checks should be added for this.
            log.debug('(%s) mode: re-parsing modes %s', self.name, modes)
            joined_modes = self.join_modes(modes)
            for modepair in self.parse_modes(channel, joined_modes):
                log.debug('(%s) mode: checking if %s a prefix mode: %s', self.name, modepair, self.prefixmodes)
                if modepair[0][-1] in self.prefixmodes:
                    if self.is_internal_client(modepair[1]):
                        # Ignore prefix modes for virtual internal clients.
                        log.debug('(%s) mode: skipping virtual client prefixmode change %s', self.name, modepair)
                        continue
                    else:
                        # For other clients, change the mode argument to nick instead of PUID.
                        nick = self.get_friendly_name(modepair[1])
                        log.debug('(%s) mode: coersing mode %s argument to %s', self.name, modepair, nick)
                        modepair = (modepair[0], nick)
                extmodes.append(modepair)

            log.debug('(%s) mode: filtered modes for %s: %s', self.name, channel, extmodes)
            if extmodes:
                self.send('MODE %s %s' % (channel, self.join_modes(extmodes)))
                # Don't update the state here: the IRCd sill respond with a MODE reply if successful.

    def nick(self, source, newnick):
        """STUB: Sends NICK changes."""
        if self.pseudoclient and source == self.pseudoclient.uid:
            self.send('NICK :%s' % newnick)
            # No state update here: the IRCd will respond with a NICK acknowledgement if the change succeeds.
        else:
            self.call_hooks([source, 'CLIENTBOT_NICK', {'newnick': newnick}])
            self.users[source].nick = newnick

    def notice(self, source, target, text):
        """Sends notices to the target."""
        # Wrap around message(), which does all the text formatting for us.
        self.message(source, target, text, notice=True)

    def ping(self, source=None, target=None):
        """
        Sends PING to the uplink.
        """
        if self.uplink:
            self.send('PING %s' % self.get_friendly_name(self.uplink))

            # Poll WHO periodically to figure out any ident/host/away status changes.
            for channel in self.pseudoclient.channels:
                self.send('WHO %s' % channel)

    def part(self, source, channel, reason=''):
        """STUB: Parts a user from a channel."""
        self.channels[channel].remove_user(source)
        self.users[source].channels.discard(channel)

        # Only parts for the main PyLink client are actually forwarded. Others are ignored.
        if self.pseudoclient and source == self.pseudoclient.uid:
            self.send('PART %s :%s' % (channel, reason))
        else:
            self.call_hooks([source, 'CLIENTBOT_PART', {'channel': channel, 'text': reason}])

    def quit(self, source, reason):
        """STUB: Quits a client."""
        userdata = self.users[source]
        self._remove_client(source)
        self.call_hooks([source, 'CLIENTBOT_QUIT', {'text': reason, 'userdata': userdata}])

    def sjoin(self, server, channel, users, ts=None, modes=set()):
        """STUB: bursts joins from a server."""
        # This stub only updates the state internally with the users
        # given. modes and TS are currently ignored.
        puids = {u[-1] for u in users}
        for user in puids:
            if self.pseudoclient and self.pseudoclient.uid == user:
                # If the SJOIN affects our main client, forward it as a regular JOIN.
                self.join(user, channel)
            else:
                # Otherwise, track the state for our virtual clients.
                self.users[user].channels.add(channel)

        self.channels[channel].users |= puids
        nicks = {self.get_friendly_name(u) for u in puids}
        self.call_hooks([server, 'CLIENTBOT_SJOIN', {'channel': channel, 'nicks': nicks}])

    def squit(self, source, target, text):
        """STUB: SQUITs a server."""
        # What this actually does is just handle the SQUIT internally: i.e.
        # Removing pseudoclients and pseudoservers.
        squit_data = self._squit(source, 'CLIENTBOT_VIRTUAL_SQUIT', [target, text])

        if squit_data.get('nicks'):
            self.call_hooks([source, 'CLIENTBOT_SQUIT', squit_data])

    def _stub(self, *args):
        """Stub outgoing command function (does nothing)."""
        return
    kill = topic = topic_burst = knock = numeric = _stub

    def update_client(self, target, field, text):
        """Updates the known ident, host, or realname of a client."""
        if target not in self.users:
            log.warning("(%s) Unknown target %s for update_client()", self.name, target)
            return

        u = self.users[target]

        if field == 'IDENT' and u.ident != text:
            u.ident = text
            if not self.is_internal_client(target):
                # We're updating the host of an external client in our state, so send the appropriate
                # hook payloads.
                self.call_hooks([self.sid, 'CHGIDENT',
                                   {'target': target, 'newident': text}])
        elif field == 'HOST' and u.host != text:
            u.host = text
            if not self.is_internal_client(target):
                self.call_hooks([self.sid, 'CHGHOST',
                                   {'target': target, 'newhost': text}])
        elif field in ('REALNAME', 'GECOS') and u.realname != text:
            u.realname = text
            if not self.is_internal_client(target):
                self.call_hooks([self.sid, 'CHGNAME',
                                   {'target': target, 'newgecos': text}])
        else:
            return  # Nothing changed

    def _get_UID(self, nick, ident=None, host=None):
        """
        Fetches the UID for the given nick, creating one if it does not already exist.

        Limited (internal) nick collision checking is done here to prevent Clientbot users from
        being confused with virtual clients, and vice versa."""
        self._check_puid_collision(nick)
        idsource = self.nick_to_uid(nick)
        is_internal = self.is_internal_client(idsource)

        # If this sender isn't known or it is one of our virtual clients, spawn a new one.
        # This also takes care of any nick collisions caused by new, Clientbot users
        # taking the same nick as one of our virtual clients, and will force the virtual client to lose.
        if (not idsource) or (is_internal and self.pseudoclient and idsource != self.pseudoclient.uid):
            if idsource:
                log.debug('(%s) Nick-colliding virtual client %s/%s', self.name, idsource, nick)
                self.call_hooks([self.sid, 'CLIENTBOT_NICKCOLLIDE', {'target': idsource, 'parse_as': 'SAVE'}])

            idsource = self.spawn_client(nick, ident or 'unknown', host or 'unknown',
                                        server=self.uplink, realname=FALLBACK_REALNAME).uid

        return idsource

    def parse_message_tags(self, data):
        """
        Parses a message with IRC v3.2 message tags, as described at http://ircv3.net/specs/core/message-tags-3.2.html
        """
        # Example query:
        # @aaa=bbb;ccc;example.com/ddd=eee :nick!ident@host.com PRIVMSG me :Hello
        if data[0].startswith('@'):
            tagdata = data[0].lstrip('@').split(';')
            for idx, tag in enumerate(tagdata):
                tag = tag.replace(r'\s', ' ')
                tag = tag.replace(r'\\', '\\')
                tag = tag.replace(r'\r', '\r')
                tag = tag.replace(r'\n', '\n')
                tag = tag.replace(r'\:', ';')
                tagdata[idx] = tag

            results = self.parse_isupport(tagdata, fallback=None)
            log.debug('(%s) parsed message tags %s', self.name, results)
            return results
        return {}

    def handle_events(self, data):
        """Event handler for the RFC1459/2812 (clientbot) protocol."""
        data = data.split(" ")

        tags = self.parse_message_tags(data)
        if tags:
            # If we have tags, split off the first argument.
            data = data[1:]

        try:
            args = self.parse_prefixed_args(data)
            sender = args[0]
            command = args[1]
            args = args[2:]

        except IndexError:
            # Raw command without an explicit sender; assume it's being sent by our uplink.
            args = self.parse_args(data)
            idsource = sender = self.uplink
            command = args[0]
            args = args[1:]
        else:
            # PyLink as a services framework expects UIDs and SIDs for everything. Since we connect
            # as a bot here, there's no explicit user introduction, so we're going to generate
            # pseudo-uids and pseudo-sids as we see prefixes.
            if ('!' not in sender) and '.' in sender:
                # Sender is a server name. XXX: make this check more foolproof
                assert '@' not in sender, "Incoming server name %r clashes with a PUID!" % sender
                if sender not in self.servers:
                    self.spawn_server(sender, internal=False)
                idsource = sender
            else:
                # Sender is a either a nick or a nick!user@host prefix. Split it into its relevant parts.
                try:
                    nick, ident, host = utils.splitHostmask(sender)
                except ValueError:
                    ident = host = None  # Set ident and host as null for now.
                    nick = sender  # Treat the sender prefix we received as a nick.
                idsource = self._get_UID(nick, ident, host)

        try:
            func = getattr(self, 'handle_'+command.lower())
        except AttributeError:  # unhandled command
            pass
        else:
            parsed_args = func(idsource, command, args)
            if parsed_args is not None:
                parsed_args['tags'] = tags  # Add message tags to this dict.
                return [idsource, command, parsed_args]

    def _do_cap_end(self):
        """
        Abort SASL login by sending CAP END.
        """
        self.send('CAP END')
        log.debug("(%s) Stopping CAP END timer.", self.name)
        self._cap_timer.cancel()

    def _try_sasl_auth(self):
        """
        Starts an authentication attempt via SASL. This returns True if SASL
        is enabled and correctly configured, and False otherwise.
        """
        if 'sasl' not in self.ircv3_caps:
            log.info("(%s) Skipping SASL auth since the IRCd doesn't support it.", self.name)
            return

        sasl_mech = self.serverdata.get('sasl_mechanism')
        if sasl_mech:
            sasl_mech = sasl_mech.upper()
            sasl_user = self.serverdata.get('sasl_username')
            sasl_pass = self.serverdata.get('sasl_password')
            ssl_cert = self.serverdata.get('ssl_certfile')
            ssl_key = self.serverdata.get('ssl_keyfile')
            ssl = self.serverdata.get('ssl')

            if sasl_mech == 'PLAIN':
                if not (sasl_user and sasl_pass):
                    log.warning("(%s) Not attempting PLAIN authentication; sasl_username and/or "
                                "sasl_password aren't correctly set.", self.name)
                    return False
            elif sasl_mech == 'EXTERNAL':
                if not ssl:
                    log.warning("(%s) Not attempting EXTERNAL authentication; SASL external requires "
                                "SSL, but it isn't enabled.", self.name)
                    return False
                elif not (ssl_cert and ssl_key):
                    log.warning("(%s) Not attempting EXTERNAL authentication; ssl_certfile and/or "
                                "ssl_keyfile aren't correctly set.", self.name)
                    return False
            else:
                log.warning('(%s) Unsupported SASL mechanism %s; aborting SASL.', self.name, sasl_mech)
                return False
            self.send('AUTHENTICATE %s' % sasl_mech, queue=False)
            return True
        return False

    def _send_auth_chunk(self, data):
        """Send Base64 encoded SASL authentication chunks."""
        enc_data = base64.b64encode(data).decode()
        self.send('AUTHENTICATE %s' % enc_data, queue=False)

    def handle_authenticate(self, source, command, args):
        """
        Handles AUTHENTICATE, or SASL authentication requests from the server.
        """
        # Client: AUTHENTICATE PLAIN
        # Server: AUTHENTICATE +
        # Client: AUTHENTICATE ...
        if not args:
            return
        if args[0] == '+':
            sasl_mech = self.serverdata['sasl_mechanism'].upper()
            if sasl_mech == 'PLAIN':
                sasl_user = self.serverdata['sasl_username']
                sasl_pass = self.serverdata['sasl_password']
                authstring = '%s\0%s\0%s' % (sasl_user, sasl_user, sasl_pass)
                self._send_auth_chunk(authstring.encode('utf-8'))
            elif sasl_mech == 'EXTERNAL':
                self.send('AUTHENTICATE +')

    def handle_904(self, source, command, args):
        """
        Handles SASL authentication status reports.
        """
        logfunc = log.info if command == '903' else log.warning
        logfunc('(%s) %s', self.name, args[-1])
        if not self.has_eob:
            self._do_cap_end()
    handle_903 = handle_902 = handle_905 = handle_906 = handle_907 = handle_904

    def _request_ircv3_caps(self):
        # Filter the capabilities we want by the ones actually supported by the server.
        available_caps = {cap for cap in IRCV3_CAPABILITIES if cap in self.ircv3_caps_available}
        # And by the ones we don't already have.
        caps_wanted = available_caps - self.ircv3_caps

        log.debug('(%s) Requesting IRCv3 capabilities %s (available: %s)', self.name, caps_wanted, available_caps)
        if caps_wanted:
            self.send('CAP REQ :%s' % ' '.join(caps_wanted), queue=False)

    def handle_cap(self, source, command, args):
        """
        Handles IRCv3 capabilities transmission.
        """
        subcmd = args[1]

        if subcmd == 'LS':
            # Server: CAP * LS * :multi-prefix extended-join account-notify batch invite-notify tls
            # Server: CAP * LS * :cap-notify server-time example.org/dummy-cap=dummyvalue example.org/second-dummy-cap
            # Server: CAP * LS :userhost-in-names sasl=EXTERNAL,DH-AES,DH-BLOWFISH,ECDSA-NIST256P-CHALLENGE,PLAIN
            log.debug('(%s) Got new capabilities %s', self.name, args[-1])
            self.ircv3_caps_available.update(self.parse_isupport(args[-1], None))
            if args[2] != '*':
                self._request_ircv3_caps()

        elif subcmd == 'ACK':
            # Server: CAP * ACK :multi-prefix sasl
            newcaps = set(args[-1].split())
            log.debug('(%s) Received ACK for IRCv3 capabilities %s', self.name, newcaps)
            self.ircv3_caps |= newcaps

            # Only send CAP END immediately if SASL is disabled. Otherwise, wait for the 90x responses
            # to do so.
            if not self._try_sasl_auth():
                if not self.has_eob:
                    self._do_cap_end()
        elif subcmd == 'NAK':
            log.warning('(%s) Got NAK for IRCv3 capabilities %s, even though they were supposedly available',
                        self.name, args[-1])
            if not self.has_eob:
                self._do_cap_end()
        elif subcmd == 'NEW':
            # :irc.example.com CAP modernclient NEW :batch
            # :irc.example.com CAP tester NEW :away-notify extended-join
            # Note: CAP NEW allows capabilities with values (e.g. sasl=mech1,mech2), while CAP DEL
            # does not.
            log.debug('(%s) Got new capabilities %s', self.name, args[-1])
            newcaps = self.parse_isupport(args[-1], None)
            self.ircv3_caps_available.update(newcaps)
            self._request_ircv3_caps()

            # Attempt SASL auth routines when sasl is added/removed, if doing so is enabled.
            if 'sasl' in newcaps and self.serverdata.get('sasl_reauth'):
                log.debug('(%s) Attempting SASL reauth due to CAP NEW', self.name)
                self._try_sasl_auth()

        elif subcmd == 'DEL':
            # :irc.example.com CAP modernclient DEL :userhost-in-names multi-prefix away-notify
            log.debug('(%s) Removing capabilities %s', self.name, args[-1])
            for cap in args[-1].split():
                # Remove the capabilities from the list available, and return None (ignore) if any fail
                self.ircv3_caps_available.pop(cap, None)
                self.ircv3_caps.discard(cap)

    def handle_001(self, source, command, args):
        """
        Handles 001 / RPL_WELCOME.
        """
        # enumerate our uplink
        self.uplink = source

    def handle_376(self, source, command, args):
        """
        Handles end of MOTD numerics, used to start things like autoperform.
        """

        # Run autoperform commands.
        for line in self.serverdata.get("autoperform", []):
            self.send(line)

        # Virtual endburst hook.
        if not self.has_eob:
            self.has_eob = True
            return {'parse_as': 'ENDBURST'}
        self.connected.set()

    handle_422 = handle_376

    def handle_353(self, source, command, args):
        """
        Handles 353 / RPL_NAMREPLY.
        """
        # <- :charybdis.midnight.vpn 353 ice = #test :ice @GL

        # Mark "@"-type channels as secret automatically, per RFC2812.
        channel = self.to_lower(args[2])
        if args[1] == '@':
            self.apply_modes(channel, [('+s', None)])

        names = set()
        modes = set()
        prefix_to_mode = {v:k for k, v in self.prefixmodes.items()}
        prefixes = ''.join(self.prefixmodes.values())

        for name in args[-1].split():
            nick = name.lstrip(prefixes)

            # Get the PUID for the given nick. If one doesn't exist, spawn
            # a new virtual user. TODO: wait for WHO responses for each nick before
            # spawning in order to get a real ident/host.
            idsource = self._get_UID(nick)

            # Queue these virtual users to be joined if they're not already in the channel,
            # or we're waiting for a kick acknowledgment for them.
            if (idsource not in self.channels[channel].users) or (idsource in \
                    self.kick_queue.get(channel, ([],))[0]):
                names.add(idsource)
            self.users[idsource].channels.add(channel)

            # Process prefix modes
            for char in name:
                if char in self.prefixmodes.values():
                    modes.add(('+' + prefix_to_mode[char], idsource))
                else:
                    break

        # Statekeeping: make sure the channel's user list is updated!
        self.channels[channel].users |= names
        self.apply_modes(channel, modes)

        log.debug('(%s) handle_353: adding users %s to %s', self.name, names, channel)
        log.debug('(%s) handle_353: adding modes %s to %s', self.name, modes, channel)

        # Unless /WHO has already been received for the given channel, we generally send the hook
        # for JOIN after /who data is received, to enumerate the ident, host, and real names of
        # users.
        if names and hasattr(self.channels[channel], 'who_received'):
            # /WHO *HAS* already been received. Send JOIN hooks here because we use this to keep
            # track of any failed KICK attempts sent by the relay bot.
            log.debug('(%s) handle_353: sending JOIN hook because /WHO was already received for %s',
                      self.name, channel)
            return {'channel': channel, 'users': names, 'modes': self.channels[channel].modes,
                    'parse_as': "JOIN"}

    def _check_puid_collision(self, nick):
        """
        Checks to make sure a nick doesn't clash with a PUID.
        """
        if nick in self.users or nick in self.servers:
            raise ProtocolError("Got bad nick %s from IRC which clashes with a PUID. Is someone trying to spoof users?" % nick)

    def handle_352(self, source, command, args):
        """
        Handles 352 / RPL_WHOREPLY.
        """
        # parameter count:               0   1     2       3         4                      5   6  7
        # <- :charybdis.midnight.vpn 352 ice #test ~pylink 127.0.0.1 charybdis.midnight.vpn ice H+ :0 PyLink
        # <- :charybdis.midnight.vpn 352 ice #test ~gl 127.0.0.1 charybdis.midnight.vpn GL H*@ :0 realname
        ident = args[2]
        host = args[3]
        nick = args[5]
        status = args[6]
        # Hopcount and realname field are together. We only care about the latter.
        realname = args[-1].split(' ', 1)[-1]

        self._check_puid_collision(nick)
        uid = self.nick_to_uid(nick)

        if uid is None:
            log.debug("(%s) Ignoring extraneous /WHO info for %s", self.name, nick)
            return

        self.update_client(uid, 'IDENT', ident)
        self.update_client(uid, 'HOST', host)
        self.update_client(uid, 'GECOS', realname)

        # The status given uses the following letters: <H|G>[*][@|+]
        # H means here (not marked /away)
        # G means away is set (we'll have to fake a message because it's not given)
        # * means IRCop.
        # The rest are prefix modes. Multiple can be given by the IRCd if multiple are set
        log.debug('(%s) handle_352: status string on user %s: %s', self.name, nick, status)
        if status[0] == 'G':
            log.debug('(%s) handle_352: calling away() with argument', self.name)
            self.away(uid, 'Away')
        elif status[0] == 'H':
            log.debug('(%s) handle_352: calling away() without argument', self.name)
            self.away(uid, '')  # Unmark away status
        else:
            log.warning('(%s) handle_352: got wrong string %s for away status', self.name, status[0])

        if self.serverdata.get('track_oper_statuses'):
            if '*' in status:  # Track IRCop status
                if not self.is_oper(uid, allowAuthed=False):
                    # Don't send duplicate oper ups if the target is already oper.
                    self.apply_modes(uid, [('+o', None)])
                    self.call_hooks([uid, 'MODE', {'target': uid, 'modes': {('+o', None)}}])
                    self.call_hooks([uid, 'CLIENT_OPERED', {'text': 'IRC Operator'}])
            elif self.is_oper(uid, allowAuthed=False) and not self.is_internal_client(uid):
                # Track deopers
                self.apply_modes(uid, [('-o', None)])
                self.call_hooks([uid, 'MODE', {'target': uid, 'modes': {('-o', None)}}])

        self.who_received.add(uid)

    def handle_315(self, source, command, args):
        """
        Handles 315 / RPL_ENDOFWHO.
        """
        # <- :charybdis.midnight.vpn 315 ice #test :End of /WHO list.
        # Join all the users in which the last batch of /who requests were received.
        users = self.who_received.copy()
        self.who_received.clear()

        channel = self.to_lower(args[1])
        c = self.channels[channel]
        c.who_received = True

        modes = set(c.modes)
        for user in users:
            # Fill in prefix modes of everyone when doing mock SJOIN.
            try:
                for mode in c.get_prefix_modes(user):
                    modechar = self.cmodes.get(mode)
                    log.debug('(%s) handle_315: adding mode %s +%s %s', self.name, mode, modechar, user)
                    if modechar:
                        modes.add((modechar, user))
            except KeyError as e:
                log.debug("(%s) Ignoring KeyError (%s) from WHO response; it's probably someone we "
                          "don't share any channels with", self.name, e)

        return {'channel': channel, 'users': users, 'modes': modes,
                'parse_as': "JOIN"}

    def handle_433(self, source, command, args):
        # <- :millennium.overdrivenetworks.com 433 * ice :Nickname is already in use.
        # HACK: I don't like modifying the config entries raw, but this is difficult because
        # irc.pseudoclient doesn't exist as an attribute until we get run the ENDBURST stuff
        # in service_support (this is mapped to 005 here).
        self.conf_nick += '_'
        self.serverdata['pylink_nick'] = self.conf_nick
        self.send('NICK %s' % self.conf_nick)
    handle_432 = handle_437 = handle_433

    def handle_join(self, source, command, args):
        """
        Handles incoming JOINs.
        """
        # <- :GL|!~GL@127.0.0.1 JOIN #whatever
        channel = self.to_lower(args[0])
        self.join(source, channel)

        return {'channel': channel, 'users': [source], 'modes': self.channels[channel].modes}

    def handle_kick(self, source, command, args):
        """
        Handles incoming KICKs.
        """
        # <- :GL!~gl@127.0.0.1 KICK #whatever GL| :xd
        channel = self.to_lower(args[0])
        target = self.nick_to_uid(args[1])

        try:
            reason = args[2]
        except IndexError:
            reason = ''

        if channel in self.kick_queue:
            # Remove this client from the kick queue if present there.
            log.debug('(%s) kick: removing %s from kick queue for channel %s', self.name, target, channel)
            self.kick_queue[channel][0].discard(target)

            if not self.kick_queue[channel][0]:
                log.debug('(%s) kick: cancelling kick timer for channel %s (all kicks accounted for)', self.name, channel)
                # There aren't any kicks that failed to be acknowledged. We can remove the timer now
                self.kick_queue[channel][1].cancel()
                del self.kick_queue[channel]

        # Statekeeping: remove the target from the channel they were previously in.
        self.channels[channel].remove_user(target)
        try:
            self.users[target].channels.remove(channel)
        except KeyError:
            pass

        if (not self.is_internal_client(source)) and not self.is_internal_server(source):
            # Don't repeat hooks if we're the kicker.
            self.call_hooks([source, 'KICK', {'channel': channel, 'target': target, 'text': reason}])

        # Delete channels that we were kicked from, for better state keeping.
        if self.pseudoclient and target == self.pseudoclient.uid:
            del self.channels[channel]

    def handle_mode(self, source, command, args):
        """Handles MODE changes."""
        # <- :GL!~gl@127.0.0.1 MODE #dev +v ice
        # <- :ice MODE ice :+Zi
        target = args[0]
        if utils.isChannel(target):
            target = self.to_lower(target)
            oldobj = self.channels[target].deepcopy()
        else:
            target = self.nick_to_uid(target)
            oldobj = None
        modes = args[1:]
        changedmodes = self.parse_modes(target, modes)
        self.apply_modes(target, changedmodes)

        if self.is_internal_client(target):
            log.debug('(%s) Suppressing MODE change hook for internal client %s', self.name, target)
            return
        if changedmodes:
            # Prevent infinite loops: don't send MODE hooks if the sender is US.
            # Note: this is not the only check in Clientbot to prevent mode loops: if our nick
            # somehow gets desynced, this may not catch everything it's supposed to.
            if (self.pseudoclient and source != self.pseudoclient.uid) or not self.pseudoclient:
                return {'target': target, 'modes': changedmodes, 'channeldata': oldobj}

    def handle_324(self, source, command, args):
        """Handles MODE announcements via RPL_CHANNELMODEIS (i.e. the response to /mode #channel)"""
        # -> MODE #test
        # <- :midnight.vpn 324 GL #test +nt
        # <- :midnight.vpn 329 GL #test 1491773459
        channel = self.to_lower(args[1])
        modes = args[2:]
        log.debug('(%s) Got RPL_CHANNELMODEIS (324) modes %s for %s', self.name, modes, channel)
        changedmodes = self.parse_modes(channel, modes)
        self.apply_modes(channel, changedmodes)

    def handle_329(self, source, command, args):
        """Handles TS announcements via RPL_CREATIONTIME."""
        channel = self.to_lower(args[1])
        ts = int(args[2])
        self.channels[channel].ts = ts

    def handle_nick(self, source, command, args):
        """Handles NICK changes."""
        # <- :GL|!~GL@127.0.0.1 NICK :GL_

        if not self.pseudoclient:
            # We haven't properly logged on yet, so any initial NICK should be treated as a forced
            # nick change for US. For example, this clause is used to handle forced nick changes
            # sent by ZNC, when the login nick and the actual IRC nick of the bouncer differ.

            # HACK: change the nick config entry so services_support knows what our main
            # pseudoclient is called.
            oldnick = self.serverdata['pylink_nick']
            self.serverdata['pylink_nick'] = self.conf_nick = args[0]
            log.debug('(%s) Pre-auth FNC: Forcing configured nick to %s from %s', self.name, args[0], oldnick)
            return

        oldnick = self.users[source].nick
        self.users[source].nick = args[0]

        return {'newnick': args[0], 'oldnick': oldnick}

    def handle_part(self, source, command, args):
        """
        Handles incoming PARTs.
        """
        # <- :GL|!~GL@127.0.0.1 PART #whatever
        channels = list(map(self.to_lower, args[0].split(',')))
        try:
            reason = args[1]
        except IndexError:
            reason = ''

        for channel in channels:
            self.channels[channel].remove_user(source)
        self.users[source].channels -= set(channels)

        self.call_hooks([source, 'PART', {'channels': channels, 'text': reason}])

        # Clear channels that are empty, or that we're parting.
        for channel in channels:
            if (self.pseudoclient and source == self.pseudoclient.uid) or not self.channels[channel].users:
                del self.channels[channel]

    def handle_ping(self, source, command, args):
        """
        Handles incoming PING requests.
        """
        self.send('PONG :%s' % args[0], queue=False)

    def handle_pong(self, source, command, args):
        """
        Handles incoming PONG.
        """
        if source == self.uplink:
            self.lastping = time.time()

    def handle_privmsg(self, source, command, args):
        """Handles incoming PRIVMSG/NOTICE."""
        # <- :sender PRIVMSG #dev :afasfsa
        # <- :sender NOTICE somenick :afasfsa
        target = args[0]

        if self.is_internal_client(source) or self.is_internal_server(source):
            log.warning('(%s) Received %s to %s being routed the wrong way!', self.name, command, target)
            return

        # We use lowercase channels internally.
        if utils.isChannel(target):
            target = self.to_lower(target)
        else:
            target = self.nick_to_uid(target)
        if target:
            return {'target': target, 'text': args[1]}
    handle_notice = handle_privmsg

    def handle_quit(self, source, command, args):
        """Handles incoming QUITs."""
        if self.pseudoclient and source == self.pseudoclient.uid:
            # Someone faked a quit from us? We should abort.
            raise ProtocolError("Received QUIT from uplink (%s)" % args[0])

        self.quit(source, args[0])
        return {'text': args[0]}


Class = ClientbotWrapperProtocol
