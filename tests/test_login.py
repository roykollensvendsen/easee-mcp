"""`easee-mcp login` asks for the Easee login without showing it, and saves it for the owner alone.

It only saves: it sends nothing to Easee, and it never prints what it was given.
"""

import os
import pathlib
import signal
import stat

import pytest

from easee_mcp import cli


def _answering(monkeypatch, *answers):
    """Stand in for the masked prompt; a plain input() would read the test's empty stdin and fail."""
    asked = []
    given = iter(answers)

    def prompt(question):
        asked.append(question)
        return next(given)

    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    return asked


def test_a_login_is_saved_where_the_server_reads_it(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "easee-mcp"
    asked = _answering(monkeypatch, "owner@example.com", "made-up-secret")
    assert cli.main(["login", "--login-dir", str(folder)]) == 0
    assert len(asked) == 2
    assert cli.saved_login(folder) == ("owner@example.com", "made-up-secret")
    out, err = capsys.readouterr()
    assert "made-up-secret" not in out + err
    assert "owner@example.com" not in out + err


def test_a_saved_login_is_readable_by_its_owner_alone(tmp_path, monkeypatch):
    folder = tmp_path / "easee-mcp"
    _answering(monkeypatch, "owner@example.com", "made-up-secret")
    cli.main(["login", "--login-dir", str(folder)])
    for name in ("user", "password"):
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o600
    assert sorted(p.name for p in folder.iterdir()) == ["password", "user"]  # no temporary left


def test_the_login_folder_is_readable_by_its_owner_alone(tmp_path, monkeypatch):
    folder = tmp_path / "easee-mcp"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    _answering(monkeypatch, "owner@example.com", "made-up-secret")
    cli.main(["login", "--login-dir", str(folder)])
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "answers", [("", "made-up-secret"), ("owner@example.com", "")], ids=["user", "password"]
)
def test_an_empty_login_is_never_saved(tmp_path, monkeypatch, capsys, answers):
    folder = tmp_path / "easee-mcp"
    (folder).mkdir()
    (folder / "user").write_text("kept@example.com")
    (folder / "password").write_text("kept-secret")
    _answering(monkeypatch, *answers)
    assert cli.main(["login", "--login-dir", str(folder)]) == 1
    assert cli.saved_login(folder) == ("kept@example.com", "kept-secret")
    assert "nothing was saved" in capsys.readouterr().err


def test_a_password_is_kept_exactly_as_typed(tmp_path, monkeypatch):
    """Spaces at either end belong to a password; stripped, it would fail later for no visible reason."""
    folder = tmp_path / "easee-mcp"
    _answering(monkeypatch, "owner@example.com", " made-up secret ")
    cli.main(["login", "--login-dir", str(folder)])
    assert cli.saved_login(folder) == ("owner@example.com", " made-up secret ")


def test_a_login_saved_by_hand_is_still_read(tmp_path):
    """A file made with `echo` ends in one newline, which is not part of the password."""
    (tmp_path / "user").write_text("owner@example.com\n")
    (tmp_path / "password").write_text("made-up-secret\n")
    assert cli.saved_login(tmp_path) == ("owner@example.com", "made-up-secret")


def test_a_stop_while_saving_never_pairs_a_new_user_with_an_old_password(tmp_path, monkeypatch):
    """Both are written first, then both put in place with the stops held off in between."""
    folder = tmp_path / "easee-mcp"
    folder.mkdir()
    (folder / "user").write_text("old@example.com")
    (folder / "password").write_text("old-secret")
    held = []
    real_replace = cli.os.replace

    def replace(*args, **kwargs):
        held.append(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(cli.os, "replace", replace)
    _answering(monkeypatch, "new@example.com", "new-secret")
    cli.main(["login", "--login-dir", str(folder)])
    assert len(held) == 2
    assert all({signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= blocked for blocked in held)
    assert signal.SIGINT not in signal.pthread_sigmask(signal.SIG_BLOCK, [])  # let go after
    assert cli.saved_login(folder) == ("new@example.com", "new-secret")


def test_a_login_folder_that_is_a_link_is_refused(tmp_path, monkeypatch, capsys):
    """A link would put the login wherever it points, perhaps where someone else can reach it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "easee-mcp"
    link.symlink_to(elsewhere)
    _answering(monkeypatch, "owner@example.com", "made-up-secret")
    assert cli.main(["login", "--login-dir", str(link)]) == 1
    assert list(elsewhere.iterdir()) == []
    assert "not a folder of yours" in capsys.readouterr().err


def test_a_login_folder_that_is_not_yours_is_refused(tmp_path, monkeypatch):
    folder = tmp_path / "easee-mcp"
    folder.mkdir()
    someone_else = os.getuid() + 1
    monkeypatch.setattr(cli.os, "getuid", lambda: someone_else)
    _answering(monkeypatch, "owner@example.com", "made-up-secret")
    assert cli.main(["login", "--login-dir", str(folder)]) == 1
    assert list(folder.iterdir()) == []


def test_a_login_folder_others_could_write_in_is_refused(tmp_path, monkeypatch, capsys):
    """Someone else may already have put a file there; closing it now would hide that."""
    folder = tmp_path / "easee-mcp"
    folder.mkdir()
    folder.chmod(0o775)
    _answering(monkeypatch, "owner@example.com", "made-up-secret")
    assert cli.main(["login", "--login-dir", str(folder)]) == 1
    assert list(folder.iterdir()) == []
    err = capsys.readouterr().err
    assert "chmod 700" in err
    assert "Look inside it" in err


@pytest.mark.parametrize("failing", ["user", "password"])
def test_a_failed_save_leaves_no_copy_of_the_login(tmp_path, monkeypatch, failing):
    """A disk that fills while saving must not leave a stray copy of the password behind."""
    folder = tmp_path / "easee-mcp"
    real_fdopen = cli.os.fdopen

    def fdopen(handle, *args, **kwargs):
        opened = real_fdopen(handle, *args, **kwargs)
        name = pathlib.Path(f"/proc/self/fd/{handle}").readlink().name
        if name.startswith(f".{failing}-"):
            opened.close()
            msg = "the disk is full"
            raise OSError(msg)
        return opened

    monkeypatch.setattr(cli.os, "fdopen", fdopen)
    _answering(monkeypatch, "owner@example.com", "made-up-secret")
    with pytest.raises(OSError, match="the disk is full"):
        cli.main(["login", "--login-dir", str(folder)])
    assert list(folder.iterdir()) == []
