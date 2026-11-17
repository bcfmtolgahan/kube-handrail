# Security policy

kube-handrail gives AI agents write access to Kubernetes behind guards. A way around a guard is a vulnerability, even when it needs a misbehaving agent.

## Supported versions

The latest `0.y` release. Fixes ship as a new patch or minor release; there are no backports before 1.0.0.

## Reporting a vulnerability

Report it privately through GitHub: the repository's **Security** tab, **Report a vulnerability**. Do not open a public issue.

Include the version or commit, the approval mode, the steps or the tool calls, and what you expected the guard to do. You will get an answer within seven days.

## In scope

- Changing a workload without a valid, unexpired, unused plan, or with arguments other than the planned ones.
- Applying without the approval the approval mode requires, or approving as someone who is not an approver.
- Acting as another agent, or past a kill switch, budget, freeze, namespace policy or bound.
- Escaping the `<untrusted>` wrapper in tool output, or getting secrets into it.
- Changing or deleting the audit record, or an apply missing from it.
- IAM, RBAC or network permissions broader than the deployment documents.

Misconfiguration of a deployment (for example a wide `approver_principals`) is out of scope unless the docs recommend it.
