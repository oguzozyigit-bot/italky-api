from __future__ import annotations

import json
from typing import Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["f2f-ws"])

ROOMS: Dict[str, Dict[str, Optional[WebSocket]]] = {}

@router.websocket("/f2f/ws/{room_id}")
async def f2f_ws(ws: WebSocket, room_id: str):
    await ws.accept()
    role: Optional[str] = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "hello":
                role = msg.get("role")
                if role not in ("host", "guest"):
                    await ws.send_text(json.dumps({"type":"info","message":"invalid role"}))
                    continue

                room = ROOMS.setdefault(room_id, {"host": None, "guest": None})
                if room.get(role) is not None:
                    await ws.send_text(json.dumps({"type":"info","message":f"{role} already connected"}))
                    continue

                room[role] = ws
                await ws.send_text(json.dumps({"type":"info","message":f"connected as {role}"}))

                if role == "guest" and room.get("host") is not None:
                    try:
                        await room["host"].send_text(json.dumps({"type":"peer_joined"}))
                    except Exception:
                        pass
                continue

            if msg.get("type") == "translated":
                room = ROOMS.get(room_id) or {}
                target = "guest" if role == "host" else "host"
                peer = room.get(target)
                if peer is None:
                    await ws.send_text(json.dumps({"type":"info","message":"peer not connected yet"}))
                    continue
                await peer.send_text(json.dumps(msg))
                continue

    except WebSocketDisconnect:
        pass
    finally:
        room = ROOMS.get(room_id)
        if room and role in ("host","guest") and room.get(role) is ws:
            room[role] = None
        if room and room.get("host") is None and room.get("guest") is None:
            ROOMS.pop(room_id, None)
