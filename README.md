# Last Touch (Deal Flow hackathon, Track 1: sourcing)

**Vision:** every person at a VC firm gets a personal AI agent. The agent interviews them to learn how they invest, then works the way they do. The agents share one firm memory and talk to each other, so the firm always knows who is talking to which founder and what happens next.

**Built on the event notes people record, plus akta.pro.** No Google or WhatsApp integrations and no mock data. After an event or a meeting, you tap the mic and say who you met. The agent does the rest, and checks every company on akta.pro (the agents' web search).

1. **Sign in or create an account.** Create an account with your name, a username (it checks availability as you type) and a password of at least 8 characters. Sign in later with your username and password. Passwords are stored hashed; 8 wrong tries lock the account for 15 minutes; changing your password in Settings signs out your other devices.
2. **Build your agent.** A four-question interview (role, sectors and stages, the signal you trust, hard no's). Your answers become your agent's scoring weights.
3. **Record a note.** The mic fills the chat box live and has no time limit. It works during the interview too, and you can also type. For example: "Met Jonah Weiss from Patchbay at the Deal Flow hackathon. Ex-Stripe infra, webhook API, 1,800 GitHub stars, raising $1.2M pre-seed. Next step: intro to Cara by Monday." The note saves straight away (no approval). The agent reads out the founder, company, event, traction and next step (Nicrron when configured, keyword rules otherwise), and looks the company up on akta.pro.
4. **Who have I been talking to?** Every founder in your notes, where you met, the latest next step, and what's due or overdue.
5. **Rank my founders.** Scores founders with your interview weights, using what your notes, your teammates' notes and akta.pro say, and flags your hard no's. "Draft follow-up" writes a message for you to copy; the agent never sends anything.
6. **Source founders.** Finds new companies on akta.pro that fit your focus (or "source founders in climate"), marking the ones you or your team already met. "Look up Patchbay" checks one company.
7. **Team.** Add people by username (it checks the username exists). Once both of you have added each other, your agents share notes. When you record a note about someone a teammate already met, their agent tells you. You can also ask "who knows Patchbay?" or "what is my team working on?".

## Safety
Reading is always allowed, and notes never need your approval. Each user picks a mode in Settings:
- **Ask mode (PRISM off)** (default): nothing is sent to PRISM.
- **Auto mode (PRISM on)**: PRISM watches the agent. Model calls, agent tool calls (akta.pro lookups, team checks, saves) and errors are traced to PRISM under one session per sign-in, and PRISM reviews each note before it is saved. A clear block stops the save; anything else saves it.

In both modes, secrets (API keys, private keys, passwords, card numbers) are removed from a note before it reaches the firm memory. Every save is logged under Settings → Recent agent actions, and Settings → Connected shows the live status of the firm memory, akta.pro, PRISM (credential and live traces from PRISM's setup doctor), Nicrron and Roundtable.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env     # fill in what you have
flask --app app run      # http://127.0.0.1:5000
```
Without `BLOB_READ_WRITE_TOKEN`, data is stored in `runtime/store/` (local development only).

## Deploy (Vercel)
1. Import the repo. `app.py` exports the Flask `app`, so no framework preset is needed.
2. Storage → create a **private** Blob store and connect it to the project. The app uses `BLOB_READ_WRITE_TOKEN` if Vercel adds it, or `BLOB_STORE_ID` with Vercel's OIDC token otherwise. Open `/api/health` on your deployment to see which storage and session key it is using.
3. Set `SECRET_KEY` (optional: without it, one random key is kept in the Blob store and shared by every server instance), `AKTA_API_KEY` and `PRISMTRACE_API_KEY` (`PRISM_API_KEY` also works), and optionally `NICRRON_API_KEY` and `ROUNDTABLE_MCP_URL` + `ROUNDTABLE_MCP_TOKEN`. Redeploy.

## Files
- `app.py`: Flask server: accounts, note saving and reading, founders, akta.pro skills, team, PRISM tracing hooks
- `page.html`: the single-page chat UI (sign-in, interview, recording, note cards, settings)
- `guard.py`: secret redaction plus the PRISM review used in Auto mode
- `engine.py`: note extraction rules, follow-up dates and scoring
- `integrations.py`: PRISM (tracing, sessions, setup doctor), Nicrron, akta.pro and Roundtable
- `env.py`: reads keys from the environment (trimming stray newlines) and scrubs secrets out of anything shown or logged
- `store.py`: firm memory on Vercel Blob (`users/`, `notes/`, `actions/`, `akta/` lookup cache)
