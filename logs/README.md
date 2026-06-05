# logs/

This directory stores structured execution logs from the Cloud Functions and Agent Engine pipeline.

## Contents

| File pattern | Description |
|---|---|
| `onAgentPlanningQueue_YYYYMMDD.jsonl` | Cloud Function execution traces (session creation → stream completion) |
| `agent_tool_calls_YYYYMMDD.jsonl` | Tool invocation records: `get_user_profile`, `get_recent_runs`, `write_training_plan` |
| `stream_errors_YYYYMMDD.jsonl` | HTTP-200 stream error captures (Agent Engine silent-failure pattern) |

## Log format

Each line is a JSON object:

```json
{
  "timestamp": "2026-06-05T09:12:34Z",
  "uid": "<anonymised>",
  "runId": "run_abc123",
  "sessionId": "session_xyz",
  "status": "completed",
  "durationMs": 48231,
  "toolsCalled": ["get_user_profile", "get_recent_runs", "write_training_plan"]
}
```

## Exporting from Cloud Logging

```bash
gcloud logging read \
  'resource.type="cloud_function" AND logName=~"onAgentPlanningQueue"' \
  --project=routerun-app \
  --format=json \
  --freshness=7d \
  > logs/onAgentPlanningQueue_$(date +%Y%m%d).jsonl
```

> **Note:** Logs containing PII are excluded from this repository.  
> Only anonymised or synthetic records are committed.
