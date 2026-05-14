You are checking whether a new story candidate duplicates any recent post on the Leviathan News Bot HQ (Telegram group).

CANDIDATE TOPIC:
<user_content>
{candidate_hint}
</user_content>

CANDIDATE URL:
<user_content>
{url}
</user_content>

RECENT BOT HQ HEADLINES (last {hours} hours, newest first):
<user_content>
{hq_headlines}
</user_content>

DUPLICATE = any HQ headline above covers the SAME underlying news event as the candidate, regardless of:
- which outlet each article comes from
- whether the framing differs (e.g. "X sues Y" vs "X bans Y employees" when both cover the same enforcement action)
- whether different entities are foregrounded (e.g. "NY AG sues Coinbase" and "NY/IL ban government employees from insider trading on prediction markets" — both are the same NY AG enforcement round)
- whether the wording rephrases the event (e.g. "60-day extension" vs "request more time to comment" on the same bill — the GENIUS Act / stablecoin bill comment period is one story)
- whether the bill is named differently (GENIUS Act = stablecoin bill = USD stablecoin oversight law — same piece of legislation)

NOT DUPLICATE = genuinely different events, even if they share a topic area:
- Two separate Iran developments on the same day
- Two separate Kelp exploits or exploit updates covering genuinely new facts
- A policy announcement vs. a later policy reversal

If in doubt, call it "duplicate". Posting a duplicate costs us reputation; skipping a legitimate post costs us one headline.

Respond with ONLY one of:
- duplicate
- not_duplicate

No explanation, no quotes, no markdown. A single token.
