# Installing for a client — zero to a live call

The path an installer walks for every new business: 30–60 minutes the first
time, most of it waiting for DNS or Twilio, not typing.

One install serves one business. A second client is a second copy of this on
their own server (or a second directory on yours with different ports).

## What you need before you start

- A server with Docker (any $5 VPS; 1 CPU / 1 GB is enough — the heavy work
  happens at OpenAI and ElevenLabs, not on the box).
- API keys: [OpenAI](https://platform.openai.com/api-keys) and
  [ElevenLabs](https://elevenlabs.io/app/settings/api-keys). The client can
  bring their own — then the per-minute cost lands on their card, not yours.
- For a real phone number: a Twilio account (docs/telephony.md).
- The business's answers from the interview sheet (docs/new-business.md).

## 1. Configure

```bash
git clone https://github.com/andriidrok1/open-receptionist.git && cd open-receptionist
cp server/.env.example server/.env
```

Edit `server/.env`:

```
BUSINESS=emergency_us            # or your client's YAML, see step 2
OPENAI_API_KEY=sk-…
ELEVENLABS_API_KEY=…
DASHBOARD_PASSWORD=…             # the owner will open the page from a phone
TWILIO_ACCOUNT_SID=AC…           # phone mode only
TWILIO_AUTH_TOKEN=…              # phone mode only
PUBLIC_HOST=agent.example.com    # phone mode only: your domain, no https://
```

## 2. Describe the business

Copy the closest existing config and edit it with the interview answers:

```bash
cp server/businesses/emergency_us.yaml server/businesses/acme_plumbing.yaml
# edit, then: BUSINESS=acme_plumbing in server/.env
```

`server/businesses/_template.yaml` documents every field. Keep a real
client's file out of the repo with `BUSINESS_CONFIG=/srv/clients/acme.yaml`.

## 3. Start

```bash
docker compose up -d --build
```

Two containers share one SQLite volume: the agent (`:7860`) and the owner
dashboard (`:8000`). Verify:

```bash
curl -X POST http://localhost:7860/   # phone mode: TwiML with your domain
# browser mode instead: open http://localhost:7860/client and talk
```

## 4. Connect the number

Follow [telephony.md](telephony.md): point the Twilio number's webhook at
`https://agent.example.com/`. HTTPS termination is yours (Caddy in front of
port 7860 is two lines; a `cloudflared` tunnel also works and needs no open
ports at all).

Call the number. You should hear the greeting.

## 5. Hand the dashboard to the owner

Open `https://<server>:8000` (or map it behind your domain), sign in with
`DASHBOARD_PASSWORD`, and walk the owner through:

- **Inbox** — every booking, cancellation and message the agent takes, with a
  checkmark the owner flips once a thing is dealt with.
- **Calls** — every conversation, with the full transcript one tap away.
- **Settings** — when the assistant answers: always, never, or around the
  owner's own working hours (the schedule applies from the very next call).

The dashboard needs nothing installed on the owner's phone — it is a web
page. Set the password, send the link.

## Day-2 operations

- **Backup** is one file:
  `docker compose cp dashboard:/data/receptionist.db backup-$(date +%F).db`
  in a cron line. That file is the whole state.
- **Update**: `git pull && docker compose up -d --build`. Database migrations
  run automatically on start and are idempotent.
- **Rotate a leaked dashboard password** by changing `DASHBOARD_PASSWORD` and
  restarting — every signed-in browser is signed out at once.
- **Watch it**: the dashboard's status dot is measured, not painted — red
  means the agent's process is actually down.

## Costs (measured, not promised)

Across our recorded test calls (20 minutes of conversation, mixed verticals):
**≈ $0.09 per minute** all-in — ElevenLabs TTS is ~80% of that, then STT,
LLM, and Twilio's ~$0.01/min. A receptionist that talks 2 hours a month costs
about $11 in usage. The per-call breakdown is stored with every call
(`metrics.cost` in the dashboard's call view), priced from
`server/pricing.yaml` — re-check it when providers change their rates.
