import asyncio
import os
import store
from store import normalize_tag
from dotenv import load_dotenv

load_dotenv()  # must run before importing modules that read env at import time

import discord
from discord import app_commands
from discord.ext import tasks

from agent import answer_question, Answer
import bs_client
from plugins import history
import plugins

plugins.load_plugins()
TOKEN = os.getenv("DISCORD_TOKEN")

#Intents = what events Discord will send us. Default is fine for slash commands
#message_content will matter in Phase 5 (threads), so we enable it right now since
#we already flipped the switch in the Developer Portal.

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)  # holds our slash commands
store.init()

# How often the poller snapshots each tracked player's battlelog. The official
# API only keeps ~25 battles, so this must stay well under the time a player
# could burn through 25 games — 20 min is safe for casual play.
POLL_MINUTES = 20


async def _snapshot(tag: str) -> None:
    """Record one player's recent battles into history.db. Best-effort — a
    failure is logged, never raised (used from the poller and from /link)."""
    try:
        new = history.record_battles(tag, await bs_client.get_battlelog(tag))
        if new:
            print(f"snapshot: {tag} +{new} new battle(s)")
    except Exception as e:
        print(f"snapshot: {tag} failed: {type(e).__name__}: {e}")


@tasks.loop(minutes=POLL_MINUTES)
async def poll_history():
    """Snapshot every tracked player's recent battles into history.db so we can
    answer longer-window questions than the ~25-battle API log allows. Dedup is
    handled by record_battles (INSERT OR IGNORE); one bad tag can't kill the loop."""
    for tag in history.tracked_tags():
        await _snapshot(tag)


@poll_history.before_loop
async def _before_poll():
    await client.wait_until_ready()  # don't poll before the gateway is up


@client.event
async def on_ready():
    # Registers our slash commands with Discord. Global sync can take up to
    # an hour to propagate; syncing is instant when scoped to one guild,
    # but global is simpler and fine for us.
    await tree.sync()
    # on_ready can fire again on reconnect — guard against double-starting.
    if not poll_history.is_running():
        poll_history.start()
    print(f"Logged in as {client.user} — bot is live. Ctrl+C to stop.")


@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    latency_ms = round(client.latency * 1000)
    await interaction.response.send_message(f"pong 🏓 ({latency_ms}ms)")

@tree.command(name="ask", description="Ask anything about Brawl Stars or your stats")
@app_commands.describe(question="Your question")
async def ask(interaction: discord.Interaction, question: str):
    age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
    print(f"ask: interaction age at handler = {age:.2f}s")
    try:
        await interaction.response.defer()  # beat the 3-second rule
    except discord.NotFound:
        # 10062: interaction token already expired before our ACK landed
        # (transient gateway/network stall). Nothing to reply to — bail.
        print("ask: interaction expired before defer (10062), dropping.")
        return
    asker_tag = store.get_tag(str(interaction.user.id))  # None if not linked
    try:
        answer = await answer_question(question, asker_tag=asker_tag)
    except Exception as e:
        print(f"ask: answer_question failed: {type(e).__name__}: {e}")
        answer = Answer(summary=f"Something broke on my end: "
                                f"`{type(e).__name__}`. Try again?",
                        color="#e84d4d")
    try:
        await _send_answer(interaction, answer)
    except discord.HTTPException as e:
        print(f"ask: failed to send followup: {e}")

@tree.command(name="link", description="Link your Discord account to your Brawl Stars tag")
@app_commands.describe(tag="Your player tag, e.g. #ABC123 (find it in your in-game profile)")
async def link(interaction: discord.Interaction, tag: str):
    try:
        clean = normalize_tag(tag)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    store.set_link(str(interaction.user.id), clean)
    history.track(clean)  # enroll in the background history poller
    # Seed history now so it doesn't wait up to POLL_MINUTES for the first tick.
    asyncio.create_task(_snapshot(clean))
    await interaction.response.send_message(
        f"✅ Linked to `{clean}`. Try `/ask what are my best brawlers?`",
        ephemeral=True,
    )

DEFAULT_COLOR = 0x4DA3FF  # neutral blue accent when the model gives no color


def _parse_color(hex_str: str | None) -> int:
    """'#f5c518' -> 0xf5c518. Bad/missing values fall back to the brand blue."""
    if not hex_str:
        return DEFAULT_COLOR
    try:
        return int(hex_str.strip().lstrip("#"), 16)
    except ValueError:
        return DEFAULT_COLOR


def _build_embed(ans: Answer) -> discord.Embed:
    """Turn an Answer into a Discord embed. Description caps at 4096; the tail,
    if any, is returned to the caller to send as plain follow-ups."""
    embed = discord.Embed(
        title=ans.title or None,
        description=(ans.summary.strip() or "(empty answer)")[:4096],
        color=_parse_color(ans.color),
    )
    for f in ans.fields:
        embed.add_field(name=f["name"], value=f["value"], inline=f["inline"])
    if ans.image_url:
        embed.set_image(url=ans.image_url)
    if ans.thumbnail_url:
        embed.set_thumbnail(url=ans.thumbnail_url)
    embed.set_footer(text="BrawlBot • not affiliated with Supercell")
    return embed


class TabView(discord.ui.View):
    """Buttons that swap between an answer's alternate views (Overview / By Mode
    / …). All views are pre-rendered embeds — a click just edits the message to
    show a different one, so there is no re-query and no LLM/API cost. Only the
    original asker can click; buttons disable themselves on timeout."""

    # Buttons stop working after this many seconds of no interaction (Discord
    # gives no signal, so we time out and grey the buttons out ourselves).
    TIMEOUT = 300

    def __init__(self, embeds: list[discord.Embed], labels: list[str],
                 author_id: int):
        super().__init__(timeout=self.TIMEOUT)
        self.embeds = embeds
        self.author_id = author_id
        self.message: discord.Message | None = None  # set by caller after send
        self.active = 0
        for i, label in enumerate(labels):
            self.add_item(self._make_button(i, label))

    def _make_button(self, index: int, label: str) -> discord.ui.Button:
        btn = discord.ui.Button(label=(label or f"View {index + 1}")[:80],
                                style=self._style(index), row=0)

        async def callback(interaction: discord.Interaction):
            self.active = index
            for i, item in enumerate(self.children):
                item.style = self._style(i)  # highlight the active tab
            await interaction.response.edit_message(
                embed=self.embeds[index], view=self)

        btn.callback = callback
        return btn

    def _style(self, index: int) -> discord.ButtonStyle:
        return (discord.ButtonStyle.primary if index == self.active
                else discord.ButtonStyle.secondary)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This card isn't yours — run `/ask` to get your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass  # message deleted / too old to edit — nothing to do


async def _send_answer(interaction: discord.Interaction, ans: Answer) -> None:
    """Send an Answer as a rich embed. If it carries tabs, attach a TabView whose
    buttons swap between the pre-rendered views. Any over-4096-char summary tail
    on the default view spills into plain follow-ups. If Discord rejects the
    embed (e.g. total size over its 6000-char ceiling), fall back to plain-text
    chunks so the user still gets the answer instead of nothing."""
    try:
        embeds = [_build_embed(ans)]
        labels = ["Overview"]
        for tab in ans.tabs:
            embeds.append(_build_embed(tab))
            labels.append(tab.label or f"View {len(labels) + 1}")

        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0])
        else:
            view = TabView(embeds, labels, author_id=interaction.user.id)
            view.message = await interaction.followup.send(
                embed=embeds[0], view=view)

        overflow = (ans.summary.strip() or "")[4096:]
        if overflow.strip():
            for chunk in _chunk(overflow):
                await interaction.followup.send(chunk)
    except discord.HTTPException as e:
        print(f"ask: embed rejected ({e}); falling back to plain text.")
        for chunk in _chunk(ans.summary):
            await interaction.followup.send(chunk)


def _chunk(text: str, limit: int = 2000):
    """Split a reply into <=limit-char messages on line boundaries, so long
    answers span multiple Discord messages instead of getting cut mid-word."""
    text = text.strip() or "(empty answer)"
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:            # a single monster line
            out.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            out.append(buf)
            buf = ""
        buf += line
    if buf:
        out.append(buf)
    return out


client.run(TOKEN)