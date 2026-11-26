"""worker/argsafe: repo-/record-controlled strings can't become git/ATX options or escape the workspace."""
import pytest

from backend.src.worker import argsafe


@pytest.mark.parametrize("url", [
    "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/app", "codecommit::us-east-1://app",
])
def test_clone_url_accepts_the_api_forms(url):
    assert argsafe.safe_clone_url(f"  {url} ") == url


@pytest.mark.parametrize("url", [
    "", "--upload-pack=touch /tmp/x", "-u", "ext::sh -c id", "file:///etc", "/tmp/repo",  # nosec B108 - test input: malicious clone URLs that must be refused
    "https://-oProxyCommand=id/x", "git@-oProxyCommand=id:x", "https://github.com/a b",
    "https://github.com/acme/app.git", "ssh://git@github.com/acme/app", "git@github.com:acme/app.git",
])
def test_clone_url_refuses_options_and_local_transports(url):
    with pytest.raises(ValueError):
        argsafe.safe_clone_url(url)


@pytest.mark.parametrize("ref", ["main", "migration/app-te", "origin/master", "HEAD", "347ec91", "v1.2.3", "a+b"])
def test_ref_accepts_plain_names(ref):
    assert argsafe.safe_ref(ref) == ref


@pytest.mark.parametrize("ref", ["", "-f", "--orphan", "a..b", "a@{1}", "a//b", "/abs", "x/", "x.lock", "a b",
                                 "a~1", "a^", "a:b", "a\nb"])
def test_ref_refuses_options_and_revision_syntax(ref):
    with pytest.raises(ValueError):
        argsafe.safe_ref(ref)


def test_td_name():
    assert argsafe.safe_td_name("AWS/java-version-upgrade") == "AWS/java-version-upgrade"
    for bad in ("", "-x", "AWS/x -p /", "AWS/../x", "x;id"):
        with pytest.raises(ValueError):
            argsafe.safe_td_name(bad)


def test_dir_name():
    assert argsafe.safe_dir_name("aws-cdk-examples") == "aws-cdk-examples"
    for bad in ("", ".", "..", "a/b", "a b"):
        with pytest.raises(ValueError):
            argsafe.safe_dir_name(bad)


def test_confined(tmp_path):
    (tmp_path / "out").mkdir()
    base = tmp_path / "ws"
    base.mkdir()
    (base / "link").symlink_to(tmp_path / "out")
    assert argsafe.confined(base, "repo/sub") == (base / "repo/sub").resolve()
    for bad in ("../out", str(tmp_path / "out"), "link", "link/x"):
        with pytest.raises(ValueError):
            argsafe.confined(base, bad)
