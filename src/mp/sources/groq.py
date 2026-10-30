"""Groq -- classifier and rewriter, never a researcher.

Every call is constrained so the model cannot introduce a fact:
  * classification returns one value from a fixed enum;
  * rationale rewriting is validated against the input facts by
    sop/narrative.validate_against_facts and discarded if it drifts.

Absent GROQ_API_KEY, every function returns an empty result and callers fall back
to deterministic logic. The pipeline never depends on the model.
"""
from __future__ import annotations

import json

from .. import config
from ..budget import BudgetExceeded
from ..http import HttpClient

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
PROVIDER = "groq"

# Model ids are configured, not hard-coded: Groq's catalogue changes and an
# unavailable model returns 404, which is NOT a key problem and must not rotate.
def _model(tier: str = "fast") -> str:
    models = (config.budget()["providers"]["groq"].get("models") or {})
    return models.get(tier) or models.get("fast") or "openai/gpt-oss-20b"


# gpt-oss models are REASONING models: they emit internal reasoning tokens before
# any visible content, and those tokens count against max_tokens. A tight budget
# returns finish_reason="length" with content="" -- a silent empty answer. So we
# always add headroom for reasoning on top of the caller's desired answer length.
REASONING_HEADROOM = 700


def available(http: HttpClient) -> bool:
    return config.has_key(PROVIDER) and http.ledger.is_available(PROVIDER)


def _chat(http: HttpClient, system: str, user: str, *, context: str,
          max_tokens: int = 300, temperature: float = 0.0, tier: str = "fast") -> str:
    """Chat completion with key rotation.

    On a quota error the ledger retires the key; we then RETRY with the next one.
    Rotating without retrying would silently drop the request -- the bug this loop
    exists to prevent.
    """
    if not available(http):
        return ""

    for _ in range(len(config.keys(PROVIDER)) or 1):
        api_key = http.ledger.key_for(PROVIDER)
        if not api_key:
            return ""
        try:
            resp = http.request("POST", ENDPOINT, provider=PROVIDER,
                                headers={"Authorization": f"Bearer {api_key}",
                                         "Content-Type": "application/json"},
                                json_body={"model": _model(tier),
                                           "messages": [
                                               {"role": "system", "content": system},
                                               {"role": "user", "content": user}],
                                           "max_tokens": max_tokens + REASONING_HEADROOM,
                                           "temperature": temperature,
                                           "reasoning_effort": "low"},
                                context=context, retries=1)
        except BudgetExceeded:
            return ""
        if resp.ok:
            data = resp.json() or {}
            try:
                choice = data["choices"][0]
            except (KeyError, IndexError, TypeError):
                return ""
            content = (choice.get("message", {}).get("content") or "").strip()
            if not content and choice.get("finish_reason") == "length":
                # Truncated before any visible output -- treat as no answer rather
                # than returning a partial fragment the validator would reject.
                return ""
            return content
        # Not a quota problem (e.g. 404 unknown model) -> retrying other keys is futile.
        if api_key == http.ledger.key_for(PROVIDER):
            return ""
    return ""


def classify_company_type(http: HttpClient, name: str, description: str,
                          allowed: list[str]) -> tuple[str, str]:
    """Tie-break SS4 classification. Returns (type, reason); ('','') if unavailable.

    The model may ONLY return one of `allowed` or the literal UNCLEAR.
    """
    system = ("You classify music-industry companies. You may only answer with JSON "
              '{"type": <one of the allowed values or "UNCLEAR">, "reason": <short>}. '
              "Never invent facts. If the text does not clearly support a category, "
              'answer "UNCLEAR".')
    user = (f"Allowed values: {', '.join(allowed)}, UNCLEAR\n\n"
            f"Company name: {name}\nDescription: {description[:1500]}")
    raw = _chat(http, system, user, context=f"classify:{name}", max_tokens=150)
    if not raw:
        return "", ""
    try:
        parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, IndexError):
        return "", ""
    value = str(parsed.get("type", "")).strip()
    if value not in allowed:
        return "", str(parsed.get("reason", ""))
    return value, str(parsed.get("reason", ""))


def rewrite_rationale(http: HttpClient, kind: str, facts: dict, template: str) -> str:
    """Rewrite a rationale more naturally, using ONLY the supplied facts."""
    system = ("You rewrite a factual business rationale into one or two concise, "
              "professional sentences. You may ONLY use facts present in the supplied "
              "JSON. Never add numbers, company names, people or claims that are not "
              "in the JSON. No marketing language. Return the sentence only.")
    payload = {k: v for k, v in facts.items() if v not in (None, "", [], {})}
    user = (f"Kind: {kind}\nFacts JSON: {json.dumps(payload, default=str)[:2000]}\n"
            f"Current draft: {template}")
    return _chat(http, system, user, context=f"rationale:{kind}", max_tokens=180)


def adjudicate_review(http: HttpClient, entity: str, facts: dict,
                      missing: list[str]) -> tuple[str, str]:
    """Suggest a verdict for a REVIEW-queue row. Returns (verdict, reasoning).

    Advisory only: the human column in the review CSV is what the pipeline acts on.
    """
    system = ("You triage research records against a strict SOP. Answer ONLY with JSON "
              '{"verdict": "PASS"|"REJECT"|"RESEARCH_FURTHER", "reason": <short>}. '
              "Judge only from the supplied facts. If a required fact is missing, "
              'answer "RESEARCH_FURTHER". Never assume or invent information.')
    user = (f"Entity type: {entity}\nFacts: {json.dumps(facts, default=str)[:2000]}\n"
            f"Unresolved checks: {', '.join(missing)}")
    raw = _chat(http, system, user, context=f"adjudicate:{entity}", max_tokens=200)
    if not raw:
        return "", ""
    try:
        parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, IndexError):
        return "", ""
    verdict = str(parsed.get("verdict", "")).upper()
    if verdict not in ("PASS", "REJECT", "RESEARCH_FURTHER"):
        return "", str(parsed.get("reason", ""))
    return verdict, str(parsed.get("reason", ""))


def employer_match(http: HttpClient, company: dict, person_name: str,
                   title: str) -> tuple[str, str]:
    """Is this LinkedIn headline about THIS music company? -> (YES|NO|UNCLEAR, reason).

    People-name search returns anyone whose headline shares the company's name, and
    short or generic names ("CAM", "The Vault", "Jo & Co") collide with realtors,
    funds and agencies. The model only judges the supplied text; ('','') if
    unavailable, which callers must treat as not verified.
    """
    system = ("You check whether a LinkedIn headline shows the person currently works at a "
              "SPECIFIC music company. Answer only JSON "
              '{"match": "YES"|"NO"|"UNCLEAR", "reason": <short>}. '
              'YES only if the headline clearly refers to this same music company (not a '
              "different business that shares the name, e.g. a realtor, fund, agency, cafe, "
              "school or tech startup). A brand-new venture that merely contains the name (e.g. "
              "'Founder of Mute by JL' for the label Mute) is NO. NO if the headline names a "
              "different organisation or a non-music business, or the role is unrelated to "
              "running a music company (e.g. risk management at a bank). UNCLEAR if the headline is too vague to tell. Never guess.")
    user = (f"Company: {company.get('name')}\nCompany website: {company.get('domain') or 'unknown'}\n"
            f"Company type: {company.get('type') or 'music company'}\n"
            f"Country: {company.get('country') or 'unknown'}\n\n"
            f"Person: {person_name}\nLinkedIn headline / current position: {title}")
    # Strong tier: the fast model passed "Founder of Mute by JL" as Mute (label).
    raw = _chat(http, system, user, context=f"employer:{company.get('name')}:{person_name}",
                max_tokens=600, tier="strong")
    if not raw:
        return "", ""
    try:
        parsed = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, IndexError):
        return "", ""
    value = str(parsed.get("match", "")).strip().upper()
    if value not in ("YES", "NO", "UNCLEAR"):
        return "", str(parsed.get("reason", ""))
    return value, str(parsed.get("reason", ""))
