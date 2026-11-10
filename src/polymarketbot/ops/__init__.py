"""Operational services: collector supervision, monitoring and clock probing.

Each module is a ``python -m`` entry point and owns its own long-lived loop:

* ``polymarketbot.ops.watchdog`` restarts a dead collector (Task Scheduler, 2 h);
* ``polymarketbot.ops.monitor`` samples storage and stream health every 5 min;
* ``polymarketbot.ops.clock_probe`` records venue clock offsets every 15 min.
"""
