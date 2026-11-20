import fcntl
import os
import subprocess

import pytest

from bootstrap.init_files import (
    PrivateFiles,
    SetupError,
    absolute_path,
    env_quote,
    env_values,
    merge_env,
)


def private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(content)
    path.chmod(0o600)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "$DUMMY",
        "${DUMMY}",
        "a#comment",
        "a'b",
        "a\\b",
        "two\\\\slashes",
        "\\'quote",
        "tail\\",
        "$tail\\",
        "spaces are literal",
        "$$",
        "中文路徑",
    ],
)
def test_literal_environment_roundtrip(value):
    assert env_values("TOKEN=" + env_quote(value) + "\n")["TOKEN"] == value


def test_merge_preserves_comments_unknown_multiline_and_literal_references():
    original = '# header\nOTHER="line1\nline2"\nREFERENCE=${OTHER}\nTOKEN=old\n# footer\n'
    result = merge_env(original, {"TOKEN": "new$TOKEN#'value", "NEW": "fresh"})
    assert 'OTHER="line1\nline2"\nREFERENCE=${OTHER}\n' in result
    assert result.startswith("# header\n") and "# footer\n" in result
    assert env_values(result)["TOKEN"] == "new$TOKEN#'value"
    assert env_values(result)["NEW"] == "fresh"


@pytest.mark.parametrize("text", ["KEY=one\nKEY=two\n", 'KEY="unterminated\n'])
def test_ambiguous_environment_rejected(text):
    with pytest.raises(SetupError):
        merge_env(text, {"KEY": "updated"})


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\x00b"])
def test_environment_injection_rejected(value):
    with pytest.raises(SetupError):
        env_quote(value)


def test_backup_is_private_ignored_and_exact(files, project):
    path = project / ".env.ml"
    private(path, b"ORIGINAL=unchanged\n")
    assert files.read(path) == b"ORIGINAL=unchanged\n"
    backups = files.write({path: b"REPLACEMENT=yes\n"}, {path: "backup"})
    assert path.read_bytes() == b"REPLACEMENT=yes\n"
    assert len(backups) == 1 and backups[0].read_bytes() == b"ORIGINAL=unchanged\n"
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert backups[0].stat().st_nlink == 1
    subprocess.run(
        ["git", "-C", str(project), "check-ignore", "--quiet", str(backups[0])], check=True
    )
    assert not list(project.glob("*.init-*"))


def test_new_files_and_required_empty_directories_persist(files, project):
    path = project / ".local/ml-system/roles/management.json"
    directory = project / "var"
    files.read(path)
    files.write({path: b"{}"}, {path: "overwrite"}, (directory,))
    assert directory.is_dir()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("target", ["pyproject.toml", "connections.json", ".env.example"])
def test_tracked_or_unignored_private_output_rejected(files, project, target):
    with pytest.raises(SetupError, match="Git-ignored"):
        files.read(project / target)


def test_even_ignored_tracked_output_is_rejected(files, project):
    path = project / ".env.ml"
    private(path, b"VALUE=original\n")
    subprocess.run(["git", "-C", str(project), "add", "-f", str(path)], check=True)
    with pytest.raises(SetupError):
        files.read(path)


def test_symlink_and_hardlink_rejected(files, project):
    real = project / ".env.real"
    private(real, b"PRIVATE=yes")
    alias = project / ".env.ml"
    alias.symlink_to(real)
    with pytest.raises(SetupError):
        files.read(alias)
    alias.unlink()
    os.link(real, alias)
    with pytest.raises(SetupError):
        files.read(alias)


def test_symlink_parent_rejected(files, project):
    (project / ".local").symlink_to(project, target_is_directory=True)
    with pytest.raises(SetupError):
        files.read(project / ".local/ml-system/private.json")


def test_public_mode_private_input_rejected(files, project):
    path = project / ".env.ml"
    private(path, b"PRIVATE=yes")
    path.chmod(0o644)
    with pytest.raises(SetupError):
        files.read(path)


def test_fifo_is_not_opened_blocking(files, project):
    path = project / ".env.ml"
    os.mkfifo(path, mode=0o600)
    with pytest.raises(SetupError):
        files.read(path)


def test_path_traversal_rejected(project):
    with pytest.raises(SetupError):
        absolute_path(project / "folder/../private")


@pytest.mark.parametrize("exists", [False, True])
def test_concurrent_change_rejected_without_overwrite(files, project, exists):
    path = project / ".env.ml"
    if exists:
        private(path, b"first")
    files.read(path)
    private(path, b"concurrent-value")
    with pytest.raises(SetupError):
        files.write({path: b"new"}, {path: "overwrite"})
    assert path.read_bytes() == b"concurrent-value"


@pytest.mark.parametrize("choice", ["overwrite", "backup"])
def test_multifile_failure_restores_old_removes_new_and_cleans_staging(
    files, project, monkeypatch, choice
):
    first, second, third = [project / (".env." + name) for name in ["one", "two", "three"]]
    private(first, b"old-one")
    private(third, b"old-three")
    outputs = {first: b"new-one", second: b"new-two", third: b"new-three"}
    for p in outputs:
        files.read(p)
    replace = os.replace

    def fail_third(source, destination, **kwargs):
        if destination == third.name and ".init-new-" in str(source):
            raise OSError("test write failure")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_third)
    with pytest.raises(OSError):
        files.write(outputs, {first: choice, second: "overwrite", third: "overwrite"})
    assert first.read_bytes() == b"old-one" and third.read_bytes() == b"old-three"
    assert not second.exists() and not list(project.glob("*.init-*"))
    assert PrivateFiles(project).read(first) == b"old-one"
    if choice == "backup":
        (backup,) = project.glob(first.name + ".bak-*")
        first.write_bytes(b"edited-after-rollback")
        assert backup.read_bytes() == b"old-one"
        assert backup.stat().st_nlink == 1


def test_incomplete_rollback_retains_private_ignored_old_bytes(files, project, monkeypatch):
    first, second = project / ".env.one", project / ".env.two"
    private(first, b"original-private-value")
    files.read(first)
    files.read(second)
    replace = os.replace

    def fail(source, destination, **kwargs):
        if destination == second.name or ".init-old-" in str(source):
            raise OSError("injected failure")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(SetupError, match="Rollback incomplete"):
        files.write({first: b"new", second: b"new"}, {first: "overwrite", second: "overwrite"})
    old = list(project.glob("*.init-old-*"))
    assert len(old) == 1 and old[0].read_bytes() == b"original-private-value"
    assert old[0].stat().st_mode & 0o777 == 0o600
    subprocess.run(["git", "-C", str(project), "check-ignore", "--quiet", str(old[0])], check=True)


def test_cancel_or_incomplete_choices_never_write(files, project):
    path = project / ".env.ml"
    files.read(path)
    with pytest.raises(SetupError):
        files.write({path: b"new"}, {path: "cancel"})
    assert not path.exists()


def test_stale_recovery_blocks_a_new_setup(files, project):
    path = project / ".env.ml"
    private(project / ".env.ml.init-old-retained", b"old-private-value")
    with pytest.raises(SetupError, match="Unfinished"):
        files.read(path)
    assert not path.exists()


def test_concurrent_writer_directory_lock_rejected(files, project):
    path = project / ".env.ml"
    files.read(path)
    directory = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SetupError, match="Another setup"):
            files.write({path: b"new"}, {path: "overwrite"})
    finally:
        os.close(directory)
    assert not path.exists()
