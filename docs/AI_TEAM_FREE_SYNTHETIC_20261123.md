# AI Team: free isolated synthetic documentation check

Synthetic documentation acceptance only. Product readiness, live integrations, model quality and Windows-off ingress are not accepted.

The fixed free author has no model provider. The separate frozen-byte reviewer validates this additive document outside product code. Baseline results below do not attest changed-code completion.

```json
{
  "base_sha": "f7ba14fbb5624263dde5a1b6711a61a9a4c7f802",
  "deploy": false,
  "document_path": "docs/AI_TEAM_FREE_SYNTHETIC_20261123.md",
  "job_id": "50457e9599dd",
  "merge": false,
  "model_calls": 0,
  "product_baseline": {
    "changed_code_completion": "NOT_ATTESTED",
    "credentials": false,
    "exit": 0,
    "log_sha256": "e8c2ebdecc6c68bd31c948de0b0c1dd4c57dd47d093194a6c1d241c65727824d",
    "network": false,
    "project_id": "PDF_SIGNER",
    "recipe": [
      "python",
      "-B",
      "-m",
      "pytest",
      "-q",
      "tests/test_store_connections.py"
    ],
    "reported_numeric_summaries": [
      "1 passed in 0.07s"
    ],
    "scope": "canonical Linux CI store-connection leak test only; full signer/private-PDF/Windows-font suite remains blocked",
    "source_files": 11,
    "source_head": "f7ba14fbb5624263dde5a1b6711a61a9a4c7f802",
    "source_manifest_sha256": "3ca6d0145ec51b695c601ba1b4a01b73bf084a5c52c730f139c72647df60c688",
    "status": "BASELINE_PROCESS_SUCCESS",
    "uid": 65534
  },
  "project_id": "PDF_SIGNER",
  "repository": "sergeymalinkin/pdf-signer",
  "scope": "synthetic-document-only",
  "stacks": [
    "python"
  ]
}
```
