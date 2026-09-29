"""De-identify recorded data, export fixtures and scan for leaked identifiers.

The originals are every secret read from the environment, the API key
prefix the server embeds in clientname, the identifiers the configured
JSON pointers find in recorded accounts and ca_status responses, and every
"ca" signature and scrub.account_id_keys value in any recording. build_map
gives each original a placeholder, <CATEGORY>_<n>, numbered per category in
order of first appearance and kept stable across runs by the map file under
results_root, which is the only place originals are written.

Recorded files hold a value not only verbatim but also JSON-escaped at any
depth, percent-encoded in a URL, stripped of surrounding blanks, and
inside base64 fields. scrub_text and scan match all of those forms,
case-insensitively, so an upper-case \\uXXXX escape counts too, and scan
also matches after NFKC normalization. An original shorter than
MIN_SUBSTRING_MATCH_LEN matches only as a whole token.
"""

from p1rec.scrub._common import ScrubError, category_of
from p1rec.scrub._discovery import discover, pointer_category
from p1rec.scrub._export import FixtureEntry, LeakError, export_fixtures, load_selection
from p1rec.scrub._map import build_map, categories, load_map, save_map
from p1rec.scrub._matching import MIN_SUBSTRING_MATCH_LEN, UNDECODABLE_B64, Leak, scan, scrub_text
from p1rec.scrub._run import run_scrub

__all__ = [
    "MIN_SUBSTRING_MATCH_LEN",
    "UNDECODABLE_B64",
    "FixtureEntry",
    "Leak",
    "LeakError",
    "ScrubError",
    "build_map",
    "categories",
    "category_of",
    "discover",
    "export_fixtures",
    "load_map",
    "load_selection",
    "pointer_category",
    "run_scrub",
    "save_map",
    "scan",
    "scrub_text",
]
