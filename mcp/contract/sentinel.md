# Drift sentinel

Appendix to [the contract](../CONTRACT.md).

A write tool that loses its `MUTATIVE` classification runs without approval in DevOps Agent (an unclassified tool counts as `READ_ONLY`). The sentinel is a scheduled Lambda (`mcp/sentinel/`, every 5 minutes by default) that turns remediation off when that happens, or when the gateway no longer identifies agents as deployed. It is not a gateway target and has no MCP tools.

Each run checks, against `mcp/classification.json` and the deployed configuration:

1. **Gateway.** `ListGatewayTargets`, then `GetGatewayTarget` for each expected target: the tool names in the target's inline tool schema must be exactly the classified tools of that target. A missing or extra tool, a missing target, or a target that is not in the classification is drift.
2. **Gateway configuration.** `GetGateway`. The gateway must have an interceptor configuration with the `REQUEST` interception point, that Lambda ARN and `inputConfiguration.passRequestHeaders = true`; otherwise `missing_interceptor`. `authorizerConfiguration.customJWTAuthorizer.allowedClients` may list only the agents' clients (`EXPECT_ALLOWED_CLIENTS`): each other client is `unexpected_client`, and a gateway that lists no clients at all is `unexpected_client` with the subject `any client`. A client missing from the list only locks that agent out and is not drift. Without the interceptor, gateway writes are refused anyway ([Agent identity](identity.md#agent-identity)); this check makes the failure loud and turns remediation off. Findings name the interceptor and clients without ARNs.
3. **DevOps Agent** (only when an agent space and association are configured). `GetAssociation`: the associated `mcpserver` tools must be exactly the classified tools, and every `MUTATIVE` tool must be associated as `MUTATIVE`; a tool without a `toolDetails` entry counts as `READ_ONLY`. A `READ_ONLY` tool associated as `MUTATIVE` only adds an approval and is not drift. `GetAgentSpace`: `elevatedActionsEnabled` (absent counts as `false`) must equal the configured expectation. Configure the IDs only after `enable-write`: before it, the write tool is not associated, which is reported as drift.

The gateway, gateway configuration, association and agent-space checks run independently. Findings from one are acted on even if another fails, because the only action is turning remediation off; a run with errors and no findings is `unknown`.

Outcomes, returned and logged as one JSON line `{ "status": "ok" | "drift" | "unknown", "findings": [ { "check", "issue", "subject", ... } ], "errors": [ "<Operation>: <ErrorCode>" ] }` that is also a CloudWatch embedded metric record (namespace `<name_prefix>/sentinel`, metrics `Drift` and `Unknown`, 0 or 1 per run):

| Status | When | Action |
|---|---|---|
| `ok` | No finding. | None. |
| `drift` | At least one finding (issues `missing_tool`, `extra_tool`, `missing_target`, `extra_target`, `missing_interceptor`, `unexpected_client`, `not_mutative`, `elevated_actions`). | Set the kill switch parameter to `false` (skipped if it already holds any value other than `true`, which is already off; this keeps the marker `make upgrade` writes), then publish an alert to the SNS topic listing the findings by tool name only. If either step fails, the run still does the other, then fails, so it is retried and the error alarm fires. |
| `unknown` | Any API error (including throttling and missing permissions), a response that cannot be read, a gateway target that is not `READY`, or invalid configuration. | Log the error code and operation (not the message), emit `Unknown = 1`. The kill switch is never touched and no drift alert is sent. A CloudWatch alarm notifies the same topic after 3 consecutive 5-minute periods with an unknown result or with no run at all (missing data counts as breaching). |

The sentinel only ever writes `false`, and only to the global kill switch: it has no access to the per-agent switches, so a sentinel fault cannot silently lock out one agent. Restoring remediation is manual:

1. Find the cause in the alert and fix it, for example a registration update that dropped `toolDetails`.
2. Re-run `scripts/devops_agent_register.py enable-write`, which reclassifies from `mcp/classification.json` and exits non-zero unless the write tools are `MUTATIVE` and elevated actions are on. For gateway drift, including a removed interceptor or an added client, re-apply Terraform.
3. Wait for a sentinel run to report `ok` (or invoke the function), then set the kill switch parameter back to `true`: `aws ssm put-parameter --overwrite --name <kill_switch_parameter> --value true`.

Configuration (environment variables, set by Terraform): `GATEWAY_ID`, `REMEDIATION_ENABLED_PARAM` (the write Lambda's kill switch), `ALERT_TOPIC_ARN`, `METRIC_NAMESPACE`, `TOOL_CLASSIFICATION` (the content of `mcp/classification.json`), `AGENT_SPACE_ID` and `ASSOCIATION_ID` (both or neither; empty skips the DevOps Agent checks), `EXPECT_ELEVATED_ACTIONS` (`true` or `false`, default `true`), `EXPECT_INTERCEPTOR_ARN` (the interceptor's Lambda ARN, required), `EXPECT_ALLOWED_CLIENTS` (JSON list of the agents' client IDs, required). Terraform variables: `sentinel_enabled`, `sentinel_schedule`, `devops_agent_space_id`, `devops_agent_association_id`, `expect_elevated_actions`, `alert_email`.

Known limits:

- A Terraform apply that changes the tool set updates the gateway targets before the sentinel's own configuration; a run in between reports drift and turns remediation off.
- Detection takes up to one schedule interval; until then a misclassified write tool runs without approval, within the server-side limits.
- The sentinel sees what the control-plane APIs report, not what a running DevOps Agent chat has cached (a chat keeps the tool list it was created with).
- It watches one association in one agent space. Another association of the same gateway, in this or another agent space, is not checked; in `server` or `both` mode ([Server-side approval](../CONTRACT.md#server-side-approval)) a misclassified write tool still cannot apply without a server approval.
- A deploy that adds a tool (such as `request_approval`) is drift for the association check until `enable-write` has added it to the association: with the DevOps Agent IDs configured, run `enable-write` right after `terraform apply`, wait for an `ok` run, then set the kill switch back to `true`.
- It compares tool names, not the Lambda function behind a target. Changing a target's function needs gateway write access, which is outside what classification drift covers.
