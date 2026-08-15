# セキュリティ / Security

## 信頼境界 / Trust boundary

本ツールは**同一マシン・同一 OS ユーザ**で動く Claude Code セッション同士の調整を目的とする。
掲示板は平文で、同じユーザで動くプロセスからは自由に読み書きできる。**同一ユーザからの攻撃は
スコープ外**である（そこを守ろうとすると鍵の管理が新しい単一障害点になり、fail-open が壊れる）。

This tool coordinates Claude Code sessions running as **the same OS user on the same machine**.
The board is plaintext and freely readable/writable by any process of that user. **Attacks from
the same user are out of scope** — defending there would introduce key management as a new single
point of failure and break the fail-open guarantee.

## 対象となる報告 / In scope

- **掲示板経由のプロンプトインジェクション**（他セッションの自由記述が文脈へ入る経路の防御を回避できるもの）
- **fail-open の破れ**（本ツールが原因でセッションの作業が止まる、フックが非ゼロを返す等）
- **走らなかったジョブを成功と報告する**経路（`rb run` の終了コードが実態とずれる）
- **他セッションの生きた宣言を消せる**経路（所有判定の回避）

- Prompt injection through the board that bypasses the data-marking / truncation / control-character
  stripping applied to other sessions' free text
- Any break of fail-open (the tool stopping the user's work; a hook returning non-zero)
- Any path where a job that never ran is reported as successful
- Any path that removes another session's live declaration without `--force`

## 報告方法 / How to report

GitHub の [Security Advisories](https://github.com/1-case/resource-broker/security/advisories/new)
から非公開で報告してください。公開の issue には書かないでください。

Please report privately via GitHub Security Advisories rather than a public issue.

## 利用者側の注意 / For users

- `--job` や `--observed` に**秘密を書かない**（掲示板は平文で、他セッションの文脈へ入る）
- 掲示板や監査ログを**そのまま公開の場に貼らない**（プロジェクト名と作業パスが入る）

- Do not put secrets in `--job` or `--observed`.
- Do not paste the board or the audit log anywhere public.
