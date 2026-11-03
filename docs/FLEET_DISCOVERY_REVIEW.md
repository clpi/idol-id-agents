# Discovery and bootstrap review boundaries

The discovery command uses the controller's existing pinned Python dependency. Run it with the same interpreter used to install `requirements.txt`, preferably an isolated environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/discover-fleet-host.py --help
```

A missing PyYAML dependency produces `yaml-dependency-unavailable`, not a valid empty Hermes configuration. Malformed projected fields and oversized collections refuse; this validates the metadata projection, not the entire Hermes schema. Optional absent/null configuration fields do not establish a live route. Optional Grok, Poolside, Agy and Oh My Pi executables are presence-only observations until their version invocation contracts are verified; discovery never guesses arguments or starts those agents.

Repository observations disable repository fsmonitor hooks and ordinary hooks, and avoid optional Git writes. Two successive sets of HEAD/branch/tracked-status/origin probes must agree. Drift clears the combined identities and records an inconsistent observation. Agreement is not an atomic filesystem snapshot, an execution permit, or evidence against changes after observation. Unborn repositories may have a known origin with an unknown HEAD.

Detailed output and its public receipt remain separately replaced files. The receipt now binds the exact serialized detailed bytes through `observation_sha256`; consumers must verify this before pairing the files. A concurrent writer or interruption can leave a mismatch, which must be refused rather than interpreted as a valid pair. This is not transactional publication or remote attestation. For example, after reading both files into memory:

```python
import hashlib
import json
from pathlib import Path

body = Path("details.json").read_bytes()
receipt = json.loads(Path("receipt.json").read_bytes())
assert hashlib.sha256(body).hexdigest() == receipt["observation_sha256"]
details = json.loads(body)
assert details["observed_at"] == receipt["observed_at"]
```

Model name hashes remain for compatibility; the additional `inventory_hash` covers the canonicalized sanitized model records, including digest and size. Both are null when metadata is unavailable. Inventory is not inference, provider authentication, or allowance. The local Ollama `/api/tags` contract currently documents a bare hexadecimal digest; the script retains that contract rather than claiming unsupported formats were observed. Source: https://github.com/ollama/ollama/blob/main/docs/api.md#list-local-models .

Bootstrap uses only the operator's existing package sources. Its Node-major guard refuses versions below 18; package `--engine-strict` may require a newer version. On a distribution whose configured APT sources provide only an older Node, provision a supported Node/npm in PATH before running bootstrap. The script deliberately does not replace an existing Node installation or add a third-party package repository. macOS requires existing Homebrew when dependencies are missing. These are prerequisites, not claims that every Linux distribution or package version is supported.

Writes to the shared user agent prefix and credential-template setup are serialized by `.local/.idol-bootstrap-lock`. A second bootstrap refuses before those writes, and normal completion/error removes only a lock acquired by that process. An abrupt process death may leave the lock: the operator must establish that no installer remains before removing it. There is no automatic lock breaking, process termination, secret replacement, service activation, or fleet grant.

Executable review controls:

```sh
python3 -m unittest discover -s tests -p test_discovery_review.py -v
python3 -m unittest discover -s tests -p test_bootstrap_serial.py -v
python3 -m unittest discover -s tests -v
```

The Git hook control first proves a fixture hook actually executes under ordinary Git status, then requires discovery to leave it untouched. Bootstrap tests execute real isolated shell processes with inert package-manager fixtures; no packages, production hosts, credentials, or provider services are modified by these tests.
