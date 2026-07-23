# LuxrayKit source-document audit

This is a migration snapshot, not a claim that the following work has already
been dogfooded in Ariadne.

| Old statement | Current evidence / replacement |
| --- | --- |
| v1 must stay in `LuxrayKit/tools/dev-pipeline-harness` and must not have a separate repository. | It was the original v1 boundary. The owner has now explicitly chosen the standalone `ffkiyo7/Ariadne` repository. LuxrayKit becomes a versioned project profile and migration target. |
| The workflow supports `!model`, `!effort`, and `!provider` switches during a Thread. | The deployed Discord code intentionally rejects these commands after configuration. The current product rule is one model/effort selection at Thread creation, matching the owner's decision. |
| DPH-05 was complete as a live workflow. | The old gates and `HermesExecutor` existed, but production scheduling only started provider turns; Hermes had no live caller and its direct `subprocess.run` path bypassed transient-unit recovery. Ariadne adds an explicit durable Hermes turn type. |
| VPS deployment revision/test-count facts such as r4/r7 and 42 tests are current. | They are historical snapshots. The observed active release was r9, externally installed rather than imported from the LuxrayKit checkout; the source suite at extraction time had 44 tests. Ariadne maintains its own current validation evidence. |
| The old runbook's service checkout is `/home/ubuntu/LuxrayKit`. | That path is obsolete for the program source after cutover. It remains the LuxrayKit target checkout only; Ariadne's source checkout is separate. |

The original documents remain valuable historical design context in LuxrayKit,
but Ariadne's architecture, profile contract, and cutover procedure are the
canonical forward-looking documents for this migration.
