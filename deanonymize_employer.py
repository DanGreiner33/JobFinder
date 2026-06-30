"""
Employer Finder agent.

Takes a job posting (a URL or pasted text) and identifies the most likely hiring
company using public information, including cases where the employer is not named
in the listing. Uses Claude's web search tool + a URL fetcher in a loop.

Run:
    pip install anthropic requests beautifulsoup4
    export ANTHROPIC_API_KEY=sk-...
    python deanonymize_employer.py "https://somejobboard.com/posting/12345"

Output: structured JSON with the likely employer, a confidence level, and the
evidence trail. Confidence is the point — a wrong name is worse than "unknown".
"""

import json
import os
import re
import sys
import requests
from bs4 import BeautifulSoup
import anthropic

client = anthropic.Anthropic()

MODEL = "claude-sonnet-4-6"   # bump to an Opus model for hard / high-value cases
MAX_ITERATIONS = 12
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")          # serper.dev — search
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY")  # fallback for blocked/JS pages

# Boards that block plain requests and need the scraping fallback to read.
JS_HEAVY_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com")
MIN_USABLE_CHARS = 300   # below this, treat a fetch as failed and retry via fallback

# Sentinel prefix so callers (agent + eval harness) can DETECT a failed read
# instead of silently reasoning over empty text.
FETCH_ERROR = "FETCH_FAILED:"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Tooling
# ---------------------------------------------------------------------------

def _extract_text(html: str, max_chars: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text[:max_chars]


def _fetch_plain(url: str) -> tuple[bool, str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, r.text
    except Exception as e:
        return False, str(e)


def _fetch_scrapingbee(url: str) -> tuple[bool, str]:
    """Fallback for JS-heavy / bot-blocking boards. Renders JS server-side."""
    if not SCRAPINGBEE_API_KEY:
        return False, "no SCRAPINGBEE_API_KEY"
    try:
        r = requests.get(
            "https://app.scrapingbee.com/api/v1/",
            params={"api_key": SCRAPINGBEE_API_KEY, "url": url, "render_js": "true"},
            timeout=60,
        )
        if r.status_code != 200:
            return False, f"scrapingbee HTTP {r.status_code}"
        return True, r.text
    except Exception as e:
        return False, str(e)


def fetch_url(url: str, max_chars: int = 8000) -> str:
    """Fetch a page and return its visible text.

    Strategy: try plain requests first. If the page is a known JS-heavy board, or
    plain requests fails / returns too little usable text, fall back to the
    scraping API. On total failure returns a string starting with FETCH_ERROR so
    the caller can DETECT it rather than silently reasoning over nothing.
    """
    needs_js = any(d in url for d in JS_HEAVY_DOMAINS)

    if not needs_js:
        ok, payload = _fetch_plain(url)
        if ok:
            text = _extract_text(payload, max_chars)
            if len(text) >= MIN_USABLE_CHARS:
                return text
        # plain failed or too thin -> fall through to scraping fallback

    ok, payload = _fetch_scrapingbee(url)
    if ok:
        text = _extract_text(payload, max_chars)
        if len(text) >= MIN_USABLE_CHARS:
            return text
        return f"{FETCH_ERROR} fetched {url} but content too thin ({len(text)} chars)"

    # If we skipped plain (needs_js) and scraping is unavailable, try plain as last resort
    if needs_js:
        ok2, payload2 = _fetch_plain(url)
        if ok2:
            text = _extract_text(payload2, max_chars)
            if len(text) >= MIN_USABLE_CHARS:
                return text

    return f"{FETCH_ERROR} could not read {url} (blocked/JS page and no working fallback)"


def _serper_search(query: str, num: int = 10) -> list[dict]:
    """One Google search via Serper. Returns organic results as
    [{title, link, snippet}]. Swap this single function for Brave / SerpAPI /
    Bing if you prefer — everything else is provider-agnostic."""
    if not SERPER_API_KEY:
        return []
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        return r.json().get("organic", [])
    except Exception:
        return []


def _search_exact(phrase: str, num: int = 10) -> list[dict]:
    """Exact-match (quoted) search — used by the verbatim fingerprint."""
    return _serper_search(f'"{phrase}"', num)


def verbatim_match(phrases: list[str]) -> str:
    """Take up to ~25 distinctive exact phrases from the JD, search each one in
    quotes, and score every URL by HOW MANY of the phrases it matched. A URL that
    hits a large share of the phrases is almost certainly the same posting under
    the real employer's name.

    Returns a JSON string: ranked URLs with matched/total counts, so the agent
    can go fetch the winner and read off the company name.
    """
    phrases = [p.strip() for p in phrases if p and len(p.strip()) > 12][:25]
    total = len(phrases)
    if total == 0:
        return json.dumps({"error": "no usable phrases supplied"})
    if not SERPER_API_KEY:
        return json.dumps({"error": "SERPER_API_KEY not set; cannot run exact search"})

    # url -> {matched_phrases, title, snippets}
    hits: dict[str, dict] = {}
    for phrase in phrases:
        for res in _search_exact(phrase):
            link = res.get("link")
            if not link:
                continue
            entry = hits.setdefault(link, {"matched": 0, "title": res.get("title", ""),
                                           "examples": []})
            entry["matched"] += 1
            if len(entry["examples"]) < 3:
                entry["examples"].append(phrase)

    ranked = sorted(
        ({"url": u, **v} for u, v in hits.items()),
        key=lambda d: d["matched"], reverse=True,
    )[:10]

    for r in ranked:
        r["match_score"] = round(100 * r["matched"] / total)
        r["matched_of_total"] = f"{r['matched']}/{total}"

    return json.dumps({
        "phrases_searched": total,
        "perfect_match_threshold": "match_score >= 80 means almost certainly the same posting",
        "ranked_urls": ranked,
    }, indent=2)


# Common job boards Google indexes — used for deliberate per-board sweeps.
JOB_BOARDS = ["linkedin.com/jobs", "indeed.com", "ziprecruiter.com",
              "glassdoor.com", "monster.com", "dice.com"]


def job_search(query: str, sites: list[str] | None = None, num: int = 10) -> str:
    """Broad (non-quoted) search for SIMILAR job postings.

    A plain Google query surfaces the same role across LinkedIn, Indeed,
    ZipRecruiter, Glassdoor and company career pages at once, because Google
    indexes them all. This catches the real employer's version even when it's
    WORDED DIFFERENTLY than the recruiter's copy (which exact-match would miss).

    Pass `sites` to deliberately sweep specific boards via Google's site: filter,
    or omit it for a single broad all-web search.
    """
    if not SERPER_API_KEY:
        return json.dumps({"error": "SERPER_API_KEY not set"})

    runs = [(s, f"{query} site:{s}") for s in sites] if sites else [("all_web", query)]
    out = {}
    for label, q in runs:
        out[label] = [
            {"title": r.get("title"), "link": r.get("link"), "snippet": r.get("snippet")}
            for r in _serper_search(q, num)
        ]
    return json.dumps({"query": query, "results_by_scope": out}, indent=2)


# Client-side tool the agent can call. Web search is a server-side tool the API
# runs automatically — we don't execute it here.
CUSTOM_TOOLS = [
    {
        "name": "verbatim_match",
        "description": (
            "Fingerprint the posting by exact-match search. Extract up to ~25 of "
            "the MOST distinctive verbatim phrases from the job description "
            "(8-15 words each, specific and unusual — NOT generic boilerplate like "
            "'competitive salary'), and pass them as a list. Each phrase is searched "
            "in quotes, and every URL is scored by how many phrases it matched. A URL "
            "with a high match_score (>=80) is almost certainly the SAME posting on the "
            "real employer's site. Use this FIRST, then fetch_url the top result to read "
            "off the company name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phrases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Up to ~25 distinctive verbatim phrases from the JD.",
                }
            },
            "required": ["phrases"],
        },
    },
    {
        "name": "job_search",
        "description": (
            "Broad Google search for SIMILAR job postings (NOT exact-match). Use this "
            "to find the same role posted under the real company's name across "
            "platforms — one Google query surfaces LinkedIn, Indeed, ZipRecruiter, "
            "Glassdoor and company career pages at once, because Google indexes them "
            "all. This catches the employer's version even when it's worded "
            "differently than the recruiter's copy. Build the query from job title + "
            "location + 1-2 distinctive terms (e.g. 'staff accountant St. Louis NetSuite "
            "hybrid'), no quotes. Optionally pass `sites` to sweep specific boards "
            "(e.g. ['linkedin.com/jobs','indeed.com']). Then fetch_url the results that "
            "look like the same role to find one that names the employer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Broad query: title + location + distinctive terms. No quotes.",
                },
                "sites": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional job-board domains to scope to via Google site: filter. Omit for broad all-web search.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch the visible text of a web page. Use this to read the original "
            "posting and to confirm candidate employers by reading their careers "
            "pages, LinkedIn results, or other postings you find via search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
]

TOOLS = CUSTOM_TOOLS + [{"type": "web_search_20250305", "name": "web_search"}]


def execute_custom_tool(name: str, tool_input: dict) -> str:
    if name == "fetch_url":
        return fetch_url(tool_input["url"])
    if name == "verbatim_match":
        return verbatim_match(tool_input.get("phrases", []))
    if name == "job_search":
        return job_search(tool_input["query"], tool_input.get("sites"))
    return f"Error: unknown tool '{name}'."


# ---------------------------------------------------------------------------
# The investigation methodology lives in the system prompt — this is the IP.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a recruiting business-development analyst. You are given a job posting \
that was published by a RECRUITING / STAFFING FIRM, which deliberately omits the \
name of the actual hiring company (the end client). Your job is to identify the \
real employer behind the posting using only public information.

METHODOLOGY (work through these in order, stop early once confident):

1. FINGERPRINT the posting, and separate signals by DISCRIMINATING POWER — how \
much each one narrows the field of possible employers:

   COMMODITY signals (weak — shared across the whole job category; they confirm \
   you're looking at the RIGHT TYPE of role but NEVER identify the employer):
   - Typical years of experience (e.g. "3-5 years")
   - Common tools / systems (Excel, NetSuite, Salesforce, QuickBooks)
   - Standard education and certs (Bachelor's, CPA, PMP)
   - Generic responsibilities and boilerplate perks

   DISTINCTIVE signals (strong — these actually fingerprint ONE requisition):
   - Verbatim unusual sentences / idiosyncratic phrasing
   - An UNUSUAL COMBINATION of otherwise-common requirements
   - Exact location / office address / "X days in office in [suburb]"
   - Exact comp figures or unusual benefit specifics
   - Named products, projects, internal systems, or team structure
   - A specific industry niche + size + growth-stage combination
   - Distinctive "about us" voice left in by the recruiter

   Core rule: similarity on COMMODITY signals alone means "same kind of job," NOT \
   "same employer." Many unrelated companies hire the same generic profile.

2. VERBATIM FINGERPRINT (highest yield — do this FIRST). Pull up to ~20-25 of the \
MOST distinctive verbatim phrases from the JD (8-15 words each, specific and \
unusual — skip generic boilerplate). Pass ALL of them to the verbatim_match tool \
in a single call. It searches each phrase in quotes and returns URLs ranked by how \
many phrases they matched. Interpret the result:
   - A URL with match_score >= 80 (matched most of the phrases) is almost \
certainly the SAME posting on the real employer's site. fetch_url it and read off \
the company name — this is a near-certain identification.
   - URLs matching a moderate share are worth fetching as candidates.
   - If the tool returns no strong matches, the JD was likely rewritten or has no \
public twin; move to signature search.

3. SIMILAR-JOBS SEARCH (run this even if verbatim found something — it confirms \
and widens). Use job_search with a broad query built from job title + location + \
1-2 distinctive terms (no quotes). One broad Google query pulls the same role from \
LinkedIn, Indeed, ZipRecruiter, Glassdoor and company career pages at once. This \
catches the employer's version even when it's WORDED DIFFERENTLY than the \
recruiter's copy, which verbatim_match would miss. To sweep boards deliberately, \
pass sites (e.g. ['linkedin.com/jobs','indeed.com']). For each result that looks \
like the same role, fetch_url it and apply the DISCRIMINATING-POWER test: it is \
only the twin if the DISTINCTIVE details line up (exact responsibility phrasing, \
unusual requirement combination, specific location, comp/benefit specifics, named \
products). If it matches only on COMMODITY signals (years of experience, common \
tools, standard degree/cert), it is a coincidental category neighbor — a DIFFERENT \
company hiring a similar person — NOT your employer. Discard it or mark it weak. \
You may also use the web_search tool for flexible board probing.

4. SIGNATURE SEARCH. If the above leave gaps, combine the strongest signals \
(location + role + industry + a distinctive perk or tool) and search for the \
employer directly. Then confirm by reading the candidate's careers page.

5. CROSS-REFERENCE. If useful, look at the same recruiting firm's other postings \
for shared signals that point to one client.

6. BUILD A SHORTLIST. Do not collapse to a single answer too early. Assemble the \
plausible candidate companies you found evidence for and RANK them most-likely \
first. Score by the RARITY of the matching signals, not the COUNT — one \
distinctive match outweighs ten commodity matches. Guidance:
   - match_score 85-100 / "high": verbatim twin, OR a named posting that matches \
     on multiple DISTINCTIVE signals.
   - match_score 50-84 / "medium": at least one solid distinctive signal lines up, \
     but not confirmed.
   - match_score below 50 / "low": only commodity overlap, or a single weak clue. \
     These are category neighbors — include them only as long-shots, clearly marked.
   A candidate supported ONLY by commodity signals must NOT exceed "low". Aim for \
the top 3-5 candidates when the evidence supports that many.

RULES:
- Each candidate MUST be backed by real evidence you actually found. Include \
FEWER than 5 — or even zero — rather than padding the list with companies you \
merely speculated about. A fabricated name poisons a business-development list, \
which is worse than a short list.
- Rank by match_score, highest first. The first candidate is the top pick.
- Cite the specific evidence for every candidate (which search, which page, which \
matching detail), tagging each as "distinctive" or "commodity".
- A candidate with no "distinctive" evidence cannot be ranked above "low", no \
matter how many commodity signals it shares.
- If you found nothing, return an empty candidates list and explain why in notes \
(e.g. JD was generic / rewritten, no public posting found).
- Once you've gathered and ranked what the evidence supports, STOP searching and \
output your final answer.

FINAL OUTPUT: end your last message with ONLY a JSON object, no other text:
{
  "candidates": [          // ranked, most-likely first, up to 5, may be empty
    {
      "company": string,
      "likelihood": "high" | "medium" | "low",
      "match_score": integer,            // 0-100
      "reasoning": string,
      "evidence": [ { "signal": string, "type": "distinctive" | "commodity", "source": string } ]
    }
  ],
  "top_pick": string or null,            // candidates[0].company, or null if empty
  "overall_confidence": "high" | "medium" | "low" | "unknown",
  "recruiting_firm": string or null,
  "notes": string
}
"""


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def investigate(url: str = None, description: str = None) -> dict:
    """Identify the real employer behind a recruiter posting.

    Provide a `description` (pasted JD text) and/or a `url`. If a description is
    given it is used directly — no scraping needed, which is the most reliable
    path. If only a url is given, the posting is fetched first.
    """
    if description and description.strip():
        # Pasted text — use it directly, no fetch required.
        original = description.strip()
        source_line = f"URL: {url}\n\n" if url else "(pasted job description; no URL)\n\n"
    elif url:
        # Pre-fetch the original posting so the agent starts with the JD in hand.
        original = fetch_url(url)
        # If we couldn't even read the source, stop with a clear status rather
        # than running the whole agent over an error string.
        if original.startswith(FETCH_ERROR):
            return {"candidates": [], "top_pick": None, "overall_confidence": "unknown",
                    "recruiting_firm": None,
                    "notes": f"Could not read source posting. {original}",
                    "fetch_failed": True}
        source_line = f"URL: {url}\n\n"
    else:
        return {"candidates": [], "top_pick": None, "overall_confidence": "unknown",
                "recruiting_firm": None,
                "notes": "No URL or description provided."}

    messages = [{
        "role": "user",
        "content": (
            f"Identify the real hiring company behind this recruiter posting.\n\n"
            f"{source_line}"
            f"POSTING TEXT:\n{original}"
        ),
    }]

    final_text = ""
    for _ in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"[agent] {block.text.strip()[:500]}")

        # stop_reason == "tool_use" only fires for CLIENT-side tools (fetch_url).
        # Web searches are run server-side and come back inside this same call.
        if response.stop_reason != "tool_use":
            final_text = "".join(b.text for b in response.content if b.type == "text")
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":   # our custom fetch_url
                print(f"[tool ] fetch_url({block.input.get('url')})")
                result = execute_custom_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})
    else:
        return {"candidates": [], "top_pick": None, "overall_confidence": "unknown",
                "recruiting_firm": None, "notes": "Hit iteration cap."}

    # Pull the JSON object out of the final message.
    match = re.search(r"\{.*\}", final_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {"candidates": [], "top_pick": None, "overall_confidence": "unknown",
            "recruiting_firm": None, "notes": final_text}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deanonymize_employer.py <job_posting_url>")
        sys.exit(1)

    result = investigate(url=sys.argv[1])
    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))
