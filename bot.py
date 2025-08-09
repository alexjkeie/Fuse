# made by sage

import os
import json
import re
import socket
import random
import asyncio
from datetime import datetime, timedelta

import discord
from discord import Option, Permissions
from discord.ext import tasks

# -------------------------
# CONFIG / FILES
# -------------------------
TOKEN = os.getenv("DISCORD_TOKEN") or "PUT_TOKEN_HERE"
CONFIG_FILE = "config.json"
DATA_FILE = "data.json"

DEFAULT_CONFIG = {
    "guild_test_id": None,           # set to an int for quick guild-only command registration during dev
    "mod_role_id": None,             # optional: a role id you want to treat as moderator
    "anti_link": False,
    "anti_raid": {"enabled": False, "join_limit": 5, "window_seconds": 60},
    "muted_role_name": "Muted"
}

DEFAULT_DATA = {
    "warnings": {},   # guild_id -> user_id -> [warns]
    "muted": {},      # guild_id -> user_id -> unmute_iso_or_null
    "join_log": {}    # guild_id -> [iso timestamps]
}

def ensure_file(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

def read_json(path):
    if path == CONFIG_FILE:
        ensure_file(path, DEFAULT_CONFIG)
    else:
        ensure_file(path, DEFAULT_DATA)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=4)

ensure_file(CONFIG_FILE, DEFAULT_CONFIG)
ensure_file(DATA_FILE, DEFAULT_DATA)

# -------------------------
# INTENTS & BOT
# -------------------------
intents = discord.Intents.all()
intents.message_content = True
bot = discord.Bot(intents=intents)

# helper to optionally register commands only to test guild (faster dev), read config to get guild ID
_config = read_json(CONFIG_FILE)
TEST_GUILD_ID = _config.get("guild_test_id")  # set as int if you want quick guild-only registration

# -------------------------
# JSON helpers (data stores)
# -------------------------
def add_warning(guild_id, user_id, reason, moderator_name):
    data = read_json(DATA_FILE)
    gw = data.setdefault("warnings", {}).setdefault(str(guild_id), {}).setdefault(str(user_id), [])
    gw.append({"reason": reason, "moderator": moderator_name, "timestamp": datetime.utcnow().isoformat()})
    write_json(DATA_FILE, data)

def get_warnings(guild_id, user_id):
    data = read_json(DATA_FILE)
    return data.get("warnings", {}).get(str(guild_id), {}).get(str(user_id), [])

def clear_warnings(guild_id, user_id):
    data = read_json(DATA_FILE)
    gw = data.setdefault("warnings", {}).setdefault(str(guild_id), {})
    if str(user_id) in gw:
        gw[str(user_id)] = []
        write_json(DATA_FILE, data)
        return True
    return False

def record_join(guild_id):
    data = read_json(DATA_FILE)
    log = data.setdefault("join_log", {}).setdefault(str(guild_id), [])
    log.append(datetime.utcnow().isoformat())
    write_json(DATA_FILE, data)

def recent_joins(guild_id, seconds):
    data = read_json(DATA_FILE)
    log = data.get("join_log", {}).get(str(guild_id), [])
    cutoff = datetime.utcnow() - timedelta(seconds=seconds)
    return sum(1 for t in log if datetime.fromisoformat(t) >= cutoff)

# -------------------------
# UTILITIES
# -------------------------
def has_mod_role(member: discord.Member):
    cfg = read_json(CONFIG_FILE)
    mod_id = cfg.get("mod_role_id")
    if mod_id:
        return any(r.id == int(mod_id) for r in member.roles)
    return False

async def ensure_muted_role(guild: discord.Guild):
    cfg = read_json(CONFIG_FILE)
    name = cfg.get("muted_role_name", "Muted")
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        role = await guild.create_role(name=name, reason="Created by bot for mutes")
        # apply send_messages False in all text channels (best-effort)
        for ch in guild.channels:
            try:
                await ch.set_permissions(role, send_messages=False, speak=False, add_reactions=False)
            except Exception:
                pass
    return role

# -------------------------
# EVENTS
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    check_unmutes.start()

@bot.event
async def on_member_join(member: discord.Member):
    cfg = read_json(CONFIG_FILE)
    if cfg.get("anti_raid", {}).get("enabled"):
        record_join(member.guild.id)
        if recent_joins(member.guild.id, cfg["anti_raid"].get("window_seconds", 60)) >= cfg["anti_raid"].get("join_limit", 5):
            # lockdown: remove send_messages for default role (best-effort)
            for ch in member.guild.text_channels:
                try:
                    await ch.set_permissions(member.guild.default_role, send_messages=False)
                except Exception:
                    pass
            notify = ""
            if cfg.get("mod_role_id"):
                role = member.guild.get_role(int(cfg["mod_role_id"]))
                if role:
                    notify = role.mention
            channel = discord.utils.find(lambda c: c.permissions_for(member.guild.me).send_messages, member.guild.text_channels)
            if channel:
                await channel.send(f":rotating_light: Anti-raid triggered — lockdown {notify}")

@bot.event
async def on_message(message: discord.Message):
    # run anti-link for non-bots
    if message.author.bot:
        return
    cfg = read_json(CONFIG_FILE)
    if cfg.get("anti_link"):
        if re.search(r"https?://\S+|www\.\S+", message.content):
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} Links are disabled here.", delete_after=6)
            except Exception:
                pass
            return
    # allow other on_message handling if needed
    await bot.process_commands(message)  # if you later add text commands

# -------------------------
# BACKGROUND TASKS
# -------------------------
@tasks.loop(seconds=20.0)
async def check_unmutes():
    data = read_json(DATA_FILE)
    muted = data.get("muted", {})
    changed = False
    for guild_id_str, users in list(muted.items()):
        guild = bot.get_guild(int(guild_id_str))
        if not guild:
            continue
        for user_id_str, unmute_iso in list(users.items()):
            if unmute_iso is None:
                continue
            try:
                if datetime.utcnow() >= datetime.fromisoformat(unmute_iso):
                    member = guild.get_member(int(user_id_str))
                    role = discord.utils.get(guild.roles, name=read_json(CONFIG_FILE).get("muted_role_name", "Muted"))
                    if member and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Auto unmute")
                        except:
                            pass
                    users.pop(user_id_str, None)
                    changed = True
            except Exception:
                # ignore malformed timestamps
                users.pop(user_id_str, None)
                changed = True
    if changed:
        write_json(DATA_FILE, data)

# -------------------------
# SLASH COMMANDS - Moderation
# -------------------------
# The default_member_permissions param tells Discord which permission is required;
# if the user lacks it, the UI will hide/disable the command.

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(ban_members=True),
                   description="Ban a user (requires Ban Members permission)")
async def ban(
    ctx,
    member: Option(discord.Member, "Member to ban"),
    reason: Option(str, "Reason", required=False, default="No reason provided"),
    delete_days: Option(int, "Days of messages to delete (0-7)", required=False, default=0)
):
    try:
        await member.ban(reason=reason, delete_message_days=max(0, min(7, delete_days)))
        await ctx.respond(f"✅ {member} banned. Reason: {reason}")
    except Exception as e:
        await ctx.respond(f"Failed to ban: `{e}`", ephemeral=True)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(kick_members=True),
                   description="Kick a user (requires Kick Members permission)")
async def kick(
    ctx,
    member: Option(discord.Member, "Member to kick"),
    reason: Option(str, "Reason", required=False, default="No reason provided")
):
    try:
        await member.kick(reason=reason)
        await ctx.respond(f"✅ {member} kicked. Reason: {reason}")
    except Exception as e:
        await ctx.respond(f"Failed to kick: `{e}`", ephemeral=True)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(manage_roles=True),
                   description="Mute a member by giving the configured Muted role")
async def mute(
    ctx,
    member: Option(discord.Member, "Member to mute"),
    minutes: Option(int, "Minutes to mute (0 = indefinite)", required=False, default=0),
    reason: Option(str, "Reason", required=False, default="No reason provided")
):
    role = await ensure_muted_role(ctx.guild)
    try:
        await member.add_roles(role, reason=reason)
        data = read_json(DATA_FILE)
        gmuted = data.setdefault("muted", {}).setdefault(str(ctx.guild.id), {})
        if minutes > 0:
            gmuted[str(member.id)] = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()
            await ctx.respond(f"🔇 {member.mention} muted for {minutes} minute(s). Reason: {reason}")
        else:
            gmuted[str(member.id)] = None
            await ctx.respond(f"🔇 {member.mention} muted indefinitely. Reason: {reason}")
        write_json(DATA_FILE, data)
    except Exception as e:
        await ctx.respond(f"Failed to mute: `{e}`", ephemeral=True)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(manage_roles=True),
                   description="Unmute a member (requires Manage Roles)")
async def unmute(ctx, member: Option(discord.Member, "Member to unmute")):
    role = discord.utils.get(ctx.guild.roles, name=read_json(CONFIG_FILE).get("muted_role_name", "Muted"))
    try:
        if role and role in member.roles:
            await member.remove_roles(role, reason=f"Unmuted by {ctx.author}")
        data = read_json(DATA_FILE)
        data.get("muted", {}).get(str(ctx.guild.id), {}).pop(str(member.id), None)
        write_json(DATA_FILE, data)
        await ctx.respond(f"🔊 {member.mention} unmuted.")
    except Exception as e:
        await ctx.respond(f"Failed to unmute: `{e}`", ephemeral=True)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(manage_messages=True),
                   description="Purge messages (requires Manage Messages)")
async def purge(ctx, limit: Option(int, "Number of messages to delete", required=False, default=10)):
    try:
        deleted = await ctx.channel.purge(limit=limit+1)  # include the command invocation
        await ctx.respond(f"🧹 Deleted {len(deleted)-1} message(s).", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to purge: `{e}`", ephemeral=True)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(manage_messages=True),
                   description="Warn a user (stored in data.json)")
async def warn(ctx, member: Option(discord.Member, "Member to warn"), reason: Option(str, "Reason", required=False, default="No reason")):
    add_warning(ctx.guild.id, member.id, reason, str(ctx.author))
    await ctx.respond(f"⚠️ {member.mention} warned. Reason: {reason}")

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(manage_messages=True),
                   description="View warnings for a user")
async def warnings(ctx, member: Option(discord.Member, "Member to view")):
    warns = get_warnings(ctx.guild.id, member.id)
    if not warns:
        await ctx.respond(f"{member.mention} has no warnings.", ephemeral=True)
        return
    e = discord.Embed(title=f"Warnings for {member}", color=discord.Color.orange())
    for i, w in enumerate(warns, start=1):
        e.add_field(name=f"#{i}", value=f"{w['reason']} — by {w['moderator']} at {w['timestamp']}", inline=False)
    await ctx.respond(embed=e)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(manage_messages=True),
                   description="Clear warnings for a user")
async def clearwarns(ctx, member: Option(discord.Member, "Member to clear warnings for")):
    ok = clear_warnings(ctx.guild.id, member.id)
    if ok:
        await ctx.respond(f"✅ Cleared warnings for {member.mention}")
    else:
        await ctx.respond(f"No warnings for {member.mention}", ephemeral=True)

# -------------------------
# SLASH COMMANDS - Utilities
# -------------------------
@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Get info about a user")
async def userinfo(ctx, member: Option(discord.Member, "Member to inspect", required=False, default=None)):
    member = member or ctx.author
    e = discord.Embed(title=f"{member}", timestamp=datetime.utcnow())
    e.set_thumbnail(url=member.display_avatar.url)
    joined = member.joined_at.isoformat() if member.joined_at else "Unknown"
    created = member.created_at.isoformat()
    roles = ", ".join(r.mention for r in member.roles if r != ctx.guild.default_role) or "None"
    e.add_field(name="ID", value=str(member.id), inline=True)
    e.add_field(name="Joined", value=joined, inline=True)
    e.add_field(name="Created", value=created, inline=True)
    e.add_field(name="Roles", value=roles, inline=False)
    await ctx.respond(embed=e)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Get server (guild) info")
async def serverinfo(ctx):
    g = ctx.guild
    e = discord.Embed(title=g.name, description=g.description or "", timestamp=datetime.utcnow())
    e.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    e.add_field(name="ID", value=str(g.id))
    e.add_field(name="Members", value=str(g.member_count))
    e.add_field(name="Channels", value=str(len(g.channels)))
    e.add_field(name="Roles", value=str(len(g.roles)))
    await ctx.respond(embed=e)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Show avatar")
async def avatar(ctx, member: Option(discord.Member, "Member", required=False, default=None)):
    member = member or ctx.author
    e = discord.Embed(title=f"{member.display_name}'s avatar")
    e.set_image(url=member.display_avatar.url)
    await ctx.respond(embed=e)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Create a quick poll")
async def poll(ctx, question: Option(str, "Poll question"), option1: Option(str, "Option 1"), option2: Option(str, "Option 2"), option3: Option(str, "Option 3", required=False, default=None)):
    description = f"1️⃣ {option1}\n2️⃣ {option2}"
    if option3:
        description += f"\n3️⃣ {option3}"
    e = discord.Embed(title=f"📊 {question}", description=description, timestamp=datetime.utcnow())
    m = await ctx.respond(embed=e)
    # respond returns a message-like InteractionResponse; fetch the message
    sent = await ctx.original_response()
    await sent.add_reaction("1️⃣")
    await sent.add_reaction("2️⃣")
    if option3:
        await sent.add_reaction("3️⃣")

# -------------------------
# SLASH COMMANDS - Fun
# -------------------------
@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Flip a coin")
async def coinflip(ctx):
    await ctx.respond(random.choice(["Heads", "Tails"]))

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Roll dice like 1d6 or 2d10")
async def roll(ctx, dice: Option(str, "Dice format NdM (e.g., 2d6)", required=False, default="1d6")):
    m = re.match(r"(\d+)d(\d+)", dice)
    if not m:
        await ctx.respond("Invalid format. Use NdM, e.g., 2d6", ephemeral=True)
        return
    n = int(m.group(1)); s = int(m.group(2))
    if n > 100 or s > 1000:
        await ctx.respond("Too many dice or sides (limit 100 dice, 1000 sides).", ephemeral=True)
        return
    rolls = [random.randint(1, s) for _ in range(n)]
    await ctx.respond(f"🎲 Rolls: {rolls} Total: {sum(rolls)}")

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="8ball answers your question")
async def _8ball(ctx, question: Option(str, "Your question")):
    answers = [
        "It is certain.", "Without a doubt.", "You may rely on it.",
        "Ask again later.", "Better not tell you now.", "My reply is no.",
        "Very doubtful.", "Signs point to yes."
    ]
    await ctx.respond(f"🎱 {random.choice(answers)}")

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Bonk someone (fun embed with avatar)")
async def bonk(ctx, member: Option(discord.Member, "Member to bonk", required=False, default=None)):
    target = member or ctx.author
    e = discord.Embed(title=f"{target.display_name} got bonked!", color=discord.Color.blurple())
    e.set_image(url=target.display_avatar.url)
    await ctx.respond(embed=e)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Wanted poster (fun)")
async def wanted(ctx, member: Option(discord.Member, "Member", required=False, default=None)):
    target = member or ctx.author
    e = discord.Embed(title="WANTED", description=f"{target.display_name}\nReward: ?\nCrime: Too cool", color=discord.Color.red())
    e.set_thumbnail(url=target.display_avatar.url)
    await ctx.respond(embed=e)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Meme placeholder (no external fetch)")
async def meme(ctx):
    # placeholder images — replace or hook to an API (reddit/imgflip) if you want (careful: requires requests and proper caching)
    samples = [
        "https://i.ibb.co/jC3LYH9/noFilter.png",
        "https://i.imgur.com/0rVeh9W.jpg",
        "https://i.imgur.com/3GvwNBf.png"
    ]
    url = random.choice(samples)
    e = discord.Embed(title="meme", timestamp=datetime.utcnow())
    e.set_image(url=url)
    await ctx.respond(embed=e)

# -------------------------
# SLASH COMMANDS - Multi-tool (safe)
# -------------------------
@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   default_member_permissions=Permissions(manage_guild=True),
                   description="Simple TCP connect test to see if a port is open (safe, single-port)")
async def checkport(
    ctx,
    host: Option(str, "Hostname or IP"),
    port: Option(int, "Port number", required=False, default=80),
    timeout: Option(int, "Timeout seconds", required=False, default=3)
):
    # NOTE: This attempts a single TCP connection. Do not use for scanning networks you don't own.
    await ctx.respond(f"Checking {host}:{port} ...", ephemeral=True)
    try:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, lambda: _tcp_connect(host, port, timeout))
        ok = await asyncio.wait_for(fut, timeout + 1)
        await ctx.followup.send(f"{host}:{port} is {'open' if ok else 'closed/unreachable'}.")
    except Exception as e:
        await ctx.followup.send(f"Error: `{e}`")

def _tcp_connect(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Resolve a hostname to IP (safe DNS lookup)")
async def dnslookup(ctx, hostname: Option(str, "Hostname to resolve")):
    await ctx.respond(f"Resolving {hostname} ...", ephemeral=True)
    try:
        ips = socket.gethostbyname_ex(hostname)[2]
        await ctx.followup.send(f"{hostname} -> {', '.join(ips)}")
    except Exception as e:
        await ctx.followup.send(f"Failed to resolve: `{e}`")

# -------------------------
# ADMIN / CONFIG (owner only)
# -------------------------
@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Configure some bot settings (bot owner only)")
async def setconfig(
    ctx,
    key: Option(str, "Config key (muted_role_name, mod_role_id, anti_link)", required=True),
    value: Option(str, "Value", required=True)
):
    # Only allow bot owner to run
    app_info = await bot.application_info()
    if ctx.author.id != app_info.owner.id:
        await ctx.respond("Only the bot owner may run this command.", ephemeral=True)
        return
    cfg = read_json(CONFIG_FILE)
    # support a few keys
    if key not in ("muted_role_name", "mod_role_id", "anti_link", "guild_test_id"):
        await ctx.respond("Unknown key. Allowed: muted_role_name, mod_role_id, anti_link, guild_test_id", ephemeral=True)
        return
    # coerce types
    if key in ("mod_role_id", "guild_test_id"):
        if value.lower() in ("none", "null", ""):
            cfg[key] = None
        else:
            cfg[key] = int(value)
    elif key == "anti_link":
        cfg[key] = value.lower() in ("1", "true", "on", "yes")
    else:
        cfg[key] = value
    write_json(CONFIG_FILE, cfg)
    await ctx.respond(f"Config updated: `{key}` = `{value}`", ephemeral=True)

@bot.slash_command(guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None,
                   description="Sync commands to guild (owner only, useful during dev)")
async def sync(ctx):
    app_info = await bot.application_info()
    if ctx.author.id != app_info.owner.id:
        await ctx.respond("Only the bot owner may run this.", ephemeral=True)
        return
    try:
        await bot.sync_commands(guild=discord.Object(id=TEST_GUILD_ID) if TEST_GUILD_ID else None)
        await ctx.respond("Commands synced.", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Sync failed: `{e}`", ephemeral=True)

# -------------------------
# ERROR HANDLING
# -------------------------
@bot.event
async def on_error(event_method, *args, **kwargs):
    print(f"[ERROR] Event {event_method} raised an exception", flush=True)

@bot.event
async def on_application_command_error(ctx, error):
    # nicer feedback for users
    if isinstance(error, discord.Forbidden):
        await ctx.respond("I lack permission to perform that action.", ephemeral=True)
    elif isinstance(error, discord.NotFound):
        await ctx.respond("Target not found.", ephemeral=True)
    else:
        # log server-side, keep ephemeral message short
        print("Command error:", error)
        await ctx.respond(f"An error occurred: `{type(error).__name__}`", ephemeral=True)

# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    if TOKEN.startswith("PUT_TOKEN") or not TOKEN:
        print("⚠️ DISCORD_TOKEN not set. Set the DISCORD_TOKEN env var and invite the bot with 'bot' and 'applications.commands' scopes.")
    else:
        bot.run(TOKEN)
