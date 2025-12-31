"""Utilities for game management, extracted to avoid circular imports."""

import asyncio
import os
import time
from typing import Optional
import weakref

import dill
import discord

import global_vars

BACKUP_INTERVAL_SECONDS = 20
_backup_task: Optional[asyncio.Task] = None
# Create the pending event lazily so it's bound to the active event loop.
_pending_event: Optional[asyncio.Event] = None
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
    global _pending_requests, _last_backup_reason, _pending_event
    # If there's no running loop, just increment the counter and return.
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        _pending_requests += 1
        _last_backup_reason = reason
        return

    # Ensure there's an event bound to the current loop. Create one if needed.
    if _pending_event is None:
        _pending_event = asyncio.Event()

    _pending_requests += 1
    _last_backup_reason = reason

    # Try setting the event. If its loop is closed this will raise
    # RuntimeError; recreate the event on the current loop and set it.
    try:
        _pending_event.set()
    except RuntimeError:
        _pending_event = asyncio.Event()
        _pending_event.set()


async def _periodic_backup_loop():
    global _last_backup_time, _pending_requests, _last_backup_reason, _pending_event
    try:
        while True:
            # Ensure there's an event to wait on for this loop.
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                return

            if _pending_event is None:
                _pending_event = asyncio.Event()

            try:
                await _pending_event.wait()
            except RuntimeError:
                return
            now = time.monotonic()
            wait_time = max(0, BACKUP_INTERVAL_SECONDS - (now - _last_backup_time))
            try:
                await asyncio.sleep(wait_time)
            except RuntimeError:
                return
            if _pending_requests:
                try:
                    backup("current_game.pckl")
                except Exception as exc:
                    print(f"Error during periodic backup ({_last_backup_reason or 'unspecified'}): {exc}")
                _last_backup_time = time.monotonic()
                _pending_requests = 0
                _last_backup_reason = None
            try:
                _pending_event.clear()
            except RuntimeError:
                return
    except asyncio.CancelledError:
        try:
            _pending_event.clear()
        except Exception:
            pass
        raise


async def schedule_periodic_backup():
    """Start the periodic backup task if it is not already running."""
    global _backup_task
    if _backup_task and not _backup_task.done():
        return _backup_task
    # Ensure the pending event exists on this running loop before starting.
    global _pending_event
    loop = asyncio.get_running_loop()
    try:
        if _pending_event is None:
            _pending_event = asyncio.Event()
        else:
            try:
                ev_loop = _pending_event.get_loop()
                if ev_loop.is_closed():
                    _pending_event = asyncio.Event()
            except Exception:
                _pending_event = asyncio.Event()
    except RuntimeError:
        # If no running loop (shouldn't happen here), fallback to creating
        # the task which will create/await the event as needed.
        pass

    _backup_task = loop.create_task(_periodic_backup_loop())
    # Suppress the 'Task was destroyed but it is pending!' message on
    # interpreter shutdown if the task is still pending. This sets a
    # private flag available on CPython's asyncio.Task implementation.
    try:
        if hasattr(_backup_task, "_log_destroy_pending"):
            setattr(_backup_task, "_log_destroy_pending", False)
    except Exception:
        pass
    # Ensure the task is cancelled when it is about to be garbage-collected
    # (e.g. during interpreter shutdown). Use weakref.finalize so we attempt
    # to cancel the task and suppress the destroy warning before it's
    # destroyed.
    def _finalize_cancel(task):
        try:
            # Try to disable the destroy warning flag again.
            if hasattr(task, "_log_destroy_pending"):
                try:
                    setattr(task, "_log_destroy_pending", False)
                except Exception:
                    pass
            loop = task.get_loop()
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except Exception:
                    try:
                        task.cancel()
                    except Exception:
                        pass
        except Exception:
            try:
                task.cancel()
            except Exception:
                pass

    try:
        weakref.finalize(_backup_task, _finalize_cancel, _backup_task)
    except Exception:
        pass
    return _backup_task


async def stop_periodic_backup():
    """Cancel the periodic backup task and clear pending requests."""
    global _backup_task, _last_backup_time, _pending_event
    if _backup_task:
        # If the task's loop is already closed (e.g. from a previous test run),
        # calling cancel() will try to schedule callbacks on a closed loop and
        # raise RuntimeError. Detect that and avoid canceling in that case.
        try:
            task_loop = _backup_task.get_loop()
            if task_loop.is_closed():
                # Drop the reference without attempting to cancel.
                _backup_task = None
            else:
                if not _backup_task.done():
                    _backup_task.cancel()
                try:
                    await _backup_task
                except asyncio.CancelledError:
                    pass
                _backup_task = None
        except Exception:
            # Defensive fallback: attempt to cancel but ignore errors.
            try:
                if not _backup_task.done():
                    _backup_task.cancel()
            except Exception:
                pass
            _backup_task = None
    _pending_requests = 0
    _last_backup_reason = None
    # Clear or drop the pending event depending on whether its loop is open.
    try:
        if _pending_event is not None:
            try:
                ev_loop = _pending_event.get_loop()
                if ev_loop.is_closed():
                    _pending_event = None
                else:
                    # If we're on the same loop, clear directly; otherwise
                    # schedule a thread-safe clear on the event's loop.
                    try:
                        current_loop = asyncio.get_running_loop()
                        if ev_loop is current_loop:
                            _pending_event.clear()
                        else:
                            ev_loop.call_soon_threadsafe(_pending_event.clear)
                    except RuntimeError:
                        # No running loop here; schedule clear on event loop.
                        ev_loop.call_soon_threadsafe(_pending_event.clear)
            except Exception:
                # In case of any exception, just set to None to avoid
                # keeping a reference to a closed loop.
                _pending_event = None
    except Exception:
        pass
