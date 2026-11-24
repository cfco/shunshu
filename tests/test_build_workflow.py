from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")


def test_tag_planning_uses_the_tag_version():
    assert 'TAG_ARG="--tag=$GITHUB_REF_NAME"' in WORKFLOW
    assert '${TAG_ARG:+"$TAG_ARG"}' in WORKFLOW


def test_ubuntu_job_requires_successful_release_planning():
    assert "if: ${{ !cancelled() && needs.plan.result == 'success' }}" in WORKFLOW


def test_release_tag_must_match_packaged_version():
    assert 'if ($name -ne "v$version") { throw "tag $name 与产物版本 v$version 不一致" }' in WORKFLOW
    assert 'if [[ "$name" != "v${{ steps.version.outputs.value }}" ]]' in WORKFLOW


def test_deb_constrains_the_python_abi_used_to_build_it():
    assert "Depends: python3 (>= 3.14), python3 (<< 3.15)," in WORKFLOW
