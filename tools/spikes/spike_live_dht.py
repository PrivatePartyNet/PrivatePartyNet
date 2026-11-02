"""Spike: put a signed BEP 44 record on the real BitTorrent DHT and read it back from a second client.

Measures how many nodes take the record and how long the put and the get take, from this PC. Outbound UDP only, to the
public DHT (what any BitTorrent client does). Bounded: each lookup stops after 25 seconds; the script after about 90.
    py tools\\spikes\\spike_live_dht.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from privatepartynet.dht import Dht  # noqa: E402


def main():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    value = {"a": "203.0.113.5", "p": 40000, "t": int(time.time())}
    writer = Dht(deadline=25.0)
    started = time.monotonic()
    stored = writer.put_mutable(key, value, seq=1, salt=b"privatepartynet-spike")
    put_seconds = time.monotonic() - started
    writer.close()
    print(f"put: stored on {stored} nodes in {put_seconds:.1f} s")
    reader = Dht(deadline=25.0)
    started = time.monotonic()
    found = reader.get_mutable(public, b"privatepartynet-spike")
    get_seconds = time.monotonic() - started
    reader.close()
    print(f"get: {found} in {get_seconds:.1f} s")
    ok = stored > 0 and found is not None and found[1].get(b"p") == 40000
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
