# made by sage
import os
import json
import re
import random
import asyncio
from datetime import datetime, timedelta

import discord
from discord import Option, Permissions
from discord.ext import tasks

# ==== CONFIG FILES & DEFAULTS ====

TOKEN = os.getenv("DISCORD_TOKEN") or "PUT_YOUR_BOT_TOKEN_HERE"
CONFIG_FILE = "config.json"
DATA_FILE = "data.json"

DEFAULT_CONFIG = {
    "guild_test_id": None,  # Set your dev guild id here (int) or None for global
    "mod_role_id": None,    # Optional mod role id
    "anti_link": False,
    "anti_raid": {
        "enabled": False,
        "join_limit": 5,
        "window_seconds": 60
    }
}

DEFAULT_DATA = {
    "warnings": {},
    "muted": {},
    "join_log": {}
}

def ensure_file(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4)

def load_json(path, default):
    ensure_file(path, default)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=4)

config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
data = load_json(DATA_FILE, DEFAULT_DATA)

TEST_GUILD_ID = config.get("guild_test_id")
MOD_ROLE_ID = config.get("mod_role_id")

# ==== BOT SETUP ====

intents = discord.Intents.all()
intents.message_content = True
bot = discord.Bot(intents=intents)

# ==== HELPERS ====

def is_mod(member: discord.Member):
    if MOD_ROLE_ID:
        return any(r.id == int(MOD_ROLE_ID) for r in member.roles)
    return member.guild_permissions.kick_members or member.guild_permissions.ban_members or member.guild_permissions.manage_messages

def add_warning(guild_id, user_id, reason, mod_name):
    global data
    guild_warns = data.setdefault("warnings", {}).setdefault(str(guild_id), {}).setdefault(str(user_id), [])
    guild_warns.append({
        "reason": reason,
        "moderator": mod_name,
        "timestamp": datetime.utcnow().isoformat()
    })
    save_json(DATA_FILE, data)

def get_warnings(guild_id, user_id):
    return data.get("warnings", {}).get(str(guild_id), {}).get(str(user_id), [])

def clear_warnings(guild_id, user_id):
    global data
    guild_warns = data.setdefault("warnings", {}).setdefault(str(guild_id), {})
    if str(user_id) in guild_warns:
        guild_warns[str(user_id)] = []
        save_json(DATA_FILE, data)
        return True
    return False

def record_join(guild_id):
    global data
    log = data.setdefault("join_log", {}).setdefault(str(guild_id), [])
    log.append(datetime.utcnow().isoformat())
    save_json(DATA_FILE, data)

def recent_joins(guild_id, seconds):
    log = data.get("join_log", {}).get(str(guild_id), [])
    cutoff = datetime.utcnow() - timedelta(seconds=seconds)
    return sum(1 for t in log if datetime.fromisoformat(t) >= cutoff)

async def dm_user(user: discord.User, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except Exception:
        pass

async def set_mute_overwrites(guild: discord.Guild, member: discord.Member, mute: bool):
    perms = {
        "send_messages": False,
        "speak": False,
        "add_reactions": False,
    }
    for ch in guild.channels:
        try:
            if mute:
                await ch.set_permissions(member, overwrite=discord.PermissionOverwrite(**perms))
            else:
                await ch.set_permissions(member, overwrite=None)
        except Exception:
            pass

# ==== BACKGROUND TASKS ====

@tasks.loop(seconds=20)
async def check_unmutes():
    global data
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
                unmute_time = datetime.fromisoformat(unmute_iso)
                if datetime.utcnow() >= unmute_time:
                    member = guild.get_member(int(user_id_str))
                    if member:
                        try:
                            await set_mute_overwrites(guild, member, False)
                        except Exception:
                            pass
                    users.pop(user_id_str)
                    changed = True
            except Exception:
                users.pop(user_id_str)
                changed = True
    if changed:
        save_json(DATA_FILE, data)

# ==== EVENTS ====

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    check_unmutes.start()

@bot.event
async def on_member_join(member: discord.Member):
    if config.get("anti_raid", {}).get("enabled", False):
        record_join(member.guild.id)
        limit = config["anti_raid"].get("join_limit", 5)
        window = config["anti_raid"].get("window_seconds", 60)
        if recent_joins(member.guild.id, window) >= limit:
            role = member.guild.default_role
            for ch in member.guild.text_channels:
                try:
                    await ch.set_permissions(role, send_messages=False)
                except Exception:
                    pass
            notify = ""
            if MOD_ROLE_ID:
                mod_role = member.guild.get_role(int(MOD_ROLE_ID))
                if mod_role:
                    notify = mod_role.mention
            channel = next((c for c in member.guild.text_channels if c.permissions_for(member.guild.me).send_messages), None)
            if channel:
                await channel.send(f":rotating_light: Anti-raid triggered — lockdown {notify}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if config.get("anti_link"):
        if re.search(r"https?://\S+|www\.\S+", message.content):
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} Links are disabled here.", delete_after=6)
            except Exception:
                pass
            return
    await bot.process_commands(message)

# ==== MODERATION COMMANDS ====

@bot.slash_command(description="Ban a user", default_member_permissions=Permissions(ban_members=True),
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def ban(
    ctx,
    member: Option(discord.Member, "User to ban"),
    reason: Option(str, "Reason for ban", required=False, default="No reason provided"),
    delete_days: Option(int, "Days of messages to delete (0-7)", required=False, default=0)
):
    if member == ctx.author:
        await ctx.respond("❌ You can't ban yourself.", ephemeral=True)
        return
    try:
        embed = discord.Embed(title="You have been banned", color=discord.Color.red())
        embed.add_field(name="Server", value=ctx.guild.name)
        embed.add_field(name="Reason", value=reason)
        embed.timestamp = datetime.utcnow()
        await dm_user(member, embed)
        await member.ban(reason=reason, delete_message_days=max(0, min(7, delete_days)))
        await ctx.respond(f"✅ {member} banned. Reason: {reason}")
    except Exception as e:
        await ctx.respond(f"Failed to ban: {e}", ephemeral=True)

@bot.slash_command(description="Kick a user", default_member_permissions=Permissions(kick_members=True),
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def kick(
    ctx,
    member: Option(discord.Member, "User to kick"),
    reason: Option(str, "Reason for kick", required=False, default="No reason provided")
):
    if member == ctx.author:
        await ctx.respond("❌ You can't kick yourself.", ephemeral=True)
        return
    try:
        embed = discord.Embed(title="You have been kicked", color=discord.Color.orange())
        embed.add_field(name="Server", value=ctx.guild.name)
        embed.add_field(name="Reason", value=reason)
        embed.timestamp = datetime.utcnow()
        await dm_user(member, embed)
        await member.kick(reason=reason)
        await ctx.respond(f"✅ {member} kicked. Reason: {reason}")
    except Exception as e:
        await ctx.respond(f"Failed to kick: {e}", ephemeral=True)

@bot.slash_command(description="Mute a member", default_member_permissions=Permissions(manage_roles=True),
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def mute(
    ctx,
    member: Option(discord.Member, "Member to mute"),
    minutes: Option(int, "Minutes to mute (0 = indefinite)", required=False, default=0),
    reason: Option(str, "Reason for mute", required=False, default="No reason provided")
):
    if member == ctx.author:
        await ctx.respond("❌ You can't mute yourself.", ephemeral=True)
        return
    try:
        await set_mute_overwrites(ctx.guild, member, True)
    except discord.Forbidden:
        await ctx.respond("❌ I lack permission to update channel permissions.", ephemeral=True)
        return
    global data
    guild_mutes = data.setdefault("muted", {}).setdefault(str(ctx.guild.id), {})
    if minutes > 0:
        unmute_time = datetime.utcnow() + timedelta(minutes=minutes)
        guild_mutes[str(member.id)] = unmute_time.isoformat()
        duration_str = f"{minutes} minute(s)"
    else:
        guild_mutes[str(member.id)] = None
        duration_str = "Indefinite"
    save_json(DATA_FILE, data)
    embed = discord.Embed(title="You have been muted", color=discord.Color.dark_gray())
    embed.add_field(name="Server", value=ctx.guild.name)
    embed.add_field(name="Duration", value=duration_str)
    embed.add_field(name="Reason", value=reason)
    embed.timestamp = datetime.utcnow()
    await dm_user(member, embed)
    await ctx.respond(f"🔇 {member.mention} muted for {duration_str}. Reason: {reason}")

@bot.slash_command(description="Unmute a member", default_member_permissions=Permissions(manage_roles=True),
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def unmute(ctx, member: Option(discord.Member, "Member to unmute")):
    global data
    muted_users = data.get("muted", {}).get(str(ctx.guild.id), {})
    if str(member.id) not in muted_users:
        await ctx.respond(f"❌ {member.mention} is not muted.", ephemeral=True)
        return
    try:
        await set_mute_overwrites(ctx.guild, member, False)
    except discord.Forbidden:
        await ctx.respond("❌ I lack permission to update channel permissions.", ephemeral=True)
        return
    muted_users.pop(str(member.id))
    save_json(DATA_FILE, data)
    embed = discord.Embed(title="You have been unmuted", color=discord.Color.green())
    embed.add_field(name="Server", value=ctx.guild.name)
    embed.timestamp = datetime.utcnow()
    await dm_user(member, embed)
    await ctx.respond(f"🔊 {member.mention} has been unmuted.")

@bot.slash_command(description="Warn a member", default_member_permissions=Permissions(manage_messages=True),
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def warn(
    ctx,
    member: Option(discord.Member, "Member to warn"),
    reason: Option(str, "Reason for warning", required=False, default="No reason provided")
):
    add_warning(ctx.guild.id, member.id, reason, str(ctx.author))
    embed = discord.Embed(title="You have been warned", color=discord.Color.orange())
    embed.add_field(name="Server", value=ctx.guild.name)
    embed.add_field(name="Reason", value=reason)
    embed.timestamp = datetime.utcnow()
    await dm_user(member, embed)
    await ctx.respond(f"⚠️ {member.mention} has been warned. Reason: {reason}")

@bot.slash_command(description="View warnings", default_member_permissions=Permissions(manage_messages=True),
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def warnings(ctx, member: Option(discord.Member, "Member to view warnings for")):
    warns = get_warnings(ctx.guild.id, member.id)
    if not warns:
        await ctx.respond(f"{member.mention} has no warnings.", ephemeral=True)
        return
    e = discord.Embed(title=f"Warnings for {member}", color=discord.Color.orange())
    for i, w in enumerate(warns, start=1):
        e.add_field(name=f"#{i}", value=f"{w['reason']} — by {w['moderator']} at {w['timestamp']}", inline=False)
    await ctx.respond(embed=e)

@bot.slash_command(description="Clear warnings", default_member_permissions=Permissions(manage_messages=True),
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def clearwarns(ctx, member: Option(discord.Member, "Member to clear warnings for")):
    ok = clear_warnings(ctx.guild.id, member.id)
    if ok:
        await ctx.respond(f"✅ Cleared warnings for {member.mention}")
    else:
        await ctx.respond(f"No warnings for {member.mention}", ephemeral=True)

# ==== UTILS & FUN COMMANDS ====

@bot.slash_command(description="Show user info",
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def userinfo(ctx, member: Option(discord.Member, "User to inspect", required=False, default=None)):
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

@bot.slash_command(description="Show server info",
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def serverinfo(ctx):
    g = ctx.guild
    e = discord.Embed(title=g.name, description=g.description or "", timestamp=datetime.utcnow())
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    e.add_field(name="ID", value=str(g.id))
    e.add_field(name="Members", value=str(g.member_count))
    e.add_field(name="Channels", value=str(len(g.channels)))
    e.add_field(name="Roles", value=str(len(g.roles)))
    await ctx.respond(embed=e)

@bot.slash_command(description="Show a user's avatar",
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def avatar(ctx, member: Option(discord.Member, "User", required=False, default=None)):
    member = member or ctx.author
    e = discord.Embed(title=f"{member.display_name}'s avatar")
    e.set_image(url=member.display_avatar.url)
    await ctx.respond(embed=e)

@bot.slash_command(description="Flip a coin",
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def coinflip(ctx):
    await ctx.respond(random.choice(["Heads", "Tails"]))

@bot.slash_command(description="Roll dice (e.g. 1d6 or 2d10)",
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def roll(ctx, dice: Option(str, "Dice format NdM, e.g. 2d6")):
    try:
        match = re.fullmatch(r"(\d+)d(\d+)", dice.lower())
        if not match:
            await ctx.respond("Invalid dice format. Use NdM, e.g. 2d6.", ephemeral=True)
            return
        n, m = int(match.group(1)), int(match.group(2))
        if n > 50 or m > 1000:
            await ctx.respond("Too many dice or sides.", ephemeral=True)
            return
        rolls = [random.randint(1, m) for _ in range(n)]
        total = sum(rolls)
        await ctx.respond(f"🎲 You rolled: {rolls} (Total: {total})")
    except Exception as e:
        await ctx.respond(f"Error: {e}", ephemeral=True)

@bot.slash_command(description="Create a simple poll",
                   guild_ids=[TEST_GUILD_ID] if TEST_GUILD_ID else None)
async def poll(ctx, question: Option(str, "Poll question")):
    embed = discord.Embed(title="Poll", description=question, color=discord.Color.blue(), timestamp=datetime.utcnow())
    message = await ctx.respond(embed=embed, fetch_response=True)
    # Add reactions for yes/no
    await message.add_reaction("👍")
    await message.add_reaction("👎")

# ==== RUN ====

bot.run(TOKEN)
