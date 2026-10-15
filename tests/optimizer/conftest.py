"""optimizer の用例が使う singleton の版庫。

store は ``tests.versioning.helpers.version_store`` に、書いた版を読めるようにし、mirror の main を比較交換で動かし、
GitHub の main(``fetch`` の返り値)を fast-forward の push で進める振舞いを加えたもの。
版庫は実際の ``Repo`` を ``open_repo`` で開くため、fork の読込みと merge を実際に通す。
"""

from contextlib import suppress
from typing import Any

import pytest

from tests.versioning.conftest import github, isolated_git  # noqa: F401 - 実際の git で GitHub を代役にする用例が使う
from tests.versioning.helpers import api, host_services, make_commit, minimal_tree, seed, version_store


def live_store(main: Any, files: Any) -> Any:
    """``files`` の版 ``main`` を GitHub と mirror の main とする store。

    ``write`` した版は読め、``move_main`` は比較交換で mirror の main を動かし、``push`` は親が GitHub の main の版だけを受け入れて進める。
    """
    store = version_store(main=main, versions=[(main, files)])
    write = store.write.side_effect

    async def written(tree: Any, parent: Any, message: str, created_at: Any) -> Any:
        commit = await write(tree, parent, message, created_at)
        if commit is not None:
            seed(store, (commit, tree))
        return commit

    async def move_main(expected: Any, new: str) -> bool:
        if store.read_main.return_value != expected:
            return False
        store.read_main.return_value = new
        return True

    async def push(version_id: str) -> bool:
        if store.seeded[version_id][0].parent != store.fetch.return_value:
            return False
        store.fetch.return_value = version_id
        return True

    async def is_ancestor(ancestor: str, descendant: str) -> bool:
        current: Any = descendant
        while current is not None:
            if current == ancestor:
                return True
            current = store.seeded[current][0].parent if current in store.seeded else None
        return False

    store.write.side_effect = written
    store.move_main.side_effect = move_main
    store.push.side_effect = push
    store.is_ancestor.side_effect = is_ancestor
    return store


@pytest.fixture
async def singleton(tmp_path):
    """``await singleton(files)`` は files の版を GitHub と mirror の main とする store で singleton の版庫を開いて start し、
    ``(版庫, store, main の Commit)`` を返す。終了時に閉じる(singleton も外れる)。"""
    opened: list[Any] = []

    async def open_(files: Any = None) -> tuple[Any, Any, Any]:
        modules = api()
        files = files if files is not None else minimal_tree()
        main = make_commit(files)
        store = live_store(main, files)
        repo = modules.repo.open_repo(modules.compose.prepare_components(host_services()), store=store)
        opened.append(repo)
        await repo.start()
        return repo, store, main

    yield open_
    for repo in reversed(opened):
        with suppress(RuntimeError):
            await repo.close()
