# Last Touch: a firm-wide team of deal agents (Deal Flow hackathon, Track 1)

Every person at the fund gets an agent. The agents share one firm memory and talk to each other, so no great founder is dropped between the inbox, the event and the partner meeting.

- **Associate's agent (Ava):** reads inbound (LinkedIn, email, event notes), drops spam, vendor and generic pitches with a reason, and writes a sourced brief.
- **Partners' agents (Ben "Steady", Cara "Moonshot"):** score every lead against their own thesis. The scores are rules, shown with a breakdown.
- **Router:** sends each lead to the partner whose thesis fits best. If someone at the firm already met the founder, that person sends the re-intro.
- **Watcher:** parked founders (for example, "Fund II still raising") resurface when the fund closes or a signal appears.
- **Follow-up rules:**
  - Hot: 24h, then a 14-day second touch
  - Warm: keep-in-touch note, recheck in 60 days
  - Park: wait for the fund to close or a new signal
  - Pass: polite decline with a referral

  Messages are drafts only and are never sent automatically.

## Run (2 minutes)
```bash
pip install -r requirements.txt
cp .env.example .env      # add keys; every key is optional
streamlit run app.py
```
Without keys it runs fully on cached or demo data, and every fallback is labelled in the UI.

## Sponsor tools (all four)
| Tool | Where | What it does |
|---|---|---|
| **Nicrron** | `integrations.LLM` | OpenAI-compatible (`https://api.nicrron.ai/v1`). Extracts structured fields from messy messages and polishes drafts. Turn on with the "Live AI" toggle. |
| **Akta (Wokelo)** | `integrations.Akta` | Company search (is this company real?), industry search, comparable-company lists and industry news for every brief. Also gives the Watcher its signals on parked founders. |
| **PRISM (Block Convey)** | `integrations.Tracer` | Every LLM call goes through `trace_llm`, and every agent run is submitted as a trajectory (`submit_trajectory`), one per agent per run. |
| **Roundtable** | `integrations.NotesStore` | The firm memory: one shared note per founder, saved to Roundtable via `POST /api/tools/save_note` into the `last-touch` folder and updated in place on later runs. Set `ROUNDTABLE_API_KEY` to an agent key created for this app (never a personal assistant's key). Notes are also mirrored to `runtime/roundtable_outbox/*.md`. |

## Design choices
- **Rules decide, the AI extracts and writes.** Thesis checks, scores, routing and follow-up timing are plain code, so every verdict has a reason.
- **Every claim in a brief carries a source tag:** `[m04]` is the founder's message, `[firm memory]` is the note history, `[Akta]` is outside data.
- **All people, funds and startups in `data/` are fictional.** Akta fixture comps list real companies by name only.

## Files
- `app.py`: the Streamlit UI (Today's actions, Firm channel, Inbox triage, Briefs, Watchlist, Memory & traces)
- `engine.py`: agents, rules, routing, follow-up rules, Watcher
- `integrations.py`: Nicrron, Akta, PRISM, Roundtable, plus a cache
- `data/`: synthetic inbox (14 messages), firm and partner profiles, firm-memory seed, Akta demo fixtures

## Deploy (live link for Devpost)
Push to a public GitHub repo, then on share.streamlit.io create a new app from `app.py` and add the keys under Secrets as top-level TOML. This is a Streamlit server app, so it does not run on Vercel or other serverless hosts.
