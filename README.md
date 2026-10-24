# Last Touch (Deal Flow hackathon, Track 1: sourcing)

**Vision:** every person at a VC firm gets a personal AI agent. The agent interviews them to learn how they invest, then works the way they do. The agents share one firm memory and talk to each other, so the firm always knows who is talking to which founder and what happens next.

Everything is real: no mock data. The app only shows what is in your Google account and your team's firm memory.

1. **Sign in.** Enter your name and a username. The username box checks availability as you type. If your account is linked to Google, you confirm sign-in with Google.
2. **Build your agent.** A short interview (role, sectors and stages, the signal you trust, hard no's). Your answers become your agent's scoring weights.
3. **Connect Google.** Gmail, Google Calendar and Google Chat are the only integrations.
4. **Source founders.** The agent reads your primary inbox, drops vendors, spam and non-pitches, applies your hard no's, and ranks founders with your criteria (`engine.py`). You can draft a reply and send it with Gmail.
5. **Who have I been talking to?** People from Gmail threads and Google Calendar meetings, plus your latest Google Chat messages, with what's overdue and what's next.
6. **Dictate anything.** The mic fills the chat box live and has no time limit. It works during the interview too. Anything that isn't a request is saved as a note in firm memory.
7. **Team.** Add people by username (it checks the username exists). Once both of you have added each other, your agents share notes, pipelines and who you're talking to, and "who knows Acme?" asks your teammates' agents.

## Safety: every write is guarded
Reading is always allowed. Writing includes sending an email, saving a note, and sharing your pipeline or contacts with the team. Each user picks a mode in Settings:
- **Ask for permission** (default): every write shows an approval card and waits for your OK. PRISM is not used.
- **Auto, guarded by PRISM**: each proposed write is submitted to PRISM as a trajectory and evaluated. A clear pass goes ahead and a block stops it. If PRISM is unreachable or unclear, the agent falls back to asking you (fail closed). LLM calls are traced to PRISM only in this mode.

Hard limits apply in both modes: at most 3 recipients and 25 emails a day per user, and no card numbers, API keys, private keys or passwords in what the agent writes. Every action is logged in firm memory and listed under Settings → Recent agent actions.

## Run
```bash
pip install -r requirements.txt
cp .env.example .env     # fill in what you have
flask --app app run      # http://127.0.0.1:5000
```
Without `BLOB_READ_WRITE_TOKEN`, data is stored in `runtime/store/` (local development only).

## Deploy (Vercel)
1. Import the repo. `app.py` exports the Flask `app`, so no framework preset is needed.
2. Storage → create a **private** Blob store and connect it to the project (this adds `BLOB_READ_WRITE_TOKEN`).
3. Set `SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `PRISMTRACE_API_KEY`, `PRISMTRACE_PROJECT_ID` (and optionally `NICRRON_API_KEY`).
4. Google Cloud: create an OAuth client of type "Web application" with redirect URI `https://<your-domain>/auth/google/callback`. Enable the Gmail API, Google Calendar API and Google Chat API. The Chat API also needs a Chat app configured in the same project, even for reading. While the OAuth consent screen is in testing, add each user as a test user (Gmail scopes are restricted).

## Files
- `app.py`: Flask server: accounts, Google OAuth, agent skills, team, and the action gate
- `page.html`: the single-page chat UI (sign-in, interview, dictation, approval cards, settings)
- `guard.py`: hard rules plus the PRISM review used in Auto mode
- `google_api.py`: OAuth plus Gmail, Google Calendar and Google Chat
- `store.py`: firm memory on Vercel Blob (`users/`, `notes/`, `pipeline/`, `contacts/`, `actions/`, `google/`)
- `engine.py`: triage, extraction and scoring rules
- `integrations.py`: Nicrron (optional LLM extraction) and PRISM tracing
