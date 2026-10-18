import pytest

from src.policy import URLBlocked, validate_target


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/file",
        "https://user:password@example.org/",
        "http://example.org:8080/",
        "https://example.org:80/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "http://example.org\\@127.0.0.1/",
        "http://exa%6dple.org/",
        "http://example.org/\r\nHost:metadata/",
    ],
)
def test_rejects_ambiguous_or_unsupported_url_shapes(url: str) -> None:
    with pytest.raises(URLBlocked):
        validate_target(url, ("8.8.8.8",))


@pytest.mark.parametrize(
    "ips",
    [
        (),
        ("127.0.0.1",),
        ("169.254.169.254",),
        ("10.0.0.1",),
        ("192.0.2.1",),
        ("::1",),
        ("::ffff:127.0.0.1",),
        ("2001:db8::1",),
        ("8.8.8.8", "10.0.0.1"),
    ],
)
def test_rejects_any_nonpublic_dns_answer(ips: tuple[str, ...]) -> None:
    with pytest.raises(URLBlocked):
        validate_target("https://example.org/", ips)


def test_operator_forbidden_public_cidr_is_enforced() -> None:
    with pytest.raises(URLBlocked):
        validate_target(
            "https://example.org/",
            ("8.8.8.8",),
            forbidden_cidrs=("8.8.8.0/24",),
        )


def test_public_target_is_canonicalized_without_fragment() -> None:
    assert validate_target(
        "HTTPS://ExAmPle.Org:443/a%20b?q=1#section",
        ("8.8.8.8", "2001:4860:4860::8888"),
    ) == "https://example.org/a%20b?q=1"


def test_canonical_public_ip_literal_is_allowed() -> None:
    assert validate_target("https://8.8.8.8/", ("8.8.8.8",)) == "https://8.8.8.8/"
