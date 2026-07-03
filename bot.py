#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


TEAM_NAME = os.getenv("TEAM_NAME", "Vera Forge")
TEAM_MEMBERS = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Solo Contributor").split(",") if m.strip()]
MODEL_NAME = os.getenv("MODEL_NAME", "rule-based trigger composer")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "candidate@example.com")
VERSION = os.getenv("BOT_VERSION", "1.0.0")
PORT = int(os.getenv("PORT", "8080"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")
    return value.lower() or "x"


def clamp_text(text: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.split("T")[0]


def boolish_text(message: str) -> str:
    return message.lower().strip()


def pick_first(items: List[Any], predicate) -> Optional[Any]:
    for item in items:
        if predicate(item):
            return item
    return None


@dataclass
class StoredContext:
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str]
    stored_at: str


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    trigger_id: Optional[str]
    send_as: str
    topic_kind: Optional[str]
    last_bot_body: str = ""
    last_bot_cta: str = ""
    last_sender_role: str = "vera"
    created_at: str = ""
    updated_at: str = ""
    ended: bool = False


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._contexts: Dict[str, Dict[str, StoredContext]] = {
            "category": {},
            "merchant": {},
            "customer": {},
            "trigger": {},
        }
        self._latest_versions: Dict[str, int] = {}
        self._conversations: Dict[str, ConversationState] = {}
        self._suppressed: set[str] = set()
        self.started_at = time.time()

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {scope: len(items) for scope, items in self._contexts.items()}

    def get(self, scope: str, context_id: str) -> Optional[StoredContext]:
        with self._lock:
            return self._contexts.get(scope, {}).get(context_id)

    def all_of(self, scope: str) -> Dict[str, StoredContext]:
        with self._lock:
            return dict(self._contexts.get(scope, {}))

    def put_context(self, scope: str, context_id: str, version: int, payload: Dict[str, Any], delivered_at: Optional[str]) -> tuple[bool, Dict[str, Any]]:
        with self._lock:
            current_version = self._latest_versions.get(context_id)
            if current_version is not None and version < current_version:
                return False, {"accepted": False, "reason": "stale_version", "current_version": current_version}

            if current_version is not None and version == current_version:
                stored = self._contexts.get(scope, {}).get(context_id)
                return True, {
                    "accepted": True,
                    "ack_id": f"ack_{slugify(context_id)}_v{version}",
                    "stored_at": stored.stored_at if stored else iso_now(),
                }

            stored = StoredContext(
                scope=scope,
                context_id=context_id,
                version=version,
                payload=payload,
                delivered_at=delivered_at,
                stored_at=iso_now(),
            )
            self._contexts.setdefault(scope, {})[context_id] = stored
            self._latest_versions[context_id] = version
            return True, {"accepted": True, "ack_id": f"ack_{slugify(context_id)}_v{version}", "stored_at": stored.stored_at}

    def get_trigger(self, trigger_id: str) -> Optional[Dict[str, Any]]:
        trigger = self.get("trigger", trigger_id)
        return trigger.payload if trigger else None

    def get_merchant(self, merchant_id: str) -> Optional[Dict[str, Any]]:
        merchant = self.get("merchant", merchant_id)
        return merchant.payload if merchant else None

    def get_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        customer = self.get("customer", customer_id)
        return customer.payload if customer else None

    def get_category(self, slug: str) -> Optional[Dict[str, Any]]:
        category = self.get("category", slug)
        return category.payload if category else None

    def suppress(self, suppression_key: str) -> None:
        with self._lock:
            self._suppressed.add(suppression_key)

    def is_suppressed(self, suppression_key: Optional[str]) -> bool:
        if not suppression_key:
            return False
        with self._lock:
            return suppression_key in self._suppressed

    def conversation(self, conversation_id: str) -> Optional[ConversationState]:
        with self._lock:
            return self._conversations.get(conversation_id)

    def upsert_conversation(self, state: ConversationState) -> None:
        with self._lock:
            self._conversations[state.conversation_id] = state

    def uptime_seconds(self) -> int:
        return max(0, int(time.time() - self.started_at))

    def clear(self) -> None:
        with self._lock:
            self._contexts = {
                "category": {},
                "merchant": {},
                "customer": {},
                "trigger": {},
            }
            self._latest_versions.clear()
            self._conversations.clear()
            self._suppressed.clear()


STORE = ContextStore()


def first_active_offer(merchant: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    offers = merchant.get("offers") or []
    return pick_first(offers, lambda offer: str(offer.get("status", "")).lower() == "active")


def category_slug_for_merchant(merchant: Dict[str, Any]) -> str:
    return merchant.get("category_slug") or merchant.get("identity", {}).get("category_slug") or ""


def merchant_name(merchant: Dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("name") or merchant.get("merchant_name") or "the merchant"


def owner_first_name(merchant: Dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("first_name") or "there"


def customer_name(customer: Dict[str, Any]) -> str:
    return customer.get("identity", {}).get("name") or "there"


def infer_customer_name(customer_id: Optional[str]) -> str:
    if not customer_id:
        return "there"
    match = re.match(r"c_\d+_([^_]+(?:_[^_]+)*)_for_", customer_id)
    if match:
        return match.group(1).replace("_", " ").title()
    parts = customer_id.split("_")
    if len(parts) >= 3:
        return parts[2].replace("_", " ").title()
    return "there"


def inferred_customer_identity(customer: Optional[Dict[str, Any]], trigger: Dict[str, Any]) -> Dict[str, Any]:
    if customer:
        return customer.get("identity", {}) or {}
    customer_id = trigger.get("customer_id")
    return {
        "name": infer_customer_name(customer_id),
        "language_pref": "hi-en mix",
    }


def salutation_for_category(category_slug: str, merchant: Dict[str, Any], customer: Optional[Dict[str, Any]] = None) -> str:
    if customer:
        return f"Hi {customer_name(customer)}"
    first = owner_first_name(merchant)
    if category_slug == "dentists":
        return f"Dr. {first}" if first != "there" else "Doctor"
    if category_slug == "salons":
        return f"Hi {first}"
    if category_slug == "restaurants":
        return f"Hi {first}"
    if category_slug == "gyms":
        return f"Hi {first}"
    if category_slug == "pharmacies":
        return f"Hi {first}"
    return f"Hi {first}"


def category_digest_item(category: Dict[str, Any], kind: Optional[str] = None, item_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    digest = category.get("digest") or []
    if item_id:
        found = pick_first(digest, lambda item: item.get("id") == item_id)
        if found:
            return found
    if kind:
        found = pick_first(digest, lambda item: item.get("kind") == kind)
        if found:
            return found
    return digest[0] if digest else None


def category_beat(category: Dict[str, Any], trigger_kind: str) -> Optional[Dict[str, Any]]:
    if trigger_kind in {"festival_upcoming", "category_seasonal", "seasonal_perf_dip"}:
        beats = category.get("seasonal_beats") or []
        if beats:
            return beats[0]
    if trigger_kind in {"perf_dip", "perf_spike", "milestone_reached", "curious_ask_due", "review_theme_emerged"}:
        return None
    return None


def category_voice_hint(category: Dict[str, Any], merchant: Dict[str, Any], trigger_kind: str) -> str:
    slug = category.get("slug") or category_slug_for_merchant(merchant)
    if slug == "dentists":
        return "clinical peer-to-peer"
    if slug == "salons":
        return "warm practical"
    if slug == "restaurants":
        return "operator-to-operator"
    if slug == "gyms":
        return "coach-to-operator"
    if slug == "pharmacies":
        return "precise neighbourhood pharmacist"
    return "peer-to-peer"


def category_specific_next_step(category: Dict[str, Any], merchant: Dict[str, Any], trigger_kind: str, offer_title: Optional[str]) -> str:
    slug = category.get("slug") or category_slug_for_merchant(merchant)
    if slug == "restaurants":
        return f"Use {offer_title or 'your active offer'} to lift covers and table turnover."
    if slug == "gyms":
        return f"Use {offer_title or 'your current program'} to improve trial-to-paid and reduce churn."
    if slug == "salons":
        return f"Use {offer_title or 'your current service'} to push bookings in the next peak slot."
    if slug == "pharmacies":
        return f"Use {offer_title or 'your current service'} to improve repeat-Rx retention and home delivery stickiness."
    if slug == "dentists":
        return f"Use {offer_title or 'your current service'} to improve recall and high-risk adult retention."
    return f"Use {offer_title or 'your current offer'} to turn the signal into a clear next step."


def format_money(value: Any) -> str:
    if value is None:
        return ""
    return f"₹{value}"


def available_slots_text(slots: List[Dict[str, Any]]) -> str:
    labels = [slot.get("label") or parse_date(slot.get("iso")) or "slot" for slot in slots[:2]]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]} or {labels[1]}"


def peer_position(merchant: Dict[str, Any], category: Dict[str, Any]) -> str:
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    ctr = perf.get("ctr")
    peer_ctr = peer.get("avg_ctr")
    if isinstance(ctr, (int, float)) and isinstance(peer_ctr, (int, float)):
        if ctr < peer_ctr:
            return f"CTR {ctr:.3f} is below peer avg {peer_ctr:.3f}"
        if ctr > peer_ctr:
            return f"CTR {ctr:.3f} is above peer avg {peer_ctr:.3f}"
    return ""


def build_research_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    merchant_id = merchant.get("merchant_id", "")
    cat_slug = category.get("slug") or category_slug_for_merchant(merchant)
    payload = trigger.get("payload") or {}
    item = category_digest_item(category, kind="research", item_id=payload.get("top_item_id"))
    if not item:
        item = category_digest_item(category, kind="research") or {}

    title = item.get("title") or payload.get("headline") or "this week's research item"
    source = item.get("source") or payload.get("source") or "source"
    trial_n = item.get("trial_n") or payload.get("trial_n")
    patient_segment = item.get("patient_segment") or payload.get("patient_segment")
    action = item.get("actionable") or "Worth a look."
    hero = title
    if trial_n:
        hero = f"{title} - {trial_n}-patient trial"
    if patient_segment:
        hero = f"{hero}; relevant to {patient_segment.replace('_', ' ')}"

    aggs = merchant.get("customer_aggregate") or {}
    cohort_bits: List[str] = []
    if cat_slug == "dentists" and aggs.get("high_risk_adult_count"):
        cohort_bits.append(f"your {aggs.get('high_risk_adult_count')} high-risk adults")
    elif cat_slug == "salons" and aggs.get("lapsed_90d_plus"):
        cohort_bits.append(f"your {aggs.get('lapsed_90d_plus')} lapsed 90d+ clients")
    elif cat_slug == "restaurants" and merchant.get("performance", {}).get("views"):
        cohort_bits.append(f"your current footfall base")
    elif cat_slug == "gyms" and aggs.get("monthly_churn_pct") is not None:
        cohort_bits.append(f"your churn profile")
    elif cat_slug == "pharmacies" and aggs.get("repeat_customer_pct") is not None:
        cohort_bits.append(f"your repeat-Rx base")

    salutation = salutation_for_category(cat_slug, merchant)
    body_parts = [f"{salutation}, {source} landed."]
    if cohort_bits:
        body_parts.append(f"One item that maps to {cohort_bits[0]} - {hero}.")
    else:
        body_parts.append(f"One item worth a look - {hero}.")
    body_parts.append(f"{action} Want me to pull the abstract and draft something practical for you?")
    if cat_slug == "dentists" and merchant.get("customer_aggregate", {}).get("high_risk_adult_count"):
        body_parts.append("Good fit for your recall cohort.")

    body = clamp_text(" ".join(body_parts) + f" - {source}")
    template_params = [salutation, hero, "Want me to pull the abstract and draft the next step?"]
    rationale = "Specific source-cited research hook with merchant-relevant cohort anchor and low-friction follow-up."
    cta = "open_ended"
    return {
        "conversation_id": f"conv_{merchant_id}_{slugify(trigger.get('kind', 'research'))}_{slugify(trigger.get('id', trigger.get('suppression_key', 'x')))}",
        "merchant_id": merchant_id,
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_research_digest_v1",
        "template_params": template_params,
        "body": body,
        "cta": cta,
        "suppression_key": trigger.get("suppression_key"),
        "rationale": rationale,
    }


def build_compliance_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    item = category_digest_item(category, kind="compliance", item_id=payload.get("top_item_id")) or {}
    title = item.get("title") or payload.get("title") or "a compliance update"
    source = item.get("source") or payload.get("source") or "the latest circular"
    deadline = payload.get("deadline_iso") or item.get("date") or "soon"
    if trigger.get("kind") == "gbp_unverified":
        uplift = payload.get("estimated_uplift_pct")
        verification_path = payload.get("verification_path") or "phone or postcard"
        body = clamp_text(f"{salutation_for_category(category.get('slug', ''), merchant)}, your GBP is still unverified. {verification_path.capitalize()} verification is the quickest fix here, and the expected uplift is {int(uplift * 100):.0f}% if you close it. Once verified, the profile usually gets a cleaner lead flow. Want me to draft the exact verification checklist?")
    else:
        body = clamp_text(f"{salutation_for_category(category.get('slug', ''), merchant)}, {source} is worth a look. {title}. Deadline: {parse_date(deadline) or deadline}. If you want, I can turn this into a 3-point checklist for your team.")
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'compliance'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_compliance_update_v1",
        "template_params": [salutation_for_category(category.get("slug", ""), merchant), title, f"Deadline {parse_date(deadline) or deadline}"],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Compliance message with explicit deadline and a practical next step.",
    }


def build_performance_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    metric = payload.get("metric") or "performance"
    delta_pct = payload.get("delta_pct")
    merchant_title = merchant_name(merchant)
    peer_hint = peer_position(merchant, category)
    offer = first_active_offer(merchant)
    offer_title = offer.get("title") if offer else None
    next_step = category_specific_next_step(category, merchant, trigger.get("kind", ""), offer_title)

    if trigger.get("kind") == "seasonal_perf_dip" and payload.get("is_expected_seasonal"):
        seasonal_note = category.get('seasonal_beats', [{}])[0].get('note', 'this category typically softens')
        body = clamp_text(
            f"{salutation_for_category(category.get('slug', ''), merchant)}, this dip looks seasonal rather than structural. {metric.capitalize()} is down {abs(delta_pct) * 100:.0f}% in a window where {seasonal_note}. I would pause acquisition spend, protect retention, and revisit in the stronger window. {next_step} Want me to draft a retention nudge?"
        )
    else:
        delta_text = f"{abs(delta_pct) * 100:.0f}%" if isinstance(delta_pct, (int, float)) else "materially"
        direction = "down" if isinstance(delta_pct, (int, float)) and delta_pct < 0 else "up"
        parts = [f"{salutation_for_category(category.get('slug', ''), merchant)}, {metric} is {direction} {delta_text}."]
        if peer_hint:
            parts.append(peer_hint + ".")
        if offer_title:
            parts.append(f"Your active offer {offer_title} is the cleanest lever right now.")
        parts.append(next_step)
        parts.append("Want me to suggest the next move?")
        body = clamp_text(" ".join(parts))

    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'performance'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_performance_alert_v1",
        "template_params": [merchant_title, str(metric), f"{trigger.get('kind', 'performance')} alert"],
        "body": body,
        "cta": "YES" if trigger.get("kind") in {"perf_dip", "seasonal_perf_dip", "renewal_due"} else "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Performance-aware message anchored in the merchant's own metric and a concrete next step.",
    }


def build_milestone_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    metric = payload.get("metric") or "milestone"
    value_now = payload.get("value_now")
    milestone_value = payload.get("milestone_value") or value_now
    body = clamp_text(
        f"{salutation_for_category(category.get('slug', ''), merchant)}, {metric} is at {value_now} and {milestone_value} is within reach. This is a good moment to turn the win into a GBP post or a customer-facing update. Want me to draft the copy?"
    )
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'milestone'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_milestone_v1",
        "template_params": [merchant_name(merchant), str(value_now), str(milestone_value)],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Celebrate a concrete milestone and convert it into a visible next action.",
    }


def build_curious_ask(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    salutation = salutation_for_category(category.get("slug", ""), merchant)
    merchant_title = merchant_name(merchant)
    body = clamp_text(
        f"{salutation}! Quick check - what service got the most asks this week at {merchant_title}? I can turn the answer into a Google post and a short reply draft for you. Takes 5 min."
    )
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'curious_ask'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_curious_ask_v1",
        "template_params": [salutation, merchant_title, "What service was most asked for this week?"],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Low-friction curiosity ask that offers immediate reciprocity.",
    }


def build_review_theme_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    theme = payload.get("theme") or "a repeated review theme"
    occurrences = payload.get("occurrences_30d") or payload.get("count") or 0
    quote = payload.get("common_quote") or ""
    salutation = salutation_for_category(category.get("slug", ""), merchant)
    quote_part = f" Example quote: '{quote}'." if quote else ""
    body = clamp_text(
        f"{salutation}, {occurrences} recent reviews mention {theme.replace('_', ' ')}.{quote_part} Worth fixing this week? I can draft a response or a small process change note."
    )
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'review'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_review_theme_v1",
        "template_params": [salutation, theme.replace("_", " "), str(occurrences)],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Specific review theme with quoted evidence and a practical next step.",
    }


def build_renewal_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    days_remaining = payload.get("days_remaining") or merchant.get("subscription", {}).get("days_remaining")
    plan = payload.get("plan") or merchant.get("subscription", {}).get("plan") or "plan"
    amount = payload.get("renewal_amount")
    body = clamp_text(
        f"{salutation_for_category(category.get('slug', ''), merchant)}, renewal is due in {days_remaining} days on your {plan} plan. If you want to keep the current momentum, this is the time to renew before the account pauses.{' Renewal amount: ' + format_money(amount) + '.' if amount else ''} Want me to outline the 3-step checklist?"
    )
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'renewal'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_renewal_due_v1",
        "template_params": [merchant_name(merchant), str(days_remaining), str(plan)],
        "body": body,
        "cta": "YES",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Urgent renewal reminder anchored in explicit days remaining and a simple next step.",
    }


def build_festival_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    festival = payload.get("festival") or "the festival"
    days_until = payload.get("days_until")
    cat_slug = category.get("slug", "")
    offer = first_active_offer(merchant)
    offer_title = offer.get("title") if offer else None
    seasonal = category_beat(category, "festival_upcoming")
    seasonal_note = seasonal.get("note") if seasonal else ""
    body_parts = [f"{salutation_for_category(cat_slug, merchant)}, {festival} is coming in {days_until} days." if days_until is not None else f"{salutation_for_category(cat_slug, merchant)}, {festival} is coming."]
    if seasonal_note:
        body_parts.append(f"This category usually sees: {seasonal_note}.")
    if offer_title:
        body_parts.append(f"Your active offer {offer_title} is the best lever to push now.")
    body_parts.append("Want me to draft a short festival post or customer WhatsApp?")
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'festival'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_festival_upcoming_v1",
        "template_params": [merchant_name(merchant), festival, offer_title or "seasonal offer"],
        "body": clamp_text(" ".join(body_parts)),
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Category-timed seasonal prompt using an active offer and a simple follow-up ask.",
    }


def build_competitor_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    competitor = payload.get("competitor_name") or payload.get("name") or "a new competitor"
    distance = payload.get("distance_km") or payload.get("distance")
    distance_text = f" {distance}km away" if distance is not None else " nearby"
    body = clamp_text(
        f"{salutation_for_category(category.get('slug', ''), merchant)}, {competitor} opened{distance_text}. This is the moment to sharpen your offer and GBP copy. Want me to compare their likely hook against your current offer?"
    )
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'competitor'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_competitor_opened_v1",
        "template_params": [merchant_name(merchant), competitor, str(distance or "nearby")],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Competitive alert framed as a concrete action opportunity rather than a vague heads-up.",
    }


def build_trend_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    topic = payload.get("topic") or payload.get("query") or trigger.get("kind", "trend")
    delta = payload.get("delta_yoy") or payload.get("delta_pct")
    delta_text = f"{abs(delta) * 100:.0f}%" if isinstance(delta, (int, float)) else "materially"
    slug = category.get("slug") or category_slug_for_merchant(merchant)
    if trigger.get("kind") == "category_seasonal" and slug == "pharmacies":
        trends = payload.get("trends") or []
        trend_text = ", ".join(trends[:3]) if trends else "summer demand shifts"
        body = clamp_text(f"{salutation_for_category(slug, merchant)}, {topic or 'seasonal demand'} is up {delta_text}. The shelf move is clear: {trend_text}. Move ORS/sunscreen to counter visibility and keep the slow-movers back. Want me to draft the counter-note and WhatsApp?")
    elif trigger.get("kind") == "category_seasonal" and slug == "restaurants":
        trends = payload.get("trends") or []
        trend_text = ", ".join(trends[:3]) if trends else "match-night and delivery demand shifts"
        body = clamp_text(f"{salutation_for_category(slug, merchant)}, {topic or 'seasonal demand'} is up {delta_text}. {trend_text}. For restaurants, that usually means a tighter match-night combo or delivery-only push, not a broad discount. Want me to draft one around your active offer?")
    else:
        body = clamp_text(f"{salutation_for_category(category.get('slug', ''), merchant)}, {topic} is up {delta_text}. This matches your category direction, so it may be worth a small, specific post. Want me to draft one around your active offer?")
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'trend'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_trend_signal_v1",
        "template_params": [merchant_name(merchant), topic, delta_text],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Trend signal tied to a concrete query movement and an offer-led next step.",
    }


def build_customer_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any], customer: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cat_slug = category.get("slug", category_slug_for_merchant(merchant))
    kind = trigger.get("kind", "customer")
    payload = trigger.get("payload") or {}
    identity = inferred_customer_identity(customer, trigger)
    salutation = f"Hi {identity.get('name', 'there')}"
    merchant_title = merchant_name(merchant)
    offer = first_active_offer(merchant)
    offer_title = offer.get("title") if offer else None
    language = str(identity.get("language_pref", ""))
    hi_en = "hi" in language.lower() or "mix" in language.lower()
    customer_id = trigger.get("customer_id") or (customer or {}).get("customer_id") or slugify(identity.get("name", "customer"))
    days_since_last_visit = payload.get("days_since_last_visit")
    days_to_event = payload.get("days_to_wedding") or payload.get("days_to_wedding_event")

    if kind == "recall_due":
        slots = payload.get("available_slots") or []
        slot_text = available_slots_text(slots)
        due_date = parse_date(payload.get("due_date")) or parse_date(payload.get("last_service_date")) or "soon"
        offer_piece = offer_title or payload.get("service_due", "your next visit")
        if cat_slug == "dentists":
            body = clamp_text(
                f"{salutation}, {merchant_title} here. It's been about 5 months since your last visit - your 6-month cleaning recall is due. {slot_text and f'Apke liye 2 slots ready hain: {slot_text}.'} {offer_piece} is ready whenever you are. Reply YES and I’ll confirm the slot."
            )
        elif cat_slug == "salons":
            body = clamp_text(
                f"{salutation}, {merchant_title} here. {days_since_last_visit and f'It’s been {days_since_last_visit} days since your last visit.'} {slot_text and f'2 slots ready: {slot_text}.'} {offer_piece}. Reply YES and I’ll lock one in."
            )
        elif cat_slug == "gyms":
            body = clamp_text(
                f"{salutation}, {merchant_title} here. {days_since_last_visit and f'It’s been {days_since_last_visit} days since your last session.'} {slot_text and f'2 slots ready: {slot_text}.'} {offer_piece}. Reply YES and I’ll reserve it."
            )
        elif cat_slug == "restaurants":
            body = clamp_text(
                f"{salutation}, {merchant_title} here. We’ve got a quick table for you {slot_text and f'around {slot_text}'} and {offer_piece} if you’re coming by. Reply YES and I’ll hold it."
            )
        elif cat_slug == "pharmacies":
            molecule_list = payload.get("molecule_list") or []
            molecules = ", ".join(molecule_list[:3])
            body = clamp_text(
                f"{salutation}, {merchant_title} here. Your refill window is due for {molecules or offer_piece}. {slot_text and f'We can arrange pickup/delivery around {slot_text}.'} Reply YES and I’ll keep it ready."
            )
        else:
            body = clamp_text(
                f"{salutation}, {merchant_title} here. Your next visit is due around {due_date}. {slot_text and f'2 slots ready: {slot_text}.'} {offer_piece}. Reply YES and I’ll lock one in."
            )
        cta = "YES"
        template_name = "vera_customer_recall_v1"
        template_params = [salutation.replace("Hi ", ""), offer_piece, slot_text or "your preferred time"]
        rationale = "Customer recall with explicit slots, name personalization, and a single confirmation ask."
    elif kind in {"customer_lapsed_soft", "customer_lapsed_hard", "trial_followup", "wedding_package_followup", "appointment_tomorrow", "chronic_refill_due"}:
        if kind == "trial_followup":
            next_options = payload.get("next_session_options") or []
            option_text = available_slots_text(next_options)
            body = clamp_text(
                f"{salutation}, {merchant_title} here. Good to see the trial - if you want to continue, I can hold {option_text or 'the next session slot'} for you. Reply YES and I’ll send the booking details."
            )
        elif kind == "wedding_package_followup":
            wedding_date = parse_date(payload.get("wedding_date")) or "your wedding"
            body = clamp_text(
                f"{salutation}, {merchant_title} here. You’re in the right window for pre-event prep before {wedding_date}. {days_to_event and f'Only {days_to_event} days to go.'} If you want, I can share a simple package plan and hold your preferred slot. Reply YES."
            )
        elif kind == "appointment_tomorrow":
            appointment_date = parse_date(payload.get("appointment_date")) or parse_date(payload.get("scheduled_for")) or "tomorrow"
            body = clamp_text(f"{salutation}, reminder from {merchant_title}: your appointment is {appointment_date}. If you need to reschedule, reply YES and I’ll help with the next available slot.")
        elif kind == "chronic_refill_due":
            molecule_list = payload.get("molecule_list") or []
            molecules = ", ".join(molecule_list[:3])
            stock_runs_out_iso = parse_date(payload.get("stock_runs_out_iso")) or "soon"
            body = clamp_text(f"{salutation}, {merchant_title} here. Your refill window is opening for {molecules} and the stock runs out on {stock_runs_out_iso}. If you want home delivery, reply YES and I’ll keep it ready with pharmacist counsel. We can also note the batch, MRP, and delivery slot.")
        else:
            goal = ((customer or {}).get("relationship", {}) or {}).get("services_received", [])[-1] if ((customer or {}).get("relationship", {}) or {}).get("services_received") else (payload.get("last_service") or payload.get("previous_focus") or "your last visit")
            days_last = payload.get("days_since_last_visit") or payload.get("days_since_last_touch") or 0
            if cat_slug in {"gym", "gyms"}:
                focus = payload.get('previous_focus') or goal
                body = clamp_text(f"{salutation}, {merchant_title} here. Coach check - it’s been {days_last} days since your last session and your last focus was {focus}. If you want to get back into the split, reply YES and I’ll send a low-friction PT or HIIT slot. No pressure, just one easy step back into training.")
            elif cat_slug == "salons":
                body = clamp_text(f"{salutation}, {merchant_title} here. It’s been {days_last} days since your last visit and the last service was {goal}. If you want to come back, reply YES and I’ll share the next best slot and package.")
            elif cat_slug == "restaurants":
                body = clamp_text(f"{salutation}, {merchant_title} here. It’s been {days_last} days since your last visit and the last order was {goal}. If you want to come back, reply YES and I’ll share a simple offer and table option.")
            else:
                body = clamp_text(f"{salutation}, {merchant_title} here. It’s been {days_last} days since your last visit and the last service was {goal}. If you want to come back, reply YES and I’ll send the easiest next step.")
        cta = "YES"
        template_name = "vera_customer_followup_v1"
        template_params = [salutation.replace("Hi ", ""), merchant_title, "Reply YES to continue"]
        rationale = "Customer follow-up with a specific next action and low-friction confirmation ask."
    else:
        body = clamp_text(
            f"{salutation}, {merchant_title} here. I’ve got a short update for you based on your last visit and preferences. Reply YES if you want the next option."
        )
        cta = "YES"
        template_name = "vera_customer_generic_v1"
        template_params = [salutation.replace("Hi ", ""), merchant_title, "Reply YES for the next step"]
        rationale = "Generic customer-facing follow-up with a simple confirmation CTA."

    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{customer_id}_{slugify(kind)}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": trigger.get("customer_id") or (customer or {}).get("customer_id"),
        "send_as": "merchant_on_behalf",
        "trigger_id": trigger.get("id"),
        "template_name": template_name,
        "template_params": template_params,
        "body": body,
        "cta": cta,
        "suppression_key": trigger.get("suppression_key"),
        "rationale": rationale,
    }


def compose_for_trigger(trigger: Dict[str, Any], category: Dict[str, Any], merchant: Dict[str, Any], customer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    kind = trigger.get("kind", "")
    if customer is not None or trigger.get("scope") == "customer":
        return build_customer_message(category, merchant, trigger, customer)
    if kind == "research_digest" or kind == "cde_opportunity":
        return build_research_message(category, merchant, trigger)
    if kind == "ipl_match_today":
        payload = trigger.get("payload") or {}
        match = payload.get("match") or "the match"
        venue = payload.get("venue") or "the venue"
        match_time = payload.get("match_time_iso") or "tonight"
        body = clamp_text(f"{salutation_for_category(category.get('slug', ''), merchant)}, {match} is at {venue} on {parse_date(match_time) or match_time}. For restaurants, Saturday IPL home matches usually push covers away from dine-in and into delivery, so I’d avoid a broad match-night discount today. If you have a BOGO or delivery-only offer, I’d shift that instead. Want me to draft a match-night combo or delivery banner?")
        return {
            "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'ipl'))}_{slugify(trigger.get('id', 'x'))}",
            "merchant_id": merchant.get("merchant_id", ""),
            "customer_id": None,
            "send_as": "vera",
            "trigger_id": trigger.get("id"),
            "template_name": "vera_ipl_match_day_v1",
            "template_params": [merchant_name(merchant), match, venue],
            "body": body,
            "cta": "open_ended",
            "suppression_key": trigger.get("suppression_key"),
            "rationale": "Restaurant-specific IPL guidance anchored in match time and delivery-vs-dine-in behavior.",
        }
    if kind in {"regulation_change", "supply_alert", "compliance", "gbp_unverified"}:
        return build_compliance_message(category, merchant, trigger)
    if kind in {"perf_dip", "perf_spike", "seasonal_perf_dip"}:
        return build_performance_message(category, merchant, trigger)
    if kind == "milestone_reached":
        return build_milestone_message(category, merchant, trigger)
    if kind == "curious_ask_due":
        return build_curious_ask(category, merchant, trigger)
    if kind == "review_theme_emerged":
        return build_review_theme_message(category, merchant, trigger)
    if kind == "renewal_due":
        return build_renewal_message(category, merchant, trigger)
    if kind == "festival_upcoming":
        return build_festival_message(category, merchant, trigger)
    if kind == "category_seasonal":
        return build_trend_message(category, merchant, trigger)
    if kind == "competitor_opened":
        return build_competitor_message(category, merchant, trigger)
    if kind in {"category_trend_movement", "category_seasonal", "trend", "tech", "seasonal"}:
        return build_trend_message(category, merchant, trigger)
    if kind == "active_planning_intent":
        return build_planning_response(category, merchant, trigger)
    if kind in {"dormant_with_vera", "winback_eligible"}:
        return build_dormancy_response(category, merchant, trigger)
    return build_generic_response(category, merchant, trigger)


def build_planning_response(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    topic = payload.get("intent_topic") or payload.get("ask_template") or "the plan"
    merchant_title = merchant_name(merchant)
    offer = first_active_offer(merchant)
    offer_title = offer.get("title") if offer else None
    kind = category.get("slug", "")
    if kind == "restaurants" and "thali" in topic.lower():
        body = clamp_text(f"{salutation_for_category(kind, merchant)}, here is a clean starter version for {topic}: 1) a simple tiered price ladder, 2) a fixed delivery window, 3) a short WhatsApp pitch for office admins. If you want, I can turn it into a 5-line draft using {offer_title or 'your active offer'}. Focus on covers, AOV, and table turnover.")
    elif kind == "gyms" and "pt" in topic.lower():
        body = clamp_text(f"{salutation_for_category(kind, merchant)}, for {topic} I’d keep it simple: one 2x/week option, one starter price, and one clear result promise without overclaiming. Want me to draft the package and CTA? The hook should speak to churn and trial-to-paid conversion.")
    elif kind == "salons" and "bridal" in topic.lower():
        body = clamp_text(f"{salutation_for_category(kind, merchant)}, for {topic} I’d anchor it around a trial, a prep timeline, and one premium add-on. {offer_title or 'Your active offer'} can become the entry point. Want a ready-to-send draft? Keep the copy practical, not hypey.")
    elif kind == "pharmacies":
        body = clamp_text(f"{salutation_for_category(kind, merchant)}, for {topic} I’d keep the language precise: molecule, MRP, batch, and delivery. {offer_title or 'Your active offer'} can be framed as refill convenience and pharmacist counsel. Want a draft?" )
    else:
        body = clamp_text(f"{salutation_for_category(kind, merchant)}, yes - {topic} is worth structuring. I can turn it into a simple offer ladder, a WhatsApp pitch, and a GBP post draft. Want me to draft version 1?")
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'planning'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_planning_assist_v1",
        "template_params": [merchant_title, topic, offer_title or "your active offer"],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Direct continuation of a merchant's stated intent with a draftable next step.",
    }


def build_dormancy_response(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = trigger.get("payload") or {}
    days = payload.get("days_since_expiry") or payload.get("days_since_last_touch") or 14
    next_step = category_specific_next_step(category, merchant, trigger.get("kind", ""), first_active_offer(merchant).get("title") if first_active_offer(merchant) else None)
    body = clamp_text(f"{salutation_for_category(category.get('slug', ''), merchant)}, quick check - it’s been {days} days since the last touch. If now is not the right time, I can back off; if you want momentum, I can bring one specific idea and one draft. {next_step}")
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(trigger.get('kind', 'dormant'))}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_dormancy_check_v1",
        "template_params": [merchant_name(merchant), str(days), "one specific idea"],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Gentle re-engagement that respects the merchant's time and offers a concrete next step.",
    }


def build_generic_response(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    kind = trigger.get("kind", "update")
    payload = trigger.get("payload") or {}
    payload_text = json.dumps(payload, ensure_ascii=False)
    offer = first_active_offer(merchant)
    offer_title = offer.get("title") if offer else None
    next_step = category_specific_next_step(category, merchant, kind, offer_title)
    body = clamp_text(f"{salutation_for_category(category.get('slug', ''), merchant)}, I’ve got a {kind.replace('_', ' ')} signal for {merchant_name(merchant)}. The key details are {payload_text[:120]}. {next_step} Want me to turn this into the most useful next message?")
    return {
        "conversation_id": f"conv_{merchant.get('merchant_id', '')}_{slugify(kind)}_{slugify(trigger.get('id', 'x'))}",
        "merchant_id": merchant.get("merchant_id", ""),
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": trigger.get("id"),
        "template_name": "vera_generic_trigger_v1",
        "template_params": [merchant_name(merchant), kind.replace("_", " "), "next message"],
        "body": body,
        "cta": "open_ended",
        "suppression_key": trigger.get("suppression_key"),
        "rationale": "Fallback message that still stays specific to the trigger payload and merchant identity.",
    }


def auto_reply_like(message: str) -> bool:
    text = boolish_text(message)
    patterns = [
        r"thank you for contacting",
        r"our team will respond shortly",
        r"we (will|ll) get back",
        r"currently unavailable",
        r"away",
        r"busy",
        r"please wait",
        r"out of office",
        r"we are closed",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def hostile_like(message: str) -> bool:
    text = boolish_text(message)
    patterns = [
        r"\bstop messaging me\b",
        r"\bstop\b",
        r"\buseless spam\b",
        r"\bspam\b",
        r"\bnot interested\b",
        r"\bdon'?t message\b",
        r"\bunsubscribe\b",
        r"\bleave me alone\b",
        r"\bremove me\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def wants_action(message: str) -> bool:
    text = boolish_text(message)
    patterns = [
        r"\byes\b",
        r"send me",
        r"go ahead",
        r"proceed",
        r"what's next",
        r"whats next",
        r"draft it",
        r"please share",
        r"sounds good",
        r"okay",
        r"ok",
        r"let's do it",
        r"lets do it",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def wants_wait(message: str) -> bool:
    text = boolish_text(message)
    patterns = [r"later", r"tomorrow", r"call later", r"busy now", r"next week", r"some other time"]
    return any(re.search(pattern, text) for pattern in patterns)


def kind_from_conversation(state: Optional[ConversationState]) -> str:
    if state and state.topic_kind:
        return state.topic_kind
    return ""


def reply_to_message(store: ContextStore, conversation_id: str, merchant_id: str, customer_id: Optional[str], from_role: str, message: str, turn_number: int) -> Dict[str, Any]:
    state = store.conversation(conversation_id)
    merchant = store.get_merchant(merchant_id) or {}
    category = store.get_category(category_slug_for_merchant(merchant)) or {}
    customer = store.get_customer(customer_id) if customer_id else None
    text = boolish_text(message)

    if auto_reply_like(text):
        state = state or ConversationState(conversation_id, merchant_id, customer_id, None, "vera", None, created_at=iso_now(), updated_at=iso_now())
        state.ended = True
        state.updated_at = iso_now()
        store.upsert_conversation(state)
        return {"action": "end", "rationale": "Detected a canned auto-reply pattern; ending to avoid turn waste."}

    if hostile_like(text):
        state = state or ConversationState(conversation_id, merchant_id, customer_id, None, "vera", None, created_at=iso_now(), updated_at=iso_now())
        state.ended = True
        state.updated_at = iso_now()
        store.upsert_conversation(state)
        return {"action": "end", "rationale": "Merchant signalled disengagement; stopping politely."}

    if wants_wait(text):
        state = state or ConversationState(conversation_id, merchant_id, customer_id, None, "vera", None, created_at=iso_now(), updated_at=iso_now())
        state.updated_at = iso_now()
        store.upsert_conversation(state)
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Merchant asked for time; backing off for 30 minutes."}

    topic_kind = kind_from_conversation(state)
    if not topic_kind and state and state.trigger_id:
        trigger = store.get_trigger(state.trigger_id) or {}
        topic_kind = trigger.get("kind", "")

    trigger = store.get_trigger(state.trigger_id) if state and state.trigger_id else {}
    trigger = trigger or {}
    category_slug = category.get("slug") or category_slug_for_merchant(merchant)
    salutation = salutation_for_category(category_slug, merchant, customer)
    merchant_title = merchant_name(merchant)
    offer = first_active_offer(merchant)
    offer_title = offer.get("title") if offer else None

    if wants_action(text):
        if topic_kind == "research_digest":
            item = category_digest_item(category, kind="research", item_id=(trigger.get("payload") or {}).get("top_item_id")) or {}
            title = item.get("title") or "the paper"
            source = item.get("source") or "the source"
            body = clamp_text(
                f"Done - I pulled the abstract. The main line is: {title}. {source}. If you want, I can now turn it into a patient-facing WhatsApp draft for your high-risk adults."
            )
        elif topic_kind in {"recall_due", "trial_followup", "appointment_tomorrow", "chronic_refill_due", "wedding_package_followup"} and customer:
            body = clamp_text(
                f"Done - I can take it forward. {salutation} is ready for the next step, and I can keep the message short and slot-led. Reply back if you want a tighter version before send."
            )
        elif topic_kind == "renewal_due":
            days_remaining = (trigger.get("payload") or {}).get("days_remaining") or merchant.get("subscription", {}).get("days_remaining")
            plan = (trigger.get("payload") or {}).get("plan") or merchant.get("subscription", {}).get("plan") or "plan"
            body = clamp_text(
                f"Done - the cleanest move is to renew the {plan} plan while {days_remaining} days remain. I’d keep the active offer live and avoid a visibility gap. Want a 3-point checklist?"
            )
        elif topic_kind in {"perf_dip", "seasonal_perf_dip", "perf_spike"}:
            metric = (trigger.get("payload") or {}).get("metric") or "performance"
            delta_pct = (trigger.get("payload") or {}).get("delta_pct")
            delta_text = f"{abs(delta_pct) * 100:.0f}%" if isinstance(delta_pct, (int, float)) else "a meaningful amount"
            direction = "down" if isinstance(delta_pct, (int, float)) and delta_pct < 0 else "up"
            body = clamp_text(
                f"Done - {metric} is {direction} {delta_text}, so I’d act on the current offer rather than wait. If you want, I can draft the exact post or reply text next."
            )
        elif topic_kind == "active_planning_intent":
            topic = (trigger.get("payload") or {}).get("intent_topic") or "the plan"
            body = clamp_text(
                f"Yes - I’ll structure {topic} into a simple first draft with pricing, timing, and a short CTA. That keeps it easy to review and send."
            )
        else:
            body = clamp_text(
                f"Done - I can move this forward. {offer_title + ' is the obvious lever.' if offer_title else 'I have the next step lined up.'}"
            )
        new_state = state or ConversationState(conversation_id, merchant_id, customer_id, trigger.get("id"), "vera", topic_kind or None, created_at=iso_now(), updated_at=iso_now())
        new_state.last_bot_body = body
        new_state.last_bot_cta = "open_ended"
        new_state.last_sender_role = "vera"
        new_state.updated_at = iso_now()
        store.upsert_conversation(new_state)
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Merchant expressed commitment, so the next move is to advance the action with a concrete deliverable."}

    if topic_kind in {"research_digest", "cde_opportunity"}:
        item = category_digest_item(category, kind="research", item_id=(trigger.get("payload") or {}).get("top_item_id")) or {}
        title = item.get("title") or "the item"
        source = item.get("source") or "the source"
        body = clamp_text(
            f"{salutation}, I can send the abstract summary and turn it into a practical note for your patients. The anchor is {title} ({source}). Want me to draft the patient-facing version too?"
        )
        cta = "open_ended"
    elif topic_kind == "renewal_due":
        days_remaining = (trigger.get("payload") or {}).get("days_remaining") or merchant.get("subscription", {}).get("days_remaining")
        body = clamp_text(
            f"{salutation}, renewal is due in {days_remaining} days. If you want to keep visibility steady, the safest move is to renew before the account pauses. Reply YES and I’ll outline the next step."
        )
        cta = "YES"
    elif topic_kind == "perf_dip":
        metric = (trigger.get("payload") or {}).get("metric") or "performance"
        delta_pct = (trigger.get("payload") or {}).get("delta_pct")
        delta_text = f"{abs(delta_pct) * 100:.0f}%" if isinstance(delta_pct, (int, float)) else "materially"
        body = clamp_text(
            f"{salutation}, {metric} is down {delta_text}. I would not wait on this - use the active offer and check the main surface that is leaking. Want me to spell out the most likely cause?"
        )
        cta = "YES"
    elif topic_kind in {"seasonal_perf_dip", "perf_spike"}:
        body = clamp_text(
            f"{salutation}, I’ve seen the signal. I can either help you ride the spike or avoid spending into a normal seasonal dip. Want the short version?"
        )
        cta = "open_ended"
    elif topic_kind in {"recall_due", "trial_followup", "appointment_tomorrow", "chronic_refill_due", "wedding_package_followup"} and customer:
        body = clamp_text(
            f"{salutation}, {merchant_title} here. I can send the next slot options now - short, clear, and with your preference in mind. Reply YES and I’ll do it."
        )
        cta = "YES"
    elif topic_kind in {"active_planning_intent", "curious_ask_due", "review_theme_emerged", "competitor_opened"}:
        body = clamp_text(
            f"{salutation}, I can turn this into a useful draft straight away. If you want, I’ll keep it specific to {merchant_title} and your current offer."
        )
        cta = "open_ended"
    else:
        body = clamp_text(
            f"{salutation}, I can help with the next step for {merchant_title}. If you want the direct action version, say YES and I’ll keep it tight."
        )
        cta = "YES"

    new_state = state or ConversationState(conversation_id, merchant_id, customer_id, trigger.get("id"), "vera", topic_kind or None, created_at=iso_now(), updated_at=iso_now())
    new_state.last_bot_body = body
    new_state.last_bot_cta = cta
    new_state.last_sender_role = "vera"
    new_state.updated_at = iso_now()
    store.upsert_conversation(new_state)
    return {"action": "send", "body": body, "cta": cta, "rationale": "The merchant's reply still leaves room for forward motion, so keep the conversation action-oriented."}


def choose_customer_context(trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    customer_id = trigger.get("customer_id")
    if customer_id:
        return STORE.get_customer(customer_id)
    merchant_id = trigger.get("merchant_id")
    if not merchant_id:
        return None
    # Prefer a matching customer that belongs to the merchant if the trigger asks for a customer-scoped send.
    customers = STORE.all_of("customer")
    for customer in customers.values():
        if customer.payload.get("merchant_id") == merchant_id:
            return customer.payload
    return None


def actions_for_tick(now: str, available_triggers: List[str]) -> List[Dict[str, Any]]:
    triggers: List[Dict[str, Any]] = []
    for trigger_id in available_triggers:
        trigger = STORE.get_trigger(trigger_id)
        if trigger:
            triggers.append(trigger)

    triggers.sort(key=lambda trig: (-int(trig.get("urgency", 0) or 0), trig.get("expires_at", ""), trig.get("id", "")))
    actions: List[Dict[str, Any]] = []
    seen_merchant_ids: set[str] = set()

    for trigger in triggers:
        suppression_key = trigger.get("suppression_key")
        if STORE.is_suppressed(suppression_key):
            continue

        merchant_id = trigger.get("merchant_id")
        if not merchant_id:
            continue

        merchant = STORE.get_merchant(merchant_id)
        if not merchant:
            continue

        category = STORE.get_category(category_slug_for_merchant(merchant)) or {}
        customer = choose_customer_context(trigger) if trigger.get("scope") == "customer" else None

        # Keep one proactive merchant-scoped action per merchant per tick.
        if trigger.get("scope") == "merchant" and merchant_id in seen_merchant_ids:
            continue

        action = compose_for_trigger(trigger, category, merchant, customer)
        if suppression_key:
            STORE.suppress(suppression_key)

        conversation_id = action.get("conversation_id")
        conversation = ConversationState(
            conversation_id=conversation_id,
            merchant_id=merchant_id,
            customer_id=action.get("customer_id"),
            trigger_id=trigger.get("id"),
            send_as=action.get("send_as", "vera"),
            topic_kind=trigger.get("kind", ""),
            last_bot_body=action.get("body", ""),
            last_bot_cta=action.get("cta", ""),
            last_sender_role="vera",
            created_at=iso_now(),
            updated_at=iso_now(),
        )
        STORE.upsert_conversation(conversation)

        actions.append(action)
        if trigger.get("scope") == "merchant":
            seen_merchant_ids.add(merchant_id)

        if len(actions) >= 5:
            break

    return actions


class BotHandler(BaseHTTPRequestHandler):
    server_version = "VeraChallengeBot/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        if self.path in {"/", "/v1"}:
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "Vera challenge bot",
                    "routes": ["/v1/healthz", "/v1/metadata", "/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown"],
                },
            )
            return

        if self.path in {"/v1/context", "/v1/tick", "/v1/reply", "/v1/teardown"}:
            self._send_json(
                405,
                {
                    "error": "method_not_allowed",
                    "path": self.path,
                    "allowed_methods": ["POST"],
                    "note": "This endpoint accepts POST requests only.",
                },
            )
            return

        if self.path == "/v1/healthz":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "uptime_seconds": STORE.uptime_seconds(),
                    "contexts_loaded": STORE.counts(),
                },
            )
            return

        if self.path == "/v1/metadata":
            self._send_json(
                200,
                {
                    "team_name": TEAM_NAME,
                    "team_members": TEAM_MEMBERS,
                    "model": MODEL_NAME,
                    "approach": "rule-based trigger composer with stateful context and template dispatch by trigger kind",
                    "contact_email": CONTACT_EMAIL,
                    "version": VERSION,
                    "submitted_at": os.getenv("SUBMITTED_AT", iso_now()),
                },
            )
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
        except Exception as exc:
            self._send_json(400, {"accepted": False, "reason": "invalid_json", "details": str(exc)})
            return

        if self.path == "/v1/context":
            self.handle_context(payload)
            return
        if self.path == "/v1/tick":
            self.handle_tick(payload)
            return
        if self.path == "/v1/reply":
            self.handle_reply(payload)
            return
        if self.path == "/v1/teardown":
            self.handle_teardown()
            return

        self._send_json(404, {"error": "not_found"})

    def handle_context(self, payload: Dict[str, Any]) -> None:
        scope = payload.get("scope")
        context_id = payload.get("context_id")
        version = payload.get("version")
        context_payload = payload.get("payload")
        delivered_at = payload.get("delivered_at")

        if scope not in {"category", "merchant", "customer", "trigger"}:
            self._send_json(400, {"accepted": False, "reason": "invalid_scope", "details": str(scope)})
            return
        if not isinstance(context_id, str) or not context_id:
            self._send_json(400, {"accepted": False, "reason": "invalid_context_id"})
            return
        if not isinstance(version, int) or version < 1:
            self._send_json(400, {"accepted": False, "reason": "invalid_version"})
            return
        if not isinstance(context_payload, dict):
            self._send_json(400, {"accepted": False, "reason": "invalid_payload"})
            return

        accepted, response = STORE.put_context(scope, context_id, version, context_payload, delivered_at)
        self._send_json(200 if accepted else 409, response)

    def handle_tick(self, payload: Dict[str, Any]) -> None:
        available_triggers = payload.get("available_triggers") or []
        if not isinstance(available_triggers, list):
            self._send_json(400, {"error": "invalid_available_triggers"})
            return
        trigger_ids = [trigger_id for trigger_id in available_triggers if isinstance(trigger_id, str)]
        actions = actions_for_tick(payload.get("now", iso_now()), trigger_ids)
        self._send_json(200, {"actions": actions})

    def handle_reply(self, payload: Dict[str, Any]) -> None:
        required = ["conversation_id", "merchant_id", "from_role", "message", "received_at", "turn_number"]
        missing = [field for field in required if field not in payload]
        if missing:
            self._send_json(400, {"error": "missing_fields", "fields": missing})
            return

        result = reply_to_message(
            STORE,
            conversation_id=str(payload.get("conversation_id")),
            merchant_id=str(payload.get("merchant_id")),
            customer_id=payload.get("customer_id"),
            from_role=str(payload.get("from_role")),
            message=str(payload.get("message")),
            turn_number=int(payload.get("turn_number") or 0),
        )
        self._send_json(200, result)

    def handle_teardown(self) -> None:
        STORE.clear()
        self._send_json(200, {"accepted": True, "wiped": True})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BotHandler)
    print(f"Vera challenge bot listening on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()