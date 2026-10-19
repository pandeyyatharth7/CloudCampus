"""Deterministic, clearly fictional sample data for CloudCampus.

Shared by the local test fixtures and by the production `POST /seed` endpoint so
there is exactly one definition of the sample data.

Repeatability contract
----------------------
* Every event has a **stable id** (``seed-<slug>``). Re-running the seed never
  creates a second copy of the same event.
* ``attendees`` / ``cancelled`` describe the synthetic registration rows written
  alongside each event. ``attendees`` is always ``<= capacity`` so seeding can
  never create an over-booked event.
* Synthetic attendees use the ``seed-attendee-`` prefix so they can be told
  apart from, and never collide with, real Cognito users.
"""
from datetime import datetime, timedelta, timezone

SEED_ID_PREFIX = 'seed-'
SEED_ATTENDEE_PREFIX = 'seed-attendee-'


def attendee_id(n):
    """Stable synthetic attendee id for position ``n`` (1-based)."""
    return f'{SEED_ATTENDEE_PREFIX}{n:02d}'


# deadline = 18:00 UTC on the day before the event
SEED_EVENTS = [
    {
        'id': 'seed-career-fair-2026',
        'title': 'Campus Career Fair 2026',
        'description': 'Sample data: 60 fictional employers across tech, finance and '
                       'consulting, plus resume reviews and mock interviews.',
        'category': 'Career',
        'date': '2026-11-15', 'time': '09:00',
        'location': 'University Auditorium',
        'capacity': 500, 'attendees': 3, 'cancelled': 1, 'status': 'published',
    },
    {
        'id': 'seed-startup-pitch-night',
        'title': 'Startup Pitch Night',
        'description': 'Sample data: student teams pitch fictional ventures to faculty '
                       'and local founders. Best pitch wins a co-working pass.',
        'category': 'Tech',
        'date': '2026-11-20', 'time': '18:00',
        'location': 'Innovation Lab, Block B',
        'capacity': 100, 'attendees': 12, 'cancelled': 2, 'status': 'published',
    },
    {
        'id': 'seed-ai-ml-workshop',
        'title': 'AI and ML Hands-on Workshop',
        'description': 'Sample data: a deliberately tiny session on machine learning '
                       'fundamentals using Python notebooks. Seats fill up fast.',
        'category': 'Education',
        'date': '2026-12-05', 'time': '14:00',
        'location': 'CS Building, Lab 3',
        'capacity': 3, 'attendees': 3, 'cancelled': 0, 'status': 'published',
    },
    {
        'id': 'seed-midnight-hackathon',
        'title': 'Midnight Hackathon (Fictional Edition)',
        'description': 'Sample data: 24-hour build sprint on a made-up theme, with '
                       'mentors on hand and free fictional pizza.',
        'category': 'Tech',
        'date': '2026-12-12', 'time': '22:00',
        'location': 'Computing Lab, Block C',
        'capacity': 60, 'attendees': 5, 'cancelled': 2, 'status': 'published',
    },
    {
        'id': 'seed-spring-cultural-festival',
        'title': 'Spring Cultural Festival',
        'description': 'Sample data: student societies stage music, dance and food '
                       'stalls around the central quad. Open to all year groups.',
        'category': 'Culture',
        'date': '2027-01-24', 'time': '16:00',
        'location': 'Central Quad',
        'capacity': 800, 'attendees': 0, 'cancelled': 0, 'status': 'published',
    },
    {
        'id': 'seed-research-proposal-workshop',
        'title': 'Research Proposal Workshop',
        'description': 'Sample data: draft a one-page research proposal and get feedback '
                       'from the fictional ethics committee.',
        'category': 'Academic',
        'date': '2026-11-28', 'time': '11:00',
        'location': 'Library Seminar Room 2',
        'capacity': 40, 'attendees': 0, 'cancelled': 0, 'status': 'draft',
    },
    {
        'id': 'seed-alumni-homecoming-gala',
        'title': 'Alumni Homecoming Gala',
        'description': 'Sample data: a cancelled event kept for the demo so cancelled '
                       'records and cancellation states are visible in the dashboard.',
        'category': 'Culture',
        'date': '2026-10-05', 'time': '19:00',
        'location': 'Grand Ballroom',
        'capacity': 300, 'attendees': 2, 'cancelled': 1, 'status': 'cancelled',
    },
]

for _spec in SEED_EVENTS:
    if _spec['attendees'] > _spec['capacity']:
        raise ValueError(f"seed event {_spec['id']} is over capacity")
    assert _spec['id'].startswith(SEED_ID_PREFIX), _spec['id']


def deadline_for(date):
    """Registration deadline for an event date (``YYYY-MM-DD``).

    Returned in a canonical, lexicographically sortable UTC form so the DynamoDB
    condition ``deadline > :now`` can be a plain string comparison.
    """
    dt = datetime.fromisoformat(f'{date}T18:00:00+00:00') - timedelta(days=1)
    return dt.replace(microsecond=0).isoformat()


def attendee_counts(spec):
    """(active_count, cancelled_count) for a seed event."""
    return spec.get('attendees', 0), spec.get('cancelled', 0)


def max_attendees(spec):
    active, cancelled = attendee_counts(spec)
    return max(active, cancelled)