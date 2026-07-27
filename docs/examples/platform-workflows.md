# Platform Workflow Examples

## Turning a source into an idea

1. `collect_item(source_type="web", ...)`
2. `create_task(inbox_item_id=..., assigned_agents=["research-agent"])`
3. The assigned agent calls `submit_analysis`.
4. An admin reviews it with `get_task_context`.
5. A draft idea artifact is created as a new series.
6. If it needs to go out anywhere, a separate approval is requested for that.

## Reviewing a piece of drafted content

1. Save the idea and its supporting evidence as a draft artifact.
2. Create the final draft as a new artifact revision under the same series.
3. Call `request_approval` on the final version.
4. An admin calls `decide_approval(..., "approved")`.
5. Until an `actions` API exists, this only records the approval — nothing is published automatically.

## Analysis with a follow-up recommendation

1. Collect source material (articles, data, etc.) into the inbox.
2. Create an analysis task and assign it to an agent.
3. Save the analysis, risks, and recommendation as an artifact.
4. An admin requests changes or approves it.
5. For now, this ends at recording the approved recommendation.
6. A future executor would only act on an approved recommendation after verifying the exact artifact ID and hash.
