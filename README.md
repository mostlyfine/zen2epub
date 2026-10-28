# zenn2epub

Zenn の本（`https://zenn.dev/<user>/books/<id>`）をチャプターごとに Markdown として保存し、EPUB に変換するスクリプトです。

## 特徴

- Selenium（headless Chrome）でレンダリング後のページから本文を取得します。
- 本文抽出は `.znc` などの CSS セレクタ、trafilatura、readability、`<body>` の順にフォールバックします。
- カバー画像と Zenn の本文用スタイル（`.znc` を対象とするルールのみ）を取得し、EPUB に含めます。
- 本文中の画像は EPUB 内に埋め込みます。
- 取得済みのチャプターはスキップするため、中断後に再実行できます。
- リクエスト間に 1〜3 秒のランダムな待機を入れます。

## 必要なもの

- Python 3.10 以上
- [uv](https://docs.astral.sh/uv/)（PEP 723 のインラインメタデータで依存関係を解決します）
- Google Chrome

## 使い方

```sh
uv run zenn_book_epub.py https://zenn.dev/<user>/books/<id>
```

既定では `books/<id>/` に Markdown・メタデータ・画像を保存し、`books/<id>.epub` を出力します。

### オプション

| オプション | 説明 |
| --- | --- |
| `-d`, `--dir DIR` | 本ごとの Markdown を保存するディレクトリ（既定: `books`） |
| `-o`, `--output FILE` | 出力する EPUB のパス（既定: `<DIR>/<id>.epub`） |
| `-r`, `--refetch` | 取得済みのチャプターも再取得して上書きする |
| `-l`, `--local` | Zenn にアクセスせず、保存済みの Markdown から EPUB のみを生成する |
| `-v`, `--verbose` | DEBUG レベルのログを表示する |

`--refetch` と `--local` は同時に指定できません。

### 例

```sh
# 保存済みの Markdown を編集してから EPUB を作り直す
uv run zenn_book_epub.py --local https://zenn.dev/<user>/books/<id>

# 出力先を指定する
uv run zenn_book_epub.py -o ~/Downloads/book.epub https://zenn.dev/<user>/books/<id>
```

## 出力ファイル

```
books/
├── <id>/
│   ├── book.json        # タイトル・著者・チャプター一覧
│   ├── cover.png        # カバー画像
│   ├── style.css        # Zenn 本文用スタイル
│   ├── 001_<slug>.md
│   └── ...
└── <id>.epub
```

## 注意

取得した本の利用は個人での閲覧に留め、Zenn の利用規約と著作権を守ってください。
