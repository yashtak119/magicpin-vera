# Vera — Merchant Messaging Bot
### magicpin AI Challenge Submission — Team: Kaustabhi

---

## What it does

A deterministic message composer for Vera, magicpin's merchant growth assistant. Given a category, merchant, trigger, and optional customer context, it returns a grounded, specific, ready-to-send message with a CTA, suppression key, send-as identity, and rationale.

## Approach

**Context-first, not template-first.**

Every message is composed from the actual context pushed via `/v1/context` — no hardcoded strings, no fabricated facts. The composer dispatches on trigger kind, then pulls specifics from:

- `CategoryContext` — voice rules, peer stats, digest items, offer catalog
- `MerchantContext` — owner name, performance metrics, active offers, customer aggregate, signals
- `TriggerContext` — kind, payload fields, urgency, suppression key
- `CustomerContext` (optional) — name, language preference, relationship state, consent scope

**Message shape follows the case study anchors:**

- Lead with the source or signal (not a generic opener)
- One specific finding with numbers from the actual context
- One concrete CTA with a named deliverable
- Source citation at end for research/compliance triggers
- `send_as: merchant_on_behalf` for customer-facing messages

## Architecture

Single-file Python HTTP server (`app.py`) using only stdlib — no dependencies, no LLM API calls at runtime. Fully deterministic for the same input.

| Endpoint | Purpose |
|---|---|
| `GET /v1/healthz` | Liveness check |
| `GET /v1/metadata` | Team and model info |
| `POST /v1/context` | Push category/merchant/customer/trigger context (idempotent by version) |
| `POST /v1/tick` | Compose and return actions for available triggers |
| `POST /v1/reply` | Handle merchant replies — auto-reply detection, intent transition, hostile handling |

## Trigger handlers

Covers all 25 seed trigger kinds plus expanded variants:

`research_digest`, `regulation_change`, `recall_due`, `perf_dip`, `perf_spike`, `renewal_due`, `festival_upcoming`, `wedding_package_followup`, `curious_ask_due`, `winback_eligible`, `ipl_match_today`, `review_theme_emerged`, `milestone_reached`, `active_planning_intent`, `seasonal_perf_dip`, `customer_lapsed_hard`, `trial_followup`, `supply_alert`, `chronic_refill_due`, `category_seasonal`, `gbp_unverified`, `cde_opportunity`, `competitor_opened`, `dormant_with_vera`, `appointment_tomorrow`

## Model choice

No LLM at inference time. Deterministic rule-based composition with:
- Trigger dispatch table
- Context field resolution with fallback chains
- Language-preference-aware code-mix (hi-en for Hindi-preferring customers)
- Taboo vocab filter per category voice rules
- Suppression key deduplication per session

**Tradeoff:** loses the flexibility of LLM-generated copy, gains full determinism, zero latency variance, zero hallucination risk, and no API cost per message.

## Conversation handling

- **Auto-reply detection**: backs off for 1800s on canned WhatsApp responses; ends after repeated detection
- **Intent transition**: switches to action mode on explicit merchant commitment ("yes", "let's do it")
- **Hostile handling**: ends conversation immediately on opt-out signals
- **Consent enforcement**: customer-facing messages respect `consent.scope` from CustomerContext

## Deployment

Hosted on Railway. Bot URL:
```
https://magicpin-vera-production-a048.up.railway.app
```
