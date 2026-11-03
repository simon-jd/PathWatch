This laboratory compatibility recipe builds independent v1.0 and v1.1 binaries
from an explicit frozen Task14b SOURCE commit. It copies only tracked Go/proto/SQL
inputs and the unchanged module locks. It does not copy the repository, caches,
history or private keys, install tools, or claim a preexisting released version.

The additive v1.1 variant adds optional Describe.build_note field8, advertises
minor1 and capability bit16, and sends minor1 metadata. Required capabilities
remain15 so new clients accept baseline cores. Existing fields/Report wire stay
unchanged. Both versions accept major1/minor0 or1; other versions fail closed.
The script regenerates bindings with the already verified pinned tools, retains
all variant inputs, emits the complete patch including generated bindings, and
records source/input/binary hashes and command output. Run only after the stage
resource forecast and focused race gate, under the stage allocation watchdog.

`python3 build.py FULL_SOURCE_SHA` creates `.cache/task14b/bin/{ingest,fixture}`
and `.cache/task14b/compat-bin/ingest`. The native fixture's `--compat-bin` option
then proves real new-frontend/old-core and old-frontend/new-core Reports and typed
queries. Core replacement remains serial with the same durable owner identity.
This is not simultaneous core canary, an HA proof, or a Kubernetes rollback test.

An optional second argument, e.g. `corrected`, creates distinct suffixed source,
binary and evidence paths so superseded builds remain preserved. No existing
variant output is overwritten.
