# Synapse Commander application

Read README.org, DEVELOPMENT.org and the assigned external-tracker bead.
This is the application repository, separate from event coordination.

- Use the canonical external tracker supplied by the launch/context binding.
  Never create a second `.embead/` or commit private coordination records here.
- Work in your own application worktree. Rig worktrees do not isolate this
  repository. Preserve other contributors' edits and existing evidence.
- Write tests for new behavior, verify the relevant suite, review the diff,
  and record the exact app commit before requesting independent acceptance.
  Authors do not approve their own work or bypass review gates.
- Keep DEVELOPMENT.org current for cross-cutting architecture and verified
  run/check entrypoints; keep task state and receipts in the tracker.
- Do not expose arbitrary scripts, hidden simulation state or unbounded
  execution to model-controlled players. Game rules apply equally to humans
  and agents. Separate real networking from simulated communications.
- Keep private transcripts, local machine paths, credentials and event
  logistics out of application history. Local commits do not authorize
  remote creation, publication or release. Preserve upstream licenses and
  provenance; do not assume all art assets share the engine's license.
