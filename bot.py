import os
import json
import re
import random
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

# -------------------------
# CONFIG / FILE NAMES / lol
# -------------------------
TOKEN = os.getenv("DISCORD_TOKEN") or "PUT_TOKEN_HERE_BUT_USE_ENV_PLS"  # don't commit token! xd
CONFIG_FILE = "config.json"
DATA_FILE = "data.json"

# -------------------------
# JSON helpers (simple & safe)
# -------------------------
def ensure_file(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

def read_json(path):
    ensure_file(path, {})
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# default config/data (will be created automatically)
ensure_file(CONFIG_FILE, {
    "prefix": "!",
    "mod_role_id": None,
    "anti_link": False,
    "anti_raid": {"enabled": False, "join_limit": 5, "window_seconds": 60}
})
ensure_file(DATA_FILE, {
    "warnings": {},    # guild_id -> user_id -> [warns]
    "muted": {},       # guild_id -> user_id -> unmute_iso_or_null
    "join_log": {}     # guild_id -> [iso timestamps]
})

# -------------------------
# Bot setup
# -------------------------
cfg = read_json(CONFIG_FILE)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=cfg.get("prefix", "!"), intents=intents, help_command=None)

# -------------------------
# Utilities (tiny helpers)
# -------------------------
def is_admin(member: discord.Member):
    return member.guild_permissions.administrator

def get_mod_role_id():
    c = read_json(CONFIG_FILE)
    return c.get("mod_role_id")

def mod_check():
    async def predicate(ctx):
        cfg = read_json(CONFIG_FILE)
        mod_role_id = cfg.get("mod_role_id")
        if mod_role_id is None:
            await ctx.send("Moderator role not set. Ask an admin to run `!setmodrole @Role` — lol.")
            return False
        if is_admin(ctx.author):
            return True
        # check role membership
        return any(r.id == mod_role_id for r in ctx.author.roles)
    return commands.check(predicate)

async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="Muted")
    if role is None:
        role = await guild.create_role(name="Muted", reason="Created by bot for mutes (xd)")
        # apply channel overwrites
        for ch in guild.channels:
            try:
                await ch.set_permissions(role, send_messages=False, speak=False, add_reactions=False)
            except Exception:
                pass
    return role

# warnings helpers
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

# anti-raid helpers
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
# Events & security xd
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id}) — ready to chaos lol")
    check_unmutes.start()

@bot.event
async def on_member_join(member: discord.Member):
    cfg = read_json(CONFIG_FILE)
    if cfg.get("anti_raid", {}).get("enabled"):
        record_join(member.guild.id)
        c = recent_joins(member.guild.id, cfg["anti_raid"].get("window_seconds", 60))
        if c >= cfg["anti_raid"].get("join_limit", 5):
            # quick lockdown: disable send_messages for @everyone on text channels
            for ch in member.guild.text_channels:
                try:
                    await ch.set_permissions(member.guild.default_role, send_messages=False)
                except Exception:
                    pass
            # notify first channel where bot can speak
            notify = None
            mod_role_id = cfg.get("mod_role_id")
            if mod_role_id:
                notify_role = member.guild.get_role(mod_role_id)
                notify = notify_role.mention if notify_role else ""
            channel = discord.utils.find(lambda m: m.permissions_for(member.guild.me).send_messages, member.guild.text_channels)
            if channel:
                await channel.send(f":rotating_light: Anti-raid triggered — server lockdown {notify} (check logs)")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    cfg = read_json(CONFIG_FILE)
    # anti-link simple check
    if cfg.get("anti_link"):
        if re.search(r"https?://\S+|www\.\S+", message.content):
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} Links are disabled here lol.", delete_after=6)
            except Exception:
                pass
            return

    # Carl-like text aliases example (we support text commands, not full import of Carl)
    # e.g. if user typed "!rank" we could emulate behavior — basic aliases below
    # Let normal commands run too
    await bot.process_commands(message)

# background task for auto-unmute
@tasks.loop(seconds=20.0)
async def check_unmutes():
    changed = False
    data = read_json(DATA_FILE)
    muted = data.get("muted", {})
    for guild_id_str, users in list(muted.items()):
        guild = bot.get_guild(int(guild_id_str))
        if not guild:
            continue
        for user_id_str, unmute_iso in list(users.items()):
            if unmute_iso is None:
                continue
            if datetime.utcnow() >= datetime.fromisoformat(unmute_iso):
                member = guild.get_member(int(user_id_str))
                role = discord.utils.get(guild.roles, name="Muted")
                try:
                    if member and role in member.roles:
                        await member.remove_roles(role, reason="Auto unmute (lol time's up)")
                except Exception:
                    pass
                users.pop(user_id_str, None)
                changed = True
    if changed:
        write_json(DATA_FILE, data)

# -------------------------
# Commands (all in one file, baby)
# -------------------------
# Admin / server config
@bot.command()
@commands.has_permissions(administrator=True)
async def setprefix(ctx, prefix: str):
    cfg = read_json(CONFIG_FILE)
    cfg["prefix"] = prefix
    write_json(CONFIG_FILE, cfg)
    bot.command_prefix = prefix  # note: this won't change already-parsed prefix instances in runtime but ok
    await ctx.send(f"Prefix changed to `{prefix}` — nice.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setmodrole(ctx, role: discord.Role):
    cfg = read_json(CONFIG_FILE)
    cfg["mod_role_id"] = role.id
    write_json(CONFIG_FILE, cfg)
    await ctx.send(f"Moderator role set to {role.mention} — use it wisely xd")

@bot.command()
@commands.has_permissions(administrator=True)
async def antiraid(ctx, mode: str = None, limit: int = 5, window: int = 60):
    cfg = read_json(CONFIG_FILE)
    if mode is None:
        status = cfg.get("anti_raid", {})
        await ctx.send(f"Anti-raid: enabled={status.get('enabled')}, limit={status.get('join_limit')}, window={status.get('window_seconds')}s")
        return
    if mode.lower() in ("on", "enable"):
        cfg["anti_raid"]["enabled"] = True
        cfg["anti_raid"]["join_limit"] = int(limit)
        cfg["anti_raid"]["window_seconds"] = int(window)
        write_json(CONFIG_FILE, cfg)
        await ctx.send(f"Anti-raid enabled (limit {limit} joins / {window}s).")
    elif mode.lower() in ("off", "disable"):
        cfg["anti_raid"]["enabled"] = False
        write_json(CONFIG_FILE, cfg)
        await ctx.send("Anti-raid disabled.")
    else:
        await ctx.send("Usage: `!antiraid on|off [limit] [window_seconds]`")

@bot.command()
@commands.has_permissions(administrator=True)
async def antlink(ctx, toggle: str = None):
    cfg = read_json(CONFIG_FILE)
    if toggle is None:
        await ctx.send(f"Anti-link is {'enabled' if cfg.get('anti_link') else 'disabled'}.")
        return
    cfg["anti_link"] = toggle.lower() in ("on", "enable", "true", "1", "yes")
    write_json(CONFIG_FILE, cfg)
    await ctx.send(f"Anti-link set to {cfg['anti_link']} — links are { 'blocked' if cfg['anti_link'] else 'allowed' } lol")

# Moderation: warn / warnings / clearwarns
@bot.command(name="warn")
@mod_check()
async def _warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    add_warning(ctx.guild.id, member.id, reason, str(ctx.author))
    await ctx.send(f":warning: {member.mention} has been warned. Reason: {reason}")

@bot.command(name="warnings")
@mod_check()
async def _warnings(ctx, member: discord.Member):
    warns = get_warnings(ctx.guild.id, member.id)
    if not warns:
        return await ctx.send(f"{member.mention} has no warnings — clean record :)")
    e = discord.Embed(title=f"Warnings for {member}", color=discord.Color.orange())
    for i, w in enumerate(warns, start=1):
        e.add_field(name=f"#{i}", value=f"{w['reason']} — by {w['moderator']} at {w['timestamp']}", inline=False)
    await ctx.send(embed=e)

@bot.command(name="clearwarns")
@mod_check()
async def _clearwarns(ctx, member: discord.Member):
    ok = clear_warnings(ctx.guild.id, member.id)
    if ok:
        await ctx.send(f"Cleared warnings for {member.mention}.")
    else:
        await ctx.send(f"No warnings found for {member.mention}.")

# Kick / ban
@bot.command()
@mod_check()
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await ctx.send(f"{member} kicked. Reason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed to kick: {e}")

@bot.command()
@mod_check()
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.ban(reason=reason)
        await ctx.send(f"{member} banned. Reason: {reason}")
    except Exception as e:
        await ctx.send(f"Failed to ban: {e}")

# Mute / unmute
@bot.command()
@mod_check()
async def mute(ctx, member: discord.Member, minutes: int = 0, *, reason: str = "No reason provided"):
    role = await ensure_muted_role(ctx.guild)
    try:
        await member.add_roles(role, reason=reason)
        data = read_json(DATA_FILE)
        guild_muted = data.setdefault("muted", {}).setdefault(str(ctx.guild.id), {})
        if minutes > 0:
            guild_muted[str(member.id)] = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()
            await ctx.send(f"{member.mention} muted for {minutes} minute(s). Reason: {reason}")
        else:
            guild_muted[str(member.id)] = None
            await ctx.send(f"{member.mention} muted indefinitely. Reason: {reason}")
        write_json(DATA_FILE, data)
    except Exception as e:
        await ctx.send(f"Failed to mute: {e}")

@bot.command()
@mod_check()
async def unmute(ctx, member: discord.Member):
    role = discord.utils.get(ctx.guild.roles, name="Muted")
    try:
        if role and role in member.roles:
            await member.remove_roles(role, reason="Unmuted by mod")
        data = read_json(DATA_FILE)
        guild_muted = data.setdefault("muted", {}).setdefault(str(ctx.guild.id), {})
        guild_muted.pop(str(member.id), None)
        write_json(DATA_FILE, data)
        await ctx.send(f"{member.mention} unmuted.")
    except Exception as e:
        await ctx.send(f"Failed to unmute: {e}")

# Carl-like aliases (small emulation)
@bot.command(aliases=["roleinfo"])
async def role_info(ctx, role: discord.Role):
    e = discord.Embed(title=f"Role info — {role.name}")
    e.add_field(name="ID", value=str(role.id))
    e.add_field(name="Members", value=str(len(role.members)))
    e.add_field(name="Mentionable", value=str(role.mentionable))
    await ctx.send(embed=e)

# Fun commands
@bot.command()
async def coinflip(ctx):
    await ctx.send(random.choice(["Heads", "Tails"]) + " — gg")

@bot.command(name="8ball")
async def _8ball(ctx, *, question: str):
    answers = [
        "It is certain.", "Without a doubt.", "You may rely on it.",
        "Ask again later.", "Better not tell you now.", "My reply is no.",
        "Very doubtful.", "Signs point to yes."
    ]
    await ctx.send(f"🎱 {random.choice(answers)} — idk lol")

@bot.command()
async def bonk(ctx, member: discord.Member = None):
    target = member or ctx.author
    avatar = target.display_avatar.url
    e = discord.Embed(title=f"{target.display_name} got bonked!", color=discord.Color.blurple())
    e.set_image(url=avatar)
    await ctx.send(embed=e)

@bot.command()
async def wanted(ctx, member: discord.Member = None):
    target = member or ctx.author
    e = discord.Embed(title="WANTED", description=f"{target.display_name}\nReward: ?\nCrime: Too cool", color=discord.Color.red())
    e.set_thumbnail(url=target.display_avatar.url)
    await ctx.send(embed=e)

# say (mod only)
@bot.command()
@mod_check()
async def say(ctx, *, text: str):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await ctx.send(text)

# quick help
@bot.command(name="helpme")
async def helpme(ctx):
    p = read_json(CONFIG_FILE).get("prefix", "!")
    txt = f"""
**Multi-tool Bot (single file)** — prefix `{p}`

Moderation:
`{p}setmodrole @Role` (admin)
`{p}kick @user [reason]` (mod)
`{p}ban @user [reason]` (mod)
`{p}mute @user [minutes] [reason]` (mod)
`{p}unmute @user` (mod)
`{p}warn @user [reason]` (mod)
`{p}warnings @user` (mod)

Security:
`{p}antlink on|off`
`{p}antiraid on|off [limit] [window_seconds]`

Fun:
`{p}coinflip`, `{p}8ball <q>`, `{p}bonk [user]`, `{p}wanted [user]`

Feel free to fork, edit, and add more commands. xd
"""
    await ctx.send(txt)

# -------------------------
# Error handling
# -------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("You don't have permission to run that (lol).")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing args — check usage. xd")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Bad arg type — try mentioning the user/role properly.")
    else:
        # log to console and send short message (avoid exposing tracebacks in server)
        print("Unhandled command error:", error)
        await ctx.send(f"An error occurred: `{type(error).__name__}` — check console.")

# -------------------------
# Run the bot
# -------------------------
if __name__ == "__main__":
    if TOKEN.startswith("PUT_TOKEN") or TOKEN == "":
        print("Warning: DISCORD_TOKEN not set. Set the DISCORD_TOKEN env var on Render or locally.")
    bot.run(TOKEN)
