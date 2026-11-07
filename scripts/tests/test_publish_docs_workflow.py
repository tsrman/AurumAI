"""Pins .github/workflows/publish-docs.yml: when the site is published and with what token."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / ".github/workflows/publish-docs.yml").read_text()
WORKFLOW = yaml.safe_load(TEXT)
JOB = WORKFLOW["jobs"]["docs"]
STEPS = {step.get("name"): step for step in JOB["steps"]}


def test_it_runs_for_release_tags_and_by_hand_and_reads_only_this_repository():
    triggers = WORKFLOW[True]  # YAML reads the key `on` as a boolean
    assert triggers["push"] == {"tags": ["v*"]}
    assert triggers["workflow_dispatch"]["inputs"]["publish"]["default"] is False
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_a_prerelease_tag_does_not_publish_and_a_manual_run_publishes_only_when_asked():
    publish = JOB["env"]["PUBLISH"]
    assert "github.event_name == 'push'" in publish
    assert "!contains(github.ref_name, '-')" in publish
    assert "inputs.publish == true" in publish


def test_the_site_is_built_with_the_script_and_the_publish_steps_need_the_token():
    assert "scripts/build-docs-site.py" in STEPS["Build the site"]["run"]
    for name in ("Check out the site repository", "Publish"):
        condition = STEPS[name]["if"]
        assert "env.PUBLISH == 'true'" in condition and "env.HAS_TOKEN == 'true'" in condition
    assert JOB["env"]["HAS_TOKEN"] == "${{ secrets.DOCS_SITE_TOKEN != '' }}"
    checkout = STEPS["Check out the site repository"]["with"]
    assert checkout["token"] == "${{ secrets.DOCS_SITE_TOKEN }}"
    assert checkout["repository"] == "${{ env.SITE_REPOSITORY }}"


def test_the_token_reaches_nothing_but_the_checkout_and_the_ref_name_is_never_interpolated_in_a_script():
    # Only the existence check and the checkout read the secret; no script does.
    assert TEXT.count("secrets.DOCS_SITE_TOKEN") == 2
    for step in JOB["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
