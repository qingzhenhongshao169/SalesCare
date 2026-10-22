"""Durable tenant-scoped model outage protection; no extra health-check calls."""
from sqlalchemy import select
from .models import Usage

CUSTOMER_WAIT_SECONDS = 8
FAILURE_THRESHOLD = 2
COOLDOWN_SECONDS = 30
FAILURES = ('fallback_timeout', 'fallback_connection')


def model_health(session, tid, fingerprint, now):
    # Only completed calls to this tenant's exact model settings affect its circuit.
    history = list(session.scalars(select(Usage).where(
        Usage.tenant_id == tid, Usage.kind == 'llm',
        Usage.detail['model_fingerprint'].as_string() == fingerprint,
        Usage.created_at >= now-600,
        Usage.outcome.not_in(('reserved', 'retrieving', 'suppressed_job', 'suppressed_human',
                              'suppressed_channel', 'suppressed_timeout')),
    ).order_by(Usage.created_at.desc(), Usage.id).limit(FAILURE_THRESHOLD)))
    failures = len(history) == FAILURE_THRESHOLD and all(u.outcome in FAILURES for u in history)
    if not failures:
        return {'state': 'available', 'allow_call': True}
    latest = history[0]
    until = latest.created_at + latest.latency_ms/1000 + COOLDOWN_SECONDS
    if now < until:
        return {'state': 'cooldown', 'allow_call': False, 'retry_after_seconds': max(1, round(until-now))}
    # Caller holds the existing tenant ai-usage serialization lock. Reserve one
    # normal customer call as a probe; concurrent requests use the safe fallback.
    active = session.scalar(select(Usage.id).where(
        Usage.tenant_id == tid, Usage.kind == 'llm', Usage.outcome == 'reserved',
        Usage.detail['model_fingerprint'].as_string() == fingerprint,
        Usage.created_at >= now-120,
    ).limit(1))
    return {'state': 'probe_in_progress' if active else 'probe', 'allow_call': not bool(active)}
