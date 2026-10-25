"""DPI roulette module (part 2).

* ``service.DpiService`` - picks values, drives the overlay, applies DPI through a backend;
* ``backends``           - HID++ (real mouse), fake (development), none;
* ``routes.router``      - API + ``/overlay/dpi``;
* ``grid``               - sensor DPI grid and snapping.

See ``docs/DPI_PLAN.md`` for the research behind the HID++ path and the G HUB caveats.
"""
from .routes import router  # noqa: F401
from .service import DpiService, DpiSettings  # noqa: F401
