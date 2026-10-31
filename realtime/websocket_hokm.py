import asyncio
import json
import logging
from typing import Any, Dict, Optional
from fastapi import WebSocket, WebSocketDisconnect

from games.hokm.service import HokmService
from realtime.auth import consume_ticket
from realtime.manager import ConnectionManager

log = logging.getLogger("L-bot")
MAX_MESSAGE_SIZE = 4096


async def handle_hokm_websocket(
    ws: WebSocket,
    ticket: str,
    manager: ConnectionManager,
    hokm_service: HokmService,
) -> None:
    """WebSocket connection lifecycle handler for Hokm multiplayer matches."""
    # 1. Ticket validation
    user = consume_ticket(ticket)
    if not user:
        log.warning("Hokm WS rejected: invalid or expired ticket")
        await ws.close(code=4001, reason="invalid_or_expired_ticket")
        return

    user_id = user["user_id"]
    name = user["name"]

    await ws.accept()
    await manager.connect(ws, user_id, name)

    # 2. Check if user belongs to an active room (reconnection)
    try:
        current_room_id = hokm_service._user_to_room.get(user_id)
        if current_room_id and current_room_id in hokm_service.rooms:
            room = hokm_service.rooms[current_room_id]
            await manager.join_room(user_id, current_room_id)
            # Mark reconnected
            for p in room.engine.players:
                if p.user_id == user_id:
                    p.is_connected = True
                    p.disconnect_time = None
                    break
            # Send current state
            await hokm_service._broadcast_room_state(room)
    except Exception:
        log.error("Hokm reconnection sync error for user %d", user_id, exc_info=True)

    # 3. Event Loop
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            if len(raw) > MAX_MESSAGE_SIZE:
                await ws.send_json({"type": "error", "error": "message_too_large"})
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "invalid_json"})
                continue

            if not isinstance(data, dict):
                await ws.send_json({"type": "error", "error": "invalid_message"})
                continue

            msg_type = data.get("type")
            payload = data.get("payload", {})
            if not isinstance(payload, dict):
                await ws.send_json({"type": "error", "error": "invalid_payload"})
                continue

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
                continue

            elif msg_type == "hokm.solo_ai.start":
                mode = payload.get("mode", "hokm_4p")
                diff = payload.get("difficulty", "medium")
                time_limit = int(payload.get("turn_time_limit", 30))

                room = await hokm_service.create_solo_ai_game(
                    host_id=user_id,
                    host_name=name,
                    mode_str=mode,
                    difficulty=diff,
                    turn_time_limit=time_limit,
                )
                await manager.join_room(user_id, room.room_id)
                await ws.send_json({
                    "type": "hokm.room.created",
                    "room_id": room.room_id,
                    "invite_code": room.invite_code,
                    "game_mode": room.game_mode.value,
                })
                await hokm_service._broadcast_room_state(room)

            elif msg_type == "hokm.room.create":
                mode = payload.get("mode", "hokm_4p")
                time_limit = int(payload.get("turn_time_limit", 30))
                is_ranked = bool(payload.get("is_ranked", False))
                allow_spec = bool(payload.get("allow_spectators", True))

                room = await hokm_service.create_room(
                    host_id=user_id,
                    host_name=name,
                    mode_str=mode,
                    turn_time_limit=time_limit,
                    is_ranked=is_ranked,
                    allow_spectators=allow_spec,
                )
                await manager.join_room(user_id, room.room_id)
                await ws.send_json({
                    "type": "hokm.room.created",
                    "room_id": room.room_id,
                    "invite_code": room.invite_code,
                    "game_mode": room.game_mode.value,
                })
                await hokm_service._broadcast_room_state(room)

            elif msg_type == "hokm.room.join":
                code_or_id = payload.get("room_id_or_code", "").strip()
                ok, msg, room = await hokm_service.join_room(user_id, name, code_or_id)
                if not ok or not room:
                    await ws.send_json({"type": "error", "error": msg})
                else:
                    await manager.join_room(user_id, room.room_id)
                    await ws.send_json({
                        "type": "hokm.room.joined",
                        "room_id": room.room_id,
                        "invite_code": room.invite_code,
                        "message": msg,
                    })
                    await hokm_service._broadcast_room_state(room)

            elif msg_type == "hokm.room.leave":
                room_id = str(payload.get("room_id") or "").strip()
                if not room_id:
                    await ws.send_json({"type": "error", "error": "شناسه اتاق الزامی است."})
                    continue
                ok, message = await hokm_service.leave_room(room_id, user_id)
                if not ok:
                    await ws.send_json({"type": "error", "error": message})
                else:
                    await ws.send_json({
                        "type": "hokm.room.left",
                        "room_id": room_id,
                        "message": message,
                    })

            elif msg_type == "hokm.match.rematch":
                room_id = str(payload.get("room_id") or "").strip()
                if not room_id:
                    await ws.send_json({"type": "error", "error": "شناسه مسابقه الزامی است."})
                    continue
                ok, message, new_room = await hokm_service.request_rematch(room_id, user_id)
                if new_room is not None:
                    await ws.send_json({
                        "type": "hokm.match.rematch.started",
                        "room_id": new_room.room_id,
                        "message": message,
                    })
                elif not ok:
                    await ws.send_json({
                        "type": "hokm.match.rematch.pending",
                        "room_id": room_id,
                        "message": message,
                    })

            elif msg_type == "hokm.room.add_ai":
                room_id = payload.get("room_id")
                diff = payload.get("difficulty", "medium")
                ok, msg = await hokm_service.add_ai_player(room_id, user_id, diff)
                if not ok:
                    await ws.send_json({"type": "error", "error": msg})

            elif msg_type == "hokm.room.start":
                room_id = payload.get("room_id")
                ok, msg = await hokm_service.start_game(room_id, user_id)
                if not ok:
                    await ws.send_json({"type": "error", "error": msg})

            elif msg_type == "hokm.trump.select":
                room_id = payload.get("room_id")
                suit = payload.get("suit")
                ok, msg = await hokm_service.handle_trump_selection(room_id, user_id, suit)
                if not ok:
                    await ws.send_json({"type": "error", "error": msg})

            elif msg_type == "hokm.card.select":
                # 2-Player draft selection
                room_id = payload.get("room_id")
                card = payload.get("card")
                ok, msg = await hokm_service.handle_draft_selection(room_id, user_id, card)
                if not ok:
                    await ws.send_json({"type": "error", "error": msg})

            elif msg_type == "hokm.card.play":
                room_id = payload.get("room_id")
                card = payload.get("card")
                ok, msg = await hokm_service.handle_play_card(room_id, user_id, card)
                if not ok:
                    await ws.send_json({"type": "error", "error": msg})

            elif msg_type == "hokm.chat.message":
                room_id = payload.get("room_id")
                text = str(payload.get("text", "")).strip()[:150]
                if text and room_id:
                    await manager.broadcast_to_room(
                        room_id,
                        {
                            "type": "hokm.chat.message",
                            "user_id": user_id,
                            "name": name,
                            "text": text,
                        },
                    )

            elif msg_type == "hokm.chat.reaction":
                room_id = payload.get("room_id")
                reaction = str(payload.get("reaction", "")).strip()[:10]
                if reaction and room_id:
                    await manager.broadcast_to_room(
                        room_id,
                        {
                            "type": "hokm.chat.reaction",
                            "user_id": user_id,
                            "name": name,
                            "reaction": reaction,
                        },
                    )

            else:
                await ws.send_json({"type": "error", "error": f"Unknown action: {msg_type}"})

    except WebSocketDisconnect:
        log.info("Hokm WS disconnected normally for user %d (%s)", user_id, name)
    except Exception:
        log.error("Hokm WS unexpected error for user %d", user_id, exc_info=True)
    finally:
        room_id = await manager.disconnect(user_id, ws)
        if room_id:
            await hokm_service.handle_disconnect(room_id, user_id)
