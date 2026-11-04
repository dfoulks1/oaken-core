# Step 7: the conformance kit

Goal: prove core is generic. Run products that are not mail through it.

Build:
1. A stub plugin for every kind. A conformance suite per kind.
2. Two fake products in the test tree.
   - A feed reader. A provider pulls items. A triager drops junk. An enricher summarizes. An action posts.
   - An efax. Binary payloads. An OCR stage on its own queue. A send that waits for a callback.
   No mail word. No person word. In core or in either fake.
3. A parity test: every operation reaches the doors its mode says.
4. A snapshot of the JSON Schema for every operation and contract. A change fails until updated on purpose.
5. A property test per invariant in `PLAN.md`.
6. The standalone test: the suite passes with no oaken plugin and no model backend.
7. Port `tests_to_port/test_guard.py`. Empty `tests_to_port/`.

Done when:
- Both fakes run end to end with no edit to core.
- A needed core edit is a finding. Report it. Do not paper over it.

Open from the efax run, decide with the owner: blob payload references, a parked job that waits on a
callback, push intake with its own signature check.
