"""実行、session、ログ、エラーと core のインスタンス構成。

Runtime は作業単位を管理し、app が queue の消費・LLM・接続を組み立てる。
具体 Agent・Tool・Source は版の構成モジュールを通して利用する。
"""
