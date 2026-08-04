from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".claude" / "skills" / "onboard-company"


def test_onboard_company_skill_is_complete_and_portable() -> None:
    required = (
        SKILL_ROOT / "SKILL.md",
        SKILL_ROOT / "templates" / "verify_subagent_prompt.md",
        SKILL_ROOT / "templates" / "groundtruth_subagent_prompt.md",
        ROOT / "docs" / "COMPANY_ONBOARDING.md",
    )
    assert all(path.is_file() for path in required)

    skill = required[0].read_text(encoding="utf-8")
    assert skill.startswith("---\nname: onboard-company\n")
    assert "description:" in skill.split("---", 2)[1]

    packaged_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in SKILL_ROOT.rglob("*.md")
    )
    legacy_mount = Path("/").joinpath("Volumes", "Vault").as_posix()
    assert legacy_mount not in packaged_text
    assert "docs/COMPANY_ONBOARDING.md" in skill
