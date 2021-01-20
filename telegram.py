
import logging
import os
import telethon

# Configuration

# GET API_ID and API_HASH from https://my.telegram.org/apps
# AND PUT HERE BEFORE RUNNING irgramd

TELEGRAM_API_ID             =
TELEGRAM_API_HASH           = ''

NICK_MAX_LENGTH             = 20

    # Telegram

class TelegramHandler(object):
    def __init__(self, irc, config_dir):
        self.logger     = logging.getLogger()
        self.config_dir = config_dir
        self.irc        = irc

    def initialize_telegram(self):
        # Setup media folder
        self.telegram_media_dir = os.path.join(self.config_dir, 'media')
        if not os.path.exists(self.telegram_media_dir):
            os.makedirs(self.telegram_media_dir)

        # Setup session folder
        self.telegram_session_dir = os.path.join(self.config_dir, 'session')
        if not os.path.exists(self.telegram_session_dir):
            os.makedirs(self.telegram_session_dir)

        # Construct Telegram client
        telegram_session     = os.path.join(self.telegram_session_dir, 'telegram')
        self.telegram_client = telethon.TelegramClient(telegram_session,
            TELEGRAM_API_ID, TELEGRAM_API_HASH
        )

        # Initialize Telegram ID to IRC nick mapping
        self.tid_to_iid = {}

        # Register Telegram callbacks
        callbacks = (
            (self.handle_telegram_message    , telethon.events.NewMessage),
            (self.handle_telegram_chat_action, telethon.events.ChatAction),
        )
        for handler, event in callbacks:
            self.telegram_client.add_event_handler(handler, event)

        # Start Telegram client
        self.telegram_client.start()
        # Update IRC <-> Telegram mapping
        for dialog in self.telegram_client.iter_dialogs():
            chat = dialog.entity
            if isinstance(chat, telethon.types.User):
                user = self.get_telegram_nick(chat)
                self.tid_to_iid[chat.id] = user
                self.irc.iid_to_tid[user] = chat.id
            else:
                channel = self.get_telegram_channel(chat)
                self.tid_to_iid[chat.id] = channel
                self.irc.iid_to_tid[channel] = chat.id

    def get_telegram_nick(self, user):
        nick = (user.username
                or telethon.utils.get_display_name(user)
                or str(user.id))
        nick = nick.replace(' ', '')[:NICK_MAX_LENGTH]
        while nick in self.irc.iid_to_tid:
            nick += '_'
        return nick

    def get_telegram_channel(self, chat):
        return '#' + chat.title.lower().replace(' ', '-')

    async def get_irc_nick_from_telegram_id(self, tid, entity=None):
        if tid not in self.tid_to_iid:
            user = entity or await self.telegram_client.get_entity(tid)
            nick = self.get_telegram_nick(user)
            self.tid_to_iid[tid]  = nick
            self.irc.iid_to_tid[nick] = tid

        return self.tid_to_iid[tid]

    async def get_irc_channel_from_telegram_id(self, tid, entity=None):
        if tid not in self.tid_to_iid:
            chat    = entity or await self.telegram_client.get_entity(tid)
            channel = self.get_telegram_channel(chat)
            self.tid_to_iid[tid]     = channel
            self.irc.iid_to_tid[channel] = tid

        return self.tid_to_iid[tid]

    async def get_telegram_channel_participants(self, tid):
        channel = self.tid_to_iid[tid]
        nicks   = []
        async for user in self.telegram_client.iter_participants(tid):
            user_nick = await self.get_irc_nick_from_telegram_id(user.id, user)

            nicks.append(user_nick)
            self.irc.irc_channels[channel].add(user_nick)

        return nicks

    async def handle_telegram_message(self, event):
        self.logger.debug('Handling Telegram Message: %s', event)

        if event.message.is_private:
            await self.handle_telegram_private_message(event)
        else:
            await self.handle_telegram_channel_message(event)

    async def handle_telegram_private_message(self, event):
        self.logger.debug('Handling Telegram Private Message: %s', event)

        nick = await self.get_irc_nick_from_telegram_id(event.sender_id)
        for message in event.message.message.splitlines():
            for user in self.irc.users:
                await self.irc.send_irc_command(user, ':{} PRIVMSG {} :{}'.format(
                    self.irc.get_irc_user_mask(nick), user.irc_nick, message
                ))

    async def handle_telegram_channel_message(self, event):
        self.logger.debug('Handling Telegram Channel Message: %s', event)

        # Join IRC channel if not already in it
        entity  = await event.message.get_chat()
        channel = await self.get_irc_channel_from_telegram_id(event.message.chat_id, entity)
        if channel not in self.irc.irc_channels:
            await self.irc.join_irc_channel(self.irc.irc_nick, channel, True)

        nick = await self.get_irc_nick_from_telegram_id(event.sender_id)
        if nick not in self.irc.irc_channels[channel]:
            await self.irc.join_irc_channel(nick, channel, False)

        # Format messages with media
        messages = event.message.message.splitlines() if event.message.message else []
        if event.message.media and (event.message.photo or event.message.gif):
            message = await self.download_telegram_media(event.message, 'Image')
            if message:
                messages.insert(0, message)
        elif event.message.media and (event.message.sticker):
            messages.insert(0, 'Sticker: {}'.format(event.message.sticker.id))

        # Send all messages to IRC
        for message in messages:
            for user in self.irc.users:
                await self.irc.send_irc_command(user, ':{} PRIVMSG {} :{}'.format(
                    self.irc.get_irc_user_mask(nick), channel, message
                ))

    async def handle_telegram_chat_action(self, event):
        self.logger.debug('Handling Telegram Chat Action: %s', event)

        try:
            tid = event.action_message.to_id.channel_id
        except AttributeError:
            tid = event.action_message.to_id.chat_id
        finally:
            irc_channel = await self.get_irc_channel_from_telegram_id(tid)
            await self.get_telegram_channel_participants(tid)

        try:                                        # Join Chats
            irc_nick = await self.get_irc_nick_from_telegram_id(event.action_message.action.users[0])
        except (IndexError, AttributeError):
            try:                                    # Kick
                irc_nick = await self.get_irc_nick_from_telegram_id(event.action_message.action.user_id)
            except (IndexError, AttributeError):    # Join Channels
                irc_nick = await self.get_irc_nick_from_telegram_id(event.action_message.sender_id)

        if event.user_added or event.user_joined:
            await self.irc.join_irc_channel(irc_nick, irc_channel, False)
        elif event.user_kicked or event.user_left:
            await self.irc.part_irc_channel(irc_nick, irc_channel)

    async def join_all_telegram_channels(self):
        async for dialog in self.telegram_client.iter_dialogs():
            chat = dialog.entity
            if not isinstance(chat, telethon.types.User):
                channel = self.get_telegram_channel(chat)
                self.tid_to_iid[chat.id] = channel
                self.irc.iid_to_tid[channel] = chat.id
                await self.irc.join_irc_channel(self.irc.irc_nick, channel, True)

    async def download_telegram_media(self, message, tag):
        local_path = await message.download_media(self.telegram_media_dir)
        return
        if not local_path:
            return

        request  = tornado.httpclient.HTTPRequest(
            url    = 'https://yld.me/paste',
            method = 'POST',
            body   = open(local_path, 'rb').read(),
        )
        response = await tornado.httpclient.AsyncHTTPClient().fetch(request)

        os.unlink(local_path)
        return tag + ': ' + response.body.decode().strip()