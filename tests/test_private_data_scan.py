"""Nothing committed says where the owner lives, names their charger, or holds their login."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

import private_data_scan


def test_a_home_network_address_is_never_committed():
    # Built at run time, so this file does not itself hold what it forbids.
    for address in (".".join(p) for p in (("10", "0", "0", "7"), ("192", "168", "1", "50"))):
        assert private_data_scan.offences(f"bridge at http://{address}:8000", set()), address


def test_a_street_address_is_never_committed():
    street = "Storgata" + " 12"
    assert private_data_scan.offences(f"the shop in {street}", set())
    assert private_data_scan.offences("Kirke" + "veien 3B", set())


def test_coordinates_more_exact_than_two_decimals_are_never_committed():
    assert private_data_scan.offences("home = " + "59.91" + "38, 10.75" + "22", set())
    assert private_data_scan.offences("home = 59.91, 10.75", set()) == []


def test_a_word_from_the_private_list_is_never_committed():
    name = "Garasje " + "Bakgarden"
    assert private_data_scan.offences(f"charger {name.lower()}", {name})


def test_a_real_charger_id_is_never_committed():
    # Built at run time, so this file does not itself hold what it forbids.
    made_up = "EH" + "7Q2K9Z"
    assert private_data_scan.offences(f"charger {made_up} is charging", set())


def test_the_made_up_charger_ids_are_allowed():
    assert private_data_scan.offences("charger EHTEST01 and EHTEST02", set()) == []
    assert private_data_scan.offences("EXAMPLES, ENGLISH, EH, 12 A, EHLO", set()) == []


def test_the_login_is_never_committed(tmp_path):
    user = tmp_path / "user"
    user.write_text("someone@example.invalid\n")
    password = tmp_path / "password"
    password.write_text("made-up-password-0123\n")
    found = private_data_scan.private_values(tmp_path / "none.toml", [user, password])
    assert private_data_scan.offences("login someone@example.invalid", found)
    assert private_data_scan.offences("password made-up-password-0123", found)


def test_the_private_list_is_read_from_local_config(tmp_path):
    words = tmp_path / "private.toml"
    words.write_text('words = ["Garasje Bakgarden", "ab"]\n')
    # A word too short to be told from ordinary text is left out.
    assert private_data_scan.private_values(words, []) == {"Garasje Bakgarden"}


def test_ordinary_text_and_documentation_addresses_are_allowed():
    text = "Easee Home, 192.0.2.10, version 1.2.3.4, 7.4 kW, 16 A, 10 per 60 minutes"
    assert private_data_scan.offences(text, set()) == []


def _clean_run(tmp_path, *flags):
    return private_data_scan.main([*flags, "--words", str(tmp_path / "none.toml")])


def test_the_tracked_tree_is_clean(tmp_path):
    assert _clean_run(tmp_path, "--tracked") == 0


def test_the_history_is_clean(tmp_path):
    assert _clean_run(tmp_path, "--history") == 0
