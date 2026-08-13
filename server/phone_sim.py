"""A caller that arrives the way Twilio delivers one — no phone bill attached.

The browser demo exercises the pipeline over 16 kHz WebRTC. This exercises the
telephony path instead: it connects to the bot's websocket, performs Twilio's
Media Streams handshake, and holds a spoken conversation in 20 ms μ-law frames
at 8 kHz — exactly the wire format a real phone call produces. Provider
detection, the Twilio serializer, the 8 kHz pipeline branch, barge-in (clear
events) and the CRM write all run for real; the only things it cannot prove
are Twilio's own webhook delivery and the REST hang-up, which need a live call.

The caller's lines are synthesised with ElevenLabs in ulaw_8000 and played on
a turn-taking loop: each line goes out once the agent has replied and then
stayed quiet. Edit UTTERANCES to match the business you are testing against.

Usage:
    python bot.py -t twilio -x example.com   # in one terminal
    python phone_sim.py [ws://localhost:7860/ws]

Produces voice_lab_out/phone_sim_call.wav — stereo, caller on the left
channel, agent on the right — and a timestamped event log on stdout.
"""

import asyncio
import audioop
import base64
import json
import sys
import time
import wave
from pathlib import Path

import aiohttp
import websockets

WS_URL = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:7860/ws"
SR = 8000
FRAME_BYTES = 160  # 20 ms of μ-law at 8 kHz
FRAME_S = 0.02
SILENCE = audioop.lin2ulaw(b"\x00\x00", 2) * FRAME_BYTES
STREAM_SID = "MZsimulated0000000000000000000000"

HERE = Path(__file__).parent
OUT = HERE / "voice_lab_out" / "phone_sim_call.wav"

# What the caller says, in order — written for the emergency_us business.
UTTERANCES = [
    "Hi! My kitchen sink is leaking really badly, there's water dripping all over the floor.",
    "It's 2210 Taraval Street in San Francisco. And my name is Mark Spencer.",
    "Sure. Six five zero. Five five five. Zero one nine eight.",
    "Yes, that all sounds right. Thank you so much. Bye!",
]

VOICE = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel"; any premade voice works


def read_env_key() -> str:
    env = HERE / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("ELEVENLABS_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"ELEVENLABS_API_KEY not found in {env}")


async def tts_ulaw(session: aiohttp.ClientSession, key: str, text: str) -> bytes:
    """One caller line as raw μ-law 8 kHz — the codec of a real phone call."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=ulaw_8000"
    async with session.post(
        url,
        headers={"xi-api-key": key},
        json={"text": text, "model_id": "eleven_turbo_v2_5"},
    ) as r:
        body = await r.read()
        if r.status != 200:
            sys.exit(f"TTS failed {r.status}: {body[:200]!r}")
    if body[:4] == b"RIFF":  # strip a WAV container if one arrives
        body = body[44:]
    return body


async def main() -> None:
    key = read_env_key()
    async with aiohttp.ClientSession() as session:
        clips = [await tts_ulaw(session, key, u) for u in UTTERANCES]
    print(f"[sim] synthesised {len(clips)} caller lines "
          f"({sum(len(c) for c in clips) / SR:.1f}s of speech)")

    ws = await websockets.connect(WS_URL)
    t0 = time.monotonic()

    def now() -> float:
        return time.monotonic() - t0

    # Twilio's Media Streams handshake: "connected", then "start" with the
    # stream/call identifiers. The SIDs are fake, which is fine everywhere
    # except the REST hang-up — that call 404s and the serializer logs it.
    await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
    await ws.send(json.dumps({
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "streamSid": STREAM_SID,
            "callSid": "CAsimulated0000000000000000000000",
            "accountSid": "ACsimulated0000000000000000000000",
            "tracks": ["inbound"],
            "customParameters": {},
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": SR, "channels": 1},
        },
        "streamSid": STREAM_SID,
    }))
    print(f"[sim] {now():6.2f}s connected + start sent")

    outgoing = bytearray()                        # μ-law queued to send
    caller_track = bytearray()                    # every byte actually sent
    agent_events: list[tuple[float, bytes]] = []  # (arrival time, μ-law)
    agent_last = {"t": None, "spoke": False}
    closed = asyncio.Event()

    async def receiver() -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "media":
                    chunk = base64.b64decode(msg["media"]["payload"])
                    agent_events.append((now(), chunk))
                    agent_last["t"] = now()
                    agent_last["spoke"] = True
                elif event == "clear":
                    print(f"[sim] {now():6.2f}s agent cleared its audio (barge-in)")
                elif event != "mark":
                    print(f"[sim] {now():6.2f}s <- {event}: {raw[:120]}")
        except websockets.ConnectionClosed as e:
            print(f"[sim] {now():6.2f}s websocket closed by server ({e.code})")
        finally:
            closed.set()

    async def sender() -> None:
        """One frame every 20 ms, silence when the caller is quiet — a phone
        line never stops carrying audio, and neither may we."""
        seq, ts = 2, 0
        next_at = time.monotonic()
        while not closed.is_set():
            if outgoing:
                frame = bytes(outgoing[:FRAME_BYTES]).ljust(FRAME_BYTES, SILENCE[:1])
                del outgoing[:FRAME_BYTES]
            else:
                frame = SILENCE
            caller_track.extend(frame)
            try:
                await ws.send(json.dumps({
                    "event": "media",
                    "sequenceNumber": str(seq),
                    "media": {"track": "inbound", "chunk": str(seq),
                              "timestamp": str(ts),
                              "payload": base64.b64encode(frame).decode()},
                    "streamSid": STREAM_SID,
                }))
            except websockets.ConnectionClosed:
                return
            seq += 1
            ts += 20
            next_at += FRAME_S
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    async def choreographer() -> None:
        for i, clip in enumerate(clips):
            while not closed.is_set():
                quiet = agent_last["t"] is not None and now() - agent_last["t"] > 1.2
                if agent_last["spoke"] and quiet and not outgoing:
                    break
                await asyncio.sleep(0.05)
            if closed.is_set():
                return
            print(f"[sim] {now():6.2f}s caller says: {UTTERANCES[i][:60]}…")
            agent_last["spoke"] = False
            outgoing.extend(clip)
            while outgoing and not closed.is_set():
                await asyncio.sleep(0.05)
        print(f"[sim] {now():6.2f}s caller is done talking, waiting for the hangup")
        try:
            await asyncio.wait_for(closed.wait(), timeout=30)
        except TimeoutError:
            print(f"[sim] {now():6.2f}s no hangup after 30s — closing from our side")
            await ws.close()

    recv_task = asyncio.create_task(receiver())
    send_task = asyncio.create_task(sender())
    await choreographer()
    closed.set()
    await asyncio.gather(recv_task, send_task, return_exceptions=True)

    # Stereo assembly: the caller track is sample-accurate by construction
    # (one frame per tick); agent bursts are placed at arrival time and pushed
    # back-to-back within a burst, the way a phone would play them.
    total_s = len(caller_track) / SR
    agent_pcm = bytearray(int(total_s * SR) * 2)
    cursor = None
    for t, chunk in agent_events:
        offset = int(t * SR) * 2
        if cursor is not None and offset < cursor:
            offset = cursor
        pcm = audioop.ulaw2lin(chunk, 2)
        end = offset + len(pcm)
        if end > len(agent_pcm):
            agent_pcm.extend(b"\x00" * (end - len(agent_pcm)))
        agent_pcm[offset:end] = pcm
        cursor = end
    caller_pcm = audioop.ulaw2lin(bytes(caller_track), 2)
    if len(caller_pcm) < len(agent_pcm):
        caller_pcm += b"\x00" * (len(agent_pcm) - len(caller_pcm))
    else:
        agent_pcm.extend(b"\x00" * (len(caller_pcm) - len(agent_pcm)))

    mixed = audioop.add(
        audioop.tostereo(caller_pcm, 2, 1, 0),
        audioop.tostereo(bytes(agent_pcm), 2, 0, 1),
        2,
    )
    OUT.parent.mkdir(exist_ok=True)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(mixed)
    agent_s = sum(len(c) for _, c in agent_events) / SR
    print(f"[sim] saved {OUT} — {total_s:.1f}s call, agent spoke {agent_s:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
