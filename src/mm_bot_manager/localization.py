"""Per-user resolution with a bounded profile cache; no global current language."""

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from .i18n import normalize_language
from .mm_client import MattermostClient
from .repository import PreferenceRepo

if TYPE_CHECKING:
    from .commands.context import CommandContext
    from .db import DbConn


class LanguageResolver:
    def __init__(
        self, conn: DbConn, mm: MattermostClient, default: str = "en", *, ttl: float = 300
    ) -> None:
        self.preferences = PreferenceRepo(conn)
        self.mm = mm
        self.default = normalize_language(default) or "en"
        self.ttl = ttl
        self.cache: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def invalidate(self, user_id: str) -> None:
        self.cache.pop(user_id, None)

    async def resolve(self, user_id: str) -> str:
        preference, _ = self.preferences.get(user_id)
        if preference != "auto":
            return preference
        cached = self.cache.get(user_id)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(user_id)
            return cached[1]
        # Independent misses must not block other users behind a slow profile API call.
        try:
            async with asyncio.timeout(1.5):
                account = await self.mm.get_user(user_id)
            locale = normalize_language(account.get("locale")) or self.default
            ttl = self.ttl
        except Exception:
            locale, ttl = cached[1] if cached else self.default, min(self.ttl, 30)
        self.cache[user_id] = (time.monotonic() + ttl, locale)
        self.cache.move_to_end(user_id)
        while len(self.cache) > 1024:
            self.cache.popitem(last=False)
        return locale


async def user_language(ctx: CommandContext, user_id: str) -> str:
    if ctx.language_resolver is not None:
        return await ctx.language_resolver.resolve(user_id)
    preference, _ = PreferenceRepo(ctx.bot_repo.conn).get(user_id)
    return preference if preference != "auto" else ctx.default_language
