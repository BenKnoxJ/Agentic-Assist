"""Tests for who may talk to Gojo.

JWT validation proves a message came from Azure Bot Service for our bot. It
does not prove an authorised person sent it - any tenant user who installs
the app produces valid tokens. This is the check that makes Gojo single-user
in fact rather than in intent (GOJO-MASTER.md 1.3).

Every case here is a denial except one. That ratio is the point.
"""

from gojo.config import Settings
from gojo.teams import is_authorised

ME = "11111111-1111-1111-1111-111111111111"
SOMEONE_ELSE = "22222222-2222-2222-2222-222222222222"
MY_TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_TENANT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

ALLOWED = frozenset({ME})


def test_authorised_user_in_correct_tenant_is_allowed() -> None:
    assert is_authorised(ME, MY_TENANT, ALLOWED, MY_TENANT) is True


def test_other_user_in_same_tenant_is_denied() -> None:
    """A colleague who installs the app still cannot use it."""
    assert is_authorised(SOMEONE_ELSE, MY_TENANT, ALLOWED, MY_TENANT) is False


def test_right_user_wrong_tenant_is_denied() -> None:
    assert is_authorised(ME, OTHER_TENANT, ALLOWED, MY_TENANT) is False


def test_empty_allow_list_denies_everyone() -> None:
    """Unset must lock the door, not remove it."""
    assert is_authorised(ME, MY_TENANT, frozenset(), MY_TENANT) is False


def test_missing_object_id_is_denied() -> None:
    """Absent aad_object_id must never be treated as a wildcard."""
    assert is_authorised(None, MY_TENANT, ALLOWED, MY_TENANT) is False


def test_missing_tenant_is_denied() -> None:
    assert is_authorised(ME, None, ALLOWED, MY_TENANT) is False


def test_empty_string_object_id_is_denied() -> None:
    assert is_authorised("", MY_TENANT, ALLOWED, MY_TENANT) is False


class TestAllowListParsing:
    """The allow-list comes from a comma-separated env var."""

    def _settings(self, raw: str) -> Settings:
        return Settings(_env_file=None, allowed_user_ids=raw)

    def test_single_id(self) -> None:
        assert self._settings(ME).allowed_users == {ME}

    def test_multiple_ids_with_whitespace(self) -> None:
        assert self._settings(f" {ME} , {SOMEONE_ELSE} ").allowed_users == {
            ME,
            SOMEONE_ELSE,
        }

    def test_empty_is_empty_not_a_blank_entry(self) -> None:
        """A blank string must not parse to a set containing "" - that would
        authorise a sender whose object ID failed to arrive."""
        assert self._settings("").allowed_users == frozenset()

    def test_trailing_comma_produces_no_blank_entry(self) -> None:
        assert self._settings(f"{ME},").allowed_users == {ME}
