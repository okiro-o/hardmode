"""Donations: DonationAlerts / DonatePay polling, rules «amount/keyword → action», viewer weapon queue.

* ``aliases``   - weapon names and slang in free text («крокодил» = XM1014), editable additions;
* ``queue``     - viewer weapon requests, separate from the arms race (data/viewer_queue.json);
* ``providers`` - HTTP clients for DonationAlerts (OAuth) and DonatePay (API key);
* ``service``   - settings, rules, polling thread, dispatch of donations to actions;
* ``routes``    - API, OAuth callback.
"""
