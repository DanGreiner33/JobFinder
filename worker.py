"""
Queue worker. Polls the Supabase `posting_queue` table for pending job-posting
URLs, runs the de-anonymization agent on each, and writes the result back.

Deploy this as a long-running worker (e.g. Railway). It is NOT an HTTP server —
it just loops forever processing the queue.

Env vars required:
    ANTHROPIC_API_KEY
    SERPER_API_KEY         (serper.dev — powers the 20-phrase exact-match search)
    SCRAPINGBEE_API_KEY    (strongly recommended — needed to read LinkedIn/Indeed/Glassdoor)
    SUPABASE_URL
    SUPABASE_SERVICE_KEY   (service role key — server-side only, never ship to client)
"""

import os
import time
import traceback
from supabase import create_client

# Reuses the agent you already built (deanonymize_employer.py in same dir)
from deanonymize_employer import investigate

POLL_SECONDS = 5
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def claim_one_pending():
    """Grab a single pending row and mark it 'processing'.

    For a SINGLE worker this select-then-update is fine. If you run multiple
    workers, replace this with a Postgres RPC using
    `SELECT ... FOR UPDATE SKIP LOCKED` so two workers can't grab the same row.
    """
    rows = (
        sb.table("posting_queue")
        .select("*")
        .eq("status", "pending")
        .order("created_at")
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return None
    row = rows[0]
    sb.table("posting_queue").update({"status": "processing"}).eq("id", row["id"]).execute()
    return row


def process_row(row):
    try:
        result = investigate(url=row.get("url"), description=row.get("description"))
        sb.table("posting_queue").update({
            "status": "done",
            "result": result,
            "error": None,
        }).eq("id", row["id"]).execute()
        print(f"[done] {row['url']} -> {result.get('top_pick')} "
              f"({result.get('overall_confidence')}, "
              f"{len(result.get('candidates', []))} candidates)")
    except Exception:
        err = traceback.format_exc()
        sb.table("posting_queue").update({
            "status": "error",
            "error": err,
        }).eq("id", row["id"]).execute()
        print(f"[error] {row['url']}\n{err}")


def main():
    print("Worker started. Polling posting_queue...")
    while True:
        row = claim_one_pending()
        if row:
            process_row(row)
        else:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
