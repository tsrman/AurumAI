# Sample source skills

These source bundles demonstrate the supported SKILL.md/code-sandbox format.
They are development examples, not preinstalled tenant grants or production tools.
The greet, multistep-math and pdf-report directories contain their source inputs;
ZIP copies must remain reproducible and receive the same license/secret review.

Tenant code runs in an ephemeral bounded sandbox. Grant skills explicitly and
keep MCP/S3/session credentials outside uploaded source. Reference MCP fixtures
are enabled only through their opt-in profile. Acceptance removes its owned skills;
`seed-skills.sh` is a deliberate development convenience after prerequisite profiles
are ready, not automatic production provisioning.

See [README](../README.md), [architecture](../docs/ARCHITECTURE.md) and
[testing](../docs/TESTING.md).
