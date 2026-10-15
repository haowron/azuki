"""版庫の singleton: VR55(docs/components/OPTIMIZER.md)。

store は代役にし、PostgreSQL を使わない。計画した関数は用例の中で引き、実装前は各用例が red になる。
"""

from contextlib import suppress
from typing import Any
from unittest.mock import Mock

import pytest

import trackeverything.config as config
from tests.versioning.helpers import api, host_services, version_store


@pytest.fixture
async def opened():
    """用例が開いた版庫。終了時に閉じる(singleton も外れる)。"""
    repos: list[Any] = []
    yield repos
    for repo in reversed(repos):
        with suppress(RuntimeError):
            await repo.close()


async def test_vr55_one_repository_per_process(opened):
    """VR55 (a)-(d): 開く前と閉じた後は RuntimeError、開いたままの二度目の open_repo は拒否、閉じれば開き直せる。"""
    modules = api()
    build = modules.compose.prepare_components(host_services())
    with pytest.raises(RuntimeError):
        modules.repo.current_repo()

    store = version_store()
    first = modules.repo.open_repo(build, store=store)
    opened.append(first)
    assert modules.repo.current_repo() is first
    store.read_main.assert_not_awaited()

    with pytest.raises(RuntimeError):
        modules.repo.open_repo(build, store=version_store())
    assert modules.repo.current_repo() is first

    await first.close()
    with pytest.raises(RuntimeError):
        modules.repo.current_repo()

    second = modules.repo.open_repo(build, store=version_store())
    opened.append(second)
    assert second is not first and modules.repo.current_repo() is second

    # singleton でない版庫を閉じても singleton は外れない。
    direct = modules.repo.Repo(version_store(), build)
    await direct.close()
    assert modules.repo.current_repo() is second


async def test_vr55_the_default_store_is_the_configured_git_mirror(opened, monkeypatch, tmp_path):
    """VR55 (e): store を省くと、設定の COMPONENT_REPO_DIR・COMPONENT_REPO_URL・COMPONENT_REPO_SSH_KEY に Host の hooks と package の既定組件を添えた GitVersionStore を作る。"""
    modules = api()
    monkeypatch.setattr(config, "COMPONENT_REPO_DIR", str(tmp_path / "components.git"))
    monkeypatch.setattr(config, "COMPONENT_REPO_URL", "git@github.com:example/components.git")
    monkeypatch.setattr(config, "COMPONENT_REPO_SSH_KEY", str(tmp_path / "deploy_key"))
    factory = Mock(return_value=version_store())
    monkeypatch.setattr(modules.store, "GitVersionStore", factory)
    monkeypatch.setattr(modules.repo, "GitVersionStore", factory, raising=False)

    repo = modules.repo.open_repo(modules.compose.prepare_components(host_services()))
    opened.append(repo)

    factory.assert_called_once_with(str(tmp_path / "components.git"), "git@github.com:example/components.git", ssh_key=str(tmp_path / "deploy_key"),
                                    hooks=modules.repo.HOOKS_DIR, seed=modules.repo.PACKAGE_ROOT)
    assert modules.repo.current_repo() is repo


async def test_vr55_without_a_repository_url_the_default_store_is_the_local_repository(opened, monkeypatch, tmp_path):
    """VR55 (f): COMPONENT_REPO_URL が空でも、store を省いた open_repo は URL なしの同じ GitVersionStore を作る(本地の仓库。最初の main は package の 5 フォルダ)。

    hooks は Host の checkout の ``scripts/component-hooks``(pre-push・pre-receive)。package だけを読む store はない。
    """
    modules = api()
    monkeypatch.setattr(config, "COMPONENT_REPO_DIR", str(tmp_path / "components.git"))
    monkeypatch.setattr(config, "COMPONENT_REPO_URL", "")
    monkeypatch.setattr(config, "COMPONENT_REPO_SSH_KEY", "")
    factory = Mock(return_value=version_store())
    monkeypatch.setattr(modules.store, "GitVersionStore", factory)
    monkeypatch.setattr(modules.repo, "GitVersionStore", factory, raising=False)

    repo = modules.repo.open_repo(modules.compose.prepare_components(host_services()))
    opened.append(repo)

    factory.assert_called_once_with(str(tmp_path / "components.git"), "", ssh_key="", hooks=modules.repo.HOOKS_DIR, seed=modules.repo.PACKAGE_ROOT)
    assert modules.repo.PACKAGE_ROOT.name == "trackeverything" and (modules.repo.PACKAGE_ROOT / "core").is_dir()
    expected = modules.repo.PACKAGE_ROOT.parents[1] / "scripts" / "component-hooks"
    assert expected == modules.repo.HOOKS_DIR
    assert (expected / "pre-push").is_file() and (expected / "pre-receive").is_file()
    assert not hasattr(modules.store, "PackageVersionStore") and not hasattr(modules.store, "NoComponentRepository")
    assert modules.repo.current_repo() is repo
