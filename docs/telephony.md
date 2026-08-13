# Connecting a real phone number (Twilio)

The bot already speaks Twilio's Media Streams protocol — `bot.py -t twilio`
starts a server that answers Twilio's webhook with TwiML and takes the call
audio over a websocket. Connecting a number is configuration, not code.

## What you need

- A Twilio account and a phone number with the **Voice** capability.
  A trial account works: the number is paid from trial credit, and inbound
  calls play a short trial notice ("press any key") before connecting.
- A URL Twilio can reach. In production that is your server's domain; on a
  laptop, a tunnel: `cloudflared tunnel --url http://localhost:7860` prints a
  `https://….trycloudflare.com` URL (no account needed; the URL changes on
  every restart, so re-save the webhook when you restart the tunnel).

## 1. Put the Twilio keys in `server/.env`

```
TWILIO_ACCOUNT_SID=AC…
TWILIO_AUTH_TOKEN=…
```

Both are on the Twilio Console home page under **Account Info**. They are not
used to receive calls — Twilio pushes those to you — but the bot needs them to
**hang up**: when the agent says goodbye, it ends the call through Twilio's
REST API. Without the keys the bot refuses phone calls at startup rather than
leave callers listening to silence after every goodbye.

## 2. Start the bot in phone mode

```bash
BUSINESS=emergency_us python bot.py -t twilio -x your-domain.example.com
```

`-x` is the public hostname (no `https://`) — it is written into the TwiML so
Twilio knows where to open the audio websocket. Verify from another machine:

```bash
curl -X POST https://your-domain.example.com/
# → <Response><Connect><Stream url="wss://your-domain.example.com/ws"/></Connect>…
```

## 3. Point the number at the bot

Twilio Console → Phone Numbers → your number → **Voice Configuration**:

- *A call comes in*: **Webhook**, URL `https://your-domain.example.com/`,
  method **HTTP POST**. Save.
- Everything else (A2P 10DLC, emergency address) concerns SMS and outbound
  911 — an inbound receptionist needs neither.

Call the number. The agent answers first, as it does in the browser.

## Testing without a phone

`phone_sim.py` is a caller that arrives the way Twilio delivers one: the same
handshake, the same 20 ms μ-law frames at 8 kHz, lines synthesised in the
telephone codec. It talks to the agent on a turn-taking loop and records the
whole conversation to a stereo WAV (caller left, agent right):

```bash
python bot.py -t twilio -x localhost   # terminal 1
python phone_sim.py                    # terminal 2
```

It proves everything except Twilio's own webhook delivery and the REST
hang-up, which need one live call.

## Telnyx and Plivo

The same code path handles both (`-t telnyx` / `-t plivo` — the provider is
actually auto-detected from the first websocket message). Neither has been
tried against a real number yet; Twilio has.

## Notes from the first live call

- The webhook and websocket worked through a `trycloudflare.com` quick tunnel,
  including the audio stream — a paid tunnel or domain is not required to demo.
- Twilio sends and expects 8 kHz μ-law; the pipeline switches sample rate
  automatically when it sees a websocket transport (`is_phone` in `bot.py`),
  so the browser demo and the phone share every other line of code.
