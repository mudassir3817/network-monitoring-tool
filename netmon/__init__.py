"""netmon - a small network monitoring tool.

Four jobs, kept in four modules so each can be read on its own:

  ping.py    reachability and round-trip time, from the OS ping command
  snmp.py    an SNMPv2c GET, for interface up/down status
  store.py   SQLite: every check is written down, uptime is read back out
  alerts.py  when a run of failures becomes an alert, and when it clears

monitor.py joins them into one scheduled pass over the device list.

Nothing here needs root, and nothing here needs a library that is not in the
standard library, so it runs on a laptop as well as on a jump host.
"""

__version__ = "1.0.0"
