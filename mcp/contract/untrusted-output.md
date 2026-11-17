# Untrusted output

Appendix to [the contract](../CONTRACT.md).

Text that anyone with write access to the cluster can set, such as condition and event reasons and messages, container images and ReplicaSet change-causes, is untrusted: it can carry instructions aimed at the agent reading it, secrets, or characters that hide what it says. Every such field is passed through `handrail_common.untrusted.untrusted_text` before it is returned, in this order:

1. **Control characters.** Characters of Unicode category `Cc` (C0 and C1 controls), except `\n` and `\t`, are removed, and so are all of category `Cf` (format characters: bidi embeddings, overrides and isolates U+202A-U+202E and U+2066-U+2069, zero-width characters U+200B-U+200F, U+2060, U+FEFF, soft hyphen, tag characters) and `Cs` (lone surrogates). They go first, so a zero-width character inside a secret cannot hide it from step 2.
2. **Secrets.** Each match is replaced by `[redacted:<kind>]`:

   | Kind | Matches |
   |---|---|
   | `private_key` | a PEM block from `-----BEGIN ... PRIVATE KEY-----` to its `END` line, or to the end of the text when the `END` line is missing |
   | `jwt` | `eyJ<base64url>.eyJ<base64url>.<base64url>` |
   | `bearer` | the token after `Bearer ` (case-insensitive), at least 8 characters; the word `Bearer` is kept |
   | `aws_access_key_id` | `AKIA` or `ASIA` followed by 16 upper-case letters or digits |
   | `aws_secret_access_key` | 40 base64 characters after a key name such as `aws_secret_access_key`, `secret_access_key` or `SecretAccessKey` and `=` or `:`; the key name is kept |
   | `github_token` | `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` followed by at least 36 letters or digits, and `github_pat_` followed by at least 22 |
   | `slack_token` | `xoxa-`, `xoxb-`, `xoxp-` or `xoxr-` followed by at least 10 letters, digits or dashes |
   | `credential` | the value in `<key>=<value>`, `<key>:<value>` (no space after the colon) or `"<key>": <value>`, where the key name ends in `password`, `passwd`, `secret`, `token` or `api_key` / `apikey` / `api-key` (case-insensitive); the key name is kept. The value is a quoted string or runs to the next space, quote, `,`, `;` or `&` |

   A `credential` value that is already a complete `[redacted:<kind>]` or `[truncated N chars]` marker is left as it is. Redaction is pattern-based and cannot find every secret; it is a backstop, not a reason to let secrets into events or logs. Redaction comes before truncation, so a secret cut in half at the limit is still recognised.
3. **Delimiters.** Each `<` followed by optional whitespace (including newlines), an optional `/`, optional whitespace and the word `untrusted` (case-insensitive), such as `</untrusted>` or `< UNTRUSTED >`, is replaced by `&lt;`, so the text cannot close its own wrapper or open a new one.
4. **Field limit.** A field longer than 1024 characters keeps its first characters and ends with the marker `[truncated N chars]`, where `N` is the number of characters removed; the result, marker included, is at most 1024 characters. The cut never falls inside a `[redacted:<kind>]` marker; it moves to the start of the marker instead.
5. **Wrapper.** The result is returned as the string `<untrusted>` + text + `</untrusted>`. A field that is `null` stays `null`.

Steps 1-4 are idempotent: applying them to their own output changes nothing. Step 5 is not, and is not meant to be: text shaped like a wrapped value is neutralized and wrapped again, never passed through.

The `get_workload_events` response is limited to 32768 bytes of serialized `data` (`json.dumps`, which escapes non-ASCII characters, so the bytes on the wire are no more). Whole events are dropped from the end of the list, the oldest first, until it fits; the response then has `truncated: true` in `data` and in the envelope. A field cut at its limit also sets the envelope's `truncated`. `get_workload_status` needs no response limit: a Deployment has a handful of condition types, each with two fields of at most 1024 characters.

The delimiters tell a model which text did not come from kube-handrail; they are a hint, not a security boundary, and a model may still follow instructions inside them. The boundary is elsewhere: no `READ_ONLY` tool can change the cluster, and every change needs a plan whose exact arguments a human approves (in the client, in `server` mode on the server, or both). Text inside a tool response can at most make an agent propose a plan, which the approver sees with the server's own record of its arguments and impact.

## Workload events

`get_workload_events(namespace, name, limit?)` returns recent Kubernetes Events about a Deployment and the objects it owns. `namespace` and `name` are validated and the namespace checked against `ALLOWED_NAMESPACES` as for the other tools; `limit` is an integer from 1 to 50 (default 20; anything else is `invalid_argument`).

1. Read the Deployment (`not_found` if it does not exist).
2. List the ReplicaSets in the namespace (`apps/v1`) and keep those with an `ownerReferences` entry whose `uid` is the Deployment's `uid`.
3. List the Events in the namespace (`core/v1`). An Event is kept when its `involvedObject.namespace` is absent or the namespace, its `involvedObject.name` is a DNS-1123 subdomain, and it is about:
   - the Deployment: `kind` `Deployment` and the Deployment's name;
   - one of the ReplicaSets from step 2: `kind` `ReplicaSet` and its name;
   - a Pod of one of them: `kind` `Pod` and a name that is the ReplicaSet's name plus `-`, cut to 58 characters as the API server does for generated names, followed by exactly 5 lower-case letters or digits. Pods are matched by name only; the reader cannot read Pods.

   When `involvedObject.uid` is set for a Deployment or ReplicaSet, it must be that object's `uid`, so the events of a deleted object with the same name are left out.

   Only ReplicaSets that still exist are matched. Events about a ReplicaSet already removed by the Deployment's `revisionHistoryLimit`, and about its Pods, are left out although `kubectl get events` still shows them, so an empty list does not mean no failure.
4. Sort newest first by the first set of `lastTimestamp`, `series.lastObservedTime`, `eventTime`, `firstTimestamp` and `metadata.creationTimestamp`; events without any valid timestamp go last; ties are broken by event name. Keep the first `limit`, then apply the response limit from [Untrusted output](#untrusted-output).

Both lists are read as raw JSON (`_preload_content=False`), like the impact preview. A failed read of either is `kubernetes_error`, never an empty list.

```json
{
  "namespace": "sandbox",
  "name": "web",
  "events": [
    {
      "type": "<untrusted>Normal</untrusted>",
      "reason": "<untrusted>ScalingReplicaSet</untrusted>",
      "message": "<untrusted>Scaled up replica set web-5d4f8c7b9 to 2</untrusted>",
      "object": { "kind": "Deployment", "name": "web" },
      "count": 1,
      "first_timestamp": "2026-01-01T12:00:00Z",
      "last_timestamp": "2026-01-01T12:00:00Z"
    }
  ],
  "matched": 1,
  "truncated": false
}
```

`type`, `reason` and `message` are untrusted text. `object` is the matched object, so it holds only names kube-handrail derived itself. `count` is `count`, else `series.count`, else 1. `first_timestamp` is `firstTimestamp`, else `eventTime`; `last_timestamp` is the timestamp the event was sorted by; each is the API server's RFC 3339 string, or `null` when absent or not a valid timestamp. `matched` is the number of Events kept in step 3, before `limit` and the response limit.

`get_workload_status` passes each condition's `reason` and `message` through the same function; its `data` is otherwise unchanged.
