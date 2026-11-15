"""Real ping output, kept as fixtures so the parser is tested against the thing
it actually has to read.

Provenance, because it matters when one of these stops matching:

  WINDOWS_*   captured on Windows 11 by running ping and copying what it
              printed. The "time<1ms" in WINDOWS_OK is the reason: it is what
              Windows prints for any reply faster than a millisecond, and a
              regex written from memory as `time=(\\d+)ms` misses it entirely.
  LINUX_*     iputils ping, the ping on more or less every Linux distribution.
  MACOS_*     the BSD ping that ships with macOS. Note "3 packets received"
              where Linux says "3 received", and "round-trip" where Linux says
              "rtt" - two small differences that each break a parser written
              for only the other one.

The unresolvable-name cases are here because they are the one failure that
must not be reported as a device being down: no statistics block is printed at
all, on any platform.
"""

WINDOWS_OK = """
Pinging 127.0.0.1 with 32 bytes of data:
Reply from 127.0.0.1: bytes=32 time<1ms TTL=128
Reply from 127.0.0.1: bytes=32 time<1ms TTL=128
Reply from 127.0.0.1: bytes=32 time<1ms TTL=128

Ping statistics for 127.0.0.1:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 0ms, Average = 0ms
"""

WINDOWS_SLOW = """
Pinging 10.0.0.2 with 32 bytes of data:
Reply from 10.0.0.2: bytes=32 time=182ms TTL=118
Reply from 10.0.0.2: bytes=32 time=191ms TTL=118
Reply from 10.0.0.2: bytes=32 time=176ms TTL=118

Ping statistics for 10.0.0.2:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 176ms, Maximum = 191ms, Average = 183ms
"""

WINDOWS_PARTIAL = """
Pinging 10.0.0.2 with 32 bytes of data:
Reply from 10.0.0.2: bytes=32 time=21ms TTL=118
Request timed out.
Reply from 10.0.0.2: bytes=32 time=23ms TTL=118
Request timed out.

Ping statistics for 10.0.0.2:
    Packets: Sent = 4, Received = 2, Lost = 2 (50% loss),
Approximate round trip times in milli-seconds:
    Minimum = 21ms, Maximum = 23ms, Average = 22ms
"""

WINDOWS_DOWN = """
Pinging 192.0.2.1 with 32 bytes of data:
Request timed out.
Request timed out.

Ping statistics for 192.0.2.1:
    Packets: Sent = 2, Received = 0, Lost = 2 (100% loss),
"""

WINDOWS_BAD_NAME = (
    "Ping request could not find host no-such-host-xyz.invalid. "
    "Please check the name and try again.\n"
)

LINUX_OK = """
PING 192.168.1.1 (192.168.1.1) 56(84) bytes of data.
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=0.345 ms
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=0.298 ms
64 bytes from 192.168.1.1: icmp_seq=3 ttl=64 time=0.412 ms

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2034ms
rtt min/avg/max/mdev = 0.298/0.351/0.412/0.047 ms
"""

LINUX_PARTIAL = """
--- 10.0.0.2 ping statistics ---
4 packets transmitted, 2 received, 50% packet loss, time 3052ms
rtt min/avg/max/mdev = 18.112/19.004/19.896/0.892 ms
"""

LINUX_DOWN = """
--- 192.0.2.1 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2047ms
"""

# iputils adds an error count when it gets ICMP unreachables back rather than
# silence. The received count is still in the same place.
LINUX_ERRORS = """
--- 192.0.2.1 ping statistics ---
2 packets transmitted, 0 received, +2 errors, 100% packet loss, time 1001ms
"""

LINUX_BAD_NAME = "ping: no-such-host-xyz.invalid: Name or service not known\n"

MACOS_OK = """
PING 192.168.1.1 (192.168.1.1): 56 data bytes
64 bytes from 192.168.1.1: icmp_seq=0 ttl=64 time=1.234 ms
64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=1.678 ms
64 bytes from 192.168.1.1: icmp_seq=2 ttl=64 time=1.456 ms

--- 192.168.1.1 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 1.234/1.456/1.678/0.182 ms
"""

MACOS_DOWN = """
--- 192.0.2.1 ping statistics ---
3 packets transmitted, 0 packets received, 100.0% packet loss
"""

MACOS_BAD_NAME = "ping: cannot resolve no-such-host-xyz.invalid: Unknown host\n"
