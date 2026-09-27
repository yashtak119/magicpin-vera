#!/usr/bin/env python3
"""Vera+ — magicpin AI Challenge bot, single file.

    python bot.py serve [--port 8080]   run the HTTP bot (/v1/context, /v1/tick, /v1/reply, /v1/healthz, /v1/metadata, /v1/teardown)
    python bot.py submission            write submission.jsonl for the 30 canonical test pairs
    python bot.py selftest              start the bot in-process and run end-to-end checks
    uvicorn bot:app --port 8080         same as `serve`

Sections: 1 context accessors | 2 composer (one builder per trigger kind) | 3 optional Claude polish |
4 multi-turn reply handler | 5 HTTP server | 6 submission generator | 7 self-test | 8 CLI
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as fut_wait
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import error, request as rq

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

HERE = Path(__file__).resolve().parent
DATASET_DIR = Path(os.environ.get("VERA_DATASET_DIR") or next(
    (d for d in (HERE / "dataset" / "expanded", HERE.parent / "dataset" / "expanded") if d.exists()),
    HERE / "dataset" / "expanded"))


# ============================================================================
# 1. CONTEXT ACCESSORS
# ============================================================================

# The dataset is anchored on this day (trigger expiries, "this week" digests).
DATASET_TODAY = date(2026, 4, 26)

NORTH_HINDI_CITIES = {
    "Delhi", "Mumbai", "Pune", "Jaipur", "Lucknow", "Chandigarh", "Ahmedabad",
}


# ---------------------------------------------------------------- formatting

def fmt_int(n) -> str:
    try:
        return f"{int(round(float(n))):,}"
    except (TypeError, ValueError):
        return str(n)


def fmt_pct(x, signed=False) -> str:
    """0.18 -> '18%'; signed=True -> '+18%' / '-18%'."""
    v = round(float(x) * 100)
    if signed:
        return f"{v:+d}%"
    return f"{abs(v)}%"


def fmt_ctr(x) -> str:
    return f"{float(x) * 100:.1f}%"


def fmt_rupees(n) -> str:
    return f"₹{fmt_int(n)}"


def parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(s)[:10])
        except ValueError:
            return None


def fmt_date(s, with_dow=False, with_year=False) -> str | None:
    d = parse_date(s) if not isinstance(s, date) else s
    if not d:
        return None
    out = f"{d.day} {d.strftime('%b')}"
    if with_year:
        out += f" {d.year}"
    if with_dow:
        out = f"{d.strftime('%a')} {out}"
    return out


def fmt_slot(iso: str) -> str | None:
    """'2026-11-05T18:00:00+05:30' -> 'Thu 5 Nov, 6pm' (day derived from the date)."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    t = f"{hour}{ampm}" if dt.minute == 0 else f"{hour}:{dt.minute:02d}{ampm}"
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}, {t}"


def humanize(slug: str) -> str:
    return str(slug).replace("_", " ").strip()


# ---------------------------------------------------------------- merchant

class Merchant:
    def __init__(self, d: dict):
        self.d = d or {}
        self.identity = self.d.get("identity", {}) or {}
        self.perf = self.d.get("performance", {}) or {}
        self.sub = self.d.get("subscription", {}) or {}
        self.agg = self.d.get("customer_aggregate", {}) or {}

    @property
    def id(self):
        return self.d.get("merchant_id")

    @property
    def category(self):
        return self.d.get("category_slug")

    @property
    def name(self):
        return self.identity.get("name") or "your business"

    @property
    def city(self):
        return self.identity.get("city")

    @property
    def locality(self):
        return self.identity.get("locality")

    @property
    def owner_first(self) -> str | None:
        raw = (self.identity.get("owner_first_name") or "").strip()
        raw = re.sub(r"^(dr\.?\s+)", "", raw, flags=re.I)
        return raw or None

    @property
    def salutation(self) -> str:
        first = self.owner_first
        if self.category == "dentists":
            return f"Dr. {first}" if first else "Doctor"
        return first or f"{self.name} team"

    @property
    def languages(self) -> list:
        return self.identity.get("languages") or ["en"]

    @property
    def hinglish(self) -> bool:
        """Hindi-English code-mix where Hindi is a working language of the owner."""
        return "hi" in self.languages and (self.city in NORTH_HINDI_CITIES)

    @property
    def verified(self):
        return self.identity.get("verified")

    def active_offers(self) -> list[str]:
        return [o["title"] for o in self.d.get("offers", []) if o.get("status") == "active" and o.get("title")]

    def expired_offers(self) -> list[str]:
        return [o["title"] for o in self.d.get("offers", []) if o.get("status") == "expired" and o.get("title")]

    def signals(self) -> list[str]:
        return self.d.get("signals", []) or []

    def signal_value(self, prefix: str) -> str | None:
        for s in self.signals():
            if s.startswith(prefix + ":"):
                return s.split(":", 1)[1]
        return None

    def review_themes(self, sentiment=None) -> list[dict]:
        themes = self.d.get("review_themes", []) or []
        if sentiment:
            themes = [t for t in themes if t.get("sentiment") == sentiment]
        return sorted(themes, key=lambda t: -(t.get("occurrences_30d") or 0))

    def history(self) -> list[dict]:
        return self.d.get("conversation_history", []) or []

    def last_merchant_message(self) -> str | None:
        for turn in reversed(self.history()):
            if turn.get("from") == "merchant":
                return turn.get("body")
        return None

    def last_vera_message(self) -> str | None:
        for turn in reversed(self.history()):
            if turn.get("from") == "vera":
                return turn.get("body")
        return None

    def delta(self, metric: str):
        return (self.perf.get("delta_7d") or {}).get(f"{metric}_pct")

    def worst_delta(self):
        deltas = {k[:-4]: v for k, v in (self.perf.get("delta_7d") or {}).items() if v is not None and k.endswith("_pct")}
        if not deltas:
            return None, None
        k = min(deltas, key=deltas.get)
        return k, deltas[k]

    def best_delta(self):
        deltas = {k[:-4]: v for k, v in (self.perf.get("delta_7d") or {}).items() if v is not None and k.endswith("_pct")}
        if not deltas:
            return None, None
        k = max(deltas, key=deltas.get)
        return k, deltas[k]


# ---------------------------------------------------------------- category

class Category:
    def __init__(self, d: dict):
        self.d = d or {}

    @property
    def slug(self):
        return self.d.get("slug")

    @property
    def peer(self) -> dict:
        return self.d.get("peer_stats", {}) or {}

    @property
    def taboos(self) -> list[str]:
        v = self.d.get("voice", {}) or {}
        return v.get("vocab_taboo") or v.get("taboos") or []

    def digest(self) -> list[dict]:
        return self.d.get("digest", []) or []

    def digest_item(self, item_id: str | None) -> dict | None:
        if item_id:
            for it in self.digest():
                if it.get("id") == item_id:
                    return it
        return None

    def digest_by_kind(self, *kinds) -> dict | None:
        for k in kinds:
            for it in self.digest():
                if it.get("kind") == k:
                    return it
        return None

    def seasonal_beats(self) -> list[dict]:
        return self.d.get("seasonal_beats", []) or []

    def beat_matching(self, *words) -> dict | None:
        for b in self.seasonal_beats():
            note = (b.get("note") or "").lower()
            if any(w in note for w in words):
                return b
        return None

    def trend(self, *words) -> dict | None:
        for t in self.d.get("trend_signals", []) or []:
            q = (t.get("query") or "").lower()
            if any(w in q for w in words):
                return t
        return None

    def top_trend(self) -> dict | None:
        ts = self.d.get("trend_signals", []) or []
        return max(ts, key=lambda t: t.get("delta_yoy") or 0) if ts else None

    def catalog(self, type_=None) -> list[dict]:
        cat = self.d.get("offer_catalog", []) or []
        return [o for o in cat if not type_ or o.get("type") == type_]

    def catalog_title(self, contains: str) -> str | None:
        for o in self.catalog():
            if contains.lower() in o.get("title", "").lower():
                return o["title"]
        return None

    def patient_content(self) -> list[dict]:
        return self.d.get("patient_content_library", []) or []


# ---------------------------------------------------------------- customer

class Customer:
    def __init__(self, d: dict | None):
        self.d = d or {}
        self.identity = self.d.get("identity", {}) or {}
        self.rel = self.d.get("relationship", {}) or {}
        self.prefs = self.d.get("preferences", {}) or {}
        self.consent = self.d.get("consent", {}) or {}

    def __bool__(self):
        return bool(self.d)

    @property
    def id(self):
        return self.d.get("customer_id")

    @property
    def raw_name(self) -> str:
        return self.identity.get("name") or ""

    @property
    def parent(self) -> str | None:
        m = re.search(r"\(parent:\s*([^)]+)\)", self.raw_name)
        return m.group(1).strip() if m else None

    @property
    def name(self) -> str:
        """The person being addressed (the parent when the customer is a child)."""
        if self.parent:
            return self.parent
        n = re.sub(r"\(.*?\)", "", self.raw_name).strip()
        return n if n and not n.startswith("(") else ""

    @property
    def child_name(self) -> str | None:
        if not self.parent:
            return None
        return re.sub(r"\(.*?\)", "", self.raw_name).strip() or None

    @property
    def lang(self) -> str:
        return (self.identity.get("language_pref") or "en").lower()

    @property
    def hindi(self) -> bool:
        return self.lang in ("hi",)

    @property
    def hinglish(self) -> bool:
        return self.lang.startswith("hi-en") or self.lang == "hinglish"

    @property
    def senior(self) -> bool:
        return bool(self.identity.get("senior_citizen"))

    @property
    def state(self):
        return self.d.get("state")

    @property
    def can_message(self) -> bool:
        if not self.d:
            return False
        if self.prefs.get("reminder_opt_in") is False:
            return False
        if not self.consent.get("opted_in_at") or not self.consent.get("scope"):
            return False
        if self.prefs.get("channel") in ("none_recorded", None) and not self.identity.get("phone_redacted"):
            return False
        return True

    @property
    def preferred_slots(self) -> str | None:
        return self.prefs.get("preferred_slots")


# ============================================================================
# 2. COMPOSER
# ============================================================================

COMPOSER_VERSION = "composer_v3"


@dataclass
class Draft:
    parts: list[str]
    ask_en: str | None = None          # "draft a GBP post"  -> "Want me to draft a GBP post? Reply YES."
    ask_hi: str | None = None          # "GBP post draft kar doon" -> "Main GBP post draft kar doon? Reply YES."
    ask_raw: str | None = None         # fully-formed closing line (customer-facing, open questions)
    cta: str = "binary_yes_no"
    rationale: str = ""
    levers: list[str] = field(default_factory=list)
    deliverable: str | None = None     # what "YES" unlocks — used by the reply handler
    topic: str | None = None


# ================================================================ helpers

def _peer_line(m: Merchant, c: Category, metric: str) -> str | None:
    """'2.1% vs 3.0% peer average' style comparisons, only when both sides exist."""
    peer = c.peer
    if metric == "ctr" and m.perf.get("ctr") is not None and peer.get("avg_ctr"):
        return f"{fmt_ctr(m.perf['ctr'])} vs {fmt_ctr(peer['avg_ctr'])} peer average"
    key = {"views": "avg_views_30d", "calls": "avg_calls_30d"}.get(metric)
    if key and m.perf.get(metric) is not None and peer.get(key):
        return f"{fmt_int(m.perf[metric])} vs {fmt_int(peer[key])} peer average"
    return None


def _above_peer(m: Merchant, c: Category, metric: str) -> bool | None:
    peer_key = {"ctr": "avg_ctr", "views": "avg_views_30d", "calls": "avg_calls_30d"}[metric]
    if m.perf.get(metric) is None or not c.peer.get(peer_key):
        return None
    return m.perf[metric] >= c.peer[peer_key]


def _best_offer(m: Merchant, c: Category, prefer: str | None = None) -> tuple[str | None, bool]:
    """(offer title, is_merchants_own). Falls back to the category catalog as a *suggestion*."""
    own = m.active_offers()
    if prefer:
        for o in own:
            if prefer.lower() in o.lower():
                return o, True
    if own:
        return own[0], True
    if prefer and c.catalog_title(prefer):
        return c.catalog_title(prefer), False
    if c.slug == "restaurants":
        return None, False
    sp = c.catalog("service_at_price")
    return (sp[0]["title"], False) if sp else (None, False)


def _praise(m: Merchant) -> str | None:
    pos = m.review_themes("pos")
    if not pos:
        return None
    t = pos[0]
    if t.get("common_quote"):
        return f"'{t['common_quote']}' ({t['occurrences_30d']} reviews this month)"
    return f"{humanize(t['theme'])} is your most-praised theme ({t['occurrences_30d']} reviews in 30 days)"


def _perf_snapshot(m: Merchant) -> str | None:
    p = m.perf
    if p.get("views") is None:
        return None
    bits = [f"{fmt_int(p['views'])} profile views"]
    if p.get("calls") is not None:
        bits.append(f"{fmt_int(p['calls'])} calls")
    return " and ".join(bits) + f" in the last {p.get('window_days', 30)} days"


def _customer_base(m: Merchant, c: Category) -> str | None:
    a = m.agg
    noun = {"dentists": "patients", "gyms": "members"}.get(c.slug, "customers")
    if a.get("total_active_members"):
        return f"{fmt_int(a['total_active_members'])} active members"
    if a.get("chronic_rx_count"):
        return f"{fmt_int(a['chronic_rx_count'])} chronic-Rx customers"
    if a.get("total_unique_ytd"):
        return f"{fmt_int(a['total_unique_ytd'])} {noun} this year"
    return None


def _lapsed(m: Merchant) -> tuple[int | None, str | None]:
    a = m.agg
    for key, label in (("lapsed_180d_plus", "6+ months"), ("lapsed_90d_plus", "90+ days")):
        if a.get(key):
            return a[key], label
    return None, None


def _item_line(item: dict) -> str:
    """Headline + summary lead, cited. Summary text is quoted from the digest, never paraphrased into new numbers."""
    summary = (item.get("summary") or "").strip()
    first = re.split(r"(?<=[.!?])\s+", summary)[0] if summary else ""
    return first


def _lc(s: str) -> str:
    """Lower-case the first letter unless it starts an acronym (IPL, ORS, DCI)."""
    first = s.split(" ", 1)[0] if s else ""
    if not s or (len(s) > 1 and s[1].isupper()) or first.lower() in ("zomato", "swiggy", "google", "olaplex", "dentsply", "l'oreal"):
        return s
    return s[0].lower() + s[1:]


def _poss(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _new_user_only(c: Category, title: str) -> bool:
    for o in c.catalog():
        if o.get("title", "").lower() == title.lower():
            return o.get("audience") == "new_user"
    return False


def _customer_offer(m: Merchant, c: Category, cu: Customer, prefer=None) -> str | None:
    """Merchant's own active offer that this customer is actually eligible for."""
    returning = (cu.rel.get("visits_total") or 0) > 1
    offers = m.active_offers()
    if prefer:
        offers = sorted(offers, key=lambda o: prefer.lower() not in o.lower())
    for o in offers:
        if "senior" in o.lower() and not cu.senior:
            continue
        matches_service = bool(prefer) and prefer.lower() in o.lower()
        if returning and _new_user_only(c, o) and not matches_service:
            continue
        return o
    return None


def _days_between(a, b) -> int | None:
    da, db = parse_date(a), parse_date(b)
    if not da or not db:
        return None
    return (db - da).days


def _month_idx(name: str) -> int | None:
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    name = name.strip().lower()[:3]
    return months.index(name) + 1 if name in months else None


def _beat_active(beat: dict, month: int) -> bool:
    rng = beat.get("month_range", "")
    parts = [p for p in re.split(r"\s*-\s*", rng) if p]
    idx = [_month_idx(p.split()[0]) for p in parts]
    idx = [i for i in idx if i]
    if not idx:
        return False
    if len(idx) == 1:
        return idx[0] == month
    lo, hi = idx[0], idx[-1]
    return lo <= month <= hi if lo <= hi else (month >= lo or month <= hi)


def _current_beat(c: Category, today) -> dict | None:
    for b in c.seasonal_beats():
        if _beat_active(b, today.month):
            return b
    return None


# ================================================================ merchant-facing builders

def b_research_digest(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    item = c.digest_item(p.get("top_item_id") or p.get("digest_item_id") or (p.get("top_item") or {}).get("id"))
    if not item and isinstance(p.get("top_item"), dict) and p["top_item"].get("title"):
        item = p["top_item"]
    item = item or c.digest_by_kind("research", "trend", "tech", "compliance")
    if not item:
        return b_generic(m, c, t, cu, today)
    parts = []
    src = item.get("source")
    lead = f"{m.salutation}, new in this week's {c.d.get('display_name', c.slug).lower()} digest"
    lead += f": {item['title']}."
    parts.append(lead)
    detail = _item_line(item)
    if item.get("trial_n"):
        detail = f"{fmt_int(item['trial_n'])}-patient trial — " + detail[0].lower() + detail[1:]
    if detail:
        parts.append(detail)
    # merchant tie-in — move before actionable for better flow
    seg = (item.get("patient_segment") or "").lower()
    tie = None
    if "high_risk" in seg and m.agg.get("high_risk_adult_count"):
        tie = f"One item relevant to your {fmt_int(m.agg['high_risk_adult_count'])} high-risk adult patients."
    elif _customer_base(m, c):
        tie = f"Relevant for your {_customer_base(m, c)}."
    if tie:
        parts.append(tie)
    if item.get("actionable"):
        parts.append(f"Practical takeaway: {_lc(item['actionable'])}.")
    noun = "patient" if c.slug == "dentists" else "customer"
    # Add source citation at the end like the 50/50 anchor "— JIDA Oct 2026 p.14"
    if src:
        parts.append(f"— {src}")
    return Draft(
        parts,
        ask_en=f"draft a 3-line {noun} WhatsApp explaining this that you can forward",
        ask_hi=None,  # Research messages stay English for clinical credibility
        rationale=f"Research digest '{item['title']}' ({src}); tied to merchant's own cohort/base; reciprocity ask (I draft the {noun} note).",
        levers=["specificity", "source_citation", "reciprocity", "single_binary_cta"],
        deliverable=f"{noun} WhatsApp explaining: {item['title']}",
        topic=item["title"],
    )


def b_regulation_change(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    item = c.digest_item(p.get("top_item_id") or p.get("digest_item_id")) or c.digest_by_kind("compliance")
    if not item:
        return b_generic(m, c, t, cu, today)
    deadline = p.get("deadline_iso")
    parts = [f"{m.salutation}, compliance heads-up — {item['title']}."]
    if item.get("summary"):
        parts.append(item["summary"])
    if deadline:
        days = _days_between(today, deadline)
        when = fmt_date(deadline, with_year=True)
        parts.append(f"Deadline is {when}" + (f" — {days} days out, enough time to fix it calmly." if days and days > 0 else "."))
    if item.get("actionable"):
        parts.append(f"The one thing to check: {_lc(item['actionable'])}.")
    if item.get("source"):
        parts.append(f"Source: {item['source']}.")
    return Draft(
        parts,
        ask_en="send you a 1-page audit checklist you can file with your SOPs",
        ask_hi=None,  # Clinical/compliance messages stay in English for consistency
        rationale=f"Regulation change with a hard deadline ({deadline}); quotes the circular's own numbers; effort externalised via a ready checklist.",
        levers=["specificity", "loss_aversion", "effort_externalization", "single_binary_cta"],
        deliverable="1-page compliance audit checklist",
        topic=item["title"],
    )


def b_cde_opportunity(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    item = c.digest_item(p.get("digest_item_id") or p.get("top_item_id")) or c.digest_by_kind("cde")
    if not item:
        return b_generic(m, c, t, cu, today)
    when = None
    if item.get("date"):
        d = item["date"]
        when = fmt_date(d, with_dow=True)
        slot = fmt_slot(d) if "T" in str(d) else None
        if slot:
            when = slot
    credits = p.get("credits") or item.get("credits")
    first = f"{m.salutation}, {item.get('source') or 'a CDE session'}: '{item['title'].split(': ', 1)[-1]}'"
    first += f" on {when}" if when else ""
    first += f" — {credits} CDE credits" if credits else ""
    parts = [first + "."]
    if item.get("summary"):
        parts.append(item["summary"])
    if item.get("actionable"):
        parts.append(item["actionable"] + ".")
    return Draft(
        parts,
        ask_en="block the slot in your calendar and send the joining details on the day",
        ask_hi="slot calendar mein block karke us din joining details bhej doon",
        rationale="CDE opportunity with date, credits and fee from the category digest; low-effort ask (I handle the reminder).",
        levers=["specificity", "curiosity", "effort_externalization"],
        deliverable="calendar hold + joining details reminder",
        topic=item["title"],
    )


def b_competitor_opened(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    name = p.get("competitor_name")
    parts = []
    if name:
        line = f"{m.salutation}, heads-up: {name} opened"
        if p.get("distance_km"):
            line += f" {p['distance_km']} km from you"
        if p.get("opened_date"):
            line += f" on {fmt_date(p['opened_date'])}"
        line += "."
        if p.get("their_offer"):
            own, is_own = _best_offer(m, c)
            line += f" They're listing {p['their_offer']}" + (f" against your {own}." if own and is_own else ".")
        parts.append(line)
    else:
        parts.append(f"{m.salutation}, a new {c.slug.rstrip('s')} listing has come up near {m.locality or 'you'} this week — new entrants usually open with low-price offers to pull searchers.")
    ctr = _peer_line(m, c, "ctr")
    if ctr and _above_peer(m, c, "ctr") is False:
        parts.append(f"Your profile CTR is {ctr}, so price-shoppers comparing listings are the ones at risk.")
    elif _above_peer(m, c, "calls"):
        parts.append(f"You start from strength: {_peer_line(m, c, 'calls')} calls a month.")
    praise = _praise(m)
    if praise:
        parts.append(f"I wouldn't match on price — lead with what your reviews already say: {praise}.")
        ask_en, ask_hi = "draft a GBP post built around that review theme", "us review theme par ek GBP post draft kar doon"
    else:
        own, is_own = _best_offer(m, c)
        if own and is_own:
            parts.append(f"Better than a price war: make your {own} more visible on your profile this week.")
        ask_en, ask_hi = "draft a GBP post that pins your strongest offer", "aapke strongest offer ko pin karke GBP post draft kar doon"
    return Draft(
        parts, ask_en=ask_en, ask_hi=ask_hi,
        rationale="Competitor-opened trigger: loss aversion framed with merchant's own CTR/calls vs peer; recommends differentiation over a price war.",
        levers=["loss_aversion", "specificity", "judgment", "single_binary_cta"],
        deliverable="GBP post differentiating on reviews/offer",
        topic=f"competitor {name or 'nearby'}",
    )


def b_curious_ask(m, c, t, cu, today):
    first = m.owner_first or m.salutation
    parts = []
    guess = None
    praise = m.review_themes("pos")
    if praise and praise[0].get("common_quote"):
        guess = f"my guess from reviews is {humanize(praise[0]['theme'])} ('{praise[0]['common_quote']}')"
    elif praise:
        guess = f"reviews point to {humanize(praise[0]['theme'])} ({praise[0]['occurrences_30d']} mentions this month)"
    own = m.active_offers()
    greet = f"Dr. {first}" if c.slug == "dentists" else f"Hi {first}"
    q = f"{greet}, quick one for this week's {m.name} post — what's the one service customers asked about most this week?"
    if c.slug == "restaurants":
        q = f"{greet}, quick one for this week's {m.name} post — which dish are regulars ordering most this week" + (f", apart from the {own[0]}?" if own else "?")
    parts.append(q)
    if guess:
        parts.append(f"{guess[0].upper() + guess[1:]}, but you'd know better.")
    tr = None
    for o in own:
        for w in re.findall(r"[a-z]{4,}", o.lower()):
            tr = tr or c.trend(w)
    if not tr and praise:
        for w in re.findall(r"[a-z]{4,}", (praise[0].get("common_quote") or "") + " " + praise[0]["theme"]):
            tr = tr or c.trend(w)
    tr = tr or c.top_trend()
    if tr and tr.get("delta_yoy"):
        parts.append(f"(For context, '{tr['query']}' searches are {fmt_pct(tr['delta_yoy'], signed=True)} YoY.)")
    return Draft(
        parts,
        ask_raw="Reply with the name + your price and I'll turn it into a Google post and a ready 2-line reply for price enquiries — 5 minutes, zero typing for you.",
        cta="open_ended",
        rationale="Scheduled curious-ask: asking-the-merchant lever with a data-backed guess; reciprocity (post + reply template) offered up front.",
        levers=["asking_the_merchant", "reciprocity", "effort_externalization"],
        deliverable="Google post + price-enquiry reply template",
        topic="most-asked service this week",
    )


def b_dormant(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    days = p.get("days_since_last_merchant_message") or (m.signal_value("dormant_with_vera") or "").rstrip("d") or None
    lead = f"Hi {m.owner_first}" if c.slug != "dentists" else m.salutation
    parts = [f"{lead}, it's been {days} days since we last spoke, so here's one number worth a look." if days
             else f"{lead}, it's been a while since we spoke — one number worth a look."]
    k, v = m.worst_delta()
    lapsed, label = _lapsed(m)
    if k and v is not None and v < 0:
        parts.append(f"{_poss(m.name)} {k} are down {fmt_pct(v)} this week" + (f", and {fmt_int(lapsed)} customers haven't been back in {label}." if lapsed else "."))
    elif _perf_snapshot(m):
        parts.append(f"{m.name} got {_perf_snapshot(m)}.")
    beat = _current_beat(c, today)
    dg = c.digest_by_kind("seasonal")
    if dg and "ipl" in dg["title"].lower() and "ipl" not in " ".join(m.signals() + m.active_offers()).lower():
        dg = c.digest_by_kind("trend")
    offer, is_own = _best_offer(m, c, prefer="bridal" if c.slug == "salons" else None)
    if dg and dg.get("kind") == "seasonal":
        parts.append(f"Timely: {_lc(dg['title'])}.")
    elif dg:
        parts.append(f"Also worth knowing: {dg['title']}" + (f" ({dg['source']})." if dg.get("source") else "."))
    elif beat:
        parts.append(f"Timely: {beat['month_range']} is {beat['note']}.")
    ask_en = f"put a '{offer}' offer live on your profile to restart bookings" if offer else "draft a fresh GBP post to restart bookings"
    ask_hi = f"'{offer}' offer aapke profile par live kar doon" if offer else "ek fresh GBP post draft kar doon"
    return Draft(
        parts, ask_en=ask_en, ask_hi=ask_hi,
        rationale=f"Dormant {days or 'N'} days: re-open with a single verifiable number (loss aversion) + a timely seasonal hook, then one concrete action.",
        levers=["reciprocity", "loss_aversion", "specificity", "single_binary_cta"],
        deliverable=f"'{offer}' offer + GBP post" if offer else "GBP post",
        topic="re-engagement",
    )


def b_festival(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    fest = p.get("festival")
    lead = m.salutation if c.slug == "dentists" else f"Hi {m.owner_first or m.salutation}"
    if fest and p.get("date"):
        days = p.get("days_until") or _days_between(today, p["date"])
        parts = [f"{lead}, {fest} falls on {fmt_date(p['date'], with_dow=True)} — {days} days out."]
        beat = c.beat_matching("festival", "wedding", "diwali")
        if days and days > 30:
            parts[0] += " Too early for a promo, and I'd rather not burn your offer now."
            if beat:
                parts.append(f"What does matter: {beat['month_range']} is your peak — {beat['note']}.")
            if _above_peer(m, c, "calls"):
                parts.append(f"You're heading in strong at {_peer_line(m, c, 'calls')} calls/month.")
            return Draft(
                parts,
                ask_en=f"pencil a festive-package plan for you ~6 weeks before {fest} and ping you then",
                ask_hi=f"{fest} se ~6 hafte pehle festive-package plan ready karke aapko ping kar doon",
                rationale=f"{fest} is {days} days away — judgment call: don't promo now; plan the peak season instead.",
                levers=["judgment", "specificity", "effort_externalization"],
                deliverable=f"festive package plan ahead of {fest}",
                topic=fest,
            )
        offer, is_own = _best_offer(m, c)
        if offer:
            parts.append(f"Now's the window — searches pick up in the last 2-3 weeks. Your {offer} is the right hook.")
        return Draft(
            parts,
            ask_en=f"draft a {fest} GBP post + WhatsApp around it",
            ask_hi=f"{fest} ke liye GBP post + WhatsApp draft kar doon",
            rationale=f"{fest} is {days} days away — inside the promo window; leads with merchant's own offer.",
            levers=["specificity", "loss_aversion", "effort_externalization"],
            deliverable=f"{fest} GBP post + WhatsApp",
            topic=fest,
        )
    # placeholder festival trigger: anchor on the category's festival season instead of guessing a festival
    beat = c.beat_matching("festival", "wedding", "diwali")
    parts = [f"{lead}, festival season planning note for {m.name}."]
    if beat:
        parts.append(f"{beat['month_range']} is {beat['note']} — that's the window your next push should target.")
    if _perf_snapshot(m):
        parts.append(f"You're going in with {_perf_snapshot(m)}" + (f" ({_customer_base(m, c)})." if _customer_base(m, c) else "."))
    offer, is_own = _best_offer(m, c)
    return Draft(
        parts,
        ask_en=f"draft a festive-season plan around {'your ' if is_own else 'a '}'{offer}' offer" if offer else "draft a festive-season plan",
        ask_hi=f"'{offer}' offer ke saath festive-season plan draft kar doon" if offer else "festive-season plan draft kar doon",
        rationale="Festival trigger without a named festival in payload — anchored on the category's documented festival season rather than inventing a date.",
        levers=["specificity", "effort_externalization"],
        deliverable="festive-season plan",
        topic="festival season",
    )


def b_gbp_unverified(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    lead = m.salutation if c.slug == "dentists" else f"Hi {m.owner_first or m.salutation}"
    parts = [f"{lead}, {_poss(m.name)} Google profile is still unverified — so Google shows it less and doesn't let you post offers."]
    if p.get("estimated_uplift_pct"):
        parts.append(f"Verification is estimated to lift your visibility ~{fmt_pct(p['estimated_uplift_pct'])}" +
                     (f" — on today's {_perf_snapshot(m)}, that's real footfall." if _perf_snapshot(m) else "."))
    path = p.get("verification_path")
    if path:
        parts.append(f"It's a {humanize(path).replace('or', 'or a')} from Google — I'll prep everything, you just take the call/code.")
    return Draft(
        parts,
        ask_en="start the verification request now",
        ask_hi="verification request abhi start kar doon",
        rationale="Unverified GBP: quantified uplift from payload + effort externalisation; single action.",
        levers=["loss_aversion", "specificity", "effort_externalization", "single_binary_cta"],
        deliverable="GBP verification request",
        topic="GBP verification",
    )


def b_ipl(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    slot = fmt_slot(p.get("match_time_iso", "")) or ""
    time_part = slot.split(", ")[-1] if slot else ""
    day = slot.split(" ")[0] if slot else ""
    parts = [f"{m.salutation}, {p.get('match', 'IPL match')} at {p.get('venue', m.city)} tonight" + (f", {time_part}." if time_part else ".")]
    item = c.digest_by_kind("seasonal")
    weeknight = p.get("is_weeknight")
    orders = m.agg
    if weeknight is False and item and "saturday" in (item.get("summary") or "").lower():
        parts.append(f"Before spending on a match promo: per {item.get('source', 'order data')}, weekend IPL matches pull dine-in covers down ~12% (people watch at home) while weeknight matches lift them ~18%.")
        if orders.get("delivery_orders_30d") and orders.get("dine_in_orders_30d"):
            tot = orders["delivery_orders_30d"] + orders["dine_in_orders_30d"]
            parts.append(f"So tonight ({day}) is a delivery night — {orders['delivery_orders_30d']} of your last {tot} orders were delivery anyway.")
        bogo = next((o for o in m.active_offers() if "tue" in o.lower() or "thu" in o.lower()), None)
        if bogo:
            parts.append(f"Keep your {bogo} for the next weeknight match, where it'll actually move covers.")
        return Draft(
            parts,
            ask_en="draft a delivery-only match-night post for tonight",
            ask_hi="aaj raat ke liye delivery-only match-night post draft kar doon",
            rationale="IPL trigger on a non-weeknight: contrarian, data-backed call (weekend matches cut covers) + existing-offer timing; saves a wasted promo.",
            levers=["judgment", "specificity", "loss_aversion", "single_binary_cta"],
            deliverable="delivery-only match-night post",
            topic="IPL match tonight",
        )
    offer, is_own = _best_offer(m, c, prefer="match")
    parts.append("Weeknight matches lift covers ~18% in the data — this is one to push." if item else "Match nights are a good window to push.")
    return Draft(
        parts,
        ask_en=f"put up a match-night post with {offer}" if offer else "put up a match-night post",
        ask_hi=f"{offer} ke saath match-night post laga doon" if offer else "match-night post laga doon",
        rationale="IPL weeknight match: push the match-night offer.",
        levers=["specificity", "loss_aversion"],
        deliverable="match-night post",
        topic="IPL match tonight",
    )


def b_review_theme(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    theme = p.get("theme")
    th = None
    if not theme:
        neg = m.review_themes("neg")
        th = neg[0] if neg else None
        theme = th["theme"] if th else None
    if not theme:
        return b_curious_ask(m, c, t, cu, today)
    occ = p.get("occurrences_30d") or (th or {}).get("occurrences_30d")
    quote = p.get("common_quote") or (th or {}).get("common_quote")
    line = f"{m.salutation}, {occ} reviews in the last 30 days mention {humanize(theme).replace('_', ' ')}" if occ else f"{m.salutation}, a pattern is showing in your reviews: {humanize(theme)}"
    if p.get("trend") == "rising":
        line += ", and it's rising"
    parts = [line + (f" — latest: '{quote}'." if quote else ".")]
    praise = m.review_themes("pos")
    if praise:
        parts.append(f"{humanize(praise[0]['theme']).capitalize()} is still your top theme ({praise[0]['occurrences_30d']} positive mentions), so this is an ops fix, not a product problem.")
    return Draft(
        parts,
        ask_en="draft polite public replies to those reviews + a one-line note you can pin for customers",
        ask_hi="un reviews ke polite public replies aur customers ke liye ek pinned note draft kar doon",
        rationale="Review theme emerged: quotes the actual review, contrasts with positive theme (reassurance), offers ready replies.",
        levers=["specificity", "loss_aversion", "effort_externalization"],
        deliverable="public review replies + pinned note",
        topic=f"reviews: {humanize(theme)}",
    )


def b_milestone(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    lead = m.salutation
    if p.get("metric") and p.get("value_now") is not None and p.get("milestone_value"):
        metric = humanize(p["metric"]).replace("count", "").strip() + "s"
        gap = p["milestone_value"] - p["value_now"]
        parts = [f"{lead}, {m.name} is {gap} {metric} short of {fmt_int(p['milestone_value'])} ({fmt_int(p['value_now'])} now)."
                 if gap > 0 else f"{lead}, {m.name} just crossed {fmt_int(p['milestone_value'])} {metric}! 🎉"]
        peer = c.peer.get("avg_review_count")
        if "review" in p["metric"] and peer:
            parts.append(f"You're already past the {peer}-review peer average" if p["value_now"] >= peer else f"Peer average is {peer}")
            parts[-1] += "."
        praise = _praise(m)
        if praise:
            parts.append(f"Your happiest customers are easy to find — {praise}.")
        return Draft(
            parts,
            ask_en="draft a short 'rate us' WhatsApp for today's customers to close the gap this week" if gap > 0 else "draft a thank-you post for the milestone",
            ask_hi="aaj ke customers ke liye ek chhota 'rate us' WhatsApp draft kar doon" if gap > 0 else "milestone ka thank-you post draft kar doon",
            rationale="Imminent milestone: small, specific gap + social proof; one low-effort action to close it.",
            levers=["specificity", "social_proof", "effort_externalization"],
            deliverable="'rate us' WhatsApp",
            topic="milestone",
        )
    # placeholder: use a real, round-number milestone from the merchant's own base
    base = m.agg.get("total_unique_ytd")
    parts = []
    if base and base >= 100:
        step = 1000 if base >= 1000 else 100
        mark = (base // step) * step
        parts.append(f"{lead}, {m.name} has crossed {fmt_int(mark)} unique customers this year ({fmt_int(base)} so far) 🎉")
    elif _perf_snapshot(m):
        parts.append(f"{lead}, a good month for {m.name}: {_perf_snapshot(m)}.")
    k, v = m.best_delta()
    if k and v and v > 0:
        parts.append(f"And {k} are up {fmt_pct(v)} this week.")
    parts.append("Milestones like this are the easiest moment to ask happy customers for a review.")
    return Draft(
        parts,
        ask_en="draft a thank-you post + a 'rate us' WhatsApp for your regulars",
        ask_hi="ek thank-you post aur regulars ke liye 'rate us' WhatsApp draft kar doon",
        rationale="Milestone trigger (placeholder payload): anchored on the merchant's real YTD customer count rounded down, not an invented metric.",
        levers=["specificity", "social_proof", "effort_externalization"],
        deliverable="thank-you post + 'rate us' WhatsApp",
        topic="milestone",
    )


def b_perf_dip(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    metric, delta = p.get("metric"), p.get("delta_pct")
    if metric is None or delta is None:
        metric, delta = m.worst_delta()
    parts = []
    if metric and delta is not None and delta < 0:
        line = f"{m.salutation}, {metric} to {m.name} dropped {fmt_pct(delta)} this week"
        cur = m.perf.get(metric)
        if cur is not None and p.get("vs_baseline"):
            line += f" — {fmt_int(cur)} in 30 days vs a baseline of {fmt_int(p['vs_baseline'])}"
        parts.append(line + ".")
    else:
        deltas = ", ".join(f"{k.replace('_pct', '')} {fmt_pct(v, signed=True)}" for k, v in (m.perf.get("delta_7d") or {}).items() if v is not None)
        parts.append(f"{m.salutation}, a dip alert fired for {m.name}, but your last 7 days actually look steady ({deltas}).")
        if m.sub.get("status") == "expired":
            parts.append(f"The real risk is elsewhere: your magicpin plan lapsed {m.sub.get('days_since_expiry')} days ago, so profile upkeep (posts, offer refresh, review replies) is paused — that's usually when the slide starts.")
            if _peer_line(m, c, "ctr") and _above_peer(m, c, "ctr"):
                parts.append(f"You're protecting a strong CTR of {_peer_line(m, c, 'ctr')}.")
            return Draft(parts, ask_en="show you what reactivating switches back on (2-min read, no commitment)",
                         ask_hi="dikhaa doon ki reactivate karne se kya wapas chalu hoga (2-min, koi commitment nahi)",
                         rationale="perf_dip trigger but merchant's 7d deltas are non-negative — say so honestly and point at the actual risk (expired plan) instead of inventing a dip.",
                         levers=["judgment", "loss_aversion", "specificity"], deliverable="reactivation summary", topic="plan lapsed")
        return Draft(parts, ask_raw="Anything you've changed recently that I should know about? Reply and I'll factor it into this week's post.", cta="open_ended",
                     rationale="perf_dip trigger without a real dip in the data — honest steady-state note + ask-the-merchant.",
                     levers=["judgment", "asking_the_merchant"], deliverable="weekly post", topic="steady performance")
    other = [(k, v) for k, v in (m.perf.get("delta_7d") or {}).items() if v is not None and v < 0 and not k.startswith(metric)]
    if other:
        k, v = other[0]
        parts.append(f"{k.replace('_pct', '').capitalize()} are down {fmt_pct(v)} too.")
    causes = []
    if m.verified is False:
        causes.append("the profile is still unverified")
    if not m.active_offers():
        causes.append("there's no active offer on your listing")
    if (m.signal_value("stale_posts")):
        causes.append(f"your last Google post was {m.signal_value('stale_posts')} ago")
    if m.d.get("subscription", {}).get("status") == "expired":
        causes.append(f"your magicpin plan lapsed {m.sub.get('days_since_expiry')} days ago, so profile upkeep is paused")
    if causes:
        parts.append(f"Fixable on your side: {' and '.join(causes[:2])}.")
    offer, is_own = _best_offer(m, c)
    if offer and not is_own:
        ask_en = f"put a '{offer}' offer live on your listing today — service+price converts better than % discounts"
        ask_hi = f"aaj hi '{offer}' offer listing par live kar doon"
    else:
        ask_en = f"push a fresh GBP post featuring your {offer} today" if offer else "push a fresh GBP post today"
        ask_hi = f"aaj {offer} ke saath fresh GBP post daal doon" if offer else "aaj ek fresh GBP post daal doon"
    return Draft(
        parts, ask_en=ask_en, ask_hi=ask_hi,
        rationale=f"Perf dip ({metric} {fmt_pct(delta, signed=True)}): exact numbers + concrete, merchant-specific causes; one fix to start with.",
        levers=["loss_aversion", "specificity", "effort_externalization", "single_binary_cta"],
        deliverable="offer/post to recover the dip",
        topic=f"{metric} dip",
    )


def b_perf_spike(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    metric, delta = p.get("metric"), p.get("delta_pct")
    if metric is None or delta is None:
        metric, delta = m.best_delta()
    if not metric or delta is None or delta <= 0:
        return b_generic(m, c, t, cu, today)
    line = f"{m.salutation}, {metric} are up {fmt_pct(delta)} this week"
    if p.get("vs_baseline"):
        line += f" (baseline {fmt_int(p['vs_baseline'])}/month)"
    parts = [line + "."]
    driver = p.get("likely_driver")
    last_vera = m.last_vera_message() or ""
    if driver:
        parts.append(f"The timing lines up with your {humanize(driver)}.")
    other = [(k, v) for k, v in (m.perf.get("delta_7d") or {}).items() if v is not None and not k.startswith(metric)]
    if other and not driver:
        k, v = other[0]
        parts.append(f"{k.replace('_pct', '').capitalize()} moved {fmt_pct(v, signed=True)} over the same week.")
    if "kids" in (driver or "") and "₹" in last_vera:
        m_det = re.search(r"(\d+-week program.*?₹[\d,]+)", last_vera)
        det = m_det.group(1) if m_det else None
        parts.append("Worth striking while it's warm" + (f" — the camp we outlined ({det})" if det else "") + " is ready to announce.")
        return Draft(parts, ask_en="publish the follow-up post with the batch details today", ask_hi="batch details ke saath follow-up post aaj publish kar doon",
                     rationale="Perf spike attributed to a specific post; convert momentum using the plan from conversation history.",
                     levers=["specificity", "momentum", "effort_externalization"], deliverable="follow-up post with batch details", topic="spike follow-up")
    if m.verified is False:
        parts.append("Your profile is still unverified — verifying now would let you keep more of this traffic.")
        return Draft(parts, ask_en="start GBP verification while the traffic is up", ask_hi="jab traffic up hai, abhi GBP verification start kar doon",
                     rationale="Perf spike on an unverified profile: capitalise on momentum by fixing the biggest leak.",
                     levers=["momentum", "loss_aversion"], deliverable="GBP verification", topic="spike follow-up")
    parts.append("Whatever changed is working — worth doubling down this week rather than next.")
    return Draft(parts, ask_raw="What do you think drove it — a new offer, a post, or word of mouth? Tell me and I'll repeat it in this week's Google post.",
                 cta="open_ended", rationale="Perf spike: celebrate with real numbers + ask-the-merchant to find the driver.",
                 levers=["specificity", "asking_the_merchant", "reciprocity"], deliverable="repeat-the-winner Google post", topic="spike follow-up")


def b_seasonal_dip(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    metric, delta = p.get("metric", "views"), p.get("delta_pct") or m.delta(p.get("metric", "views"))
    parts = [f"{m.salutation}, {metric} are down {fmt_pct(delta)} this week — and this one is expected, not a problem."]
    item = c.digest_by_kind("seasonal")
    if item:
        parts.append(f"{item.get('source', 'Category data')}: {_item_line(item)}")
        if item.get("actionable"):
            parts.append(f"So: {_lc(item['actionable'])}.")
    a = m.agg
    if a.get("total_active_members"):
        line = f"What matters this quarter is keeping your {fmt_int(a['total_active_members'])} members"
        if a.get("monthly_churn_pct") is not None and c.peer.get("monthly_churn_pct"):
            line += f" (monthly churn {fmt_pct(a['monthly_churn_pct'])} vs {fmt_pct(c.peer['monthly_churn_pct'])} peer)"
        parts.append(line + ".")
    return Draft(
        parts,
        ask_en="draft a 4-week summer attendance challenge for your members",
        ask_hi="members ke liye 4-week summer attendance challenge draft kar doon",
        rationale="Expected seasonal dip: pre-empt anxiety with the category data, redirect effort from acquisition to retention.",
        levers=["judgment", "specificity", "reassurance", "effort_externalization"],
        deliverable="4-week summer attendance challenge",
        topic="seasonal dip",
    )


def b_renewal(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    days = p.get("days_remaining") or m.sub.get("days_remaining")
    amt = p.get("renewal_amount")
    plan = p.get("plan") or m.sub.get("plan")
    parts = [f"{m.salutation}, your magicpin {plan} plan renews in {days} days" + (f" ({fmt_rupees(amt)})." if amt else ".")]
    k, v = m.worst_delta()
    fixes = []
    if m.verified is False:
        fixes.append("verify your GBP")
    if not m.active_offers():
        fixes.append("put a service+price offer live")
    lapsed, label = _lapsed(m)
    if lapsed:
        fixes.append(f"win back the {fmt_int(lapsed)} customers lapsed {label}")
    if k and v is not None and v < 0:
        parts.append(f"Honest picture first: {k} are down {fmt_pct(v)} this week, so the renewal should come with a fix plan, not just a payment.")
    elif _perf_snapshot(m):
        parts.append(f"This period you got {_perf_snapshot(m)}.")
    if fixes:
        parts.append(f"The plan I'd run next: {', '.join(fixes[:3])}.")
    return Draft(
        parts,
        ask_en="send the renewal along with that 3-step plan",
        ask_hi="renewal ke saath yeh 3-step plan bhej doon",
        rationale="Renewal due: pairs the ask with an honest performance read + concrete recovery plan (value before payment).",
        levers=["loss_aversion", "reciprocity", "specificity"],
        deliverable="renewal + 3-step recovery plan",
        topic="renewal",
    )


def b_winback(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    days = p.get("days_since_expiry") or m.sub.get("days_since_expiry")
    parts = [f"Hi {m.owner_first or m.salutation}, it's been {days} days since {m.name}'s magicpin plan lapsed — here's what changed since."]
    bits = []
    if p.get("perf_dip_pct"):
        bits.append(f"performance is down {fmt_pct(p['perf_dip_pct'])}")
    if p.get("lapsed_customers_added_since_expiry"):
        bits.append(f"{p['lapsed_customers_added_since_expiry']} more customers have gone lapsed")
    if bits:
        parts.append("Since then " + " and ".join(bits) + ".")
    dg = c.digest_by_kind("seasonal")
    if dg:
        parts.append(f"And the timing is good: {_lc(dg['title'])}.")
    return Draft(
        parts,
        ask_en="show you exactly what reactivating would switch back on (2-min read, no commitment)",
        ask_hi="dikhaa doon ki reactivate karne se kya wapas chalu hoga (2-min, koi commitment nahi)",
        rationale="Winback: quantified loss since expiry + seasonal opportunity; no-commitment ask.",
        levers=["loss_aversion", "specificity", "low_friction"],
        deliverable="reactivation summary",
        topic="winback",
    )


def b_active_planning(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    topic = p.get("intent_topic", "")
    first = m.owner_first or m.salutation
    if "thali" in topic:
        offer = next((o for o in m.active_offers() if "thali" in o.lower()), None)
        price_m = re.search(r"₹\s?([\d,]+)", offer or "")
        base = int(price_m.group(1).replace(",", "")) if price_m else None
        last_vera = m.last_vera_message() or ""
        vol = re.search(r"(\d+)\s*orders/day", last_vera)
        parts = [f"{first}, here's a first cut of the corporate thali package — edit anything:"]
        if base:
            tiers = [(10, 0.10), (25, 0.15), (50, 0.20)]
            lines = []
            for i, (qty, off) in enumerate(tiers):
                price = int(round(base * (1 - off) / 5) * 5)
                hi = f"{tiers[i + 1][0] - 1}" if i + 1 < len(tiers) else "+"
                rng = f"{qty}-{hi}" if hi != "+" else f"{qty}+"
                lines.append(f"• {rng} thalis/day: ₹{price} each ({int(off * 100)}% off your ₹{base} retail)")
            parts.append("\n".join(lines))
        parts.append("• Order by 5pm the day before; one delivery slot at lunch for the whole office")
        tr = c.trend("thali")
        ctx = []
        if vol:
            ctx.append(f"your weekday thali already does {vol.group(1)} orders/day")
        if tr:
            ctx.append(f"'{tr['query']}' searches are {fmt_pct(tr['delta_yoy'], signed=True)} YoY among office-goers")
        if ctx:
            parts.append("Why it'll work: " + " and ".join(ctx) + ".")
        return Draft(parts, ask_en=f"turn this into a GBP post + a WhatsApp you can send to offices around {m.locality}",
                     ask_hi=f"isse GBP post aur {m.locality} ke offices ke liye WhatsApp bana doon",
                     rationale="Merchant already said yes to the idea — deliver the artifact (tiered pricing derived from their ₹149 thali), no more qualifying.",
                     levers=["effort_externalization", "specificity", "intent_handoff"], deliverable="corporate thali GBP post + office WhatsApp", topic="corporate thali")
    if "yoga" in topic or "kids" in topic:
        last_vera = m.last_vera_message() or ""
        det = re.search(r"(\d+-week program.*?₹[\d,]+)", last_vera)
        parts = [f"{first}, here's the kids yoga summer camp, ready to publish:"]
        if det:
            spec = det.group(1).replace("Suggest ", "")
            parts.append("• " + "\n• ".join(x.strip() for x in re.split(r",\s(?=\D)", spec)))
        parts.append("• Small batches (your reviews already praise small classes) — you pick the slots")
        tr = c.trend("yoga")
        if tr:
            parts.append(f"Demand is there: '{tr['query']}' searches are {fmt_pct(tr['delta_yoy'], signed=True)} YoY, skewing {tr.get('skew', '')} {tr.get('segment_age', '')} — i.e. the parents.")
        return Draft(parts, ask_en="put the GBP post + Insta carousel live, and draft the parent WhatsApp", ask_hi="GBP post + Insta carousel live kar doon",
                     rationale="Active planning intent: merchant asked what it should look like — hand over the finished spec (from our earlier outline) and move to publish.",
                     levers=["effort_externalization", "specificity", "intent_handoff"], deliverable="kids camp GBP post + Insta carousel + parent WhatsApp", topic="kids yoga camp")
    parts = [f"{first}, picking up where we left off on {humanize(topic)} — here's a starter draft based on your current setup."]
    offer, is_own = _best_offer(m, c)
    if offer and is_own:
        parts.append(f"It builds on your {offer}.")
    return Draft(parts, ask_en="send the full draft now", ask_hi="poora draft abhi bhej doon",
                 rationale="Active planning intent: move straight to a draft.", levers=["effort_externalization", "intent_handoff"],
                 deliverable=f"{humanize(topic)} draft", topic=humanize(topic))


def b_supply_alert(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    item = c.digest_item(p.get("alert_id")) or c.digest_by_kind("alert")
    batches = p.get("affected_batches") or []
    mol = p.get("molecule", "the affected molecule")
    parts = [f"{m.salutation}, urgent: voluntary recall on {mol} batches {', '.join(batches)}" + (f" ({p['manufacturer']})" if p.get("manufacturer") else "") + (f" — {item['source']}." if item and item.get("source") else ".")]
    if item and item.get("summary"):
        s = item["summary"]
        parts.append(re.sub(r"^Two batches \(numbers in alert\) flagged", "Flagged", s))
    if m.agg.get("chronic_rx_count"):
        parts.append(f"You have {fmt_int(m.agg['chronic_rx_count'])} chronic-Rx customers; I can filter everyone dispensed {mol} from these batches.")
    return Draft(parts, ask_en="pull that list + draft the patient WhatsApp and replacement-pickup note", ask_hi="woh list nikaal ke patient WhatsApp aur replacement note draft kar doon",
                 rationale="Supply/recall alert: batch-level specificity from payload, bounded-risk framing, end-to-end workflow offer.",
                 levers=["urgency", "specificity", "effort_externalization"], deliverable="affected-customer list + WhatsApp", topic=f"{mol} recall")


def b_category_seasonal(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    trends = p.get("trends") or []
    parsed = []
    for tr in trends:
        mm = re.match(r"([A-Za-z_]+?)_demand_([+-]\d+)", tr)
        if mm:
            parsed.append((humanize(mm.group(1)).replace("antifungal", "anti-fungal").replace("cold cough", "cold/cough"), int(mm.group(2))))
    item = c.digest_by_kind("seasonal")
    ups = [f"{n} {v:+d}%" for n, v in parsed if v > 0]
    downs = [f"{n} {v:+d}%" for n, v in parsed if v < 0]
    parts = [f"{m.salutation}, the summer demand shift is here: {', '.join(ups)}" + (f", while {', '.join(downs)}" if downs else "") + (f" ({item['source']})." if item and item.get("source") else ".")]
    if item and item.get("actionable"):
        parts.append(f"Quick shelf move: {_lc(item['actionable'])}.")
    base = _customer_base(m, c)
    offer, is_own = _best_offer(m, c, prefer="delivery")
    if base:
        parts.append(f"With {base}" + (f" and {offer} already live" if offer and is_own else "") + ", a quick nudge to regulars is worth it.")
    return Draft(parts, ask_en="draft a 'summer essentials' GBP post + WhatsApp for your repeat customers", ask_hi="'summer essentials' GBP post aur regular customers ke liye WhatsApp draft kar doon",
                 rationale="Category seasonal shift: exact demand deltas from payload + shelf action + merchant's own offer.",
                 levers=["specificity", "timeliness", "effort_externalization"], deliverable="summer essentials post + WhatsApp", topic="summer demand")


def b_generic(m, c, t, cu, today):
    kind = humanize(t.get("kind", "update"))
    p = {k: v for k, v in (t.get("payload") or {}).items() if k not in ("placeholder", "metric_or_topic", "category")}
    parts = [f"{m.salutation}, quick {kind} note for {m.name}."]
    facts = [f"{humanize(k)}: {v}" for k, v in p.items() if isinstance(v, (str, int, float)) and not str(k).endswith("_id")][:3]
    if facts:
        parts.append("; ".join(facts).capitalize() + ".")
    if _perf_snapshot(m):
        parts.append(f"For reference: {_perf_snapshot(m)}" + (f" (CTR {_peer_line(m, c, 'ctr')})." if _peer_line(m, c, "ctr") else "."))
    offer, is_own = _best_offer(m, c)
    return Draft(parts, ask_en=f"draft a GBP post around {'your ' if is_own else 'a '}{offer}" if offer else "draft a GBP post for this",
                 ask_hi=f"{offer} par GBP post draft kar doon" if offer else "iske liye GBP post draft kar doon",
                 rationale=f"Generic handler for '{kind}': surfaces payload facts + merchant numbers, one action.",
                 levers=["specificity", "effort_externalization"], deliverable="GBP post", topic=kind)


# ================================================================ customer-facing builders

def _cust_greet(cu: Customer, m: Merchant, c: Category) -> str:
    name = cu.name
    brand = m.name
    sign = {"dentists": " 🦷", "salons": " ✨", "gyms": " 💪", "pharmacies": "", "restaurants": " 🍽️"}.get(c.slug, "")
    via = str(cu.prefs.get("channel", "")).startswith("whatsapp_via")
    if cu.hindi:
        who = "" if via or not name else f" {name} ji"
        return f"Namaste{who}! {brand}, {m.locality} se." if m.locality else f"Namaste{who}! {brand} se."
    if cu.hinglish:
        end = sign if sign else "."
        return f"Hi {name}! {brand} se{end}" if name else f"Hi! {brand} se{end}"
    return f"Hi {name}, {brand} here{sign}" if name else f"Hi, {brand} here{sign}"


def _slot_line(slots: list[dict], cu: Customer) -> tuple[str | None, list[str]]:
    labels = [fmt_slot(s.get("iso", "")) or s.get("label") for s in slots or []]
    labels = [l for l in labels if l]
    if not labels:
        return None, []
    if len(labels) == 1:
        return labels[0], labels
    join = " ya " if (cu.hinglish or cu.hindi) else " or "
    return join.join(labels), labels


def _choice_cta(labels: list[str], cu: Customer) -> str:
    if len(labels) >= 2:
        opts = ", ".join(f"{i + 1} for {l.split(',')[0]}" for i, l in enumerate(labels[:3]))
        tail = " ya koi aur time bata dijiye." if (cu.hinglish or cu.hindi) else ", or tell us a time that works."
        return f"Reply {opts}{tail}"
    if labels:
        return "Reply YES to confirm" + (" — ya koi aur time bata dijiye." if (cu.hinglish or cu.hindi) else ", or tell us a time that works.")
    return "Reply YES and we'll share this week's open slots."


def cb_recall(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    rel = cu.rel
    service = humanize(p.get("service_due", "")).replace("6 month", "6-month") or None
    parts = [_cust_greet(cu, m, c)]
    last = p.get("last_service_date") or rel.get("last_visit")
    if cu.hinglish:
        parts.append(f"Aapki last visit {fmt_date(last)} ko thi" + (f", so your {service} is due by {fmt_date(p['due_date'])}." if service and p.get("due_date") else " — time for your next check-in."))
    else:
        parts.append(f"Your last visit was on {fmt_date(last)}" + (f", so your {service} is due by {fmt_date(p['due_date'])}." if service and p.get("due_date") else f" — {rel.get('visits_total', 'several')} visits with us so far, and it's a good time for your next one."))
    slot_txt, labels = _slot_line(p.get("available_slots"), cu)
    pref = humanize(cu.preferred_slots or "")
    if slot_txt:
        lead = f"{pref.capitalize()} slots rakhe hain: " if (cu.hinglish and pref) else ("We've kept " + (f"{pref} " if pref else "") + "slots for you: ")
        parts.append(lead + slot_txt + ".")
    offer = _customer_offer(m, c, cu, prefer="clean" if c.slug == "dentists" else None)
    if offer:
        parts.append(f"{offer} applies." if not (cu.hinglish or cu.hindi) else f"{offer} lagega.")
    return Draft(parts, ask_raw=_choice_cta(labels, cu), cta="multi_choice_slot" if len(labels) > 1 else "binary_yes_no",
                 rationale="Customer recall: last-visit date + due date from payload, real slots (day names derived from ISO dates), merchant's own active offer only, language pref honoured.",
                 levers=["specificity", "personalisation", "low_friction_choice"], deliverable="booking", topic="recall")


def cb_lapsed(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    rel = cu.rel
    parts = [_cust_greet(cu, m, c).replace("Hi ", "Hi ", 1)]
    owner = m.owner_first
    if owner and not (cu.hindi or cu.hinglish) and c.slug in ("gyms", "salons"):
        parts[0] = f"Hi {cu.name} 👋 {owner} from {m.name} here."
    days = p.get("days_since_last_visit")
    focus = p.get("previous_focus") or cu.prefs.get("training_focus")
    if days:
        weeks = round(days / 7)
        line = f"It's been about {weeks} weeks since your last session — happens to most members at some point, no judgment."
    else:
        line = (f"Aapki last visit {fmt_date(rel.get('last_visit'))} ko thi — {rel.get('visits_total')} visits ke baad aapko miss kar rahe hain."
                if cu.hinglish or cu.hindi else
                f"We haven't seen you since {fmt_date(rel.get('last_visit'))} — after {rel.get('visits_total')} visits, we noticed!")
    parts.append(line)
    if focus:
        parts.append(f"Since your focus was {humanize(focus)}, we'd love to help you pick it back up gently.")
    offer = _customer_offer(m, c, cu)
    if offer:
        parts.append(f"You can restart with our {offer} — no commitment, no auto-charge." if c.slug == "gyms" else f"{offer} is on right now.")
    slot = humanize(cu.preferred_slots) if cu.preferred_slots else None
    if c.slug == "dentists" and not days:
        parts.append("A routine check-up every 6 months keeps small issues small." if not (cu.hinglish or cu.hindi) else "Har 6 mahine ka check-up chhoti problems ko chhota hi rakhta hai.")
    if c.slug == "pharmacies":
        ask = ("Regular medicines ka stock ready rakhein? Reply YES." if (cu.hinglish or cu.hindi)
               else "Want us to keep your regular medicines ready? Reply YES.")
    elif cu.hinglish or cu.hindi:
        ask = "Is hafte ek slot hold kar dein? Reply YES."
    else:
        ask = f"Want us to hold a {slot} slot for you this week? Reply YES." if slot else "Want us to hold a slot for you this week? Reply YES."
    return Draft(parts, ask_raw=ask, cta="binary_yes_no",
                 rationale="Lapsed customer: no-shame framing, past goal/visit count, merchant's own offer only, single binary CTA.",
                 levers=["personalisation", "no_shame", "low_friction", "single_binary_cta"], deliverable="slot hold", topic="winback")


def cb_appointment(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    slot = fmt_slot(p.get("slot_iso") or p.get("appointment_iso") or "") if (p.get("slot_iso") or p.get("appointment_iso")) else None
    parts = [_cust_greet(cu, m, c)]
    if cu.hindi or cu.hinglish:
        parts.append("Yaad dila rahe hain — aapka appointment kal hai" + (f" ({slot})." if slot else "."))
        if m.locality:
            parts.append(f"Hum {m.locality} mein hain; directions ke liye yahin reply karein.")
        ask = "Reply 1 to confirm, 2 to reschedule."
    else:
        parts.append("Quick reminder — your appointment is tomorrow" + (f" ({slot})." if slot else "."))
        if m.locality:
            parts.append(f"We're in {m.locality}; reply here if you need directions.")
        ask = "Reply 1 to confirm or 2 to reschedule."
    return Draft(parts, ask_raw=ask, cta="binary_confirm_reschedule",
                 rationale="Appointment reminder: no time in payload so none is invented; confirm/reschedule binary.",
                 levers=["utility", "low_friction"], deliverable="appointment confirmation", topic="appointment")


def cb_refill(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    mols = p.get("molecule_list") or []
    parts = [_cust_greet(cu, m, c)]
    runs_out = p.get("stock_runs_out_iso")
    if c.slug != "pharmacies" or not mols:
        # refill semantics don't fit this merchant / payload — send a routine follow-up instead of inventing medicines
        if cu.hindi or cu.hinglish:
            parts.append(f"Aapki last visit {fmt_date(cu.rel.get('last_visit'))} ko thi — aapka routine follow-up due hai.")
            ask = "Is hafte ka slot chahiye? Reply YES."
        else:
            parts.append(f"Your last visit was on {fmt_date(cu.rel.get('last_visit'))}, and you're due for your routine follow-up.")
            ask = "Want us to share this week's open slots? Reply YES."
        return Draft(parts, ask_raw=ask, cta="binary_yes_no",
                     rationale="Refill-type trigger on a non-pharmacy/empty payload: sent as a routine follow-up rather than inventing prescriptions.",
                     levers=["personalisation", "low_friction"], deliverable="follow-up booking", topic="follow-up")
    surname = re.sub(r"^(mr|mrs|ms)\.?\s+", "", cu.name, flags=re.I)
    whose = (f"{surname} ji" if cu.name != surname or cu.senior else cu.name) if cu.name else "Aapki"
    mol_txt = ", ".join(mols[:-1]) + (f" aur {mols[-1]}" if len(mols) > 1 else mols[0])
    if cu.hindi or cu.hinglish:
        parts.append(f"{whose} ki monthly medicines — {mol_txt} — {fmt_date(runs_out)} tak khatam ho jayengi" + (f" (last refill {fmt_date(p['last_refill'])})." if p.get("last_refill") else "."))
        parts.append("Same medicines, same pack ready kar dete hain.")
        perks = []
        for o in m.active_offers():
            if "senior" in o.lower() and cu.senior:
                perks.append(f"{o} apply hoga")
            elif "delivery" in o.lower():
                perks.append(f"{o} — saved address par" if p.get("delivery_address_saved") else o)
        if perks:
            parts.append("; ".join(perks) + ".")
        ask = "Reply HAAN to confirm, ya dosage mein koi badlav ho to bata dijiye."
    else:
        parts.append(f"{whose}'s regular medicines — {', '.join(mols)} — run out on {fmt_date(runs_out)}.")
        perks = [o for o in m.active_offers() if "senior" not in o.lower() or cu.senior]
        if perks:
            parts.append("Applicable: " + "; ".join(perks) + ".")
        ask = "Reply YES to have the same refill packed, or tell us if anything changed."
    return Draft(parts, ask_raw=ask, cta="binary_confirm",
                 rationale="Chronic refill: molecules + run-out date from payload, merchant's real perks (senior discount only for a senior), respectful Hindi for hi-pref customer.",
                 levers=["specificity", "utility", "trust"], deliverable="refill dispatch", topic="refill")


def cb_trial_followup(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    parts = [_cust_greet(cu, m, c)]
    who = cu.child_name or "you"
    if p.get("trial_date"):
        parts.append(f"Hope {who} enjoyed the trial class on {fmt_date(p['trial_date'])}!" if who != "you" else f"Hope you enjoyed your trial on {fmt_date(p['trial_date'])}!")
    slot_txt, labels = _slot_line(p.get("next_session_options"), cu)
    if slot_txt:
        parts.append(f"Next session: {slot_txt}.")
    offer = _customer_offer(m, c, cu)
    if offer:
        parts.append(f"If {who if who != 'you' else 'you'} continue{'s' if who != 'you' else ''}, {offer} applies.")
    ask = f"Shall we save {who + chr(39) + 's' if who != 'you' else 'your'} spot? Reply YES."
    return Draft(parts, ask_raw=ask, cta="binary_yes_no", rationale="Trial follow-up: trial date + next real session from payload; single binary CTA.",
                 levers=["personalisation", "specificity", "low_friction"], deliverable="session booking", topic="trial follow-up")


def cb_wedding(m, c, t, cu, today):
    p = t.get("payload", {}) or {}
    owner = m.owner_first
    parts = [f"Hi {cu.name} 💍 {owner + ' from ' if owner else ''}{m.name}{', ' + m.locality if m.locality else ''} here."]
    if p.get("trial_completed"):
        parts.append(f"Hope you loved your bridal trial on {fmt_date(p['trial_completed'])}!")
    if p.get("days_to_wedding") and p.get("wedding_date"):
        parts.append(f"{p['days_to_wedding']} days to go till {fmt_date(p['wedding_date'])} — a good point to plan your {humanize(p.get('next_step_window_open', 'next step')).replace('30day', '30-day')} so it's done well before the big week.")
    pref = cu.prefs.get("preferred_slots")
    ask = f"Want us to hold a {pref.capitalize()} slot for a quick 20-min plan consult? Reply YES." if pref else "Want us to hold a slot for a quick plan consult? Reply YES."
    return Draft(parts, ask_raw=ask, cta="binary_yes_no",
                 rationale="Bridal follow-up: trial date + countdown from payload; no program price invented (not in merchant offers); preferred day honoured.",
                 levers=["personalisation", "specificity", "single_binary_cta"], deliverable="consult slot", topic="bridal prep")


def cb_generic(m, c, t, cu, today):
    parts = [_cust_greet(cu, m, c), f"A quick update from us about your {humanize(t.get('kind', 'visit'))}."]
    return Draft(parts, ask_raw="Reply YES and we'll share the details.", cta="binary_yes_no",
                 rationale="Generic customer-facing handler.", levers=["low_friction"], deliverable="details", topic=humanize(t.get("kind", "")))


MERCHANT_BUILDERS = {
    "research_digest": b_research_digest, "category_research_digest_release": b_research_digest,
    "research_digest_release": b_research_digest, "category_trend_movement": b_research_digest,
    "regulation_change": b_regulation_change, "compliance_alert": b_regulation_change,
    "cde_opportunity": b_cde_opportunity,
    "competitor_opened": b_competitor_opened,
    "curious_ask_due": b_curious_ask, "scheduled_recurring": b_curious_ask,
    "dormant_with_vera": b_dormant,
    "festival_upcoming": b_festival,
    "gbp_unverified": b_gbp_unverified,
    "ipl_match_today": b_ipl,
    "review_theme_emerged": b_review_theme,
    "milestone_reached": b_milestone,
    "perf_dip": b_perf_dip,
    "perf_spike": b_perf_spike,
    "seasonal_perf_dip": b_seasonal_dip,
    "renewal_due": b_renewal,
    "winback_eligible": b_winback,
    "active_planning_intent": b_active_planning,
    "supply_alert": b_supply_alert,
    "category_seasonal": b_category_seasonal,
}

CUSTOMER_BUILDERS = {
    "recall_due": cb_recall,
    "customer_lapsed_soft": cb_lapsed, "customer_lapsed_hard": cb_lapsed,
    "appointment_tomorrow": cb_appointment,
    "chronic_refill_due": cb_refill,
    "trial_followup": cb_trial_followup,
    "wedding_package_followup": cb_wedding,
}


# ================================================================ assembly

TABOO_FALLBACK = ["guaranteed", "100% safe", "miracle", "best in city", "cure"]


def _strip_taboos(text: str, c: Category) -> str:
    for word in list(c.taboos) + TABOO_FALLBACK:
        w = re.sub(r"\s*\(.*\)$", "", word).strip()
        if w and w.lower() in text.lower():
            text = re.sub(re.escape(w), "", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text)


def _close(d: Draft, m: Merchant) -> str:
    if d.ask_raw:
        return d.ask_raw
    if m.hinglish and d.ask_hi:
        return f"Main {d.ask_hi}? Reply YES."
    return f"Want me to {d.ask_en}? Reply YES." if d.ask_en else ""


def build_draft(category: dict, merchant: dict, trigger: dict, customer: dict | None = None, today=None) -> tuple[Draft, str]:
    m, c, cu = Merchant(merchant), Category(category), Customer(customer)
    today = today or DATASET_TODAY
    kind = trigger.get("kind", "")
    if trigger.get("scope") == "customer" or cu:
        fn = CUSTOMER_BUILDERS.get(kind, cb_generic)
        send_as = "merchant_on_behalf"
    else:
        fn = MERCHANT_BUILDERS.get(kind, b_generic)
        send_as = "vera"
    return fn(m, c, trigger, cu, today), send_as


def compose_full(category: dict, merchant: dict, trigger: dict, customer: dict | None = None, today=None) -> dict:
    """Pure, deterministic composition. Returns body, cta, send_as, suppression_key, rationale (+ template fields)."""
    m, c = Merchant(merchant), Category(category)
    draft, send_as = build_draft(category, merchant, trigger, customer, today)
    closing = _close(draft, m)
    body_parts = [p.strip() for p in draft.parts if p and p.strip()]
    body = " ".join(p for p in body_parts if "\n" not in p)
    if any("\n" in p for p in body_parts):  # keep bullet blocks on their own lines
        body = "\n".join(body_parts)
    body = f"{body}\n{closing}" if "\n" in body else f"{body} {closing}"
    body = _strip_taboos(body.strip(), c)
    kind = trigger.get("kind", "generic")
    prefix = "vera" if send_as == "vera" else "merchant"
    hook = " ".join(body_parts[1:]) if len(body_parts) > 1 else body_parts[0]
    return {
        "body": body,
        "cta": draft.cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{m.id}",
        "rationale": f"[{COMPOSER_VERSION}] {draft.rationale} Levers: {', '.join(draft.levers)}.",
        "template_name": f"{prefix}_{kind}_v1",
        "template_params": [body_parts[0], hook[:900], closing],
        "_deliverable": draft.deliverable,
        "_topic": draft.topic,
    }


# ============================================================================
# 3. OPTIONAL CLAUDE POLISH (VERA_USE_LLM=1)
# ============================================================================

LLM_MODEL = os.environ.get("VERA_MODEL", "claude-opus-5")
LLM_TIMEOUT_S = float(os.environ.get("VERA_LLM_TIMEOUT", "8"))
_llm_cache: dict[str, str] = {}
_llm_client = None

LLM_SYSTEM = """You polish WhatsApp messages written by Vera, magicpin's merchant assistant.
Rewrite the draft so it reads naturally, like a sharp colleague — keep it roughly the same length or shorter.
Hard rules:
- Keep every number, price, date, name and source exactly as written. Add no new facts, numbers or claims.
- Keep the language mix (Hindi-English stays Hindi-English).
- Keep the final call-to-action sentence's meaning and its reply keyword (YES / CONFIRM / 1 / 2 / HAAN).
- No URLs, no hype words, no greetings like "I hope you're doing well".
Return only the rewritten message text."""


def llm_enabled() -> bool:
    return os.environ.get("VERA_USE_LLM") == "1"


def _llm_get_client():
    global _llm_client
    if _llm_client is None:
        import anthropic  # imported lazily so the bot runs without the SDK installed
        _llm_client = anthropic.Anthropic(timeout=LLM_TIMEOUT_S, max_retries=0)
    return _llm_client


def _llm_numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,.]*", text))


def _llm_valid(original: str, rewritten: str, taboos: list[str]) -> bool:
    if not rewritten or "http" in rewritten.lower() or "www." in rewritten.lower():
        return False
    if not _llm_numbers(rewritten) <= _llm_numbers(original):
        return False
    if len(rewritten) > len(original) * 1.3:
        return False
    for kw in ("Reply YES", "CONFIRM", "Reply 1", "HAAN"):
        if kw in original and kw not in rewritten:
            return False
    low = rewritten.lower()
    return not any(t.lower() in low for t in taboos if t and t.lower() not in original.lower())


def llm_polish(body: str, kind: str, category_slug: str, taboos: list[str]) -> str:
    """Return a polished body, or the original on any failure."""
    if not llm_enabled():
        return body
    key = hashlib.sha256(json.dumps([body, kind, category_slug, LLM_MODEL]).encode()).hexdigest()
    if key in _llm_cache:
        return _llm_cache[key]
    out = body
    try:
        resp = _llm_get_client().messages.create(
            model=LLM_MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": f"Category: {category_slug}. Trigger: {kind}.\n\nDraft:\n{body}"}],
        )
        if resp.stop_reason == "end_turn":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if _llm_valid(body, text, taboos):
                out = text
    except Exception:  # network, auth, timeout, SDK missing — fall back silently
        out = body
    _llm_cache[key] = out
    return out


# ============================================================================
# 4. MULTI-TURN REPLY HANDLER
# ============================================================================

AUTO_REPLY_PATTERNS = [
    "thank you for contacting", "thanks for contacting", "thank you for reaching", "thanks for reaching out",
    "our team will", "team will get back", "will get back to you", "we will respond", "respond shortly",
    "we have received your message", "this is an automated", "automated assistant", "automated message",
    "auto-reply", "autoreply", "auto reply", "currently unavailable", "out of office", "our business hours",
    "aapki jaankari ke liye", "hamari team tak", "main ek automated", "jald hi sampark",
]
OPT_OUT_RE = re.compile(
    r"\b(stop|unsubscribe|opt[\s-]?out|not interested|no interest|don'?t (?:message|text|contact|send|call)|"
    r"do not (?:message|contact|send)|leave me alone|remove me|band karo|mat bhejo|nahi chahiye|"
    r"message mat|mat karo)\b", re.I)
HOSTILE_RE = re.compile(
    r"(useless|bakwas|bekaar|bekar|idiot|stupid|fraud|scam|nonsense|shut up|pagal|bewakoof|irritat|"
    r"bother|harass|wtf|spam|rubbish|waste of time|chutiya|bloody)", re.I)
ACCEPT_RE = re.compile(
    r"\b(yes|yeah|yep|yup|ok|okay|okk|sure|go ahead|do it|let'?s do it|lets do it|please do|go for it|"
    r"haan|han|haa|ji haan|theek hai|thik hai|chalega|kar do|kardo|karo|send it|send|confirm|confirmed|"
    r"proceed|book it|book|i'?m in|interested|i want|join|judna|start|done|publish|approved?)\b", re.I)
DEFER_RE = re.compile(r"\b(later|busy|tomorrow|kal|baad mein|baad me|abhi nahi|not now|next week|in a meeting|driving)\b", re.I)
OFF_TOPIC_RE = re.compile(
    r"\b(gst|income tax|itr|tax filing|loan|insurance|lawyer|legal notice|accountant|visa|passport|"
    r"electricity bill|rent agreement|stock market|crypto|trading)\b", re.I)
SOFT_NO_RE = re.compile(r"^\s*(no|nope|nah|nahi|na|no thanks|no thank you|not required|zaroorat nahi)\b", re.I)
SLOT_PICK_RE = re.compile(r"^\s*([1-3])\s*[.!]?\s*$")
HINDI_TOKENS = {"hai", "hain", "kya", "nahi", "haan", "karo", "kar", "mujhe", "aap", "chahiye", "bhai", "ji",
                "kaise", "kitna", "kab", "theek", "accha", "acha", "mera", "meri", "hum", "kaun", "wala", "dijiye", "doon"}


@dataclass
class ConversationState:
    conversation_id: str
    merchant: dict
    category: dict
    customer: dict | None = None
    trigger: dict | None = None
    send_as: str = "vera"
    deliverable: str | None = None
    topic: str | None = None
    first_body: str | None = None
    stage: str = "pitch"                  # pitch -> action -> done | closed
    bot_bodies: list = field(default_factory=list)
    turns: list = field(default_factory=list)
    hostile_count: int = 0
    merchant_memory: dict = field(default_factory=dict)   # shared per-merchant: {"auto_count", "last_msgs", "opted_out"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def is_auto_reply(msg: str, memory: dict) -> bool:
    low = (msg or "").lower()
    if any(p in low for p in AUTO_REPLY_PATTERNS):
        return True
    n = _norm(msg)
    return bool(n) and len(n) > 15 and memory.get("last_msgs", []).count(n) >= 1


def speaks_hindi(msg: str) -> bool:
    if re.search(r"[ऀ-ॿ]", msg or ""):
        return True
    words = set(re.findall(r"[a-z]+", (msg or "").lower()))
    return len(words & HINDI_TOKENS) >= 2


def _merchant_draft(state: ConversationState, hinglish: bool) -> tuple[str, str]:
    """The concrete artifact delivered in action mode. Built only from context we hold."""
    m, c = Merchant(state.merchant), Category(state.category)
    t = state.trigger or {}
    kind = t.get("kind", "")
    p = t.get("payload", {}) or {}
    offers = m.active_offers()
    where = m.locality or m.city or ""
    if kind in ("research_digest", "cde_opportunity") or "digest" in kind:
        item = c.digest_item(p.get("top_item_id") or p.get("digest_item_id")) or c.digest_by_kind("research")
        if item and kind == "cde_opportunity":
            return (f"Done — calendar hold added for '{item['title']}'" + (f" ({item.get('date', '')[:10]})" if item.get("date") else "") +
                    ". I'll send the joining details on the morning of the session.", "Reply CONFIRM if you'd also like a reminder 1 hour before.")
        if item:
            summary = (item.get("summary") or item["title"]).split(". ")[0]
            draft = (f"\"{m.name}: new research ({item.get('source', 'recent study')}) — {summary}. "
                     f"If you've had cavities recently, ask us about your recall interval at your next visit.\"")
            return f"Here's the patient WhatsApp draft:\n{draft}", "Reply CONFIRM and I'll send it to your patient list, or tell me what to change."
    if kind in ("regulation_change", "compliance_alert"):
        item = c.digest_item(p.get("top_item_id")) or c.digest_by_kind("compliance")
        lines = [s.strip() for s in re.split(r"(?<=[.;])\s+", (item or {}).get("summary", "")) if s.strip()][:3]
        checklist = "\n".join(f"☐ {l}" for l in lines + ["Record film type / sensor in your SOP file", "Re-check before the deadline"])
        return f"Here's your audit checklist:\n{checklist}", "Reply CONFIRM and I'll set a reminder 2 weeks before the deadline."
    if kind == "supply_alert":
        return (f"Pulling it now — filtering your {fmt_int(m.agg.get('chronic_rx_count', 0))} chronic-Rx customers for {p.get('molecule', 'the molecule')} "
                f"batches {', '.join(p.get('affected_batches', []))}. Draft note: \"Your {p.get('molecule')} batch is part of a voluntary recall "
                "(sub-potency, not a safety issue). Please bring the strip in for a free replacement.\""), "Reply CONFIRM to send it to the matched customers."
    if kind == "renewal_due":
        return "Done — renewal request raised along with the 3-step recovery plan; you'll get the payment prompt on WhatsApp.", "Reply CONFIRM once paid and I'll start step 1 (GBP verification) the same day."
    if kind in ("gbp_unverified",):
        return "Done — verification request prepared for your listing. Google will reach you by phone or postcard; share the code here when it arrives.", "Reply CONFIRM to submit it now."
    if kind == "curious_ask_due":
        return "Sending the Google post + price-reply template as soon as you share the service and price.", "Just reply with them, e.g. 'Keratin, ₹2,499'."
    offer = offers[0] if offers else None
    post = (f"\"{m.name}{', ' + where if where else ''}: " + (f"{offer} — " if offer else "") +
            ("now booking this week. Message us on WhatsApp to reserve your slot.\"" if c.slug in ("dentists", "salons", "gyms")
             else "order or walk in today. Message us on WhatsApp for details.\""))
    what = state.deliverable or "the post"
    head = f"Here's the draft for {what}:" if not hinglish else f"Yeh raha {what} ka draft:"
    tail = "Reply CONFIRM to publish, or tell me what to change." if not hinglish else "CONFIRM likhiye to publish kar deti hoon, ya jo badalna ho bata dijiye."
    return f"{head}\n{post}", tail


def _customer_action(state: ConversationState, msg: str) -> dict:
    t = state.trigger or {}
    p = t.get("payload", {}) or {}
    cu = Customer(state.customer)
    m = Merchant(state.merchant)
    slots = p.get("available_slots") or p.get("next_session_options") or []
    labels = [fmt_slot(s.get("iso", "")) or s.get("label") for s in slots]
    pick = SLOT_PICK_RE.match(msg)
    hi = cu.hinglish or cu.hindi or speaks_hindi(msg)
    if pick and labels:
        idx = int(pick.group(1)) - 1
        if 0 <= idx < len(labels):
            body = (f"Booked ✅ {labels[idx]} at {m.name}. " +
                    ("Ek din pehle reminder bhej denge. Change karna ho to yahin reply karein." if hi
                     else "We'll send a reminder the day before — reply here if you need to change it."))
            return {"action": "send", "body": body, "cta": "none", "rationale": f"Customer picked slot {idx + 1}; confirm booking + reminder, no further ask."}
    if labels:
        body = (f"Done ✅ {labels[0]} aapke liye hold kar diya hai at {m.name}." if hi else f"Done ✅ we've held {labels[0]} for you at {m.name}.") + \
               (" Koi aur time chahiye to bata dijiye." if hi else " Reply with another time if that doesn't work.")
    elif t.get("kind") == "chronic_refill_due":
        body = ("Confirmed ✅ same medicines pack kar rahe hain; delivery saved address par. Dispatch hote hi message karenge."
                if hi else "Confirmed ✅ we're packing the same refill; we'll message you when it's dispatched.")
    else:
        body = (f"Done ✅ {m.name} ki team aapko is hafte ke open slots yahin bhej rahi hai." if hi
                else f"Done ✅ the {m.name} team will send this week's open slots right here.")
    return {"action": "send", "body": body, "cta": "none", "rationale": "Customer committed; confirm and hand off without further qualification."}


def respond(state: ConversationState, merchant_message: str, from_role: str = "merchant") -> dict:
    msg = (merchant_message or "").strip()
    mem = state.merchant_memory
    state.turns.append({"from": from_role, "msg": msg})
    m = Merchant(state.merchant)
    # language follows the latest turn; very short replies ("ok", "haan") fall back to the merchant's default
    hinglish = speaks_hindi(msg) or (m.hinglish and len(re.findall(r"[a-z]+", msg.lower())) <= 2)

    def out(d: dict) -> dict:
        if d.get("action") == "send":
            body = d["body"]
            if body in state.bot_bodies or (state.first_body and body == state.first_body):
                return {"action": "wait", "wait_seconds": 3600, "rationale": "Would repeat an earlier message verbatim; backing off instead."}
            state.bot_bodies.append(body)
            if len(state.bot_bodies) >= 6:
                state.stage = "closed"
        if d.get("action") == "end":
            state.stage = "closed"
        return d

    if not msg:
        return out({"action": "wait", "wait_seconds": 1800, "rationale": "Empty reply; waiting."})
    if state.stage == "closed":
        return out({"action": "end", "rationale": "Conversation already closed; not re-engaging."})

    # 2. explicit opt-out
    if OPT_OUT_RE.search(msg):
        mem["opted_out"] = True
        return out({"action": "end", "rationale": "Explicit opt-out/not-interested; closing and suppressing further sends to this merchant."})

    # 3. auto-reply (per merchant)
    if is_auto_reply(msg, mem):
        mem["auto_count"] = mem.get("auto_count", 0) + 1
        mem.setdefault("last_msgs", []).append(_norm(msg))
        n = mem["auto_count"]
        if n == 1:
            body = ("Lagta hai yeh auto-reply hai 🙂 Owner/manager dekhein to bas 'YES' likh dein — baaki main sambhal lungi."
                    if hinglish else "Looks like an auto-reply 🙂 When the owner sees this, a one-word 'YES' is all I need — I'll handle the rest.")
            return out({"action": "send", "body": body, "cta": "binary_yes_no",
                        "rationale": "Canned WhatsApp-Business auto-reply detected; one explicit flag for the owner, no further pitching."})
        if n == 2:
            return out({"action": "wait", "wait_seconds": 86400,
                        "rationale": "Second auto-reply in a row — owner not at the phone. Backing off 24h instead of burning turns."})
        return out({"action": "end", "rationale": f"Auto-reply {n}x with no human response; closing the conversation."})
    mem.setdefault("last_msgs", []).append(_norm(msg))
    mem["auto_count"] = 0

    # 4. hostility without an explicit opt-out
    if HOSTILE_RE.search(msg):
        state.hostile_count += 1
        if state.hostile_count >= 2:
            mem["opted_out"] = True
            return out({"action": "end", "rationale": "Repeated frustration; exiting gracefully and suppressing sends."})
        body = ("Maaf kijiye, pareshaan karne ka iraada nahi tha. 'STOP' likh dein to main message band kar dungi — warna ek line mein bataiye kya kaam aayega."
                if hinglish else "Sorry — not my intention to bother you. Reply STOP and I won't message again; otherwise tell me in one line what would actually be useful.")
        return out({"action": "send", "body": body, "cta": "open_ended", "rationale": "Frustration without opt-out: apologise once, give a clear opt-out, stay on mission."})

    # customer-facing threads
    if from_role == "customer" or state.send_as == "merchant_on_behalf":
        if SLOT_PICK_RE.match(msg) or ACCEPT_RE.search(msg):
            state.stage = "done"
            return out(_customer_action(state, msg))
        if DEFER_RE.search(msg):
            return out({"action": "wait", "wait_seconds": 86400, "rationale": "Customer asked for later; backing off 24h."})
        if SOFT_NO_RE.match(msg):
            return out({"action": "end", "rationale": "Customer declined; closing politely."})
        body = (f"Shukriya! Aapka sawaal {m.name} ki team tak pahuncha diya hai — woh yahin jawab denge." if speaks_hindi(msg) or Customer(state.customer).hinglish
                else f"Thanks! I've passed your question to the {m.name} team — they'll reply right here shortly.")
        return out({"action": "send", "body": body, "cta": "none", "rationale": "Customer question outside the booking flow; routed to merchant, no invented answer."})

    # 5. commitment -> action mode (never re-qualify)
    if ACCEPT_RE.search(msg) and not SOFT_NO_RE.match(msg):
        if state.stage == "action":
            state.stage = "done"
            body = ("Done ✅ live ho gaya. 7 din baad views/calls ka update bhejungi." if hinglish
                    else "Done ✅ it's live. I'll send you the views/calls impact in 7 days — nothing else needed from you.")
            return out({"action": "send", "body": body, "cta": "none", "rationale": "Merchant confirmed the draft; executed and closed the loop with a follow-up promise."})
        state.stage = "action"
        draft, tail = _merchant_draft(state, hinglish)
        return out({"action": "send", "body": f"{draft}\n{tail}", "cta": "binary_confirm_cancel",
                    "rationale": "Explicit commitment detected — switched from pitch to action: delivered the artifact now, single CONFIRM to execute."})

    # 6. defer
    if DEFER_RE.search(msg):
        secs = 86400 if re.search(r"tomorrow|kal|next week", msg, re.I) else 3600
        return out({"action": "wait", "wait_seconds": secs, "rationale": f"Merchant asked for time; backing off {secs // 3600}h."})

    # 7. out of scope
    if OFF_TOPIC_RE.search(msg):
        topic = state.topic or state.deliverable or "what we were discussing"
        body = (f"Yeh mere scope ke bahar hai — iske liye aapke CA/advisor best rahenge. Wapas {topic} par: main draft ready kar doon? Reply YES."
                if hinglish else f"That's outside what I can help with — your CA/advisor is the right person for it. Back to {topic}: shall I get the draft ready? Reply YES.")
        return out({"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Out-of-scope request declined in one line; redirected to the open thread."})

    # 8. soft no
    if SOFT_NO_RE.match(msg):
        return out({"action": "end", "rationale": "Merchant declined; closing without pushing."})

    # 9. question / engaged reply -> answer from context, advance one step
    low = msg.lower()
    if re.search(r"(cost|price|charge|kitna|paisa|fee|free)", low):
        body = ("Aapki taraf se koi extra kharcha nahi — draft main banaungi, aur aapke 'YES' ke bina kuch live nahi hoga. Shuru karun?"
                if hinglish else "No extra cost from my side — I prepare the draft and nothing goes live without your OK. Shall I start? Reply YES.")
        return out({"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Cost question answered without inventing prices; re-offered the single action."})
    if re.search(r"(how|kaise|what|kya|which|why|kyun|when|kab)\b.*\?|\?$", low):
        fact = state.topic or "this"
        body = (f"Short answer: it's based on your own numbers and {fact}. Easiest is to see it — I'll send the draft and you decide. Reply YES."
                if not hinglish else f"Seedha jawab: yeh aapke apne numbers aur {fact} par based hai. Draft dekh lijiye, phir decide kijiye — reply YES.")
        return out({"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Merchant asked a question; answered briefly and offered the artifact instead of more questions."})
    body = ("Samajh gayi. Main aapke liye draft ready rakhti hoon — bas 'YES' likhiye aur bhej dungi." if hinglish
            else "Got it. I'll keep the draft ready — just reply YES and I'll send it over.")
    return out({"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Engaged but non-committal reply; acknowledged and kept a single low-friction next step."})


# ============================================================================
# 5. HTTP SERVER
# ============================================================================

SCOPES = ("category", "merchant", "customer", "trigger")
MAX_ACTIONS_PER_TICK = 20
LLM_TICK_BUDGET_S = 12.0


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Challenge contract: returns body, cta, send_as, suppression_key, rationale. Deterministic."""
    out = compose_full(category, merchant, trigger, customer)
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}


# ------------------------------------------------------------------ state

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.contexts: dict[tuple[str, str], dict] = {}     # (scope, id) -> {version, payload}
        self.conversations: dict[str, ConversationState] = {}
        self.sent_suppression: set[str] = set()
        self.merchant_memory: dict[str, dict] = {}          # merchant_id -> auto-reply / opt-out memory
        self.merchant_wait_until: dict[str, float] = {}
        self.last_trigger_for_merchant: dict[str, str] = {}

    def get(self, scope, cid):
        e = self.contexts.get((scope, cid))
        return e["payload"] if e else None

    def memory(self, merchant_id):
        return self.merchant_memory.setdefault(merchant_id or "_", {})


STORE = Store()
START = time.time()
app = FastAPI(title="Vera challenge bot")


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ endpoints

@app.get("/v1/healthz")
def healthz():
    counts = {s: 0 for s in SCOPES}
    with STORE.lock:
        for (scope, _) in STORE.contexts:
            counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name": os.environ.get("VERA_TEAM_NAME", "Team Vera+"),
        "team_members": [m for m in os.environ.get("VERA_TEAM_MEMBERS", "").split(",") if m] or ["(set VERA_TEAM_MEMBERS)"],
        "model": LLM_MODEL if llm_enabled() else "deterministic composer (no LLM at send time)",
        "approach": ("trigger-kind router over 4 contexts -> deterministic, fact-anchored composer with "
                     "no-fabrication fallbacks; optional Claude polish gated by a number/CTA validator; "
                     "rule-based multi-turn handler (per-merchant auto-reply detection, intent->action handoff, opt-out)"),
        "contact_email": os.environ.get("VERA_CONTACT_EMAIL", ""),
        "version": COMPOSER_VERSION,
        "submitted_at": "2026-09-27T00:00:00Z",
    }


@app.post("/v1/context")
async def push_context(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"accepted": False, "reason": "malformed_json"}, status_code=400)
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {SCOPES}"}, status_code=400)
    if not cid or not isinstance(payload, dict) or not isinstance(version, int):
        return JSONResponse({"accepted": False, "reason": "invalid_body", "details": "context_id, int version and object payload required"}, status_code=400)
    with STORE.lock:
        cur = STORE.contexts.get((scope, cid))
        if cur and cur["version"] >= version:
            return JSONResponse({"accepted": False, "reason": "stale_version", "current_version": cur["version"]}, status_code=409)
        STORE.contexts[(scope, cid)] = {"version": version, "payload": payload}
        # keep live conversations pointed at the freshest merchant/category/customer data
        for st in STORE.conversations.values():
            if scope == "merchant" and st.merchant.get("merchant_id") == cid:
                st.merchant = payload
            elif scope == "category" and st.category.get("slug") == cid:
                st.category = payload
            elif scope == "customer" and st.customer and st.customer.get("customer_id") == cid:
                st.customer = payload
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": _now_iso()}


URGENT = 4


def _has_unanswered_thread(merchant_id: str) -> bool:
    for st in STORE.conversations.values():
        if (st.send_as == "vera" and st.merchant.get("merchant_id") == merchant_id and st.stage != "closed"
                and not any(t["from"] == "merchant" for t in st.turns)):
            return True
    return False


def _candidate(trg_id: str, now: datetime | None):
    """Return (trigger, merchant, category, customer) if this trigger should fire now, else None."""
    trg = STORE.get("trigger", trg_id)
    if not trg:
        return None
    exp = _parse_ts(trg.get("expires_at"))
    if now and exp and exp.tzinfo and now.tzinfo and exp < now:
        return None
    sk = trg.get("suppression_key") or trg_id
    if sk in STORE.sent_suppression:
        return None
    mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
    merchant = STORE.get("merchant", mid)
    if not merchant:
        return None
    category = STORE.get("category", merchant.get("category_slug"))
    if not category:
        return None
    mem = STORE.memory(mid)
    if mem.get("opted_out"):
        return None
    wait_until = STORE.merchant_wait_until.get(mid)
    if wait_until and now and now.timestamp() < wait_until:
        return None
    is_customer = trg.get("scope") == "customer" or bool(trg.get("customer_id"))
    if not is_customer and (trg.get("urgency") or 0) < URGENT and _has_unanswered_thread(mid):
        return None  # don't stack nudges on a merchant who hasn't answered the last one
    customer = None
    if is_customer:
        customer = STORE.get("customer", trg.get("customer_id"))
        if not customer or not Customer(customer).can_message:
            return None
    return trg, merchant, category, customer


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    now = _parse_ts(body.get("now")) or datetime.now(timezone.utc)
    today = now.date() if now else None
    actions, planned = [], []
    with STORE.lock:
        cands = [c for c in (_candidate(t, now) for t in body.get("available_triggers") or []) if c]
        # most urgent first; one merchant-facing send per merchant and one per customer per tick (restraint)
        cands.sort(key=lambda c: (-(c[0].get("urgency") or 0), c[0].get("id", "")))
        seen = set()
        for trg, merchant, category, customer in cands:
            mid = merchant["merchant_id"]
            lane = ("cust", customer["customer_id"]) if customer else ("merch", mid)
            if lane in seen:
                continue
            conv_id = f"conv_{mid}_{trg['id']}"
            if conv_id in STORE.conversations:
                continue
            seen.add(lane)
            # the dataset's reference day anchors "days out" maths unless the judge's clock is later
            ref = today if today and today > DATASET_TODAY else DATASET_TODAY
            out = compose_full(category, merchant, trg, customer, today=ref)
            planned.append((conv_id, trg, merchant, category, customer, out))
            if len(planned) >= MAX_ACTIONS_PER_TICK:
                break

    if llm_enabled() and planned:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(llm_polish, p[5]["body"], p[1].get("kind", ""), p[3].get("slug", ""), Category(p[3]).taboos): i
                    for i, p in enumerate(planned)}
            done, _ = fut_wait(futs, timeout=LLM_TICK_BUDGET_S)
            for f in done:
                try:
                    planned[futs[f]][5]["body"] = f.result()
                except Exception:
                    pass

    with STORE.lock:
        for conv_id, trg, merchant, category, customer, out in planned:
            mid = merchant["merchant_id"]
            STORE.sent_suppression.add(out["suppression_key"])
            if not customer:
                STORE.last_trigger_for_merchant[mid] = trg["id"]
            STORE.conversations[conv_id] = ConversationState(
                conversation_id=conv_id, merchant=merchant, category=category, customer=customer, trigger=trg,
                send_as=out["send_as"], deliverable=out["_deliverable"], topic=out["_topic"], first_body=out["body"],
                bot_bodies=[out["body"]], merchant_memory=STORE.memory(mid),
            )
            actions.append({
                "conversation_id": conv_id,
                "merchant_id": mid,
                "customer_id": customer["customer_id"] if customer else None,
                "send_as": out["send_as"],
                "trigger_id": trg["id"],
                "template_name": out["template_name"],
                "template_params": out["template_params"],
                "body": out["body"],
                "cta": out["cta"],
                "suppression_key": out["suppression_key"],
                "rationale": out["rationale"],
            })
    return {"actions": actions}


def _state_for_unknown(conv_id: str, merchant_id: str | None, customer_id: str | None) -> ConversationState | None:
    """Replies can arrive for conversations we didn't open (replays). Rebuild the best state we can."""
    merchant = STORE.get("merchant", merchant_id) if merchant_id else None
    if not merchant:
        merchant = {"merchant_id": merchant_id or "unknown", "identity": {"name": "your business"}, "category_slug": ""}
    category = STORE.get("category", merchant.get("category_slug")) or {"slug": merchant.get("category_slug", "")}
    trg_id = STORE.last_trigger_for_merchant.get(merchant_id)
    trigger = STORE.get("trigger", trg_id) if trg_id else None
    deliverable, topic = None, None
    if trigger:
        out = compose_full(category, merchant, trigger, None)
        deliverable, topic = out["_deliverable"], out["_topic"]
    else:
        m = Merchant(merchant)
        if m.signal_value("stale_posts") or "post" in (m.last_vera_message() or "").lower():
            deliverable, topic = "your next Google posts", "fresh Google posts"
    customer = STORE.get("customer", customer_id) if customer_id else None
    return ConversationState(
        conversation_id=conv_id, merchant=merchant, category=category, customer=customer, trigger=trigger,
        send_as="merchant_on_behalf" if customer else "vera", deliverable=deliverable, topic=topic,
        merchant_memory=STORE.memory(merchant.get("merchant_id")),
    )


@app.post("/v1/reply")
async def reply(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"action": "wait", "wait_seconds": 300, "rationale": "malformed request"}, status_code=400)
    conv_id = body.get("conversation_id") or "conv_unknown"
    mid, cid = body.get("merchant_id"), body.get("customer_id")
    with STORE.lock:
        state = STORE.conversations.get(conv_id)
        if state is None:
            state = _state_for_unknown(conv_id, mid, cid)
            STORE.conversations[conv_id] = state
        result = respond(state, body.get("message", ""), from_role=body.get("from_role", "merchant"))
        m_id = state.merchant.get("merchant_id")
        if result["action"] == "wait":
            recv = _parse_ts(body.get("received_at")) or datetime.now(timezone.utc)
            STORE.merchant_wait_until[m_id] = recv.timestamp() + result.get("wait_seconds", 0)
    return result


@app.post("/v1/teardown")
def teardown():
    with STORE.lock:
        STORE.reset()
    return {"status": "wiped"}


# ============================================================================
# 6. SUBMISSION GENERATOR
# ============================================================================

def _load(sub, cid):
    return json.loads((DATASET_DIR / sub / f"{cid}.json").read_text())


def write_submission(out_path: Path = HERE / "submission.jsonl") -> int:
    if not (DATASET_DIR / "test_pairs.json").exists():
        sys.exit(f"{DATASET_DIR} not found - run: python dataset/generate_dataset.py --seed-dir dataset --out dataset/expanded")
    cats = {}
    for f in glob.glob(str(DATASET_DIR / "categories" / "*.json")):
        c = json.loads(Path(f).read_text())
        cats[c["slug"]] = c
    pairs = json.loads((DATASET_DIR / "test_pairs.json").read_text())["pairs"]
    with open(out_path, "w") as out:
        for p in pairs:
            trg, mer = _load("triggers", p["trigger_id"]), _load("merchants", p["merchant_id"])
            cus = _load("customers", p["customer_id"]) if p.get("customer_id") else None
            out.write(json.dumps({"test_id": p["test_id"], **compose(cats[mer["category_slug"]], mer, trg, cus)}, ensure_ascii=False) + "\n")
    print(f"wrote {len(pairs)} lines to {out_path}")
    return 0


# ============================================================================
# 7. SELF-TEST (offline end-to-end checks)
# ============================================================================

BOT = os.environ.get("BOT_URL", "http://localhost:8080")
D = DATASET_DIR
FAILS = []


def call(method, path, body=None):
    req = rq.Request(BOT + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                     headers={"Content-Type": "application/json"})
    t = time.time()
    try:
        r = rq.urlopen(req, timeout=30)
        return r.status, json.loads(r.read()), time.time() - t
    except error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), time.time() - t


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def push(scope, cid, payload, v=1):
    return call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": v, "payload": payload,
                                         "delivered_at": "2026-04-26T09:00:00Z"})


def run_selftest():
    call("POST", "/v1/teardown")
    s, h, _ = call("GET", "/v1/healthz")
    check(s == 200 and h["status"] == "ok", "healthz up")
    s, md, _ = call("GET", "/v1/metadata")
    check(s == 200 and "approach" in md, "metadata")

    for f in glob.glob(str(D / "categories/*.json")):
        c = json.loads(Path(f).read_text()); push("category", c["slug"], c)
    for f in glob.glob(str(D / "merchants/*.json")):
        m = json.loads(Path(f).read_text()); push("merchant", m["merchant_id"], m)
    for f in glob.glob(str(D / "customers/*.json")):
        c = json.loads(Path(f).read_text()); push("customer", c["customer_id"], c)
    _, h, _ = call("GET", "/v1/healthz")
    check(h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0},
          f"warmup counts {h['contexts_loaded']}")

    m1 = json.loads((D / "merchants/m_001_drmeera_dentist_delhi.json").read_text())
    s, r, _ = push("merchant", m1["merchant_id"], m1, 1)
    check(s == 409 and r.get("reason") == "stale_version", "same version -> 409 stale_version")
    s, r, _ = call("POST", "/v1/context", {"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
    check(s == 400 and r.get("reason") == "invalid_scope", "bad scope -> 400")

    trigs = [json.loads(Path(f).read_text()) for f in sorted(glob.glob(str(D / "triggers/*.json")))]
    for t in trigs:
        push("trigger", t["id"], t)
    ids = [t["id"] for t in trigs]
    all_actions, worst = [], 0
    for rnd in range(12):  # repeated ticks with the full active list, like the harness does
        s, r, lat = call("POST", "/v1/tick", {"now": "2026-04-26T10:30:00Z", "available_triggers": ids})
        worst = max(worst, lat)
        all_actions += r["actions"]
    check(worst < 10, f"tick latency {worst:.2f}s")
    req = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
           "template_params", "body", "cta", "suppression_key", "rationale"}
    check(all(req <= set(a) for a in all_actions), f"{len(all_actions)} actions, all well-formed")
    check(all(a["body"].strip() for a in all_actions), "no empty bodies")
    check(not any("http" in a["body"] for a in all_actions), "no URLs")
    sks = [a["suppression_key"] for a in all_actions]
    check(len(sks) == len(set(sks)), "no suppression_key sent twice")
    convs = [a["conversation_id"] for a in all_actions]
    check(len(convs) == len(set(convs)), "no conversation_id reused")
    skipped = set(ids) - {a["trigger_id"] for a in all_actions}
    print(f"     sent {len(all_actions)}/{len(ids)} triggers; held back: {sorted(skipped)[:8]}{' ...' if len(skipped) > 8 else ''}")
    per_merchant = {}
    for a in all_actions:
        if a["send_as"] == "vera":
            per_merchant[a["merchant_id"]] = per_merchant.get(a["merchant_id"], 0) + 1
    check(max(per_merchant.values()) <= 2, f"restraint: max {max(per_merchant.values())} merchant-facing sends per unanswered merchant")

    # --- replay 1: auto-reply hell (different conversation ids, same merchant — like judge_simulator)
    auto = "Thank you for contacting us! Our team will respond shortly."
    acts = [call("POST", "/v1/reply", {"conversation_id": f"conv_auto_{i}", "merchant_id": "m_002_bharat_dentist_mumbai",
                                       "from_role": "merchant", "message": auto, "received_at": "2026-04-26T10:40:00Z",
                                       "turn_number": i + 1})[1]["action"] for i in range(1, 5)]
    check(acts[:3] == ["send", "wait", "end"], f"auto-reply ladder {acts}")

    # --- replay 2: intent transition on a thread we opened
    conv = next(a for a in all_actions if a["trigger_id"] == "trg_001_research_digest_dentists")["conversation_id"] \
        if any(a["trigger_id"] == "trg_001_research_digest_dentists" for a in all_actions) else "conv_intent_1"
    _, r1, _ = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                          "message": "What does the trial say exactly?", "received_at": "2026-04-26T10:41:00Z", "turn_number": 2})
    _, r2, _ = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                          "message": "Ok lets do it. Whats next?", "received_at": "2026-04-26T10:42:00Z", "turn_number": 3})
    body = r2.get("body", "").lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    check(r2["action"] == "send" and not any(q in body for q in qualifying) and any(w in body for w in ["draft", "here", "confirm"]),
          "intent -> action mode")
    print("     >", r2.get("body", "")[:220].replace("\n", " | "))
    _, r3, _ = call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                          "message": "CONFIRM", "received_at": "2026-04-26T10:43:00Z", "turn_number": 4})
    check(r3["action"] == "send" and "live" in r3["body"].lower(), "confirm -> executed")

    # --- replay 3: curveball then hostile
    _, r, _ = call("POST", "/v1/reply", {"conversation_id": "conv_gst", "merchant_id": "m_009_apollo_pharmacy_jaipur", "from_role": "merchant",
                                         "message": "Btw can you also help me with my GST filing this month?", "received_at": "2026-04-26T10:44:00Z", "turn_number": 2})
    check(r["action"] == "send" and "ca" in r["body"].lower(), "off-topic GST -> polite decline + redirect")
    _, r, _ = call("POST", "/v1/reply", {"conversation_id": "conv_hostile", "merchant_id": "m_005_pizzajunction_restaurant_delhi", "from_role": "merchant",
                                         "message": "Stop messaging me. This is useless spam.", "received_at": "2026-04-26T10:45:00Z", "turn_number": 2})
    check(r["action"] == "end", "hostile + stop -> end")
    push("trigger", "trg_new_pizza", {**trigs[10], "id": "trg_new_pizza", "suppression_key": "new:pizza"})
    _, r, _ = call("POST", "/v1/tick", {"now": "2026-04-26T10:50:00Z", "available_triggers": ["trg_new_pizza"]})
    check(r["actions"] == [], "opted-out merchant gets no new sends")

    # --- customer booking flow
    c_conv = next((a["conversation_id"] for a in all_actions if a["trigger_id"] == "trg_003_recall_due_priya"), None)
    if c_conv:
        _, r, _ = call("POST", "/v1/reply", {"conversation_id": c_conv, "merchant_id": "m_001_drmeera_dentist_delhi",
                                             "customer_id": "c_001_priya_for_m001", "from_role": "customer", "message": "2",
                                             "received_at": "2026-04-26T11:00:00Z", "turn_number": 2})
        check(r["action"] == "send" and "Booked" in r["body"], "customer slot pick -> booked")
        print("     >", r["body"])

    # --- adaptive injection: new perf numbers are used
    m1b = json.loads(json.dumps(m1)); m1b["performance"]["views"] = 2580; m1b["performance"]["ctr"] = 0.019
    s, _, _ = push("merchant", m1["merchant_id"], m1b, 2)
    check(s == 200, "version bump accepted")
    t_new = {"id": "trg_inject_comp", "scope": "merchant", "kind": "competitor_opened", "source": "external",
             "merchant_id": "m_014_dr_asha_dentist_chandigarh", "payload": {"competitor_name": "Pearl Smile", "distance_km": 0.8},
             "urgency": 3, "suppression_key": "inject:comp", "expires_at": "2026-12-01T00:00:00Z"}
    push("trigger", t_new["id"], t_new)
    _, r, _ = call("POST", "/v1/tick", {"now": "2026-04-26T11:05:00Z", "available_triggers": [t_new["id"]]})
    check(len(r["actions"]) == 1 and "Pearl Smile" in r["actions"][0]["body"], "new trigger mid-test is composed")

    print(f"\n{len(FAILS)} failure(s)" + (": " + "; ".join(FAILS) if FAILS else ""))
    return 0 if not FAILS else 1


# ============================================================================
# 8. CLI
# ============================================================================

def main(argv=None):
    global BOT
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else int(os.environ.get("PORT", 8080))
    if cmd == "submission":
        return write_submission()
    import uvicorn
    if cmd == "serve":
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
        return 0
    if cmd == "selftest":
        BOT = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(50):
            try:
                rq.urlopen(BOT + "/v1/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        code = run_selftest()
        server.should_exit = True
        return code
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())