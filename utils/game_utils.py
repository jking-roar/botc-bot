"""Utilities for game management, extracted to avoid circular imports."""

import asyncio
import os
import time
from typing import Optional

import dill
import discord

import global_vars

BACKUP_INTERVAL_SECONDS = 20
_backup_task: Optional[asyncio.Task] = None
_pending_event = asyncio.Event()
_pending_requests = 0
_last_backup_reason: Optional[str] = None
_last_backup_time = 0.0


async def update_presence(client):
    """Updates Discord Presence based on the current game state.
    
    Args:
        client: The Discord client
    """
    from model.game.whisper_mode import WhisperMode

    # Skip actual presence updates during tests
    if not hasattr(client, 'ws') or client.ws is None:
        return
        
    if global_vars.game is None or not hasattr(global_vars, 'game') or global_vars.game.seatingOrder == []:
        await client.change_presence(
            status=discord.Status.dnd, activity=discord.Game(name="No ongoing game!")
        )
    elif not global_vars.game.isDay:
        await client.change_presence(
            status=discord.Status.idle, activity=discord.Game(name="It's nighttime!")
        )
    else:
        clopen = ["Closed", "Open"]

        whisper_state = "to " + global_vars.game.whisper_mode if global_vars.game.days[
                                                                     -1].isPms and global_vars.game.whisper_mode != WhisperMode.ALL else \
            clopen[
                global_vars.game.days[-1].isPms]
        status = "PMs {}, Nominations {}!".format(whisper_state, clopen[global_vars.game.days[-1].isNoms])
        await client.change_presence(
            status=discord.Status.online,
            activity=discord.Game(
                name=status
            ),
        )


def remove_backup(fileName):
    """Removes a backup file and its associated object files.
    
    Args:
        fileName: The name of the backup file
    """
    if os.path.exists(fileName):
        os.remove(fileName)

    for obj in [
        x
        for x in dir(global_vars.game)
        if not x.startswith("__") and not callable(getattr(global_vars.game, x))
    ]:
        obj_file = obj + "_" + fileName
        if os.path.exists(obj_file):
            os.remove(obj_file)


def backup(fileName):
    """
    Backs up the game-state.
    
    Args:
        fileName: The name of the backup file
    """
    from model.game.game import NULL_GAME

    if not global_vars.game or global_vars.game is NULL_GAME:
        return

    objects = [
        x
        for x in dir(global_vars.game)
        if not x.startswith("__") and not callable(getattr(global_vars.game, x))
    ]
    with open(fileName, "wb") as file:
        dill.dump(objects, file)

    for obj in objects:
        with open(obj + "_" + fileName, "wb") as file:
            if obj == "seatingOrderMessage":
                dill.dump(getattr(global_vars.game, obj).id, file)
            elif obj == "info_channel_seating_order_message":
                msg = getattr(global_vars.game, obj)
                if msg is not None:
                    dill.dump(msg.id, file)
                else:
                    dill.dump(None, file)
            else:
                dill.dump(getattr(global_vars.game, obj), file)


async def load(fileName):
    """
    Loads the game-state.
    
    Args:
        fileName: The name of the backup file
        
    Returns:
        The loaded game object
    """
    from model.game.game import Game
    from model.game.script import Script

    with open(fileName, "rb") as file:
        objects = dill.load(file)

    game = Game([], None, None, Script([]))
    for obj in objects:
        if not os.path.isfile(obj + "_" + fileName):
            print("Incomplete backup found.")
            return None
        with open(obj + "_" + fileName, "rb") as file:
            if obj == "seatingOrderMessage":
                id = dill.load(file)
                msg = await global_vars.channel.fetch_message(id)
                setattr(game, obj, msg)
            elif obj == "info_channel_seating_order_message":
                id = dill.load(file)
                if id is not None and global_vars.info_channel:
                    try:
                        msg = await global_vars.info_channel.fetch_message(id)
                        setattr(game, obj, msg)
                    except:
                        # Message may have been deleted or channel may not exist
                        setattr(game, obj, None)
                else:
                    setattr(game, obj, None)
            else:
                setattr(game, obj, dill.load(file))

    return game


async def request_backup(reason: str):
    """Queue a backup request and return immediately.

    Args:
        reason: Description of why the backup was requested.
    """
    global _pending_requests, _last_backup_reason
    _pending_requests += 1
    _last_backup_reason = reason
    _pending_event.set()


async def _periodic_backup_loop():
    global _last_backup_time, _pending_requests, _last_backup_reason
    try:
        while True:
            await _pending_event.wait()
            now = time.monotonic()
            wait_time = max(0, BACKUP_INTERVAL_SECONDS - (now - _last_backup_time))
            await asyncio.sleep(wait_time)
            if _pending_requests:
                try:
                    backup("current_game.pckl")
                except Exception as exc:
                    print(f"Error during periodic backup ({_last_backup_reason or 'unspecified'}): {exc}")
                _last_backup_time = time.monotonic()
                _pending_requests = 0
                _last_backup_reason = None
            _pending_event.clear()
    except asyncio.CancelledError:
        _pending_event.clear()
        raise


async def schedule_periodic_backup():
    """Start the periodic backup task if it is not already running."""
    global _backup_task
    if _backup_task and not _backup_task.done():
        return _backup_task
    loop = asyncio.get_running_loop()
    _backup_task = loop.create_task(_periodic_backup_loop())
    return _backup_task


async def stop_periodic_backup():
    """Cancel the periodic backup task and clear pending requests."""
    global _backup_task, _last_backup_time
    if _backup_task:
        _backup_task.cancel()
        try:
            await _backup_task
        except asyncio.CancelledError:
            pass
        _backup_task = None
    _pending_requests = 0
    _last_backup_reason = None
    _pending_event.clear()
    _last_backup_time = 0.0
