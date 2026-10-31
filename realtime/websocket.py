import asyncio
import json
import logging
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect

from realtime.auth import consume_ticket
from realtime.manager import ConnectionManager
from realtime.protocol import (
    DrawAcceptMessage,
    DrawDeclineMessage,
    DrawOfferMessage,
    MoveMessage,
    PingMessage,
    RequestSyncMessage,
    ResignMessage,
    make_draw_declined,
    make_draw_offer,
    make_error,
    make_player_event,
    make_pong,
    parse_client_message,
)

log = logging.getLogger("L-bot")
MAX_MESSAGE_SIZE = 4096


async def handle_websocket(
    ws: WebSocket,
    ticket: str,
    manager: ConnectionManager,
    chess_service,
) -> None:
    """WebSocket connection lifecycle handler with ticket authentication and message routing."""
    # 1. Ticket validation
    user = consume_ticket(ticket)
    if not user:
        log.warning("WebSocket connection rejected: invalid or expired ticket")
        await ws.close(code=4001, reason="invalid_or_expired_ticket")
        return

    user_id = user["user_id"]
    name = user["name"]

    await ws.accept()
    await manager.connect(ws, user_id, name)

    # 2. Reconnection recovery: if user is in an active/waiting room, link & notify
    try:
        active_room_ids = chess_service.get_rooms_for_player(user_id)
        for room_id in active_room_ids:
            await manager.join_room(user_id, room_id)
            # Notify opponent about reconnection
            await manager.broadcast_to_room(
                room_id,
                make_player_event("player.connected", room_id, user_id),
                exclude_user_id=user_id,
            )
            # Send latest state to reconnected player
            sync_payload = await chess_service.get_sync_state(room_id)
            if sync_payload:
                await manager.send_to_user(user_id, sync_payload)
    except Exception:
        log.error("Reconnection sync error for user %d", user_id, exc_info=True)

    # 3. Message loop
    try:
        while True:
            try:
                # 60s timeout; sends internal ping if idle
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            if len(raw) > MAX_MESSAGE_SIZE:
                await ws.send_json(make_error("message_too_large", "Message exceeds 4KB limit"))
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json(make_error("invalid_json", "Failed to parse JSON message"))
                continue

            msg = parse_client_message(data)
            if msg is None:
                await ws.send_json(
                    make_error("invalid_message", f"Unknown or invalid schema for type: {data.get('type')}")
                )
                continue

            await _route_message(ws, msg, user_id, name, manager, chess_service)

    except WebSocketDisconnect:
        log.info("WebSocket disconnected normally for user %d (%s)", user_id, name)
    except Exception:
        log.error("Unexpected WebSocket exception for user %d", user_id, exc_info=True)
    finally:
        # Cleanup connection and notify rooms
        room_id = await manager.disconnect(user_id, ws)
        if room_id:
            await manager.broadcast_to_room(
                room_id,
                make_player_event("player.disconnected", room_id, user_id),
                exclude_user_id=user_id,
            )


async def _route_message(ws, msg, user_id: int, name: str, manager, chess_service) -> None:
    if isinstance(msg, PingMessage):
        await ws.send_json(make_pong())
        return

    if isinstance(msg, MoveMessage):
        await _handle_move(ws, msg, user_id, manager, chess_service)
    elif isinstance(msg, ResignMessage):
        await _handle_resign(ws, msg, user_id, manager, chess_service)
    elif isinstance(msg, DrawOfferMessage):
        await _handle_draw_offer(ws, msg, user_id, manager, chess_service)
    elif isinstance(msg, DrawAcceptMessage):
        await _handle_draw_accept(ws, msg, user_id, manager, chess_service)
    elif isinstance(msg, DrawDeclineMessage):
        await _handle_draw_decline(ws, msg, user_id, manager, chess_service)
    elif isinstance(msg, RequestSyncMessage):
        await _handle_sync(ws, msg, user_id, manager, chess_service)


async def _handle_move(ws, msg: MoveMessage, user_id: int, manager, chess_service) -> None:
    try:
        res = await chess_service.make_move(
            room_id=msg.game_id,
            player_id=user_id,
            uci=msg.payload.uci,
            move_id=msg.move_id,
            expected_seq=msg.expected_seq,
        )
        if "error" in res:
            await ws.send_json(make_error(res["error"], res.get("message", "Move rejected")))
            return

        # Broadcast the human move before asking the AI to respond.
        await manager.send_to_room(msg.game_id, res["state_message"])

        if res.get("finished"):
            if res.get("finished_message"):
                await manager.send_to_room(msg.game_id, res["finished_message"])
            return

        # A bot reply is generated only after a successful human move and only
        # when the authoritative board says it is the bot's turn.
        room = chess_service.get_room(msg.game_id)
        if room and room.is_bot_game:
            bot_res = await chess_service.make_bot_move(msg.game_id)
            if "error" in bot_res:
                log.error(
                    "Bot move failed in room %s: %s",
                    msg.game_id,
                    bot_res.get("error"),
                )
                await ws.send_json(
                    make_error("bot_move_failed", "حرکت ربات انجام نشد؛ وضعیت بازی را دوباره همگام‌سازی کنید.")
                )
                return

            if bot_res.get("state_message"):
                await manager.send_to_room(msg.game_id, bot_res["state_message"])
            if bot_res.get("finished") and bot_res.get("finished_message"):
                await manager.send_to_room(msg.game_id, bot_res["finished_message"])
    except Exception:
        log.error("Error executing move in room %s", msg.game_id, exc_info=True)
        await ws.send_json(make_error("internal_error", "Server error processing move"))


async def _handle_resign(ws, msg: ResignMessage, user_id: int, manager, chess_service) -> None:
    try:
        res = await chess_service.resign(msg.game_id, user_id)
        if "error" in res:
            await ws.send_json(make_error(res["error"], res.get("message", "Resignation rejected")))
            return
        await manager.send_to_room(msg.game_id, res["state_message"])
        await manager.send_to_room(msg.game_id, res["finished_message"])
    except Exception:
        log.error("Error handling resignation in room %s", msg.game_id, exc_info=True)
        await ws.send_json(make_error("internal_error", "Server error"))


async def _handle_draw_offer(ws, msg: DrawOfferMessage, user_id: int, manager, chess_service) -> None:
    try:
        res = await chess_service.offer_draw(msg.game_id, user_id)
        if "error" in res:
            await ws.send_json(make_error(res["error"], res.get("message", "")))
            return
        await manager.broadcast_to_room(
            msg.game_id,
            make_draw_offer(msg.game_id, user_id),
            exclude_user_id=user_id,
        )
    except Exception:
        log.error("Error handling draw offer in room %s", msg.game_id, exc_info=True)
        await ws.send_json(make_error("internal_error", "Server error"))


async def _handle_draw_accept(ws, msg: DrawAcceptMessage, user_id: int, manager, chess_service) -> None:
    try:
        res = await chess_service.accept_draw(msg.game_id, user_id)
        if "error" in res:
            await ws.send_json(make_error(res["error"], res.get("message", "")))
            return
        await manager.send_to_room(msg.game_id, res["state_message"])
        await manager.send_to_room(msg.game_id, res["finished_message"])
    except Exception:
        log.error("Error handling draw accept in room %s", msg.game_id, exc_info=True)
        await ws.send_json(make_error("internal_error", "Server error"))


async def _handle_draw_decline(ws, msg: DrawDeclineMessage, user_id: int, manager, chess_service) -> None:
    try:
        res = await chess_service.decline_draw(msg.game_id, user_id)
        if "error" in res:
            await ws.send_json(make_error(res["error"], res.get("message", "")))
            return
        await manager.broadcast_to_room(
            msg.game_id,
            make_draw_declined(msg.game_id, user_id),
            exclude_user_id=user_id,
        )
    except Exception:
        log.error("Error handling draw decline in room %s", msg.game_id, exc_info=True)
        await ws.send_json(make_error("internal_error", "Server error"))


async def _handle_sync(ws, msg: RequestSyncMessage, user_id: int, manager, chess_service) -> None:
    try:
        sync_payload = await chess_service.get_sync_state(msg.game_id)
        if not sync_payload:
            await ws.send_json(make_error("room_not_found", "Room not found"))
            return

        # A sync request also subscribes the authenticated player to the room.
        # This is needed when the WebSocket was opened on the lobby screen,
        # before the player had a room_id.
        room = chess_service.get_room(msg.game_id)
        if room is None:
            await ws.send_json(make_error("room_not_found", "Room not found"))
            return
        if user_id not in (room.player1_id, room.player2_id):
            await ws.send_json(make_error("unauthorized", "You are not a player in this room"))
            return

        await manager.join_room(user_id, msg.game_id)
        await ws.send_json(sync_payload)
    except Exception:
        log.error("Error handling request sync in room %s", msg.game_id, exc_info=True)
        await ws.send_json(make_error("internal_error", "Server error"))
