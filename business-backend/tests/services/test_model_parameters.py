from src.services.model_parameters import PLATFORM_DEFAULT_TEMPERATURE, resolve_temperature


def test_override_wins_over_everything() -> None:
    assert resolve_temperature(0.1, 0.5) == 0.1
    assert resolve_temperature(0.1, None) == 0.1


def test_org_default_wins_when_no_override() -> None:
    assert resolve_temperature(None, 0.5) == 0.5


def test_platform_default_when_nothing_configured() -> None:
    assert resolve_temperature(None, None) == PLATFORM_DEFAULT_TEMPERATURE


def test_override_of_zero_is_respected_not_treated_as_falsy() -> None:
    # 0.0 is a valid, meaningful temperature — must not be confused with
    # "not set" the way `override or org_default` would.
    assert resolve_temperature(0.0, 0.5) == 0.0
