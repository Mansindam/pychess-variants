from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone

import aiohttp_session
from aiohttp import web

from broadcast import lobby_broadcast, round_broadcast
from const import ANON_PREFIX, NOTIFY_PAGE_SIZE, STARTED, VARIANTS
from glicko2.glicko2 import gl2, DEFAULT_PERF, Rating
from login import RESERVED_USERS
from newid import id8, new_id
from const import TYPE_CHECKING
if TYPE_CHECKING:
    from pychess_global_app_state import PychessGlobalAppState
from pychess_global_app_state_utils import get_app_state
from seek import get_seeks

log = logging.getLogger(__name__)

SILENCE = 15 * 60
ANON_TIMEOUT = 10 * 60
PENDING_SEEK_TIMEOUT = 10
ABANDON_TIMEOUT = 90


class User:
    def __init__(
        self,
        app_state: PychessGlobalAppState,
        bot=False,
        username=None,
        anon=False,
        title="",
        perfs=None,
        pperfs=None,
        enabled=True,
        lang=None,
        theme="dark",
    ):
        self.app_state = app_state
        self.bot = False if username == "PyChessBot" else bot
        self.anon = anon
        self.lang = lang
        self.theme = theme
        self.notifications = None

        if username is None:
            self.anon = True
            self.username = ANON_PREFIX + id8()
        else:
            self.username = username

        self.seeks = {}
        self.lobby_sockets = set()
        self.tournament_sockets = {}  # {tournamentId: set()}

        self.notify_channels = set()

        self.puzzles = {}  # {pizzleId: vote} where vote 0 = not voted, 1 = up, -1 = down
        self.puzzle_variant = None

        self.game_sockets = {}
        self.title = title
        self.game_in_progress = None
        self.abandon_game_task = None
        self.correspondence_games = []

        if self.bot:
            self.event_queue = asyncio.Queue()
            self.game_queues = {}
            self.title = "BOT"

        self.online = False

        if perfs is None:
            self.perfs = {variant: DEFAULT_PERF for variant in VARIANTS}
        else:
            self.perfs = {
                variant: perfs[variant] if variant in perfs else DEFAULT_PERF
                for variant in VARIANTS
            }

        if pperfs is None:
            self.pperfs = {variant: DEFAULT_PERF for variant in VARIANTS}
        else:
            self.pperfs = {
                variant: pperfs[variant] if variant in pperfs else DEFAULT_PERF
                for variant in VARIANTS
            }

        self.enabled = enabled
        self.fen960_as_white = None

        # last game played
        self.tv = None

        # lobby chat spammer time out (10 min)
        self.silence = 0

        # purge inactive anon users after ANON_TIMEOUT sec
        if self.anon and self.username not in RESERVED_USERS:
            self.remove_task = asyncio.create_task(self.remove())

    async def remove(self):
        while True:
            await asyncio.sleep(ANON_TIMEOUT)
            if not self.online:
                # give them a second chance
                await asyncio.sleep(3)
                if not self.online:
                    try:
                        del self.app_state.users[self.username]
                    except KeyError:
                        log.error("Failed to del %s from users", self.username, exc_info=True)
                    break

    async def abandon_game(self, game):
        abandon_timeout = ABANDON_TIMEOUT * (2 if game.base >= 3 else 1)
        await asyncio.sleep(abandon_timeout)
        if game.status <= STARTED and game.id not in self.game_sockets:
            if game.bot_game or self.anon:
                response = await game.game_ended(self, "abandon")
                await round_broadcast(game, response)
            else:
                # TODO: message opp to let him claim win
                pass

    def update_online(self):
        self.online = (
            len(self.game_sockets) > 0
            or len(self.lobby_sockets) > 0
            or len(self.tournament_sockets) > 0
        )

    def get_rating(self, variant: str, chess960: bool) -> Rating:
        if variant in self.perfs:
            gl = self.perfs[variant + ("960" if chess960 else "")]["gl"]
            la = self.perfs[variant + ("960" if chess960 else "")]["la"]
            return gl2.create_rating(gl["r"], gl["d"], gl["v"], la)
        rating = gl2.create_rating()
        self.perfs[variant + ("960" if chess960 else "")] = DEFAULT_PERF
        return rating

    def get_puzzle_rating(self, variant: str, chess960: bool) -> Rating:
        if variant in self.pperfs:
            gl = self.pperfs[variant + ("960" if chess960 else "")]["gl"]
            la = self.pperfs[variant + ("960" if chess960 else "")]["la"]
            return gl2.create_rating(gl["r"], gl["d"], gl["v"], la)
        rating = gl2.create_rating()
        self.pperfs[variant + ("960" if chess960 else "")] = DEFAULT_PERF
        return rating

    def set_silence(self):
        self.silence += SILENCE

        async def silencio():
            await asyncio.sleep(SILENCE)
            self.silence -= SILENCE

        asyncio.create_task(silencio())

    async def set_rating(self, variant, chess960, rating):
        if self.anon:
            return
        gl = {"r": rating.mu, "d": rating.phi, "v": rating.sigma}
        la = datetime.now(timezone.utc)
        nb = self.perfs[variant + ("960" if chess960 else "")].get("nb", 0)
        self.perfs[variant + ("960" if chess960 else "")] = {
            "gl": gl,
            "la": la,
            "nb": nb + 1,
        }

        if self.app_state.db is not None:
            await self.app_state.db.user.find_one_and_update(
                {"_id": self.username}, {"$set": {"perfs": self.perfs}}
            )

    async def set_puzzle_rating(self, variant, chess960, rating):
        if self.anon:
            return
        gl = {"r": rating.mu, "d": rating.phi, "v": rating.sigma}
        la = datetime.now(timezone.utc)
        nb = self.pperfs[variant + ("960" if chess960 else "")].get("nb", 0)
        self.pperfs[variant + ("960" if chess960 else "")] = {
            "gl": gl,
            "la": la,
            "nb": nb + 1,
        }

        if self.app_state.db is not None:
            await self.app_state.db.user.find_one_and_update(
                {"_id": self.username}, {"$set": {"pperfs": self.pperfs}}
            )

    async def notify_game_end(self, game):
        opp_name = (
            game.wplayer.username
            if game.bplayer.username == self.username
            else game.bplayer.username
        )

        if game.result in ("1/2-1/2", "*"):
            win = None
        else:
            if (game.result == "1-0" and game.wplayer.username == self.username) or (
                game.result == "0-1" and game.bplayer.username == self.username
            ):
                win = True
            else:
                win = False

        _id = await new_id(None if self.app_state.db is None else self.app_state.db.notify)
        document = {
            "_id": _id,
            "notifies": self.username,
            "type": "gameAborted" if game.result == "*" else "gameEnd",
            "read": False,
            "createdAt": datetime.now(timezone.utc),
            "content": {
                "id": game.id,
                "opp": opp_name,
                "win": win,
            },
        }

        if self.notifications is None:
            cursor = self.app_state.db.notify.find({"notifies": self.username})
            self.notifications = await cursor.to_list(length=100)

        self.notifications.append(document)

        for queue in self.notify_channels:
            await queue.put(
                json.dumps(self.notifications[-NOTIFY_PAGE_SIZE:], default=datetime.isoformat)
            )

        if self.app_state.db is not None:
            await self.app_state.db.notify.insert_one(document)

    async def notified(self):
        self.notifications = [{**notif, "read": True} for notif in self.notifications]

        if self.app_state.db is not None:
            await self.app_state.db.notify.update_many({"notifies": self.username}, {"$set": {"read": True}})

    def as_json(self, requester):
        return {
            "_id": self.username,
            "title": self.title,
            "online": True if self.username == requester else self.online,
        }

    async def clear_seeks(self):
        if len(self.seeks) > 0:
            for seek_id in list(self.seeks):
                game_id = self.seeks[seek_id].game_id
                # preserve invites (seek with game_id) and corr seeks!
                if game_id is None and self.seeks[seek_id].day == 0:
                    del self.app_state.seeks[seek_id]
                    del self.seeks[seek_id]

            await lobby_broadcast(self.app_state.lobbysockets, get_seeks(self.app_state.seeks))

    def delete_pending_seek(self, seek):
        async def delete_seek(seek):
            await asyncio.sleep(PENDING_SEEK_TIMEOUT)

            if seek.pending:
                try:
                    del self.seeks[seek.id]
                    del self.app_state.seeks[seek.id]
                except KeyError:
                    log.error("Failed to del %s from seeks", seek.id, exc_info=True)

        asyncio.create_task(delete_seek(seek))

    async def update_seeks(self, pending=True):
        if len(self.seeks) > 0:
            for seek in self.seeks.values():
                # preserve invites (seek with game_id) and corr seeks
                if seek.game_id is None and seek.day == 0:
                    seek.pending = pending
                    if pending:
                        self.delete_pending_seek(seek)

            await lobby_broadcast(self.app_state.lobbysockets, get_seeks(self.app_state.seeks))

    async def send_game_message(self, game_id, message):
        # todo: for now just logging dropped messages, but at some point should evaluate whether to queue them when no socket 
		#       or include info about the complete round state in some more general message that is always 
		#       sent on reconnect so client doesnt lose state
        ws = self.game_sockets.get(game_id)
        log.debug("Sending message %s to %s. ws = %r", message, self.username, ws)
        if ws is not None:
            try:
                await ws.send_json(message)
            except Exception as e: #ConnectionResetError
                log.error("dropping message %s for %s", stack_info=True, exc_info=True)
        else:
            log.error("No ws for that game. Dropping message %s for %s", message, self.username)
            log.debug("Currently user %s has these game_sockets: %r", self.username, self.game_sockets)

    def __str__(self):
        return "%s %s bot=%s anon=%s chess=%s" % (
            self.title,
            self.username,
            self.bot,
            self.anon,
            self.perfs["chess"]["gl"]["r"],
        )


async def set_theme(request):
    app_state = get_app_state(request.app)
    post_data = await request.post()
    theme = post_data.get("theme")

    if theme is not None:
        referer = request.headers.get("REFERER")
        session = await aiohttp_session.get_session(request)
        session_user = session.get("user_name")
        if session_user in app_state.users:
            user = app_state.users[session_user]
            user.theme = theme
            if user.db is not None:
                await user.db.user.find_one_and_update(
                    {"_id": user.username}, {"$set": {"theme": theme}}
                )
        session["theme"] = theme
        return web.HTTPFound(referer)
    else:
        raise web.HTTPNotFound()
