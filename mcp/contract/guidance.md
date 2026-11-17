# Agent guidance assets

Appendix to [the contract](../CONTRACT.md).

`scripts/devops_agent_register.py install-guidance` installs them as DevOps Agent assets in an agent space, after validating every file locally:

- the `agents_md` asset for `--agent-type` (default `GENERIC`, every agent), with metadata `{"agent_type": ...}` and the single file `AGENTS.md` (at most 25 000 bytes). An agent space holds one per agent type; an existing one is updated only if its `AGENTS.md` starts with the marker line, or with `--replace-instructions`, so standing instructions written by an operator are never overwritten silently. `GENERIC` instructions are loaded by every agent type in addition to that type's own, so install for one agent type or the other, not both;
- one `skill` asset per skill, found by `metadata.name`, with metadata `name`, `description` and `agent_types: ["GENERIC"]` (skills load only when relevant) and the single file `SKILL.md`. An existing skill of the same name is updated only if its `SKILL.md` has the marker line after the frontmatter, or with `--replace-instructions`. An update merges metadata, so a skill's `status` set by an operator is kept; `agent_types` is set back to `GENERIC`.

Existing assets are found with `ListAssets` and checked before the first write; they are updated with `UpdateAsset`, and more than one match is an error. `status` lists the `agents_md` assets and the skills named in `agent/skills/` (`guidance.error` when the caller may not list them); `remove-guidance` deletes the `agents_md` assets and those skills whose files carry the marker. Agents load the instructions when a session starts, so a session that was already open keeps the old text.

The operator's credentials need `aidevops:ListAssets`, `aidevops:GetAssetFile`, `aidevops:CreateAsset` and `aidevops:UpdateAsset` on the agent space, and `aidevops:DeleteAsset` for `remove-guidance`; without `aidevops:ListAssets`, `status` shows the error in its `guidance` block and the rest still works.
