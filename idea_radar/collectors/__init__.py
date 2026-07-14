from .kickstarter import KickstarterCollector
from .producthunt import ProductHuntCollector
from .reddit import RedditCollector
from .x import XCollector, XSocialCollector
from .instagram import InstagramCollector, InstagramSocialCollector
from .threads import ThreadsCollector

COLLECTORS = {
    "kickstarter": KickstarterCollector,
    "producthunt": ProductHuntCollector,
    "reddit": RedditCollector,
    "x": XCollector,
    "x_social": XSocialCollector,
    "instagram": InstagramCollector,
    "instagram_social": InstagramSocialCollector,
    "threads": ThreadsCollector,
    "threads_social": ThreadsCollector,
}

__all__ = ["COLLECTORS"]
