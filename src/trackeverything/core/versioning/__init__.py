"""Agent・Tool・Skill・Harness・Source の版を git から取り、main を選び、名前から Agent を返す。

版は git の commit(5 フォルダの tree)で、runtime は bare の mirror(store)の main を読み込む(snapshot・loader)。
版庫(repo)は共有の main の取得と採用(sync)・fork・merge(載せ直しと push)と、run ごとの取得口を持つ。
commit の検証は CLI(``python -m trackeverything.core.versioning check``)で行う。
"""
