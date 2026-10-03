"""Repeated sampling presentation entry point; metrics remain in next_stage_views."""
from next_stage_views import page as stage_page

def page(analysis=False):
    return stage_page(analysis=analysis, section="repeated")
