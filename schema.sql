-- Run this in the Supabase SQL editor.

create table if not exists posting_queue (
  id              uuid primary key default gen_random_uuid(),
  url             text,                            -- a link, OR leave blank and use description
  description     text,                            -- pasted JD text (most reliable — no scraping)
  known_employer  text,                            -- OPTIONAL: fill this for test postings
  status          text not null default 'pending', -- pending | processing | done | error
  result          jsonb,                           -- the agent's structured output
  error           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint has_input check (url is not null or description is not null)
);

create index if not exists posting_queue_status_idx on posting_queue (status);

-- keep updated_at fresh
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists posting_queue_updated_at on posting_queue;
create trigger posting_queue_updated_at
  before update on posting_queue
  for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- Row-Level Security: lets the browser frontend (using the public ANON key)
-- submit postings and read back results. The worker uses the SERVICE key, which
-- bypasses RLS, so it is unaffected.
--
-- NOTE: these policies are OPEN — anyone with the anon key and the page can
-- submit and read. That's fine for an internal tool you host privately or run
-- locally. To lock it down later, turn on Supabase Auth and scope these
-- policies to authenticated users (e.g. `to authenticated`).
-- ---------------------------------------------------------------------------
alter table posting_queue enable row level security;

drop policy if exists anon_insert_postings on posting_queue;
create policy anon_insert_postings on posting_queue
  for insert to anon with check (true);

drop policy if exists anon_read_postings on posting_queue;
create policy anon_read_postings on posting_queue
  for select to anon using (true);

-- Adding work is just:
--   insert into posting_queue (url) values ('https://board.com/job/123');
--
-- VALIDATION BATCH (do this first): load postings you already know the answer to,
-- with the real company in known_employer:
--   insert into posting_queue (url, known_employer) values
--     ('https://board.com/job/1', 'Acme Manufacturing'),
--     ('https://board.com/job/2', 'Riverside Logistics');
--
-- Then once they finish, eyeball the scorecard — known answer next to the guess:
--   select known_employer            as "real company",
--          result->>'top_pick'       as "agent guessed",
--          result->>'overall_confidence' as conf,
--          url
--   from posting_queue
--   where status = 'done' and known_employer is not null
--   order by known_employer;
-- Count how many "agent guessed" match "real company". That's your accuracy.
--
-- Reading normal results:
--   select url, result->>'top_pick', result->>'overall_confidence'
--   from posting_queue where status = 'done';
-- Or expand the ranked candidate shortlist:
--   select url, c->>'company', c->>'likelihood', c->>'match_score'
--   from posting_queue, jsonb_array_elements(result->'candidates') c
--   where status = 'done' order by url, (c->>'match_score')::int desc;
