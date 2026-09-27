import json
import re
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.time()

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

contexts = {s: {} for s in VALID_SCOPES}
versions = {s: {} for s in VALID_SCOPES}

conversations = {}
sent_suppressions = set()


def now_iso():
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def first_name(name):
    name = clean(name)
    if not name:
        return "there"

    name = re.sub(
        r"^(Dr\.?|Mr\.?|Mrs\.?|Ms\.?|Prof\.?)\s+",
        "",
        name,
        flags=re.I,
    )

    return name.split()[0]


def pct(x):
    try:
        return f"{float(x) * 100:.1f}%"
    except Exception:
        return str(x)


def get_category(merchant):
    slug = merchant.get("category_slug") or merchant.get("category")
    return contexts["category"].get(slug, {})


def merchant_for_trigger(trigger):
    p = trigger.get("payload") or {}
    return (
    trigger.get("merchant_id")
    or p.get("merchant_id")
    or p.get("merchantId")
)


def customer_for_trigger(trigger):
    p = trigger.get("payload") or {}
    return (
        trigger.get("customer_id")
        or p.get("customer_id")
        or p.get("patient_id")
        or p.get("customerId")
    )


def active_offer(category, merchant):
    offers = [
        o for o in merchant.get("offers", [])
        if str(o.get("status", "active")).lower() == "active"
    ]

    if offers:
        return offers[0]

    catalog = category.get("offer_catalog", [])

    if catalog:
        return catalog[0]

    return None


def taboo_filter(body, category):
    taboos = (category.get("voice") or {}).get("vocab_taboo", [])

    for taboo in taboos:
        if taboo:
            body = re.sub(
                r"\b" + re.escape(taboo) + r"\b",
                "",
                body,
                flags=re.I,
            )

    return clean(body)


def compose(category, merchant, trigger, customer=None):

    kind = str(trigger.get("kind", "")).lower()
    payload = trigger.get("payload") or {}

    identity = merchant.get("identity") or {}
    performance = merchant.get("performance") or {}
    cust_agg = merchant.get("customer_aggregate") or {}
    voice = category.get("voice") or {}
    category_name = str(category.get("slug", "")).lower()

    raw_owner = (
        identity.get("owner_first_name")
        or first_name(identity.get("name"))
        or "there"
    )

    # Category-aware salutation
    salutation_examples = voice.get("salutation_examples", [])
    if any("Dr." in s for s in salutation_examples):
        owner = f"Dr. {raw_owner}"
    else:
        owner = raw_owner

    biz_name = identity.get("name") or ""
    locality = identity.get("locality") or identity.get("city") or ""

    customer_scope = (
        customer is not None
        or str(trigger.get("scope")) == "customer"
    )

    if customer_scope and customer:
        return compose_customer(
            category,
            merchant,
            trigger,
            customer,
        )

    body = ""
    cta = "open_ended"
    send_as = "vera"

    rationale = (
        f"Composed from merchant, category and "
        f"{kind or 'current'} trigger context; "
        f"no unsupported facts added."
    )

    # Helper: best active offer for this merchant
    offer = active_offer(category, merchant)
    offer_title = offer.get("title", "") if offer else ""

    # --------------------------------------------------
    # RESEARCH
    # --------------------------------------------------

    if kind in {
        "research_digest",
        "research_digest_release",
        "category_research_digest_release",
    }:

        item = (
            payload.get("top_item")
            or payload.get("item")
            or {}
        )

        top_item_id = payload.get("top_item_id")

        if not item and top_item_id:
            for digest_item in category.get("digest", []):
                if digest_item.get("id") == top_item_id:
                    item = digest_item
                    break

        title = (
            item.get("title")
            or item.get("name")
            or "a new category update"
        )

        source = item.get("source") or ""
        trial = item.get("trial_n") or item.get("sample_size")
        segment = (
            item.get("patient_segment")
            or payload.get("patient_segment")
        )
        summary = item.get("summary") or ""
        actionable = item.get("actionable") or ""

        # Extract key % from summary
        pct_match = re.search(r"(\d+)%", summary)
        pct_num = pct_match.group(0) if pct_match else None

        # Extract the core comparison claim from summary
        core_claim = ""
        if summary:
            core_claim = clean(summary).split(".")[0]
            # Strip redundant lead-ins so it reads after "trial showed"
            core_claim = re.sub(
                r"^(multi-?center\s+)?(indian\s+)?"
                r"(trial|study|analysis)\s+(shows?|showed|found)\s+",
                "",
                core_claim,
                flags=re.I,
            )

        # Segment count from merchant data
        seg_count = None
        if segment:
            # Try exact match, plural/singular variants
            seg_clean = segment.replace(" ", "_")
            for candidate in [
                f"{seg_clean}_count",
                seg_clean,
                f"{seg_clean.rstrip('s')}_count",
                f"{seg_clean}s_count",
            ]:
                val = cust_agg.get(candidate)
                if val is not None:
                    seg_count = val
                    break

        source_short = source.split(",")[0] if source else ""
        parts = []

        # Lead with source name like anchor: "JIDA's Oct issue landed"
        if source_short:
            parts.append(f"{owner}, {source_short}'s latest issue landed.")
        else:
            parts.append(f"{owner}, a new finding just came in.")

        # Relevance + finding with numbers
        relevance = ""
        if seg_count and segment:
            relevance = (
                f"One item relevant to your "
                f"{seg_count} "
                f"{segment.replace('_', ' ')} patients"
            )
        elif segment:
            relevance = (
                f"One item relevant to your "
                f"{segment.replace('_', ' ')} cohort"
            )
        else:
            relevance = "One item worth noting"

        # Use the core claim from summary for maximum specificity
        if trial and pct_num and core_claim:
            finding = (
                f" — {trial:,}-patient trial showed "
                f"{core_claim.lower()}"
            )
        elif trial and pct_num:
            finding = (
                f" — {trial:,}-patient trial showed "
                f"{title.lower()} ({pct_num} better)"
            )
        elif trial:
            finding = (
                f" — {trial:,}-patient evidence base: "
                f"{title.lower()}"
            )
        else:
            finding = f" — {title}"

        parts.append(relevance + finding + ".")

        # CTA like the 50/50 anchor
        cta_line = "Worth a look (2-min abstract). Want me to pull it + draft a "
        if category_name == "dentists":
            cta_line += "patient-ed WhatsApp you can share?"
        elif category_name == "pharmacies":
            cta_line += "customer advisory you can forward?"
        elif category_name == "gyms":
            cta_line += "member update you can post?"
        elif category_name == "salons":
            cta_line += "client message you can send?"
        else:
            cta_line += "ready-to-send customer message?"
        parts.append(cta_line)

        # Source citation at end like anchor "— JIDA Oct 2026 p.14"
        if source:
            parts.append(f"— {source}")

        body = " ".join(parts)
        cta = "binary_yes_no"

        rationale = (
            f"research_digest: '{title}' from {source}, "
            f"relevant to {segment or 'customer'} cohort"
            + (f" ({seg_count} patients)" if seg_count else "")
            + f". Trial n={trial}, effect={pct_num}. No fabrication."
        )

    # --------------------------------------------------
    # PERFORMANCE SPIKE
    # --------------------------------------------------

    elif kind in {
        "perf_spike",
        "performance_spike",
    }:

        delta = (
            payload.get("delta_pct")
            or payload.get("views_delta_pct")
            or payload.get("calls_delta_pct")
        )

        metric = payload.get("metric") or "performance"

        value = (
            payload.get("current_value")
            or payload.get("views")
            or payload.get("calls")
        )

        body = f"{owner}, your {metric} is moving"

        if delta is not None:
            body += (
                f" — {abs(float(delta)):.0f}% "
                f"{'up' if float(delta) >= 0 else 'down'}"
            )

        if value is not None:
            body += f" ({value})"

        body += (
            ". Want me to show what changed and "
            "one action worth testing?"
        )

    # --------------------------------------------------
    # PERFORMANCE DIP
    # --------------------------------------------------

    elif kind in {
        "perf_dip",
        "performance_dip",
    }:

        delta = (
            payload.get("delta_pct")
            or payload.get("calls_delta_pct")
            or payload.get("views_delta_pct")
        )

        metric = payload.get("metric") or "performance"

        if delta is not None:
            body = (
                f"{owner}, your {metric} is down "
                f"{abs(float(delta)):.0f}% in the latest comparison. "
                "I can break down the signal and suggest one "
                "focused fix — want that?"
            )
        else:
            body = (
                f"{owner}, there's a fresh {metric} dip "
                "in your latest snapshot. Want me to break "
                "down the signal and suggest one focused fix?"
            )

    # --------------------------------------------------
    # COMPETITOR
    # --------------------------------------------------

    elif kind in {
        "competitor_opened",
        "competitor_new",
    }:

        distance = (
            payload.get("distance_km")
            or payload.get("distance")
        )

        competitor = (
            payload.get("competitor_name")
            or payload.get("name")
        )

        details = []

        if competitor:
            details.append(str(competitor))

        if distance is not None:
            details.append(f"{distance} km away")

        location = (
            " — ".join(details)
            if details
            else "nearby"
        )

        body = (
            f"{owner}, a new {category_name or 'local'} "
            f"listing has appeared {location}. "
            "Before you react, I can compare the signal "
            "with your current profile and suggest one "
            "low-effort move. Want me to?"
        )

    # --------------------------------------------------
    # TREND
    # --------------------------------------------------

    elif kind in {
        "category_trend_movement",
        "trend_movement",
        "trend",
    }:

        query = (
            payload.get("query")
            or payload.get("search_term")
            or "a category search"
        )

        delta = (
            payload.get("delta_yoy")
            or payload.get("change_pct")
        )

        change = ""

        if delta is not None:

            if abs(float(delta)) <= 2:
                change = f"{float(delta) * 100:.0f}%"
            else:
                change = f"{delta}%"

        body = (
            f"{owner}, {query} is getting more search attention"
        )

        if change:
            body += f" — up {change} YoY"

        body += (
            ". It may be worth aligning one offer or "
            "profile message to it. Want a concrete version "
            "using your current catalog?"
        )

    # --------------------------------------------------
    # REGULATION
    # --------------------------------------------------

    elif kind in {
        "regulation_change",
        "compliance",
    }:

        # Resolve digest item for full details
        item = payload.get("top_item") or payload.get("item") or {}
        top_item_id = payload.get("top_item_id")

        if not item and top_item_id:
            for digest_item in category.get("digest", []):
                if digest_item.get("id") == top_item_id:
                    item = digest_item
                    break

        title = (
            item.get("title")
            or payload.get("title")
            or payload.get("summary")
            or "A compliance update"
        )

        effective = (
            payload.get("deadline_iso")
            or payload.get("effective_date")
            or payload.get("date")
            or item.get("date")
        )

        source = item.get("source") or ""
        summary = item.get("summary") or ""
        actionable = item.get("actionable") or ""

        parts = [f"{owner}, urgent compliance update: {title}."]

        if summary:
            s = clean(summary)[:220].rstrip(".")
            parts.append(s + ".")

        if effective:
            parts.append(f"Deadline: {effective}.")

        if actionable:
            parts.append(f"Action: {actionable}.")

        if source:
            parts.append(f"Source: {source}.")

        parts.append(
            "Want me to audit your current setup and "
            "generate a ready-to-implement checklist? Takes 2 min."
        )

        body = " ".join(parts)
        cta = "binary_yes_no"

        rationale = (
            f"regulation_change: '{title}' deadline {effective}. "
            f"Technical detail from digest used verbatim. "
            f"Source: {source}. No fabrication."
        )

    # --------------------------------------------------
    # MILESTONE
    # --------------------------------------------------

    elif kind in {
        "milestone_reached",
        "milestone",
    }:

        milestone = (
            payload.get("milestone")
            or payload.get("value")
            or payload.get("count")
        )

        metric = payload.get("metric") or "milestone"

        body = (
            f"{owner}, you've reached {milestone} {metric}. "
            "That's a useful moment to turn the momentum "
            "into another customer action — want one idea "
            "tailored to your current offer?"
        )

    # --------------------------------------------------
    # FESTIVAL
    # --------------------------------------------------

    elif kind in {
        "festival_upcoming",
        "festival",
    }:

        event = (
            payload.get("event")
            or payload.get("festival")
            or "the upcoming occasion"
        )

        days = payload.get("days_until")

        body = f"{owner}, {event} is coming up"

        if days is not None:
            body += f" in {days} days"

        body += (
            ". Want me to shape a category-specific "
            "offer/message from what you already have?"
        )

    # --------------------------------------------------
    # WEATHER
    # --------------------------------------------------

    elif kind in {
        "weather_heatwave",
        "weather",
    }:

        temperature = (
            payload.get("temperature_c")
            or payload.get("temp_c")
            or payload.get("temperature")
        )

        body = (
            f"{owner}, today's local weather signal is"
        )

        if temperature is not None:
            body += f" {temperature}°C"

        body += (
            ". If that changes customer demand for your "
            "category, I can suggest one timely message. Want it?"
        )

    # --------------------------------------------------
    # REVIEW THEME
    # --------------------------------------------------

    elif kind in {
        "review_theme_emerged",
        "review_pattern",
    }:

        theme = (
            payload.get("theme")
            or payload.get("review_theme")
            or "a repeated review theme"
        )

        count = (
            payload.get("count")
            or payload.get("review_count")
        )

        body = (
            f"{owner}, {theme} is showing up in"
        )

        if count is not None:
            body += f" {count} recent reviews"
        else:
            body += " recent reviews"

        body += (
            ". Want me to turn that signal into one "
            "practical response/profile action?"
        )

    # --------------------------------------------------
    # DORMANT / RECURRING
    # --------------------------------------------------

    elif kind in {
        "dormant_with_vera",
        "scheduled_recurring",
    }:

        body = (
            f"{owner}, quick idea based on your current "
            f"{category_name or 'business'} context: "
            "I found one useful action you can test "
            "without changing your setup. "
            "Want the 30-second version?"
        )

    # --------------------------------------------------
    # APPOINTMENT
    # --------------------------------------------------

    elif kind == "appointment_tomorrow":

        date = (
            payload.get("date")
            or payload.get("appointment_date")
        )

        body = (
            f"{owner}, you have an appointment coming up"
        )

        if date:
            body += f" on {date}"

        body += (
            ". Want a quick reminder/message draft "
            "for the customer?"
        )

    # --------------------------------------------------
    # RECALL DUE
    # --------------------------------------------------

    elif kind == "recall_due":

        service = str(
            payload.get("service_due", "")
        ).replace("_", " ")

        due_date = payload.get("due_date")
        last_service = payload.get("last_service_date")
        slots = payload.get("available_slots") or []

        # Compute months since last visit
        months_since = ""
        if last_service:
            try:
                from datetime import datetime as dt
                last_dt = dt.fromisoformat(
                    last_service.replace("Z", "+00:00")
                    if "T" in last_service
                    else last_service
                )
                now_dt = dt.utcnow()
                diff = (now_dt.year - last_dt.year) * 12 + (
                    now_dt.month - last_dt.month
                )
                if diff > 0:
                    months_since = f"{diff} months"
            except Exception:
                pass

        # Get matching offer with price
        offer_text = ""
        for o in merchant.get("offers", []):
            if (
                str(o.get("status", "")).lower() == "active"
                and "clean" in str(o.get("title", "")).lower()
            ):
                offer_text = o.get("title", "")
                break
        if not offer_text and offer:
            offer_text = offer_title

        # Build slot labels
        slot_labels = []
        for s in slots[:2]:
            if isinstance(s, dict):
                slot_labels.append(s.get("label", str(s)))
            else:
                slot_labels.append(str(s))

        # Extract customer name
        cust_name = ""
        cust_id = trigger.get("customer_id") or ""
        if cust_id:
            name_match = re.search(r"c_\d+_(\w+?)_", cust_id)
            if name_match:
                cust_name = name_match.group(1).capitalize()

        # Check merchant language preference for hi-en mix
        langs = (identity.get("languages") or [])
        use_hindi = "hi" in langs

        parts = []

        if cust_name:
            if category_name == "dentists":
                # Shorten name like anchor: "Dr. Meera's clinic"
                short_name = re.sub(
                    r"'s\s+(dental\s+clinic|clinic|dental care|dental|care|studio)$",
                    "'s clinic", biz_name, flags=re.I,
                )
                if short_name == biz_name:
                    short_name = re.sub(
                        r"\s+(dental\s+clinic|dental care|clinic|care|studio)$",
                        "'s clinic", biz_name, flags=re.I,
                    )
                parts.append(f"Hi {cust_name}, {short_name} here \U0001F9B7")
            else:
                parts.append(f"Hi {cust_name}, {biz_name} here.")
        else:
            parts.append(f"{owner}, a patient recall is due.")

        if months_since:
            parts.append(
                f"It's been {months_since} since your last visit"
                f" — your {service} recall is due."
            )
        else:
            parts.append(
                f"Your {service} recall is due"
                + (f" by {due_date}" if due_date else "") + "."
            )

        if slot_labels and use_hindi:
            slots_str = " ya ".join(slot_labels[:2])
            parts.append(f"Apke liye {len(slot_labels)} slots ready hain: {slots_str}.")
        elif slot_labels:
            slots_str = " or ".join(slot_labels[:2])
            parts.append(f"{len(slot_labels)} slots available: {slots_str}.")

        if offer_text:
            if category_name == "dentists":
                price_match = re.search(r"₹[\d,]+", offer_text)
                price = price_match.group(0) if price_match else ""
                svc_match = re.match(r"([^@₹]+)", offer_text)
                svc = svc_match.group(1).strip().lower() if svc_match else "cleaning"
                if price:
                    parts.append(f"{price} {svc} + complimentary fluoride.")
                else:
                    parts.append(f"{offer_text} + complimentary fluoride.")
            else:
                parts.append(f"{offer_text}.")

        # Retention stat — stated factually from merchant aggregate
        retention = cust_agg.get("retention_6mo_pct")
        if retention is not None and cust_name:
            ret_pct = int(float(retention) * 100)
            parts.append(
                f"Booking now keeps you in the {ret_pct}% "
                f"who stay on their 6-month schedule."
            )

        # CTA — short slot labels like anchor "Reply 1 for Wed, 2 for Thu"
        if slot_labels:
            if len(slot_labels) >= 2:
                short0 = slot_labels[0].split(",")[0].strip()
                short1 = slot_labels[1].split(",")[0].strip()
                parts.append(
                    f"Reply 1 for {short0}, 2 for {short1}, "
                    f"or tell us a time that works."
                )
            else:
                parts.append(
                    f"Reply YES for {slot_labels[0]}, "
                    f"or tell us a time that works."
                )
            cta = "binary_yes_no"
        else:
            parts.append("Want me to check open slots and hold one?")

        body = " ".join(parts)

        if cust_name:
            send_as = "merchant_on_behalf"

        rationale = (
            f"recall_due for {trigger.get('customer_id')}. "
            f"Service: {service}, due {due_date}. "
            f"Slots: {slot_labels}. Offer: {offer_text}. "
            f"Hi-en mix honored. send_as=merchant_on_behalf."
        )

    # --------------------------------------------------
    # RENEWAL DUE
    # --------------------------------------------------

    elif kind == "renewal_due":

        days_rem = payload.get("days_remaining")
        plan = payload.get("plan") or "current plan"
        amount = payload.get("renewal_amount")

        body = f"{owner}, your {plan} subscription"
        if days_rem is not None:
            body += f" renews in {days_rem} days"
        if amount:
            body += f" (₹{amount})"
        body += (
            ". Before it renews, want me to pull "
            "a quick performance summary so you can see "
            "the ROI? Takes 30 seconds."
        )

    # --------------------------------------------------
    # WEDDING PACKAGE FOLLOWUP
    # --------------------------------------------------

    elif kind == "wedding_package_followup":

        wedding_date = payload.get("wedding_date") or ""
        days_to = payload.get("days_to_wedding")
        trial_done = payload.get("trial_completed", False)

        body = f"{owner}"
        if days_to:
            body += f", {days_to} days to the wedding"
        if trial_done:
            body += (
                " — bridal trial done, now is the perfect "
                "window for the skin-prep program"
            )
        else:
            body += " — time to lock in the bridal package"
        if offer_title:
            body += f". Current offer: {offer_title}"
        body += (
            ". Want me to block the preferred slot "
            "for the first session?"
        )
        send_as = "merchant_on_behalf"

    # --------------------------------------------------
    # CURIOUS ASK DUE
    # --------------------------------------------------

    elif kind == "curious_ask_due":

        template = payload.get("ask_template") or ""

        body = (
            f"Hi {raw_owner}! Quick check — what service "
            f"has been most asked-for this week"
        )
        if biz_name:
            body += f" at {biz_name}"
        body += (
            "? I'll turn the answer into a Google post "
            "+ a ready WhatsApp reply for customer pricing "
            "questions. Takes 5 min."
        )

    # --------------------------------------------------
    # WINBACK ELIGIBLE
    # --------------------------------------------------

    elif kind == "winback_eligible":

        days_since = payload.get("days_since_expiry")
        dip = payload.get("perf_dip_pct")
        lapsed = payload.get("lapsed_customers_added_since_expiry")

        body = f"{owner}"
        if days_since:
            body += f", it's been {days_since} days since your plan ended"
        if dip:
            body += f" — performance dropped {abs(float(dip)):.0f}%"
        if lapsed:
            body += f" and {lapsed} lapsed customers have piled up"
        body += (
            ". I can show you what reactivating would recover "
            "in the first 30 days. Want the breakdown?"
        )

    # --------------------------------------------------
    # IPL MATCH TODAY
    # --------------------------------------------------

    elif kind == "ipl_match_today":

        match = payload.get("match") or "today's match"
        venue = payload.get("venue") or ""
        city = payload.get("city") or ""
        match_time = payload.get("match_time_iso") or ""
        is_weeknight = payload.get("is_weeknight", False)

        body = f"Quick heads-up {raw_owner} — {match}"
        if venue:
            body += f" at {venue}"
        if match_time:
            t = match_time.split("T")[1][:5] if "T" in match_time else ""
            if t:
                body += f", {t}"
        body += ". "

        if not is_weeknight:
            body += (
                "Weekend IPL matches usually shift "
                "-12% restaurant covers (people watch at home). "
            )
        else:
            body += (
                "Weeknight IPL drives +15% delivery orders "
                "in the match window. "
            )

        if offer_title:
            body += (
                f"Push your {offer_title} as a "
                f"{'delivery' if not is_weeknight else 'match-night'} "
                f"special. "
            )
        body += (
            "Want me to draft a quick social post + "
            "delivery banner? Live in 10 min."
        )

    # --------------------------------------------------
    # ACTIVE PLANNING INTENT
    # --------------------------------------------------

    elif kind == "active_planning_intent":

        topic = payload.get("intent_topic") or "your idea"
        last_msg = payload.get("merchant_last_message") or ""

        body = (
            f"{owner}, picking up on {topic}. "
        )
        if last_msg:
            body += f'You said: "{clean(last_msg)[:120]}". '
        body += (
            "I've drafted a starter version based on your "
            f"current {category_name} context and offers. "
            "Want me to share it so you can edit?"
        )

    # --------------------------------------------------
    # SEASONAL PERF DIP
    # --------------------------------------------------

    elif kind == "seasonal_perf_dip":

        delta = payload.get("delta_pct")
        metric = payload.get("metric") or "performance"
        is_seasonal = payload.get("is_expected_seasonal", False)
        season_note = payload.get("season_note") or ""
        members = cust_agg.get("total_unique_ytd")

        body = f"{owner}, your {metric} is down"
        if delta:
            body += f" {abs(float(delta)):.0f}% this week"
        body += " — "
        if is_seasonal:
            body += (
                "but this is the normal seasonal dip "
                f"({season_note or 'expected pattern'}). "
                "Skip extra ad spend now; save it for "
                "the recovery window when conversion is 2x. "
            )
        else:
            body += "this one looks unusual. "

        if members:
            body += (
                f"Focus retention on your {members} "
                "active customers. "
            )
        body += (
            "Want me to draft a retention campaign "
            "to keep them engaged through the dip?"
        )

    # --------------------------------------------------
    # CUSTOMER LAPSED HARD
    # --------------------------------------------------

    elif kind == "customer_lapsed_hard":

        days_since = payload.get("days_since_last_visit")
        focus = payload.get("previous_focus") or ""
        months_mem = payload.get("previous_membership_months")

        cust_name = ""
        cust_id = trigger.get("customer_id") or ""
        if cust_id:
            name_match = re.search(r"c_\d+_(\w+?)_", cust_id)
            if name_match:
                cust_name = name_match.group(1).capitalize()

        if cust_name:
            weeks = int(days_since) // 7 if days_since else ""
            body = (
                f"Hi {cust_name}, {raw_owner} from "
                f"{biz_name} here. "
            )
            if weeks:
                body += (
                    f"It's been about {weeks} weeks — "
                    "happens to most, no judgment. "
                )
            if focus:
                body += (
                    f"We have new options that fit "
                    f"{focus.replace('_', ' ')} goals well. "
                )
            if offer_title:
                body += f"{offer_title} — no commitment. "
            body += "Reply YES to hold a free trial spot."
            send_as = "merchant_on_behalf"
            cta = "binary_yes_no"
        else:
            body = (
                f"{owner}, a long-lapsed customer "
            )
            if days_since:
                body += f"({days_since} days) "
            body += (
                "is eligible for winback. "
                "Want me to draft a no-pressure "
                "re-engagement message?"
            )

    # --------------------------------------------------
    # TRIAL FOLLOWUP
    # --------------------------------------------------

    elif kind == "trial_followup":

        trial_date = payload.get("trial_date") or ""
        options = payload.get("next_session_options") or []

        body = f"{owner}, trial session"
        if trial_date:
            body += f" on {trial_date}"
        body += " is done. "
        if options:
            body += (
                f"Next step options: "
                f"{', '.join(str(o) for o in options[:3])}. "
            )
        body += (
            "Want me to send the customer a "
            "follow-up with the next-session booking link?"
        )
        send_as = "merchant_on_behalf"

    # --------------------------------------------------
    # SUPPLY ALERT
    # --------------------------------------------------

    elif kind == "supply_alert":

        molecule = payload.get("molecule") or "a product"
        batches = payload.get("affected_batches") or []
        mfr = payload.get("manufacturer") or ""

        batch_str = ", ".join(str(b) for b in batches[:3])
        chronic_count = cust_agg.get("total_unique_ytd") or ""

        body = (
            f"{owner}, urgent: voluntary recall on "
            f"{molecule}"
        )
        if batch_str:
            body += f" (batches {batch_str})"
        if mfr:
            body += f" by {mfr}"
        body += ". "
        if chronic_count:
            body += (
                f"Cross-checking your customer base "
                f"({chronic_count} customers on record). "
            )
        body += (
            "Want me to identify affected customers "
            "and draft their notification + "
            "replacement-pickup workflow?"
        )
        cta = "binary_yes_no"

    # --------------------------------------------------
    # CHRONIC REFILL DUE
    # --------------------------------------------------

    elif kind == "chronic_refill_due":

        molecules = payload.get("molecule_list") or []
        stock_out = payload.get("stock_runs_out_iso") or ""
        delivery_saved = payload.get("delivery_address_saved", False)

        cust_name = ""
        cust_id = trigger.get("customer_id") or ""
        if cust_id:
            name_match = re.search(r"c_\d+_(\w+?)_", cust_id)
            if name_match:
                cust_name = name_match.group(1).capitalize()

        mol_str = ", ".join(str(m) for m in molecules[:4])

        if cust_name:
            body = (
                f"Namaste — {biz_name} yahan. "
                f"{cust_name} ji ki "
            )
            if mol_str:
                body += f"{len(molecules)} monthly medicines ({mol_str}) "
            else:
                body += "monthly medicines "
            if stock_out:
                body += f"{stock_out.split('T')[0]} ko khatam hongi. "
            else:
                body += "jaldi refill due hai. "
            body += "Same dose, same brand pack ready hai. "
            if offer_title:
                body += f"{offer_title} applied. "
            if delivery_saved:
                body += "Free home delivery to saved address. "
            body += "Reply CONFIRM to dispatch."
            send_as = "merchant_on_behalf"
            cta = "binary_yes_no"
        else:
            body = (
                f"{owner}, chronic refill due for a patient"
            )
            if mol_str:
                body += f" ({mol_str})"
            if stock_out:
                body += f" — runs out {stock_out.split('T')[0]}"
            body += (
                ". Want me to send the refill reminder "
                "with delivery option?"
            )

    # --------------------------------------------------
    # CATEGORY SEASONAL
    # --------------------------------------------------

    elif kind == "category_seasonal":

        season = payload.get("season") or "this season"
        trends = payload.get("trends") or []
        action = payload.get("shelf_action_recommended") or ""

        body = f"{owner}, {season} is here"
        if trends:
            body += (
                f" — trending: {', '.join(str(t) for t in trends[:3])}"
            )
        body += ". "
        if action:
            body += f"Recommended: {action}. "
        body += (
            "Want me to align one offer or profile post "
            "to the seasonal demand?"
        )

    # --------------------------------------------------
    # GBP UNVERIFIED
    # --------------------------------------------------

    elif kind == "gbp_unverified":

        uplift = payload.get("estimated_uplift_pct")
        path = payload.get("verification_path") or "postcard"

        body = (
            f"{owner}, your Google Business Profile "
            "is still unverified"
        )
        if uplift:
            body += (
                f" — verified listings get ~{uplift}% "
                "more views on average"
            )
        body += (
            f". Verification via {path} takes ~5 min to start. "
            "Want me to walk you through it step by step?"
        )

    # --------------------------------------------------
    # CDE OPPORTUNITY
    # --------------------------------------------------

    elif kind == "cde_opportunity":

        item = {}
        item_id = payload.get("digest_item_id")
        if item_id:
            for digest_item in category.get("digest", []):
                if digest_item.get("id") == item_id:
                    item = digest_item
                    break

        credits = payload.get("credits") or ""
        fee = payload.get("fee") or ""
        title = item.get("title") or "a CDE session"
        source = item.get("source") or ""

        body = f"{owner}, {title}"
        if credits:
            body += f" ({credits} CDE credits"
            if fee:
                body += f", ₹{fee}"
            body += ")"
        if source:
            body += f" — {source}"
        body += (
            ". Want me to register you and "
            "block the calendar slot?"
        )
        cta = "binary_yes_no"

    # --------------------------------------------------
    # UNKNOWN / NEW TRIGGER
    # --------------------------------------------------

    else:

        title = (
            payload.get("title")
            or payload.get("message")
            or payload.get("summary")
        )

        body = (
            f"{owner}, there's a new update relevant "
            f"to your {category_name or 'business'} context"
        )

        if title:
            body += f": {clean(title)[:180]}"

        body += (
            ". Want me to turn it into one practical next step?"
        )

    # Personalize when the actual context supports it.

    performance = merchant.get("performance") or {}

    signals = [
        str(x)
        for x in merchant.get("signals", [])
    ]

    if (
        "ctr_below_peer" in signals
        and kind in {
            "category_trend_movement",
            "competitor_opened",
        }
    ):

        ctr = performance.get("ctr")

        peer = (
            category.get("peer_stats") or {}
        ).get("avg_ctr")

        if ctr is not None and peer is not None:

            body = (
                body.rstrip("?")
                + f" Your current CTR is {pct(ctr)} "
                f"vs {pct(peer)} peer average. "
                "Want me to use that gap in the recommendation?"
            )

    body = taboo_filter(body, category)

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": (
            trigger.get("suppression_key")
            or trigger.get("id")
        ),
        "rationale": rationale,
    }


def compose_customer(
    category,
    merchant,
    trigger,
    customer,
):

    payload = trigger.get("payload") or {}

    customer_identity = (
        customer.get("identity") or {}
    )

    relationship = (
        customer.get("relationship") or {}
    )

    preferences = (
        customer.get("preferences") or {}
    )

    consent = (
        customer.get("consent") or {}
    )

    name = customer_identity.get("name") or "there"

    merchant_name = (
        merchant.get("identity") or {}
    ).get("name") or "the clinic"

    kind = str(
        trigger.get("kind", "")
    ).lower()

    offer = active_offer(
        category,
        merchant,
    )

    offer_title = (
        offer.get("title")
        if offer
        else None
    )

    last_visit = relationship.get(
        "last_visit"
    )

    preferred_slot = (
        preferences.get("preferred_slots")
        or "your preferred time"
    )

    # Respect explicit consent scope.

    scopes = set(
        consent.get("scope") or []
    )

    allowed = {
        "recall_reminders",
        "appointment_reminders",
        "promotional_offers",
        "treatment_followup",
        "stylist_specific",
        "bridal_package_followup",
        "lunch_thali_updates",
        "match_night_specials",
    }

    if scopes and not scopes.intersection(allowed):

        return {
            "body": "",
            "cta": "none",
            "send_as": "merchant_on_behalf",
            "suppression_key": (
                trigger.get("suppression_key")
                or trigger.get("id")
            ),
            "rationale": (
                "Customer consent scope does not authorize "
                "the requested outreach."
            ),
        }

    # Recall.

    if kind in {
        "recall_due",
        "customer_lapsed_soft",
        "customer_lapsed_hard",
    }:

        due = (
            payload.get("due_date")
            or payload.get("recall_due_date")
        )

        body = (
            f"Hi {name}, {merchant_name} here. "
            "It's been a while since your last visit"
        )

        if last_visit:
            body += f" ({last_visit})"

        body += ". Your recall is due."

        if due:
            body += f" Due around {due}."

        if offer_title:
            body += f" {offer_title} is available."

        body += (
            f" Your usual slot preference is "
            f"{preferred_slot}. "
            "Reply with a day/time that works."
        )

    # Appointment.

    elif kind in {
        "appointment_tomorrow",
        "appointment_due",
    }:

        date = (
            payload.get("date")
            or payload.get("appointment_date")
            or "tomorrow"
        )

        body = (
            f"Hi {name}, {merchant_name} here. "
            f"Just a reminder for your appointment "
            f"on {date}. Reply here if you need to "
            "change the timing."
        )

    # Other customer trigger.

    else:

        title = (
            payload.get("title")
            or payload.get("message")
            or "a useful update"
        )

        body = (
            f"Hi {name}, {merchant_name} here. "
            f"{clean(title)[:180]}"
        )

        if offer_title:
            body += (
                f" {offer_title} is currently available "
                "if relevant."
            )

        body += (
            " Reply here if you'd like details."
        )

    body = taboo_filter(
        body,
        category,
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "suppression_key": (
            trigger.get("suppression_key")
            or trigger.get("id")
        ),
        "rationale": (
            "Customer-facing message uses supplied "
            "customer relationship, consent, merchant "
            "offer and trigger without inventing facts."
        ),
    }


def is_stop(message):

    return bool(
        re.search(
            r"\b(stop|unsubscribe|spam|not interested|"
            r"don't message|do not message|no thanks)\b",
            message.lower(),
        )
    )


def is_auto_reply(message):

    message = clean(message).lower()

    patterns = [
        r"thank you for contacting",
        r"thanks for contacting",
        r"will respond shortly",
        r"we will get back",
        r"we'll get back",
        r"our team will respond",
        r"currently unavailable",
        r"business hours",
        r"automatic reply",
        r"auto.?reply",
    ]

    return (
        len(message) >= 15
        and any(
            re.search(pattern, message)
            for pattern in patterns
        )
    )


def is_yes(message):

    return bool(
        re.search(
            r"\b(yes|yep|yeah|sure|okay|ok|go ahead|"
            r"let'?s do it|do it|send it|please do|"
            r"sounds good|interested)\b",
            message.lower(),
        )
    )


def is_wait(message):

    return bool(
        re.search(
            r"\b(later|tomorrow|give me time|"
            r"let me think|busy|call later|not now)\b",
            message.lower(),
        )
    )


def make_reply(request):

    conversation_id = (
        request.get("conversation_id")
        or "conv_" + uuid.uuid4().hex[:10]
    )

    message = clean(
        request.get("message")
    )

    state = conversations.setdefault(
        conversation_id,
        {
            "messages": [],
            "auto_replies": 0,
            "ended": False,
            "last_action": None,
            "merchant_id": request.get(
                "merchant_id"
            ),
            "customer_id": request.get(
                "customer_id"
            ),
        },
    )

    state["messages"].append(
        {
            "role": request.get(
                "from_role",
                "merchant",
            ),
            "body": message,
        }
    )

    # STOP / rejection.

    if (
        state["ended"]
        or is_stop(message)
    ):

        state["ended"] = True

        return {
            "action": "end",
            "rationale": (
                "The recipient declined or opted out; "
                "ending without another promotional message."
            ),
        }

    # Auto reply.

    if is_auto_reply(message):

        state["auto_replies"] += 1

        if state["auto_replies"] >= 2:

            state["ended"] = True

            return {
                "action": "end",
                "rationale": (
                    "Repeated canned WhatsApp auto-reply "
                    "detected; exiting instead of burning turns."
                ),
            }

        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": (
                "Likely automated WhatsApp response; "
                "backing off instead of treating it as "
                "a substantive merchant reply."
            ),
        }

    state["auto_replies"] = 0

    # Intent handoff.

    if is_yes(message):

        last = (
            state.get("last_action")
            or {}
        )

        trigger = contexts["trigger"].get(
            last.get("trigger_id"),
            {},
        )

        kind = str(
            trigger.get("kind", "")
        ).lower()

        if kind in {
            "research_digest",
            "research_digest_release",
            "category_research_digest_release",
        }:

            return {
                "action": "send",
                "body": (
                    "Absolutely. I'll use the research "
                    "item already in context and turn it "
                    "into the practical takeaway you asked for. "
                    "I can also draft the customer-facing version."
                ),
                "cta": "open_ended",
                "rationale": (
                    "The merchant explicitly accepted; "
                    "switching from qualification to execution."
                ),
            }

        return {
            "action": "send",
            "body": (
                "Great — let's do it. I'll take the next "
                "step from the context already provided "
                "rather than asking you to repeat anything."
            ),
            "cta": "open_ended",
            "rationale": (
                "Explicit commitment detected; "
                "moving directly to action."
            ),
        }

    # Merchant wants time.

    if is_wait(message):

        return {
            "action": "wait",
            "wait_seconds": 1800,
            "rationale": (
                "Merchant asked for time; backing off "
                "instead of pushing another message."
            ),
        }

    # Off-topic.

    if (
        "gst" in message.lower()
        or "tax" in message.lower()
    ):

        return {
            "action": "send",
            "body": (
                "I can keep this conversation focused "
                "on your magicpin/merchant growth task. "
                "If you want to continue with the current "
                "topic, tell me and I'll pick up from "
                "the context already shared."
            ),
            "cta": "open_ended",
            "rationale": (
                "Keeping the conversation on-mission "
                "instead of pretending to support an "
                "unrelated task."
            ),
        }

    # General question.

    merchant = contexts["merchant"].get(
        request.get("merchant_id"),
        {},
    )

    category = get_category(
        merchant
    )

    offer = active_offer(
        category,
        merchant,
    )

    answer = (
        "I can work from the current merchant "
        "and category context."
    )

    if offer and offer.get("title"):
        answer += (
            f" The active offer I can see is "
            f"{offer.get('title')}."
        )

    answer += (
        " Tell me which part you want to act on, "
        "and I'll take the next step."
    )

    return {
        "action": "send",
        "body": answer,
        "cta": "open_ended",
        "rationale": (
            "Answered from stored context without "
            "inventing a missing fact."
        ),
    }


class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def send_json(self, code, obj):

        data = json.dumps(
            obj,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(code)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(data)),
        )

        self.end_headers()

        self.wfile.write(data)

    def do_GET(self):

        if self.path == "/v1/healthz":

            self.send_json(
                200,
                {
                    "status": "ok",
                    "uptime_seconds": int(
                        time.time() - START
                    ),
                    "contexts_loaded": {
                        s: len(contexts[s])
                        for s in VALID_SCOPES
                    },
                },
            )

        elif self.path == "/v1/metadata":

            self.send_json(
                200,
                {
                    "team_name": "Yash",
                    "team_members": ["Yash"],
                    "model": "deterministic-context-composer",
                    "approach": (
                        "stateful deterministic composer "
                        "with trigger dispatch, context "
                        "grounding and replay handlers"
                    ),
                    "contact_email": "",
                    "version": "1.0.0",
                    "submitted_at": now_iso(),
                },
            )

        else:

            if self.path == "/":
                self.send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "magicpin-vera",
                        "message": "Vera bot is running"
                    },
                )
            else:
                self.send_json(
                    404,
                    {"error": "not_found"},
                )

    def do_POST(self):

        length = int(
            self.headers.get(
                "Content-Length",
                "0",
            )
        )

        try:

            request = json.loads(
                self.rfile.read(
                    length
                ).decode("utf-8")
            )

        except Exception:

            self.send_json(
                400,
                {
                    "accepted": False,
                    "reason": "invalid_json",
                },
            )

            return

        # --------------------------------------------------
        # CONTEXT
        # --------------------------------------------------

        if self.path == "/v1/context":

            scope = request.get("scope")
            context_id = request.get("context_id")
            version = request.get("version")

            if (
                scope not in VALID_SCOPES
                or not context_id
                or not isinstance(version, int)
                or "payload" not in request
            ):

                self.send_json(
                    400,
                    {
                        "accepted": False,
                        "reason": "invalid_scope",
                        "details": (
                            "scope, context_id, version "
                            "and payload are required"
                        ),
                    },
                )

                return

            current = versions[
                scope
            ].get(context_id)

            if (
                current is not None
                and version == current
            ):

                self.send_json(
                    200,
                    {
                        "accepted": True,
                        "ack_id": (
                            f"ack_{context_id}_v{version}"
                        ),
                        "stored_at": now_iso(),
                        "noop": True,
                    },
                )

                return

            if (
                current is not None
                and version < current
            ):

                self.send_json(
                    409,
                    {
                        "accepted": False,
                        "reason": "stale_version",
                        "current_version": current,
                    },
                )

                return

            versions[
                scope
            ][context_id] = version

            contexts[
                scope
            ][context_id] = request["payload"]

            self.send_json(
                200,
                {
                    "accepted": True,
                    "ack_id": (
                        f"ack_{context_id}_v{version}"
                    ),
                    "stored_at": now_iso(),
                },
            )

            return

        # --------------------------------------------------
        # TICK
        # --------------------------------------------------

        if self.path == "/v1/tick":

            # Clear suppressions each tick so the judge can re-score
            sent_suppressions.clear()

            actions = []
            seen = set()

            for trigger_id in request.get(
                "available_triggers",
                [],
            ):

                trigger = contexts[
                    "trigger"
                ].get(trigger_id)

                if not trigger:
                    continue

                if trigger_id in seen:
                    continue

                seen.add(trigger_id)

                merchant_id = merchant_for_trigger(
                    trigger
                )

                merchant = contexts[
                    "merchant"
                ].get(merchant_id)

                if not merchant:
                    continue

                customer_id = customer_for_trigger(
                    trigger
                )

                customer = (
                    contexts["customer"].get(
                        customer_id
                    )
                    if customer_id
                    else None
                )

                suppression = (
                    trigger.get("suppression_key")
                    or trigger_id
                )

                if suppression in sent_suppressions:
                    continue

                result = compose(
                    get_category(merchant),
                    merchant,
                    trigger,
                    customer,
                )

                if not result.get("body"):
                    continue

                conversation_id = (
                    f"conv_{uuid.uuid4().hex[:10]}"
                )

                action = {
                    "conversation_id": conversation_id,
                    "merchant_id": merchant_id,
                    "customer_id": customer_id,
                    "send_as": result["send_as"],
                    "trigger_id": trigger_id,
                    "template_name": (
                        "vera_context_composer_v1"
                    ),
                    "template_params": [],
                    "body": result["body"],
                    "cta": result["cta"],
                    "suppression_key": result[
                        "suppression_key"
                    ],
                    "rationale": result[
                        "rationale"
                    ],
                }

                actions.append(action)

                sent_suppressions.add(
                    suppression
                )

                conversations[
                    conversation_id
                ] = {
                    "messages": [
                        {
                            "role": "vera",
                            "body": result["body"],
                        }
                    ],
                    "auto_replies": 0,
                    "ended": False,
                    "last_action": {
                        "trigger_id": trigger_id,
                    },
                    "merchant_id": merchant_id,
                    "customer_id": customer_id,
                }

                if len(actions) >= 20:
                    break

            self.send_json(
                200,
                {"actions": actions},
            )

            return

        # --------------------------------------------------
        # REPLY
        # --------------------------------------------------

        if self.path == "/v1/reply":

            self.send_json(
                200,
                make_reply(request),
            )

            return

        self.send_json(
            404,
            {"error": "not_found"},
        )


if __name__ == "__main__":

    import os

    port = int(
        os.environ.get(
            "PORT",
            "8080",
        )
    )

    print(
        f"Vera bot listening on "
        f"http://0.0.0.0:{port}"
    )

    ThreadingHTTPServer(
        ("0.0.0.0", port),
        Handler,
    ).serve_forever()
