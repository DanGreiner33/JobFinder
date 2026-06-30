# Employer Finder — Setup Runbook

This tool identifies the most likely hiring company behind a job posting using
publicly available information. Many job postings list the role and requirements
without naming the employer; this tool analyzes the posting and cross-references
public job listings and web sources to determine which company is most likely
hiring. It returns a ranked shortlist of likely employers, each with a confidence
level and the evidence behind it — useful market intelligence for prioritizing
business-development outreach toward companies with active hiring needs.

This runbook is written so a non-technical person — or an automation agent like
Comet — can stand it up step by step.

---

## 1. The files

| File | What it is |
|------|-----------|
| `deanonymize_employer.py` | The agent (the brain). Used by everything else. |
| `worker.py` | The production runner. Watches the database queue and processes URLs. |
| `index.html` | The web form. Submit a posting (link or pasted text) and see the result. |
| `schema.sql` | Creates the database table. Run once in Supabase. |
| `Dockerfile` | Packaging so Railway can run the worker. |
| `requirements.txt` | The list of software libraries to install. |
| `run_eval.py` | Optional laptop test script. You can ignore this and validate in Supabase instead. |
| `known_answers.example.csv` | Template showing the test-data format. |
| `.gitignore` | Safety file that keeps your API keys out of GitHub. Do not delete. |

---

## 2. API keys you need to get

Get these five before deploying. The first three are new sign-ups; the last two
come from your existing Supabase project. Each has a free tier big enough to test.

| Env var name | Where to get it | What it does | Cost |
|--------------|-----------------|--------------|------|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys | Powers the agent's reasoning | Pay per use; pennies per posting |
| `SERPER_API_KEY` | serper.dev | Google search (verbatim + similar-jobs) | Free tier to start, then very cheap |
| `SCRAPINGBEE_API_KEY` | scrapingbee.com | Reads LinkedIn / Indeed / Glassdoor pages that block normal access | Free trial, then paid |
| `SUPABASE_URL` | Your Supabase project → Settings → API → Project URL | Where the queue/results live | Free (existing project) |
| `SUPABASE_SERVICE_KEY` | Your Supabase project → Settings → API → `service_role` key | Lets the worker read/write the queue | Free (existing project) |
| `SUPABASE_ANON_KEY` | Your Supabase project → Settings → API → `anon` `public` key | Lets the web form submit & read (safe to expose) | Free (existing project) |

IMPORTANT: The `service_role` key is powerful. It only ever goes into Railway's
settings — never into GitHub, never into a frontend, never shared.

Rough running cost: a few cents per posting processed. A 25-posting test batch is
well under a dollar.

---

## 3. Deploy (one-time setup)

### Step A — GitHub
1. Create a new private repository on github.com.
2. Upload every file in this folder EXCEPT skip nothing — include `.gitignore`.
3. Confirm there are no API keys anywhere in the files (there aren't — keys are
   added later in Railway).

### Step B — Supabase (creates the database table)
1. Open your Supabase project → SQL Editor.
2. Open `schema.sql`, copy its contents, paste into the editor, and click Run.
3. This creates a table called `posting_queue`. That's it.

### Step C — Railway (runs the worker)
1. Go to railway.app → New Project → Deploy from GitHub repo → pick the repo.
2. Railway sees the `Dockerfile` and builds automatically.
3. Deploy it as a **Worker** service (NOT a Web service — there is no website).
4. Open the service → Variables → add all five keys from section 2.
5. Railway redeploys. The worker is now live and watching the queue.

### Step D — The web form (`index.html`)
1. Open `index.html` in a text editor. Near the top of the script, set two values:
   - `SUPABASE_URL` = your project URL (same as above)
   - `SUPABASE_ANON_KEY` = the `anon` `public` key (NOT the service key)
   The anon key is designed to be public; the `schema.sql` you ran already added
   the security rules that let it submit and read safely.
2. Host the file, or just open it. Options, easiest first:
   - Open the file directly in a browser to use it on your own machine.
   - Drop it on Vercel (drag-and-drop a folder containing just `index.html`) for a
     shareable link.
   - Put it in Supabase Storage or any static host.

That's the whole front end — one file. It submits to the same queue the worker
watches, then shows the ranked result when it's done.

---

## 4. Validate before trusting it (do NOT skip)

You do not need a laptop or any code for this. You validate by feeding it postings
whose answer you already know and reading the result in Supabase.

1. In Supabase → Table Editor → `posting_queue`, add 20-30 rows. For each, fill in:
   - `url` = the recruiter job-posting link
   - `known_employer` = the real company (the answer your team already figured out)
   - Leave everything else blank.
2. Wait a few minutes. The worker processes them and fills in `result`.
3. In the SQL Editor, run the scorecard query (it's written at the bottom of
   `schema.sql`). It shows the real company next to what the agent guessed.
4. Count how many match. That is your accuracy.

If accuracy looks good → go to section 5. If it's poor, stop and have the developer
check: are pages failing to read (a scraping-key issue), or are guesses wrong (a
tuning issue)? Don't run real volume until this looks right.

---

## 5. Run real volume

Once validated, just insert posting URLs into `posting_queue` (leave
`known_employer` blank). The worker processes each and writes a ranked shortlist of
likely employers into the `result` column. Read results with the queries at the
bottom of `schema.sql`.

You can insert URLs from your Next.js app, an n8n workflow, or by pasting them into
the Supabase Table Editor.

---

## Notes
- The `result` for each posting includes a `candidates` list (ranked companies),
  a `top_pick`, an `overall_confidence`, and the evidence behind each candidate.
- Candidates backed only by generic requirements (years of experience, common
  tools) are capped at "low" confidence on purpose — those are coincidental
  look-alikes, not real leads. Work the high/medium ones first.
- If a posting can't be read at all, the result is marked as a fetch failure rather
  than a wrong guess — that's an infrastructure flag, usually meaning the scraping
  key needs attention.
